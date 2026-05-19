from __future__ import annotations

from typing import Any, Callable

from domain.domain.multi_tick import FEISHU_APP_NOTIFICATION_PROVIDER, normalize_notification_provider
from src.application.ledger.api import ledger_store_payload
from src.application.secret_resolver import (
    resolve_feishu_bot_config,
    resolve_feishu_holdings_config,
)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


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
    feishu_holdings = resolve_feishu_holdings_config(data_cfg)
    feishu_ready = bool(feishu_holdings.app_id and feishu_holdings.app_secret)
    holdings_ready = feishu_holdings.ready
    symbol_names = {
        str(item.get("symbol") or "").strip().upper()
        for item in (cfg.get("symbols") or [])
        if isinstance(item, dict)
    }
    if symbol_names & {"NVDA", "0700.HK"}:
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
        bot_cfg = resolve_feishu_bot_config(notifications)
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

    tools = {
        "healthcheck": {"available": True, "mode": "read"},
        "version_check": {"available": True, "mode": "read"},
        "version_update": {"available": True, "mode": "write_preview_default"},
        "config_validate": {"available": True, "mode": "read"},
        "scheduler_status": {"available": True, "mode": "read"},
        "scan_opportunities": {"available": True, "mode": "read_with_local_cache"},
        "query_cash_headroom": {"available": True, "mode": "read_with_local_cache"},
        "monthly_income_report": {"available": True, "mode": "read"},
        "option_positions_read": {"available": True, "mode": "read"},
        "get_portfolio_context": {"available": True, "mode": "read_with_local_cache"},
        "prepare_close_advice_inputs": {"available": True, "mode": "read_with_local_cache"},
        "close_advice": {"available": True, "mode": "read_with_local_cache"},
        "get_close_advice": {"available": True, "mode": "read_with_local_cache"},
        "manage_symbols": {"available": True, "mode": ("write" if write_tools_enabled() else "read_preview_only")},
        "preview_notification": {"available": True, "mode": "read"},
        "runtime_status": {"available": True, "mode": "read"},
        "openclaw_readiness": {"available": True, "mode": "read"},
        "ai_cofunder": {"available": True, "mode": "read_default_write_optional"},
    }
    critical = [item for item in checks if item["status"] == "error"]
    return (
        {
            "config": {
                "config_path": mask_path(config_path),
                "accounts": accounts,
            },
            "account_paths": account_paths,
            "checks": checks,
            "tools": tools,
            "summary": {
                "ok": not critical,
                "critical_count": len(critical),
                "warning_count": len(warnings) + len([item for item in checks if item["status"] == "warn"]),
            },
        },
        warnings,
        {"config_path": mask_path(config_path)},
    )
