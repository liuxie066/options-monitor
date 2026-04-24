from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from scripts.account_config import (
    ACCOUNT_TYPE_EXTERNAL_HOLDINGS,
    ACCOUNT_TYPE_FUTU,
    accounts_from_config,
    has_holdings_fallback,
    normalize_accounts,
    resolve_account_type,
    resolve_configured_holdings_account,
    resolve_holdings_account,
)
from scripts.agent_plugin.config import load_runtime_config, repo_base, resolve_output_root, write_tools_enabled
from scripts.agent_plugin.contracts import AgentToolError, mask_path
from scripts.close_advice import run_close_advice
from scripts.config_loader import resolve_watchlist_config
from domain.domain.fetch_source import resolve_symbol_fetch_source
from scripts.futu_portfolio_context import infer_futu_portfolio_settings, resolve_trade_intake_futu_account_ids
from scripts.notify_symbols import build_notification
from scripts.pipeline_context import load_option_positions_context, load_portfolio_context
from scripts.query_sell_put_cash import query_sell_put_cash
from scripts.io_utils import safe_read_csv
from scripts.validate_config import validate_config


def _normalize_broker(value: Any) -> str:
    return str(value or "富途").strip() or "富途"


def _resolve_data_config_ref(payload: dict[str, Any], portfolio_cfg: dict[str, Any]) -> str | None:
    value = (
        payload.get("data_config")
        or payload.get("pm_config")
        or portfolio_cfg.get("data_config")
        or portfolio_cfg.get("pm_config")
    )
    raw = str(value or "").strip()
    return raw or None


def _resolve_public_data_config_path(payload: dict[str, Any], portfolio_cfg: dict[str, Any]) -> Path:
    raw = _resolve_data_config_ref(payload, portfolio_cfg)
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (repo_base() / path).resolve()
        return path
    return (repo_base() / "secrets" / "portfolio.sqlite.json").resolve()


def _resolve_local_path(value: Any, *, default: Path) -> Path:
    raw = str(value or "").strip()
    if not raw:
        return default.resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (repo_base() / path).resolve()
    return path


