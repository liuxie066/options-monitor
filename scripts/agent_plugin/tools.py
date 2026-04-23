from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from scripts.account_config import accounts_from_config, normalize_accounts
from scripts.agent_plugin.config import load_runtime_config, repo_base, resolve_output_root, write_tools_enabled
from scripts.agent_plugin.contracts import AgentToolError, mask_path
from scripts.config_loader import resolve_watchlist_config
from scripts.notify_symbols import build_notification
from scripts.pipeline_context import load_portfolio_context
from scripts.query_sell_put_cash import query_sell_put_cash
from scripts.validate_config import validate_config


def _normalize_market(value: Any) -> str:
    return str(value or "富途").strip() or "富途"


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
    pm_config = portfolio_cfg.get("pm_config") if isinstance(portfolio_cfg, dict) else None
    if pm_config and Path(str(pm_config)).exists():
        checks.append({"name": "pm_config", "status": "ok", "message": "portfolio.pm_config found"})
    elif pm_config:
        checks.append({"name": "pm_config", "status": "error", "message": "portfolio.pm_config missing"})
    else:
        checks.append({"name": "pm_config", "status": "warn", "message": "portfolio.pm_config not configured"})
        warnings.append("portfolio.pm_config is not configured; Feishu/holdings-backed tools may not work.")

    tools = {
        "healthcheck": {"available": True, "mode": "read"},
        "scan_opportunities": {"available": True, "mode": "read_with_local_cache"},
        "query_cash_headroom": {"available": True, "mode": "read_with_local_cache"},
        "get_portfolio_context": {"available": True, "mode": "read_with_local_cache"},
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
    pm_config = payload.get("pm_config") or portfolio_cfg.get("pm_config")
    market = _normalize_market(payload.get("market") or portfolio_cfg.get("market"))
    out_root = resolve_output_root(payload.get("output_dir"))
    out_dir = (out_root / "query_cash_headroom").resolve()
    result = query_sell_put_cash(
        config=str(config_path),
        pm_config=(str(pm_config) if pm_config else None),
        market=market,
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
    market = _normalize_market(payload.get("market") or portfolio_cfg.get("market"))
    pm_config = str(payload.get("pm_config") or portfolio_cfg.get("pm_config") or "").strip()
    if not pm_config:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message="portfolio.pm_config is required for get_portfolio_context",
        )

    out_root = resolve_output_root(payload.get("output_dir"))
    state_dir = (out_root / "portfolio_context_state").resolve()
    shared_dir = (out_root / "shared").resolve()
    logs: list[str] = []
    ctx = load_portfolio_context(
        py=str((repo_base() / ".venv" / "bin" / "python").resolve()),
        base=repo_base(),
        pm_config=pm_config,
        market=market,
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
    return {
        "summary_rows": summary_rows,
        "symbol_count": len({str(r.get("symbol") or "").strip() for r in summary_rows if str(r.get("symbol") or "").strip()}),
        "row_count": len(summary_rows),
    }, [], {"config_path": mask_path(config_path), "report_dir": mask_path(report_dir)}


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
    "manage_symbols": _manage_symbols_tool,
    "preview_notification": _preview_notification_tool,
}
