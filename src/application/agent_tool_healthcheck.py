from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

from domain.domain.multi_tick import FEISHU_APP_NOTIFICATION_PROVIDER, normalize_notification_provider
from src.application.agent_tool_config import repo_base
from src.application.agent_tool_registry import AGENT_TOOL_DEFINITIONS, AgentToolDefinition
from src.application.assistant.audit import default_audit_db_path
from src.application.channels.status import build_channel_status
from src.application.environment_status import build_effective_env_with_status
from src.application.ledger.api import ledger_store_payload
from src.application.secret_resolver import (
    resolve_feishu_bot_config,
    resolve_feishu_holdings_config,
)
from src.application.service_deploy import load_service_profile, service_status_from_profile


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _read_countable_json_rows(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    for key in ("rows", "records", "items", "data", "candidates"):
        rows = value.get(key)
        if isinstance(rows, list):
            return rows
    return []


def _count_evidence_rows(path: Path) -> int:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            return sum(1 for _row in csv.DictReader(fh))
    if suffix == ".jsonl":
        with path.open("r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return len(_read_countable_json_rows(payload))
    with path.open("r", encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


def _agent_tool_mode(definition: AgentToolDefinition, *, write_enabled: bool) -> str:
    if definition.name == "version_update":
        return "write_preview_default"
    if definition.name == "manage_symbols":
        return "write" if write_enabled else "read_preview_only"
    if definition.is_pure_read():
        return "read"
    if definition.read_only and definition.side_effects:
        return "read_with_local_cache"
    return definition.resolved_risk_level()


def _agent_tool_availability(*, write_enabled: bool) -> dict[str, dict[str, Any]]:
    return {
        definition.name: {"available": True, "mode": _agent_tool_mode(definition, write_enabled=write_enabled)}
        for definition in AGENT_TOOL_DEFINITIONS
    }


def _candidate_evidence_paths(payload: dict[str, Any], *, keys: tuple[str, ...], report_dir: Path | None, patterns: tuple[str, ...]) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    for key in keys:
        for item in _list(payload.get(key)):
            text = str(item or "").strip()
            if not text:
                continue
            path = Path(text)
            marker = str(path)
            if marker not in seen:
                paths.append(path)
                seen.add(marker)
    if report_dir is not None:
        for pattern in patterns:
            for path in sorted(report_dir.glob(pattern)):
                marker = str(path)
                if marker not in seen:
                    paths.append(path)
                    seen.add(marker)
    return paths


def _candidate_evidence_check(payload: dict[str, Any], *, mask_path: Callable[[Any], str]) -> tuple[dict[str, Any] | None, list[str]]:
    relevant_keys = {
        "candidate_report_dir",
        "candidate_path",
        "candidate_paths",
        "candidate_reject_log_path",
        "candidate_reject_log_paths",
        "reject_log_path",
        "reject_log_paths",
        "candidate_trace_path",
        "candidate_trace_paths",
        "trace_path",
        "trace_paths",
    }
    if not any(payload.get(key) for key in relevant_keys):
        return None, []

    report_dir_value = str(payload.get("candidate_report_dir") or "").strip()
    report_dir = Path(report_dir_value) if report_dir_value else None
    warnings: list[str] = []
    groups = {
        "candidates": _candidate_evidence_paths(
            payload,
            keys=("candidate_path", "candidate_paths"),
            report_dir=report_dir,
            patterns=("*candidates*.csv", "*candidates*.json", "*candidates*.jsonl"),
        ),
        "reject_logs": _candidate_evidence_paths(
            payload,
            keys=("candidate_reject_log_path", "candidate_reject_log_paths", "reject_log_path", "reject_log_paths"),
            report_dir=report_dir,
            patterns=("*reject*.csv", "*reject*.json", "*reject*.jsonl"),
        ),
        "traces": _candidate_evidence_paths(
            payload,
            keys=("candidate_trace_path", "candidate_trace_paths", "trace_path", "trace_paths"),
            report_dir=report_dir,
            patterns=("*candidate_filter_trace*.json", "*candidate_filter_trace*.jsonl"),
        ),
    }
    try:
        min_sample = int(payload.get("candidate_evidence_min_sample") or payload.get("min_sample") or 5)
    except Exception:
        min_sample = 5
    min_sample = max(1, min_sample)

    files: dict[str, list[dict[str, Any]]] = {}
    totals: dict[str, int] = {}
    problems: list[str] = []
    for group, paths in groups.items():
        files[group] = []
        totals[group] = 0
        for path in paths:
            entry: dict[str, Any] = {"path": mask_path(path), "exists": path.exists(), "rows": 0}
            if not path.exists():
                entry["error"] = "file_not_found"
                problems.append(f"{group}: missing {path.name}")
            elif path.is_dir():
                entry["error"] = "path_is_directory"
                problems.append(f"{group}: {path.name} is a directory")
            else:
                try:
                    rows = _count_evidence_rows(path)
                    entry["rows"] = rows
                    totals[group] += rows
                except Exception as exc:
                    entry["error"] = f"{type(exc).__name__}: {exc}"
                    problems.append(f"{group}: failed to read {path.name}")
            files[group].append(entry)

    candidate_rows = totals["candidates"]
    trace_rows = totals["traces"]
    reject_rows = totals["reject_logs"]
    evaluable = candidate_rows >= min_sample and (trace_rows > 0 or reject_rows > 0)
    readiness_notes: list[str] = []
    if candidate_rows < min_sample:
        readiness_notes.append(f"candidate_rows_below_min_sample:{candidate_rows}/{min_sample}")
    if trace_rows == 0 and reject_rows == 0:
        readiness_notes.append("missing_filter_or_reject_evidence")
    if problems:
        readiness_notes.extend(problems)

    if readiness_notes:
        warnings.append("Candidate evidence is not ready for scan-quality diagnostics: " + "; ".join(readiness_notes))
    return (
        {
            "name": "candidate_evidence",
            "status": ("ok" if evaluable and not problems else "warn"),
            "message": (
                f"candidate evidence rows candidates={candidate_rows} "
                f"reject_logs={reject_rows} traces={trace_rows}"
            ),
            "value": {
                "evaluable": evaluable,
                "min_sample": min_sample,
                "row_counts": totals,
                "files": files,
                "notes": readiness_notes,
            },
        },
        warnings,
    )


def run_healthcheck_tool(
    payload: dict[str, Any],
    *,
    load_runtime_config: Callable[..., tuple[Any, dict[str, Any]]],
    validate_runtime_config: Callable[..., Any],
    normalize_accounts: Callable[..., list[str]],
    accounts_from_config: Callable[..., list[str]],
    resolve_data_config_ref: Callable[[dict[str, Any], dict[str, Any]], str | None],
    resolve_public_data_config_path: Callable[[dict[str, Any], dict[str, Any]], Any],
    read_json_object_or_empty: Callable[[Any], dict[str, Any]],
    mask_path: Callable[[Any], str],
    list_account_config_views: Callable[[dict[str, Any]], list[Any]],
    mask_account_id: Callable[[Any], str],
    infer_futu_portfolio_settings: Callable[..., dict[str, Any]],
    load_option_positions_repo: Callable[[Any], Any],
    run_futu_doctor: Callable[..., dict[str, Any]],
    healthcheck_symbols_for_futu: Callable[[dict[str, Any]], list[str]],
    write_tools_enabled: Callable[[], bool],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(
        config_key=payload.get("config_key"),
        config_path=payload.get("config_path"),
    )
    warnings: list[str] = []
    effective_env, environment = build_effective_env_with_status(
        env_file=payload.get("env_file"),
        mask_path=mask_path,
    )
    warnings.extend(str(item) for item in effective_env.warnings)
    checks: list[dict[str, Any]] = []
    validate_runtime_config(cfg, allow_empty_symbols=True)
    checks.append({"name": "runtime_config", "status": "ok", "message": "config validation passed"})

    accounts = normalize_accounts(payload.get("accounts"), fallback=tuple(accounts_from_config(cfg)))
    checks.append(
        {
            "name": "accounts",
            "status": "ok",
            "message": f"resolved {len(accounts)} account(s)",
            "value": accounts,
        }
    )

    portfolio_cfg = _dict(cfg.get("portfolio"))
    data_config_ref = resolve_data_config_ref(payload, portfolio_cfg)
    data_config_path = resolve_public_data_config_path(payload, portfolio_cfg)
    if data_config_path.exists():
        checks.append(
            {
                "name": "data_config",
                "status": "ok",
                "message": ("portfolio.data_config found" if data_config_ref else "portfolio runtime data config found"),
                "value": mask_path(data_config_path),
            }
        )
    else:
        status = "error" if data_config_ref else "ok"
        message = (
            "portfolio.data_config missing"
            if data_config_ref
            else "portfolio.data_config not configured; using runtime-root ledger defaults"
        )
        checks.append(
            {
                "name": "data_config",
                "status": status,
                "message": message,
            }
        )
        if data_config_ref:
            warnings.append("Configured portfolio.data_config is missing.")

    data_cfg = read_json_object_or_empty(data_config_path) if data_config_path.exists() else {}
    feishu_holdings = resolve_feishu_holdings_config(data_cfg, environ=effective_env.values)
    feishu_ready = bool(feishu_holdings.app_id and feishu_holdings.app_secret)
    holdings_ready = feishu_holdings.ready
    symbol_names = {
        str(item.get("symbol") or "").strip().upper()
        for item in (cfg.get("symbols") or [])
        if isinstance(item, dict)
    }
    starter_symbol_names = {"NVDA", "0700.HK"}
    if symbol_names and symbol_names <= starter_symbol_names:
        checks.append(
            {
                "name": "starter_symbols",
                "status": "warn",
                "message": "example starter symbol is still present",
            }
        )
        warnings.append("Replace example starter symbols before enabling long-term use or sends.")
    if str(portfolio_cfg.get("data_config") or "").strip().startswith("secrets/"):
        checks.append(
            {
                "name": "starter_data_config",
                "status": "warn",
                "message": "repo-local secrets data_config is still in use",
            }
        )
        warnings.append("Move portfolio.data_config away from repo-local secrets or remove it and use runtime-root defaults.")

    notifications = _dict(cfg.get("notifications"))
    if (
        isinstance(notifications, dict)
        and normalize_notification_provider(notifications.get("provider") or notifications.get("channel"))
        == FEISHU_APP_NOTIFICATION_PROVIDER
    ):
        bot_cfg = resolve_feishu_bot_config(notifications, environ=effective_env.values)
        target = str(bot_cfg.user_open_id or "").strip()
        if target in {"ou_xxx", "user:ou_xxx", "chat:chat_xxx"}:
            checks.append(
                {
                    "name": "notification_target_placeholder",
                    "status": "warn",
                    "message": "Feishu bot notification target is still using the example placeholder value",
                }
            )
            warnings.append("Replace the example Feishu bot user open_id before enabling real sends.")
        send_missing = list(bot_cfg.credential_missing_fields)
        if not target:
            send_missing.append(bot_cfg.user_open_id_env)
        if send_missing:
            checks.append(
                {
                    "name": "notification_credentials",
                    "status": "error",
                    "message": "Feishu bot send configuration missing from environment",
                    "value": bot_cfg.redacted_status(),
                }
            )
            warnings.append(
                "Feishu bot send configuration is incomplete; set "
                + ", ".join(send_missing)
                + " before enabling sends."
            )
        else:
            if bot_cfg.app_id == "cli_xxx" or bot_cfg.app_secret == "xxx":
                checks.append(
                    {
                        "name": "notification_credentials_placeholder",
                        "status": "warn",
                        "message": "Feishu bot credentials are still using example placeholder values",
                    }
                )
                warnings.append("Replace example Feishu bot credentials before enabling real sends.")
            checks.append(
                {
                    "name": "notification_credentials",
                    "status": "ok",
                    "message": "Feishu bot send configuration is configured from environment",
                    "value": bot_cfg.redacted_status(),
                }
            )

    feishu_inbound_check, feishu_inbound_warnings = _feishu_inbound_check(
        payload,
        mask_path=mask_path,
        environ=effective_env.values,
    )
    checks.append(feishu_inbound_check)
    warnings.extend(feishu_inbound_warnings)
    feishu_service_check, feishu_service_warnings = _feishu_ws_service_check(payload, mask_path=mask_path)
    checks.append(feishu_service_check)
    warnings.extend(feishu_service_warnings)

    channel_status = build_channel_status(
        base=repo_base(),
        runtime_root=Path(config_path).parent,
        payload=payload,
        environ=effective_env.values,
        mask_path=mask_path,
        include_service_status=bool(payload.get("include_service_status", False)),
    )
    channel_health_raw = channel_status.get("channels")
    channel_health = channel_health_raw if isinstance(channel_health_raw, dict) else {}
    channel_health_check, channel_health_warnings = _channel_health_check(channel_status)
    checks.append(channel_health_check)
    warnings.extend(channel_health_warnings)

    option_positions_bootstrap_status = None
    option_positions_bootstrap_message = None
    if data_config_path.exists() or not data_config_ref:
        try:
            option_repo = load_option_positions_repo(data_config_path)
            ledger_store = _dict(ledger_store_payload(data_config_path, option_repo))
            checks.append(
                {
                    "name": "ledger_store",
                    "status": "ok",
                    "message": (
                        f"sqlite={ledger_store.get('sqlite_path')} "
                        f"trade_events={ledger_store.get('trade_event_count')} "
                        f"position_lots={ledger_store.get('position_lot_count')}"
                    ),
                    "value": ledger_store,
                }
            )
            for warning in ledger_store.get("warnings") or []:
                warnings.append(str(warning))
            option_positions_bootstrap_status = str(getattr(option_repo, "bootstrap_status", "") or "").strip() or None
            option_positions_bootstrap_message = str(getattr(option_repo, "bootstrap_message", "") or "").strip() or None
        except Exception as exc:
            option_positions_bootstrap_status = "degraded_option_positions_repo_load_failed"
            option_positions_bootstrap_message = str(exc)

    if option_positions_bootstrap_status:
        bootstrap_check_status = "ok"
        if option_positions_bootstrap_status.startswith("degraded_"):
            bootstrap_check_status = "warn"
            warnings.append(f"option_positions bootstrap degraded: {option_positions_bootstrap_message or option_positions_bootstrap_status}")
        checks.append(
            {
                "name": "option_positions_bootstrap",
                "status": bootstrap_check_status,
                "message": (option_positions_bootstrap_message or option_positions_bootstrap_status),
                "value": {"status": option_positions_bootstrap_status},
            }
        )

    candidate_evidence_check, candidate_evidence_warnings = _candidate_evidence_check(payload, mask_path=mask_path)
    if candidate_evidence_check is not None:
        checks.append(candidate_evidence_check)
        warnings.extend(candidate_evidence_warnings)

    mapping_errors: list[str] = []
    mapping_preview: dict[str, dict[str, Any]] = {}
    primary_errors: list[str] = []
    primary_preview: dict[str, dict[str, Any]] = {}
    account_views = {item.account: item for item in list_account_config_views(cfg)}
    account_settings = _dict(cfg.get("account_settings"))
    for account in accounts:
        account_view = account_views[account]
        source_plan = account_view.portfolio_source_plan
        account_type = account_view.account_type
        mapped_ids = account_view.futu_acc_ids
        primary_preview[account] = {
            "type": account_type,
            "source": source_plan.primary_source,
            "ready": False,
        }
        mapping_preview[account] = {
            "type": account_type,
            "futu_account_ids": [mask_account_id(x) for x in mapped_ids],
        }
        if account_type == "futu":
            if not mapped_ids:
                mapping_errors.append(f"{account}: missing trade_intake.account_mapping.futu entry")
                primary_errors.append(f"{account}: missing trade_intake.account_mapping.futu entry")
                continue
            account_setting = _dict(account_settings.get(account))
            futu_setting = _dict(account_setting.get("futu"))
            configured_acc_id = str(futu_setting.get("account_id") or "").strip()
            if configured_acc_id and configured_acc_id not in {str(x).strip() for x in mapped_ids}:
                mapping_errors.append(
                    f"{account}: account_settings.{account}.futu.account_id={configured_acc_id} missing from trade_intake.account_mapping.futu"
                )
                primary_errors.append(
                    f"{account}: account_settings.{account}.futu.account_id={configured_acc_id} missing from trade_intake.account_mapping.futu"
                )
            for acc_id in mapped_ids:
                if str(acc_id).startswith("REAL_"):
                    mapping_errors.append(f"{account}: placeholder futu acc_id {acc_id}")
                    primary_errors.append(f"{account}: placeholder futu acc_id {acc_id}")
                elif not str(acc_id).isdigit():
                    mapping_errors.append(f"{account}: futu acc_id must be digits only")
                    primary_errors.append(f"{account}: futu acc_id must be digits only")
            primary_preview[account]["futu_account_ids"] = [mask_account_id(x) for x in mapped_ids]
            primary_preview[account]["ready"] = not any(msg.startswith(f"{account}:") for msg in primary_errors)
            continue

        if not feishu_ready:
            mapping_errors.append(
                f"{account}: external_holdings requires {feishu_holdings.app_id_env}/{feishu_holdings.app_secret_env}"
            )
            primary_errors.append(
                f"{account}: external_holdings requires {feishu_holdings.app_id_env}/{feishu_holdings.app_secret_env}"
            )
        if "/" not in feishu_holdings.holdings_ref:
            mapping_errors.append(f"{account}: external_holdings requires {feishu_holdings.holdings_env}")
            primary_errors.append(f"{account}: external_holdings requires {feishu_holdings.holdings_env}")
        primary_preview[account]["holdings_account"] = source_plan.holdings_account
        primary_preview[account]["ready"] = bool(holdings_ready)

    checks.append(
        {
            "name": "account_primary_paths",
            "status": ("error" if primary_errors else "ok"),
            "message": ("; ".join(primary_errors) if primary_errors else f"resolved primary account paths for {len(accounts)} account(s)"),
            "value": primary_preview,
        }
    )
    checks.append(
        {
            "name": "account_mapping",
            "status": ("error" if mapping_errors else "ok"),
            "message": ("; ".join(mapping_errors) if mapping_errors else f"resolved account setup for {len(accounts)} account(s)"),
            "value": mapping_preview,
        }
    )
    if mapping_errors:
        warnings.append("Use `./om-agent add-account --account-type futu|external_holdings` and complete the matching mapping/config fields.")
    elif any(str(value) == "user1" for value in accounts):
        warnings.append("You are still using the starter account label 'user1'; rename it before long-term use if this is not intentional.")

    # Build account-specific health checks for OpenD
    opend_endpoints: dict[str, dict[str, Any]] = {}
    for account in accounts:
        acc_view = account_views[account]
        if acc_view.portfolio_source_plan.primary_source == "futu":
            acc_settings = infer_futu_portfolio_settings(cfg, account=account)
            host = str(acc_settings.get("host") or "").strip()
            try:
                port = int(acc_settings.get("port") or 0)
            except Exception:
                port = 0
            if host and port > 0:
                key = f"{host}:{port}"
                if key not in opend_endpoints:
                    opend_endpoints[key] = {"host": host, "port": port, "accounts": []}
                opend_endpoints[key]["accounts"].append(account)

    readiness_results: dict[str, dict[str, Any]] = {}
    for key, ep in opend_endpoints.items():
        ep_host = ep["host"]
        ep_port = ep["port"]
        readiness = run_futu_doctor(
            host=ep_host,
            port=ep_port,
            symbols=healthcheck_symbols_for_futu(cfg),
            timeout_sec=int(payload.get("timeout_sec") or 20),
            telnet_host=str(payload.get("opend_telnet_host") or "127.0.0.1"),
            telnet_port=int(payload.get("opend_telnet_port") or 22222),
        )
        readiness_results[key] = readiness

    # Global path if no specific account needs Futu but global settings exist.
    futu_settings = infer_futu_portfolio_settings(cfg)
    futu_host = str(futu_settings.get("host") or "").strip()
    try:
        futu_port = int(futu_settings.get("port") or 0)
    except Exception:
        futu_port = 0

    if opend_endpoints:
        aggregate_readiness_status = "ok"
        aggregate_readiness_message = "all OpenD readiness checks passed"
        for key, ep in opend_endpoints.items():
            ep_host = ep["host"]
            ep_port = ep["port"]
            readiness = readiness_results[key]
            readiness_ok = bool(readiness.get("ok"))
            ep_accounts = ep["accounts"]

            if readiness_ok:
                readiness_message = f"OpenD readiness passed for {', '.join(ep_accounts)}"
            else:
                watchdog = _dict(readiness.get("watchdog"))
                readiness_message = f"{', '.join(ep_accounts)}: " + str(
                    watchdog.get("message")
                    or watchdog.get("error")
                    or readiness.get("message")
                    or readiness.get("watchdog_raw")
                    or "OpenD readiness probe failed"
                )

            checks.append(
                {
                    "name": f"opend_readiness_{key.replace('.', '_').replace(':', '_')}",
                    "status": ("ok" if readiness_ok else "error"),
                    "message": readiness_message,
                    "value": {
                        "host": ep_host,
                        "port": ep_port,
                        "accounts": ep_accounts,
                        "global_state": _dict(readiness.get("watchdog")).get("state"),
                        "telnet": _dict(readiness.get("telnet")),
                    },
                }
            )
            if not readiness_ok:
                aggregate_readiness_status = "error"
                aggregate_readiness_message = readiness_message
                warnings.append(f"OpenD endpoint {key} for {', '.join(ep_accounts)} is not ready.")
            telnet = _dict(readiness.get("telnet"))
            if telnet and not bool(telnet.get("ok")):
                warnings.append("OpenD Telnet is not listening; phone verification cannot be submitted through telnet.")
        checks.append(
            {
                "name": "opend_readiness",
                "status": aggregate_readiness_status,
                "message": aggregate_readiness_message,
            }
        )
    elif futu_host and futu_port > 0:
        readiness = run_futu_doctor(
            host=futu_host,
            port=futu_port,
            symbols=healthcheck_symbols_for_futu(cfg),
            timeout_sec=int(payload.get("timeout_sec") or 20),
            telnet_host=str(payload.get("opend_telnet_host") or "127.0.0.1"),
            telnet_port=int(payload.get("opend_telnet_port") or 22222),
        )
        readiness_ok = bool(readiness.get("ok"))
        checks.append(
            {
                "name": "opend_readiness_global",
                "status": ("ok" if readiness_ok else "error"),
                "message": (readiness.get("message") or "Global OpenD readiness passed"),
                "value": {
                    "host": futu_host,
                    "port": futu_port,
                    "global_state": _dict(readiness.get("watchdog")).get("state"),
                    "telnet": _dict(readiness.get("telnet")),
                },
            }
        )
        telnet = _dict(readiness.get("telnet"))
        if telnet and not bool(telnet.get("ok")):
            warnings.append("OpenD Telnet is not listening; phone verification cannot be submitted through telnet.")
    else:
        checks.append(
            {
                "name": "opend_endpoint",
                "status": "error",
                "message": "OpenD host/port not found in account_settings or symbols[].fetch",
            }
        )
        warnings.append("Set account_settings.<account>.futu.host/port or symbols[].fetch.source=futu for the public install flow.")

    opend_ready = bool(
        any(str(item.get("name") or "").startswith("opend_readiness") and item.get("status") == "ok" for item in checks)
    )
    account_paths: dict[str, dict[str, Any]] = {}
    for account in accounts:
        primary = dict(primary_preview.get(account) or {})
        primary_source = str(primary.get("source") or "").strip()
        primary_ok = bool(primary.get("ready")) and opend_ready if primary_source == "futu" else bool(primary.get("ready"))

        account_paths[account] = {
            "type": str(primary.get("type") or ""),
            "primary": {
                "source": (primary_source or None),
                "ok": bool(primary_ok),
                **({"futu_account_ids": primary.get("futu_account_ids")} if primary.get("futu_account_ids") is not None else {}),
                **({"holdings_account": primary.get("holdings_account")} if primary.get("holdings_account") is not None else {}),
            },
        }

    tools = _agent_tool_availability(write_enabled=write_tools_enabled())
    critical = [item for item in checks if item["status"] == "error"]
    return (
        {
            "config": {
                "config_path": mask_path(config_path),
                "accounts": accounts,
            },
            "environment": environment,
            "account_paths": account_paths,
            "channel_health": channel_health,
            "channel_status": channel_status,
            "checks": checks,
            "tools": tools,
            "side_lanes": {
                "research_shadow_replay": {
                    "available": True,
                    "agent_tool": False,
                    "entrypoint": "./om research ...",
                    "mode": "offline_evidence_and_replay",
                }
            },
            "summary": {
                "ok": not critical,
                "critical_count": len(critical),
                "warning_count": len(warnings) + len([item for item in checks if item["status"] == "warn"]),
            },
        },
        warnings,
        {"config_path": mask_path(config_path)},
    )


def _feishu_inbound_check(
    payload: dict[str, Any],
    *,
    mask_path: Callable[[Any], str],
    environ: dict[str, str],
) -> tuple[dict[str, Any], list[str]]:
    bot_cfg = resolve_feishu_bot_config(environ=environ)
    audit_path = _audit_db_path(payload, environ=environ)
    value: dict[str, Any] = {
        "audit_db": mask_path(audit_path),
        "audit_db_exists": audit_path.exists(),
        "credentials_configured": bot_cfg.credentials_ready,
        "allowed_open_ids_count": len(bot_cfg.allowed_open_ids),
        "pending_store": {},
    }
    configured = bool(
        bot_cfg.credentials_ready
        or bot_cfg.allowed_open_ids
        or payload.get("inbound_audit_db")
        or payload.get("audit_db")
        or environ.get("OM_INBOUND_AUDIT_DB")
    )
    if not configured and not audit_path.exists():
        return (
            {
                "name": "feishu_inbound",
                "status": "info",
                "message": "Feishu inbound is not configured and no audit DB exists",
                "value": value,
            },
            [],
        )

    problems: list[str] = []
    if not bot_cfg.credentials_ready:
        problems.append("Feishu Bot app credentials are incomplete")
    if not bot_cfg.allowed_open_ids:
        problems.append("Feishu inbound sender allowlist is empty")
    if not audit_path.exists():
        problems.append("inbound audit DB does not exist")
        return (
            {
                "name": "feishu_inbound",
                "status": "warn",
                "message": "; ".join(problems),
                "value": value,
            },
            [f"Feishu inbound audit DB missing: {mask_path(audit_path)}"],
        )

    audit_status = _read_recent_feishu_audit(audit_path, limit=5)
    value.update(audit_status)
    value["pending_store"] = _read_pending_store_status(audit_path)
    if audit_status.get("error"):
        problems.append(str(audit_status["error"]))
    if value["pending_store"].get("error"):
        problems.append(str(value["pending_store"]["error"]))

    recent_rows = audit_status.get("recent_rows") if isinstance(audit_status.get("recent_rows"), list) else []
    if not recent_rows:
        problems.append("no recent Feishu inbound audit events found")
    else:
        latest = _dict(recent_rows[0])
        missing_latest_fields = [
            key
            for key in ("sender_id", "conversation_id", "message_id")
            if not str(latest.get(key) or "").strip()
        ]
        value["latest_event"] = {
            "created_at": latest.get("created_at"),
            "sender_id": latest.get("sender_id"),
            "conversation_id": latest.get("conversation_id"),
            "message_id": latest.get("message_id"),
            "intent_name": latest.get("intent_name"),
            "decision": latest.get("decision"),
            "result_ok": bool(latest.get("result_ok")),
            "missing_fields": missing_latest_fields,
        }
        if missing_latest_fields:
            problems.append("latest Feishu inbound event is missing " + ", ".join(missing_latest_fields))
        sender = str(latest.get("sender_id") or "").strip()
        if bot_cfg.allowed_open_ids and sender and sender not in set(bot_cfg.allowed_open_ids):
            problems.append("latest Feishu sender is not in OM_FEISHU_BOT_ALLOWED_OPEN_IDS")

    if not problems:
        return (
            {
                "name": "feishu_inbound",
                "status": "ok",
                "message": "Feishu inbound audit and pending store are readable",
                "value": value,
            },
            [],
        )
    return (
        {
            "name": "feishu_inbound",
            "status": "warn",
            "message": "; ".join(problems),
            "value": value,
        },
        ["Feishu inbound check warning: " + "; ".join(problems)],
    )


def _channel_health_check(channel_status: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    channels_raw = channel_status.get("channels")
    channels: dict[str, Any] = channels_raw if isinstance(channels_raw, dict) else {}
    unavailable = [
        str(name)
        for name, raw in channels.items()
        if isinstance(raw, dict)
        and (bool(raw.get("profile_enabled")) or bool(raw.get("service_present")))
        and not bool(raw.get("available"))
    ]
    status = "warn" if unavailable else "ok"
    message = "channel health is readable"
    warnings: list[str] = []
    if unavailable:
        message = "configured channel is not available: " + ", ".join(sorted(unavailable))
        warnings.append(message)
    return (
        {
            "name": "channel_health",
            "status": status,
            "message": message,
            "value": channel_status,
        },
        warnings,
    )


def _feishu_ws_service_check(payload: dict[str, Any], *, mask_path: Callable[[Any], str]) -> tuple[dict[str, Any], list[str]]:
    profile_raw = str(payload.get("profile_path") or "").strip()
    include_status = bool(payload.get("include_service_status"))
    if not profile_raw:
        return (
            {
                "name": "feishu_ws_service",
                "status": "info",
                "message": "service profile not provided; skip Feishu WS service status",
                "value": {"status_checked": False},
            },
            [],
        )
    profile_path = Path(profile_raw).expanduser()
    value: dict[str, Any] = {
        "profile_path": mask_path(profile_path),
        "status_checked": include_status,
    }
    if not profile_path.exists():
        return (
            {
                "name": "feishu_ws_service",
                "status": "warn",
                "message": "service profile does not exist",
                "value": value,
            },
            [f"Feishu WS service profile missing: {mask_path(profile_path)}"],
        )
    try:
        profile = load_service_profile(profile_path)
        service_status = service_status_from_profile(profile, include_status=include_status)
    except Exception as exc:
        return (
            {
                "name": "feishu_ws_service",
                "status": "warn",
                "message": f"failed to inspect service profile: {type(exc).__name__}: {exc}",
                "value": value,
            },
            [f"Feishu WS service profile inspect failed: {type(exc).__name__}: {exc}"],
        )
    services = [_dict(item) for item in service_status.get("services") or []]
    feishu_service = next((item for item in services if str(item.get("name") or "") == "options-monitor-feishu-ws.service"), None)
    value.update(
        {
            "provider": service_status.get("provider"),
            "service_present": feishu_service is not None,
            "service": feishu_service,
        }
    )
    if feishu_service is None:
        return (
            {
                "name": "feishu_ws_service",
                "status": "warn",
                "message": "options-monitor-feishu-ws.service is not present in service profile",
                "value": value,
            },
            ["Feishu WS service is missing from service profile."],
        )
    if include_status and str(feishu_service.get("status") or "") != "ok":
        return (
            {
                "name": "feishu_ws_service",
                "status": "warn",
                "message": "options-monitor-feishu-ws.service is not active",
                "value": value,
            },
            ["Feishu WS service is not active."],
        )
    message = "options-monitor-feishu-ws.service is present"
    if include_status:
        message = "options-monitor-feishu-ws.service is active"
    return (
        {
            "name": "feishu_ws_service",
            "status": "ok",
            "message": message,
            "value": value,
        },
        [],
    )


def _audit_db_path(payload: dict[str, Any], *, environ: dict[str, str]) -> Path:
    raw = str(payload.get("inbound_audit_db") or payload.get("audit_db") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    raw = str(environ.get("OM_INBOUND_AUDIT_DB") or "").strip()
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else (repo_base() / path).resolve()
    return default_audit_db_path()


def _read_recent_feishu_audit(path: Path, *, limit: int) -> dict[str, Any]:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='inbound_command_audit'"
            ).fetchone()
            if table is None:
                return {"audit_table_present": False, "recent_count": 0, "recent_rows": []}
            rows = conn.execute(
                """
                SELECT *
                FROM inbound_command_audit
                WHERE channel = 'feishu'
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 20)),),
            ).fetchall()
        out = [_row_to_public_dict(row) for row in rows]
        return {"audit_table_present": True, "recent_count": len(out), "recent_rows": out}
    except Exception as exc:
        return {
            "audit_table_present": None,
            "recent_count": 0,
            "recent_rows": [],
            "error": f"failed to read inbound audit DB: {type(exc).__name__}: {exc}",
        }


def _read_pending_store_status(path: Path) -> dict[str, Any]:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='inbound_pending_operations'"
            ).fetchone()
            if table is None:
                return {"readable": True, "table_present": False, "previewed_count": 0}
            previewed_count = conn.execute(
                "SELECT COUNT(*) FROM inbound_pending_operations WHERE status = 'previewed'"
            ).fetchone()[0]
        return {"readable": True, "table_present": True, "previewed_count": int(previewed_count or 0)}
    except Exception as exc:
        return {
            "readable": False,
            "table_present": None,
            "previewed_count": 0,
            "error": f"failed to read inbound pending store: {type(exc).__name__}: {exc}",
        }


def _row_to_public_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}