def _symbol_fetch_config_map(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in resolve_watchlist_config(cfg):
        symbol = str(item.get("symbol") or "").strip().upper()
        if symbol and isinstance(item, dict):
            out[symbol] = item
    return out


def _extract_context_symbols(ctx: dict[str, Any]) -> list[str]:
    rows = ctx.get("open_positions_min") if isinstance(ctx, dict) else []
    out: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def fetch_symbol_opend(*args: Any, **kwargs: Any) -> Any:
    from scripts.fetch_market_data_opend import fetch_symbol as _fetch_symbol_opend

    return _fetch_symbol_opend(*args, **kwargs)


def save_required_data_opend(*args: Any, **kwargs: Any) -> Any:
    from scripts.fetch_market_data_opend import save_outputs as _save_required_data_opend

    return _save_required_data_opend(*args, **kwargs)


def fetch_symbol_yahoo(*args: Any, **kwargs: Any) -> Any:
    from scripts.fetch_market_data import fetch_symbol as _fetch_symbol_yahoo

    return _fetch_symbol_yahoo(*args, **kwargs)


def save_required_data_yahoo(*args: Any, **kwargs: Any) -> Any:
    from scripts.fetch_market_data import save_outputs as _save_required_data_yahoo

    return _save_required_data_yahoo(*args, **kwargs)


def _read_json_object_or_empty(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _validate_runtime_config(cfg: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    try:
        validate_config(deepcopy(cfg))
    except SystemExit as exc:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=str(exc),
        ) from exc
    return warnings


def _as_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _scan_summary_rows(summary_rows: list[dict[str, Any]]) -> dict[str, Any]:
    strategy_counts = {"sell_put": 0, "sell_call": 0}
    account_counts: dict[str, int] = {}
    symbol_counts: dict[str, int] = {}
    candidates: list[dict[str, Any]] = []
    for row in summary_rows:
        if not isinstance(row, dict):
            continue
        strategy = str(row.get("side") or row.get("strategy") or row.get("option_strategy") or "").strip().lower()
        if strategy in strategy_counts:
            strategy_counts[strategy] += 1
        account = str(row.get("account") or row.get("account_label") or "").strip().lower()
        if account:
            account_counts[account] = account_counts.get(account, 0) + 1
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol:
            symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
        candidates.append(
            {
                "symbol": symbol or None,
                "account": account or None,
                "strategy": strategy or None,
                "net_income": _as_float(row.get("net_income")),
                "annualized_return": _as_float(
                    row.get("annualized_net_return")
                    or row.get("annualized_return")
                    or row.get("annualized")
                ),
                "strike": _as_float(row.get("strike")),
                "expiration": (str(row.get("expiration") or "").strip() or None),
            }
        )
    top_candidates = sorted(
        candidates,
        key=lambda item: (
            -(item["net_income"] if item["net_income"] is not None else -10**12),
            -(item["annualized_return"] if item["annualized_return"] is not None else -10**12),
        ),
    )[:5]
    return {
        "row_count": len(summary_rows),
        "symbol_count": len(symbol_counts),
        "strategy_counts": strategy_counts,
        "account_counts": account_counts,
        "top_candidates": top_candidates,
    }


def _close_advice_rows_summary(csv_path: Path, text_path: Path) -> dict[str, Any]:
    df = safe_read_csv(csv_path)
    rows = df.to_dict(orient="records") if not df.empty else []
    tier_counts: dict[str, int] = {}
    account_counts: dict[str, int] = {}
    top_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        tier = str(row.get("tier") or "").strip().lower() or "none"
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        account = str(row.get("account") or "").strip().lower()
        if account:
            account_counts[account] = account_counts.get(account, 0) + 1
        top_rows.append(
            {
                "account": account or None,
                "symbol": (str(row.get("symbol") or "").strip().upper() or None),
                "option_type": (str(row.get("option_type") or "").strip().lower() or None),
                "expiration": (str(row.get("expiration") or "").strip() or None),
                "strike": _as_float(row.get("strike")),
                "tier": tier,
                "tier_label": (str(row.get("tier_label") or "").strip() or None),
                "remaining_annualized_return": _as_float(row.get("remaining_annualized_return")),
                "realized_if_close": _as_float(row.get("realized_if_close")),
            }
        )
    top_rows = sorted(
        top_rows,
        key=lambda item: (
            {"strong": 0, "medium": 1, "weak": 2, "none": 9}.get(str(item.get("tier") or "none"), 9),
            -(item["realized_if_close"] if item["realized_if_close"] is not None else -10**12),
        ),
    )[:5]
    notification_preview = ""
    try:
        notification_preview = text_path.read_text(encoding="utf-8").strip()
    except Exception:
        notification_preview = ""
    return {
        "row_count": len(rows),
        "tier_counts": tier_counts,
        "account_counts": account_counts,
        "top_rows": top_rows,
        "notification_preview": notification_preview,
    }


def _mask_account_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.isdigit():
        return f"...{raw[-4:]}"
    return raw


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    for idx, ch in enumerate(raw):
        if ch != "{":
            continue
        try:
            payload = json.loads(raw[idx:])
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _run_futu_doctor(*, host: str, port: int, symbols: list[str], timeout_sec: int) -> dict[str, Any]:
    py = (repo_base() / ".venv" / "bin" / "python").resolve()
    cmd = [str(py if py.exists() else Path(sys.executable)), "scripts/doctor_futu.py", "--host", host, "--port", str(int(port)), "--json"]
    if symbols:
        cmd.extend(["--symbols", *symbols])
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_base()),
            capture_output=True,
            text=True,
            timeout=int(timeout_sec),
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "error_code": "TIMEOUT",
            "message": f"doctor_futu timed out after {timeout_sec}s",
            "raw": str(exc),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error_code": "DOCTOR_FAILED",
            "message": f"{type(exc).__name__}: {exc}",
        }

    payload = _extract_json_object(proc.stdout or proc.stderr)
    if isinstance(payload, dict):
        payload.setdefault("returncode", int(proc.returncode))
        return payload
    raw = (proc.stdout or proc.stderr or "").strip()
    return {
        "ok": False,
        "error_code": "DOCTOR_INVALID_OUTPUT",
        "message": raw or "doctor_futu returned no JSON payload",
        "returncode": int(proc.returncode),
    }


def _healthcheck_symbols_for_futu(cfg: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for item in resolve_watchlist_config(cfg):
        fetch = item.get("fetch") if isinstance(item.get("fetch"), dict) else {}
        source = str(fetch.get("source") or "futu").strip().lower()
        if source not in {"futu", "opend"}:
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol or symbol in out:
            continue
        out.append(symbol)
        if len(out) >= 1:
            break
    return out


def _healthcheck_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(
        config_key=payload.get("config_key"),
        config_path=payload.get("config_path"),
    )
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []
    _validate_runtime_config(cfg)
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

    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    pm_config = _resolve_data_config_ref(payload, portfolio_cfg)
    pm_config_path = _resolve_public_data_config_path(payload, portfolio_cfg)
    if pm_config_path.exists():
        checks.append(
            {
                "name": "data_config",
                "status": "ok",
                "message": ("portfolio.data_config found" if pm_config else "repo-local SQLite data config found"),
                "value": mask_path(pm_config_path),
            }
        )
    else:
        checks.append(
            {
                "name": "data_config",
                "status": "error",
                "message": ("portfolio.data_config missing" if pm_config else "portfolio.data_config not configured"),
            }
        )
        warnings.append(
            "Minimal public setup requires a repo-local SQLite data config at secrets/portfolio.sqlite.json."
        )

    data_cfg = _read_json_object_or_empty(pm_config_path) if pm_config_path.exists() else {}
    feishu_cfg = data_cfg.get("feishu") if isinstance(data_cfg.get("feishu"), dict) else {}
    feishu_tables = feishu_cfg.get("tables") if isinstance(feishu_cfg.get("tables"), dict) else {}
    feishu_ready = bool(str(feishu_cfg.get("app_id") or "").strip()) and bool(str(feishu_cfg.get("app_secret") or "").strip())
    holdings_ref = str(feishu_tables.get("holdings") or "").strip()
    holdings_ready = feishu_ready and ("/" in holdings_ref)

    mapping_errors: list[str] = []
    fallback_warnings: list[str] = []
    mapping_preview: dict[str, dict[str, Any]] = {}
    primary_errors: list[str] = []
    primary_preview: dict[str, dict[str, Any]] = {}
    fallback_preview: dict[str, dict[str, Any]] = {}
    for account in accounts:
        account_type = resolve_account_type(cfg, account=account)
        mapped_ids = resolve_trade_intake_futu_account_ids(cfg, account=account)
        primary_preview[account] = {
            "type": account_type,
            "source": ("futu" if account_type == ACCOUNT_TYPE_FUTU else "holdings"),
            "ready": False,
        }
        mapping_preview[account] = {
            "type": account_type,
            "futu_account_ids": [_mask_account_id(x) for x in mapped_ids],
        }
        configured_holdings_account = resolve_configured_holdings_account(cfg, account=account)
        holdings_account = resolve_holdings_account(cfg, account=account)
        fallback_enabled = bool(str(configured_holdings_account or "").strip()) or account_type == ACCOUNT_TYPE_EXTERNAL_HOLDINGS
        fallback_preview[account] = {
            "enabled": fallback_enabled,
            "source": ("holdings" if fallback_enabled else None),
        }
        if fallback_enabled:
            mapping_preview[account]["holdings_account"] = holdings_account
            mapping_preview[account]["holdings_fallback_ready"] = bool(holdings_ready)
            fallback_preview[account]["holdings_account"] = holdings_account
            fallback_preview[account]["ready"] = bool(holdings_ready)
        if account_type == ACCOUNT_TYPE_FUTU:
            if not mapped_ids:
                mapping_errors.append(f"{account}: missing trade_intake.account_mapping.futu entry")
                primary_errors.append(f"{account}: missing trade_intake.account_mapping.futu entry")
                continue
            for acc_id in mapped_ids:
                if str(acc_id).startswith("REAL_"):
                    mapping_errors.append(f"{account}: placeholder futu acc_id {acc_id}")
                    primary_errors.append(f"{account}: placeholder futu acc_id {acc_id}")
                elif not str(acc_id).isdigit():
                    mapping_errors.append(f"{account}: futu acc_id must be digits only")
                    primary_errors.append(f"{account}: futu acc_id must be digits only")
            primary_preview[account]["futu_account_ids"] = [_mask_account_id(x) for x in mapped_ids]
            primary_preview[account]["ready"] = not any(
                msg.startswith(f"{account}:") for msg in primary_errors
            )
            if has_holdings_fallback(cfg, account=account) and not holdings_ready:
                fallback_warnings.append(
                    f"{account}: holdings fallback configured but feishu.app_id/app_secret/tables.holdings is incomplete in portfolio.data_config"
                )
            continue

        if not feishu_ready:
            mapping_errors.append(f"{account}: external_holdings requires feishu.app_id/app_secret in portfolio.data_config")
            primary_errors.append(f"{account}: external_holdings requires feishu.app_id/app_secret in portfolio.data_config")
        if "/" not in holdings_ref:
            mapping_errors.append(f"{account}: external_holdings requires feishu.tables.holdings in portfolio.data_config")
            primary_errors.append(f"{account}: external_holdings requires feishu.tables.holdings in portfolio.data_config")
        primary_preview[account]["holdings_account"] = holdings_account
        primary_preview[account]["ready"] = bool(holdings_ready)
    checks.append(
        {
            "name": "account_primary_paths",
            "status": ("error" if primary_errors else "ok"),
            "message": (
                "; ".join(primary_errors)
                if primary_errors
                else f"resolved primary account paths for {len(accounts)} account(s)"
            ),
            "value": primary_preview,
        }
    )
    checks.append(
        {
            "name": "account_fallback_paths",
            "status": ("warn" if fallback_warnings else "ok"),
            "message": (
                "; ".join(fallback_warnings)
                if fallback_warnings
                else "resolved fallback account paths"
            ),
            "value": fallback_preview,
        }
    )
    checks.append(
        {
            "name": "account_mapping",
            "status": ("error" if mapping_errors else "ok"),
            "message": (
                "; ".join(mapping_errors)
                if mapping_errors
                else f"resolved account setup for {len(accounts)} account(s)"
            ),
            "value": mapping_preview,
        }
    )
    if mapping_errors:
        warnings.append("Use `./om-agent add-account --account-type futu|external_holdings` and complete the matching mapping/config fields.")
    warnings.extend(fallback_warnings)

    futu_settings = infer_futu_portfolio_settings(cfg)
    futu_host = str(futu_settings.get("host") or "").strip()
    try:
        futu_port = int(futu_settings.get("port") or 0)
    except Exception:
        futu_port = 0
    if futu_host and futu_port > 0:
        checks.append(
            {
                "name": "opend_endpoint",
                "status": "ok",
                "message": "resolved OpenD endpoint from runtime config",
                "value": {"host": futu_host, "port": futu_port},
            }
        )
        doctor = _run_futu_doctor(
            host=futu_host,
            port=futu_port,
            symbols=_healthcheck_symbols_for_futu(cfg),
            timeout_sec=int(payload.get("timeout_sec") or 20),
        )
        doctor_ok = bool(doctor.get("ok"))
        doctor_message = ""
        if doctor_ok:
            doctor_message = "Futu/OpenD dependency check passed"
        else:
            watchdog = doctor.get("watchdog") if isinstance(doctor.get("watchdog"), dict) else {}
            doctor_message = str(
                watchdog.get("message")
                or watchdog.get("error")
                or doctor.get("message")
                or doctor.get("watchdog_raw")
                or "doctor_futu failed"
            )
            warnings.append("OpenD is a required dependency for the public install flow.")
        checks.append(
            {
                "name": "opend_doctor",
                "status": ("ok" if doctor_ok else "error"),
                "message": doctor_message,
                "value": {"host": futu_host, "port": futu_port},
            }
        )
    else:
        checks.append(
            {
                "name": "opend_endpoint",
                "status": "error",
                "message": "OpenD host/port not found in symbols[].fetch",
            }
        )
        warnings.append("Set symbols[].fetch.source=futu and keep host/port configured for the public install flow.")

    opend_ready = bool(futu_host and futu_port > 0 and any(item.get("name") == "opend_doctor" and item.get("status") == "ok" for item in checks))
    account_paths: dict[str, dict[str, Any]] = {}
    for account in accounts:
        primary = dict(primary_preview.get(account) or {})
        fallback = dict(fallback_preview.get(account) or {})
        primary_source = str(primary.get("source") or "").strip()
        primary_ok = False
        if primary_source == "futu":
            primary_ok = bool(primary.get("ready")) and opend_ready
        elif primary_source == "holdings":
            primary_ok = bool(primary.get("ready"))

        fallback_source = str(fallback.get("source") or "").strip()
        fallback_ok = False
        if fallback.get("enabled") and fallback_source == "holdings":
            fallback_ok = bool(fallback.get("ready"))

        account_paths[account] = {
            "type": resolve_account_type(cfg, account=account),
            "primary": {
                "source": (primary_source or None),
                "ok": bool(primary_ok),
                **({"futu_account_ids": primary.get("futu_account_ids")} if primary.get("futu_account_ids") is not None else {}),
                **({"holdings_account": primary.get("holdings_account")} if primary.get("holdings_account") is not None else {}),
            },
            "fallback": {
                "enabled": bool(fallback.get("enabled")),
                "source": (fallback_source or None),
                "ok": bool(fallback_ok),
                **({"holdings_account": fallback.get("holdings_account")} if fallback.get("holdings_account") is not None else {}),
            },
        }

    tools = {
        "healthcheck": {"available": True, "mode": "read"},
        "scan_opportunities": {"available": True, "mode": "read_with_local_cache"},
        "query_cash_headroom": {"available": True, "mode": "read_with_local_cache"},
        "get_portfolio_context": {"available": True, "mode": "read_with_local_cache"},
        "prepare_close_advice_inputs": {"available": True, "mode": "read_with_local_cache"},
        "close_advice": {"available": True, "mode": "read_with_local_cache"},
        "get_close_advice": {"available": True, "mode": "read_with_local_cache"},
        "manage_symbols": {"available": True, "mode": ("write" if write_tools_enabled() else "read_preview_only")},
        "preview_notification": {"available": True, "mode": "read"},
    }
    critical = [x for x in checks if x["status"] == "error"]
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
                "warning_count": len(warnings) + len([x for x in checks if x["status"] == "warn"]),
            },
        },
        warnings,
        {"config_path": mask_path(config_path)},
    )


def _query_cash_headroom_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(
        config_key=payload.get("config_key"),
        config_path=payload.get("config_path"),
    )
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    pm_config_path = _resolve_public_data_config_path(payload, portfolio_cfg)
    broker = _normalize_broker(
        payload.get("broker")
        or payload.get("market")
        or portfolio_cfg.get("broker")
        or portfolio_cfg.get("market")
    )
    out_root = resolve_output_root(payload.get("output_dir"))
    out_dir = (out_root / "query_cash_headroom").resolve()
    result = query_sell_put_cash(
        config=str(config_path),
        pm_config=str(pm_config_path),
        market=broker,
        account=(str(payload.get("account")).strip() if payload.get("account") else None),
        output_format="json",
        top=int(payload.get("top") or 10),
        no_fx=bool(payload.get("no_fx", False)),
        out_dir=str(out_dir),
        base_dir=repo_base(),
        runtime_config=cfg,
    )
    return result, [], {"config_path": mask_path(config_path), "output_dir": mask_path(out_dir)}


def _get_portfolio_context_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(
        config_key=payload.get("config_key"),
        config_path=payload.get("config_path"),
    )
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    account = str(payload.get("account") or portfolio_cfg.get("account") or "").strip() or None
    broker = _normalize_broker(
        payload.get("broker")
        or payload.get("market")
        or portfolio_cfg.get("broker")
        or portfolio_cfg.get("market")
    )
    pm_config = str(_resolve_public_data_config_path(payload, portfolio_cfg))

    out_root = resolve_output_root(payload.get("output_dir"))
    state_dir = (out_root / "portfolio_context_state").resolve()
    shared_dir = (out_root / "shared").resolve()
    logs: list[str] = []
    ctx = load_portfolio_context(
        py=str((repo_base() / ".venv" / "bin" / "python").resolve()),
        base=repo_base(),
        pm_config=pm_config,
        market=broker,
        account=account,
        ttl_sec=int(payload.get("ttl_sec") or 0),
        timeout_sec=int(payload.get("timeout_sec") or 60),
        is_scheduled=False,
        state_dir=state_dir,
        shared_state_dir=shared_dir,
        log=logs.append,
        runtime_config=cfg,
    )
    if not isinstance(ctx, dict):
        raise AgentToolError(
            code="DEPENDENCY_MISSING",
            message="portfolio context is unavailable",
            details={"logs": logs[-5:]},
        )
    warnings = [x for x in logs if x.startswith("[WARN]")]
    return ctx, warnings, {"config_path": mask_path(config_path), "state_dir": mask_path(state_dir)}


def _scan_opportunities_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(
        config_key=payload.get("config_key"),
        config_path=payload.get("config_path"),
    )

    from scripts.config_loader import load_config
    from scripts.config_profiles import apply_profiles
    from scripts.pipeline_context import build_pipeline_context
    from scripts.pipeline_symbol import process_symbol
    from scripts.pipeline_watchlist import run_watchlist_pipeline
    from scripts.report_builders import build_symbols_digest, build_symbols_summary

    def _log(_msg: str) -> None:
        return None

    out_root = resolve_output_root(payload.get("output_dir"))
    report_dir = (out_root / "reports").resolve()
    state_dir = (out_root / "state").resolve()
    shared_state_dir = (out_root / "shared").resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    shared_state_dir.mkdir(parents=True, exist_ok=True)

    cfg_loaded = load_config(
        base=repo_base(),
        config_path=config_path,
        is_scheduled=False,
        log=_log,
        state_dir=state_dir,
    )
    if isinstance(cfg.get("portfolio"), dict):
        cfg_loaded["portfolio"] = deepcopy(cfg["portfolio"])
    if isinstance(cfg_loaded.get("portfolio"), dict):
        data_config_ref = _resolve_data_config_ref(payload, cfg_loaded["portfolio"])
        if data_config_ref:
            cfg_loaded["portfolio"]["data_config"] = data_config_ref
            cfg_loaded["portfolio"]["pm_config"] = data_config_ref

    top_n = int(payload.get("top_n") or (cfg_loaded.get("outputs", {}) or {}).get("top_n_alerts", 3) or 3)
    runtime = cfg_loaded.get("runtime", {}) or {}
    summary_rows = run_watchlist_pipeline(
        py=str((repo_base() / ".venv" / "bin" / "python").resolve()),
        base=repo_base(),
        cfg=cfg_loaded,
        report_dir=report_dir,
        is_scheduled=False,
        top_n=top_n,
        symbol_timeout_sec=int(payload.get("symbol_timeout_sec") or runtime.get("symbol_timeout_sec", 120) or 120),
        portfolio_timeout_sec=int(payload.get("portfolio_timeout_sec") or runtime.get("portfolio_timeout_sec", 60) or 60),
        want_scan=True,
        no_context=bool(payload.get("no_context", False)),
        symbols_arg=(",".join(payload.get("symbols")) if isinstance(payload.get("symbols"), list) else payload.get("symbols")),
        log=_log,
        want_fn=lambda _step: True,
        apply_profiles_fn=apply_profiles,
        process_symbol_fn=(
            lambda *a, **kw: process_symbol(
                *a,
                **kw,
                required_data_dir=out_root,
                report_dir=report_dir,
                state_dir=state_dir,
                is_scheduled=False,
            )
        ),
        build_pipeline_context_fn=(
            lambda **kw: build_pipeline_context(
                **kw,
                state_dir=state_dir,
                shared_state_dir=shared_state_dir,
            )
        ),
        build_symbols_summary_fn=lambda rows: build_symbols_summary(rows, report_dir, is_scheduled=False),
        build_symbols_digest_fn=lambda rows, n: build_symbols_digest([r.get("symbol") for r in rows if r.get("symbol")], report_dir),
    )
    summary = _scan_summary_rows(summary_rows)
    return {
        "summary_rows": summary_rows,
        "symbol_count": len({str(r.get("symbol") or "").strip() for r in summary_rows if str(r.get("symbol") or "").strip()}),
        "row_count": len(summary_rows),
        "summary": summary,
        "top_candidates": summary["top_candidates"],
    }, [], {"config_path": mask_path(config_path), "report_dir": mask_path(report_dir)}


def _prepare_close_advice_inputs_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(
        config_key=payload.get("config_key"),
        config_path=payload.get("config_path"),
    )
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    pm_config = str(_resolve_public_data_config_path(payload, portfolio_cfg))
    account = str(payload.get("account") or portfolio_cfg.get("account") or "").strip() or None
    broker = _normalize_broker(
        payload.get("broker")
        or payload.get("market")
        or portfolio_cfg.get("broker")
        or portfolio_cfg.get("market")
    )
    out_root = resolve_output_root(payload.get("output_dir"))
    state_dir = (out_root / "state").resolve()
    shared_dir = (out_root / "shared").resolve()
    required_data_root = (out_root / "required_data").resolve()
    required_data_root.mkdir(parents=True, exist_ok=True)
    logs: list[str] = []

    try:
        ctx, _refreshed = load_option_positions_context(
            py=str((repo_base() / ".venv" / "bin" / "python").resolve()),
            base=repo_base(),
            pm_config=pm_config,
            market=broker,
            account=account,
            ttl_sec=int(payload.get("ttl_sec") or 0),
            timeout_sec=int(payload.get("timeout_sec") or 60),
            is_scheduled=False,
            report_dir=(out_root / "reports").resolve(),
            state_dir=state_dir,
            shared_state_dir=shared_dir,
            log=logs.append,
        )
    except SystemExit as exc:
        raise AgentToolError(
            code="DEPENDENCY_MISSING",
            message="option positions context refresh failed",
            hint="Check portfolio.data_config / SQLite option_positions setup before preparing close_advice inputs.",
            details={"exit_code": str(exc)},
        ) from exc
    if not isinstance(ctx, dict):
        raise AgentToolError(
            code="DEPENDENCY_MISSING",
            message="option positions context is unavailable",
            details={"logs": logs[-5:]},
        )

    symbol_map = _symbol_fetch_config_map(cfg)
    fetched: list[dict[str, Any]] = []
    warnings = [x for x in logs if x.startswith("[WARN]")]
    for symbol in _extract_context_symbols(ctx):
        symbol_cfg = symbol_map.get(symbol) or {}
        fetch_cfg = symbol_cfg.get("fetch") if isinstance(symbol_cfg.get("fetch"), dict) else {}
        src, _decision = resolve_symbol_fetch_source(fetch_cfg)
        limit_expirations = int(fetch_cfg.get("limit_expirations") or 8)
        if src == "opend":
            result = fetch_symbol_opend(
                symbol,
                limit_expirations=limit_expirations,
                host=str(fetch_cfg.get("host") or "127.0.0.1"),
                port=int(fetch_cfg.get("port") or 11111),
                spot_from_yahoo=bool(fetch_cfg.get("spot_from_yahoo", False)),
                base_dir=repo_base(),
                option_types="put,call",
                chain_cache=True,
            )
            _raw_path, csv_path = save_required_data_opend(repo_base(), symbol, result, output_root=required_data_root)
        else:
            result = fetch_symbol_yahoo(symbol, limit_expirations=limit_expirations)
            _raw_path, csv_path = save_required_data_yahoo(repo_base(), symbol, result, output_root=required_data_root)
        meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
        if meta.get("error"):
            warnings.append(f"{symbol}: {meta['error']}")
        fetched.append(
            {
                "symbol": symbol,
                "source": src,
                "rows": len(result.get("rows") or []),
                "expiration_count": int(result.get("expiration_count") or 0),
                "csv": mask_path(csv_path),
            }
        )

    return {
        "account": account,
        "broker": broker,
        "context_rows": len(ctx.get("open_positions_min") or []),
        "symbols": fetched,
        "symbol_count": len(fetched),
    }, warnings, {
        "config_path": mask_path(config_path),
        "context_path": mask_path(state_dir / "option_positions_context.json"),
        "required_data_root": mask_path(required_data_root),
    }


def _close_advice_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(
        config_key=payload.get("config_key"),
        config_path=payload.get("config_path"),
    )
    out_root = resolve_output_root(payload.get("output_dir"))
    context_path = _resolve_local_path(
        payload.get("context_path"),
        default=(out_root / "state" / "option_positions_context.json"),
    )
    required_data_root = _resolve_local_path(
        payload.get("required_data_root"),
        default=(out_root / "required_data"),
    )
    report_dir = (out_root / "reports").resolve()

    if not context_path.exists():
        raise AgentToolError(
            code="DEPENDENCY_MISSING",
            message="close_advice requires a local option_positions_context.json",
            hint="Run the scan/context pipeline first, or pass context_path explicitly.",
            details={"context_path": mask_path(context_path)},
        )
    if not required_data_root.exists():
        raise AgentToolError(
            code="DEPENDENCY_MISSING",
            message="close_advice requires a local required_data directory",
            hint="Run the scan pipeline first, or pass required_data_root explicitly.",
            details={"required_data_root": mask_path(required_data_root)},
        )

    result = run_close_advice(
        config=cfg,
        context_path=context_path,
        required_data_root=required_data_root,
        output_dir=report_dir,
        base_dir=repo_base(),
    )
    advice_summary = _close_advice_rows_summary(report_dir / "close_advice.csv", report_dir / "close_advice.txt")
    result = {
        **result,
        "summary": {
            "row_count": advice_summary["row_count"],
            "tier_counts": advice_summary["tier_counts"],
            "account_counts": advice_summary["account_counts"],
        },
        "top_rows": advice_summary["top_rows"],
        "notification_preview": advice_summary["notification_preview"],
    }
    return result, [], {
        "config_path": mask_path(config_path),
        "context_path": mask_path(context_path),
        "required_data_root": mask_path(required_data_root),
        "output_dir": mask_path(report_dir),
    }


def _get_close_advice_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    prepared_data, prepare_warnings, prepare_meta = _prepare_close_advice_inputs_tool(payload)
    advice_data, advice_warnings, advice_meta = _close_advice_tool(payload)
    combined_summary = {
        "prepared_symbol_count": int(prepared_data.get("symbol_count") or 0),
        "prepared_context_rows": int(prepared_data.get("context_rows") or 0),
        "advice_row_count": int(advice_data.get("rows") or advice_data.get("summary", {}).get("row_count") or 0),
        "notify_row_count": int(advice_data.get("notify_rows") or 0),
        "tier_counts": (
            dict(advice_data.get("summary", {}).get("tier_counts"))
            if isinstance(advice_data.get("summary"), dict) and isinstance(advice_data.get("summary", {}).get("tier_counts"), dict)
            else {}
        ),
    }
    return {
        "prepared": prepared_data,
        "close_advice": advice_data,
        "summary": combined_summary,
        "top_rows": list(advice_data.get("top_rows") or []),
        "notification_preview": advice_data.get("notification_preview"),
    }, [*prepare_warnings, *advice_warnings], {
        **prepare_meta,
        **advice_meta,
    }


def _list_symbol_rows(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in resolve_watchlist_config(cfg):
        fetch = item.get("fetch") if isinstance(item.get("fetch"), dict) else {}
        sell_put = item.get("sell_put") if isinstance(item.get("sell_put"), dict) else {}
        sell_call = item.get("sell_call") if isinstance(item.get("sell_call"), dict) else {}
        rows.append(
            {
                "symbol": str(item.get("symbol") or "").strip().upper(),
                "market": item.get("market"),
                "accounts": normalize_accounts(item.get("accounts"), fallback=()) if item.get("accounts") is not None else None,
                "use": item.get("use"),
                "limit_expirations": fetch.get("limit_expirations"),
                "sell_put": dict(sell_put),
                "sell_call": dict(sell_call),
            }
        )
    return rows


def _find_symbol_entry(cfg: dict[str, Any], symbol: str) -> tuple[int | None, dict[str, Any] | None]:
    needle = str(symbol or "").strip().upper()
    for idx, item in enumerate(resolve_watchlist_config(cfg)):
        if str(item.get("symbol") or "").strip().upper() == needle:
            return idx, item
    return None, None


def _set_path(obj: dict[str, Any], path: str, value: Any) -> None:
    cur = obj
    parts = [str(x).strip() for x in str(path).split(".") if str(x).strip()]
    if not parts:
        raise AgentToolError(code="INPUT_ERROR", message="set path cannot be empty")
    for key in parts[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _apply_symbol_mutation(cfg: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "list").strip().lower()
    symbol = str(payload.get("symbol") or "").strip().upper()
    symbols = cfg.get("symbols")
    if symbols is None:
        cfg["symbols"] = []
        symbols = cfg["symbols"]
    if not isinstance(symbols, list):
        raise AgentToolError(code="CONFIG_ERROR", message="config symbols must be a list")

    if action == "list":
        return cfg

    idx, entry = _find_symbol_entry(cfg, symbol)
    if action == "remove":
        if idx is None:
            raise AgentToolError(code="INPUT_ERROR", message=f"symbol not found: {symbol}")
        symbols.pop(idx)
        return cfg

    if action == "add":
        if not symbol:
            raise AgentToolError(code="INPUT_ERROR", message="symbol is required for add")
        if entry is not None:
            raise AgentToolError(code="INPUT_ERROR", message=f"symbol already exists: {symbol}")
        sell_put_enabled = bool(payload.get("sell_put_enabled", False))
        sell_call_enabled = bool(payload.get("sell_call_enabled", False))
        if sell_put_enabled:
            for key in ("sell_put_min_dte", "sell_put_max_dte", "sell_put_min_strike", "sell_put_max_strike"):
                if payload.get(key) is None:
                    raise AgentToolError(
                        code="INPUT_ERROR",
                        message=f"{key} is required when sell_put_enabled=true",
                    )
        if sell_call_enabled:
            for key in ("sell_call_min_dte", "sell_call_max_dte", "sell_call_min_strike"):
                if payload.get(key) is None:
                    raise AgentToolError(
                        code="INPUT_ERROR",
                        message=f"{key} is required when sell_call_enabled=true",
                    )
        entry = {
            "symbol": symbol,
            "fetch": {"limit_expirations": int(payload.get("limit_expirations") or 8)},
            "sell_put": {"enabled": sell_put_enabled},
            "sell_call": {"enabled": sell_call_enabled},
        }
        if sell_put_enabled:
            entry["sell_put"].update(
                {
                    "min_dte": int(payload.get("sell_put_min_dte")),
                    "max_dte": int(payload.get("sell_put_max_dte")),
                    "min_strike": float(payload.get("sell_put_min_strike")),
                    "max_strike": float(payload.get("sell_put_max_strike")),
                }
            )
        if sell_call_enabled:
            entry["sell_call"].update(
                {
                    "min_dte": int(payload.get("sell_call_min_dte")),
                    "max_dte": int(payload.get("sell_call_max_dte")),
                    "min_strike": float(payload.get("sell_call_min_strike")),
                }
            )
        if payload.get("market") is not None:
            entry["market"] = payload.get("market")
        if payload.get("use") is not None:
            entry["use"] = payload.get("use")
        if payload.get("accounts") is not None:
            entry["accounts"] = normalize_accounts(payload.get("accounts"), fallback=())
        symbols.append(entry)
        return cfg

    if action == "edit":
        if entry is None or idx is None:
            raise AgentToolError(code="INPUT_ERROR", message=f"symbol not found: {symbol}")
        sets = payload.get("set")
        if not isinstance(sets, dict) or not sets:
            raise AgentToolError(code="INPUT_ERROR", message="edit requires non-empty set object")
        for key, value in sets.items():
            _set_path(entry, str(key), value)
        symbols[idx] = entry
        return cfg

    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported manage_symbols action: {action}")


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _manage_symbols_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(
        config_key=payload.get("config_key"),
        config_path=payload.get("config_path"),
    )
    action = str(payload.get("action") or "list").strip().lower()
    dry_run = bool(payload.get("dry_run", False))
    confirm = bool(payload.get("confirm", False))
    if action != "list" and not dry_run:
        if not write_tools_enabled():
            raise AgentToolError(
                code="PERMISSION_DENIED",
                message="write tools are disabled",
                hint="Set OM_AGENT_ENABLE_WRITE_TOOLS=true to enable config writes.",
            )
        if not confirm:
            raise AgentToolError(
                code="CONFIRMATION_REQUIRED",
                message="confirm=true is required for non-dry-run symbol mutations",
            )

    mutated = _apply_symbol_mutation(deepcopy(cfg), payload)
    _validate_runtime_config(mutated)
    rows = _list_symbol_rows(mutated)
    result = {
        "action": action,
        "dry_run": dry_run,
        "symbols": rows,
        "symbol_count": len(rows),
    }
    if action != "list" and not dry_run:
        _write_json_atomic(config_path, mutated)
    return result, [], {"config_path": mask_path(config_path), "write_applied": (action != "list" and not dry_run)}


def _preview_notification_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    alerts_text = str(payload.get("alerts_text") or "").strip()
    changes_text = str(payload.get("changes_text") or "").strip()
    account_label = str(payload.get("account_label") or "当前账户").strip() or "当前账户"
    if not alerts_text and payload.get("alerts_path"):
        alerts_text = Path(str(payload.get("alerts_path"))).read_text(encoding="utf-8")
    if not changes_text and payload.get("changes_path"):
        changes_text = Path(str(payload.get("changes_path"))).read_text(encoding="utf-8")
    preview = build_notification(changes_text, alerts_text, account_label=account_label)
    return {
        "account_label": account_label,
        "notification_text": preview,
    }, [], {}


TOOL_HANDLERS = {
    "healthcheck": _healthcheck_tool,
    "query_cash_headroom": _query_cash_headroom_tool,
    "get_portfolio_context": _get_portfolio_context_tool,
    "scan_opportunities": _scan_opportunities_tool,
    "prepare_close_advice_inputs": _prepare_close_advice_inputs_tool,
    "close_advice": _close_advice_tool,
    "get_close_advice": _get_close_advice_tool,
    "manage_symbols": _manage_symbols_tool,
    "preview_notification": _preview_notification_tool,
}
