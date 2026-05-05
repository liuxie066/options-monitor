from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, cast

from src.application.agent_tool_contracts import AgentToolError
from src.application.watchlist_mutations import (
    ensure_symbols_list as _ensure_symbols_list,
    find_symbol_entry as _find_symbol_entry,
    normalize_symbol as _normalize_symbol,
    set_path as _shared_set_path,
)


def list_symbol_rows(cfg: dict[str, Any], *, resolve_watchlist_config, normalize_accounts) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in resolve_watchlist_config(cfg):
        fetch = item.get("fetch") if isinstance(item.get("fetch"), dict) else {}
        sell_put = item.get("sell_put") if isinstance(item.get("sell_put"), dict) else {}
        sell_call = item.get("sell_call") if isinstance(item.get("sell_call"), dict) else {}
        rows.append(
            {
                "symbol": str(item.get("symbol") or "").strip().upper(),
                "broker": item.get("broker"),
                "accounts": normalize_accounts(item.get("accounts"), fallback=()) if item.get("accounts") is not None else None,
                "use": item.get("use"),
                "limit_expirations": fetch.get("limit_expirations"),
                "sell_put": dict(sell_put),
                "sell_call": dict(sell_call),
            }
        )
    return rows


def find_symbol_entry(cfg: dict[str, Any], symbol: str, *, resolve_watchlist_config) -> tuple[int | None, dict[str, Any] | None]:
    return _find_symbol_entry(cfg, symbol, resolve_watchlist_config=resolve_watchlist_config)


def set_path(obj: dict[str, Any], path: str, value: Any) -> None:
    try:
        _shared_set_path(
            obj,
            path,
            value,
            error_factory=lambda message: AgentToolError(code="INPUT_ERROR", message=message),
        )
    except AgentToolError:
        raise


def require_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if value is None:
        raise AgentToolError(code="INPUT_ERROR", message=f"{key} is required")
    return int(value)


def require_float(payload: dict[str, Any], key: str) -> float:
    value = payload.get(key)
    if value is None:
        raise AgentToolError(code="INPUT_ERROR", message=f"{key} is required")
    return float(value)


def optional_float(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if value is None:
        return None
    return float(value)


def apply_symbol_mutation(cfg: dict[str, Any], payload: dict[str, Any], *, normalize_accounts, resolve_watchlist_config) -> dict[str, Any]:
    action = str(payload.get("action") or "list").strip().lower()
    symbol = _normalize_symbol(str(payload.get("symbol") or ""))
    symbols = _ensure_symbols_list(cfg, error_factory=lambda message: AgentToolError(code="CONFIG_ERROR", message=message))
    if action == "list":
        return cfg
    idx, found_entry = find_symbol_entry(cfg, symbol, resolve_watchlist_config=resolve_watchlist_config)
    if action == "remove":
        if idx is None:
            raise AgentToolError(code="INPUT_ERROR", message=f"symbol not found: {symbol}")
        symbols.pop(idx)
        return cfg
    if action == "add":
        if not symbol:
            raise AgentToolError(code="INPUT_ERROR", message="symbol is required for add")
        if found_entry is not None:
            raise AgentToolError(code="INPUT_ERROR", message=f"symbol already exists: {symbol}")
        sell_put_enabled = bool(payload.get("sell_put_enabled", False))
        sell_call_enabled = bool(payload.get("sell_call_enabled", False))
        if sell_put_enabled:
            for key in ("sell_put_min_dte", "sell_put_max_dte"):
                if payload.get(key) is None:
                    raise AgentToolError(code="INPUT_ERROR", message=f"{key} is required when sell_put_enabled=true")
        if sell_call_enabled:
            for key in ("sell_call_min_dte", "sell_call_max_dte"):
                if payload.get(key) is None:
                    raise AgentToolError(code="INPUT_ERROR", message=f"{key} is required when sell_call_enabled=true")
        entry: dict[str, Any] = {
            "symbol": symbol,
            "fetch": {"limit_expirations": int(payload.get("limit_expirations") or 8)},
            "sell_put": {"enabled": sell_put_enabled},
            "sell_call": {"enabled": sell_call_enabled},
        }
        if sell_put_enabled:
            sell_put_entry = cast(dict[str, Any], entry["sell_put"])
            sell_put_entry.update({"min_dte": require_int(payload, "sell_put_min_dte"), "max_dte": require_int(payload, "sell_put_max_dte")})
            min_strike = optional_float(payload, "sell_put_min_strike")
            max_strike = optional_float(payload, "sell_put_max_strike")
            if min_strike is not None:
                sell_put_entry["min_strike"] = min_strike
            if max_strike is not None:
                sell_put_entry["max_strike"] = max_strike
        if sell_call_enabled:
            sell_call_entry = cast(dict[str, Any], entry["sell_call"])
            sell_call_entry.update({"min_dte": require_int(payload, "sell_call_min_dte"), "max_dte": require_int(payload, "sell_call_max_dte")})
            min_strike = optional_float(payload, "sell_call_min_strike")
            max_strike = optional_float(payload, "sell_call_max_strike")
            if min_strike is not None:
                sell_call_entry["min_strike"] = min_strike
            if max_strike is not None:
                sell_call_entry["max_strike"] = max_strike
        if payload.get("broker") is not None:
            entry["broker"] = payload.get("broker")
        if payload.get("use") is not None:
            entry["use"] = payload.get("use")
        if payload.get("accounts") is not None:
            entry["accounts"] = normalize_accounts(payload.get("accounts"), fallback=())
        symbols.append(entry)
        return cfg
    if action == "edit":
        if found_entry is None or idx is None:
            raise AgentToolError(code="INPUT_ERROR", message=f"symbol not found: {symbol}")
        entry = found_entry
        sets = payload.get("set")
        if not isinstance(sets, dict) or not sets:
            raise AgentToolError(code="INPUT_ERROR", message="edit requires non-empty set object")
        for key, value in sets.items():
            set_path(entry, str(key), value)
        symbols[idx] = entry
        return cfg
    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported manage_symbols action: {action}")


def manage_symbols_tool(
    payload: dict[str, Any],
    *,
    load_runtime_config,
    deepcopy_fn: Callable[[Any], Any] = deepcopy,
    write_tools_enabled,
    apply_symbol_mutation_fn,
    validate_runtime_config,
    list_symbol_rows_fn,
    write_json_atomic,
    mask_path,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
    action = str(payload.get("action") or "list").strip().lower()
    dry_run = bool(payload.get("dry_run", False))
    confirm = bool(payload.get("confirm", False))
    if action != "list" and not dry_run:
        if not write_tools_enabled():
            raise AgentToolError(code="PERMISSION_DENIED", message="write tools are disabled", hint="Set OM_AGENT_ENABLE_WRITE_TOOLS=true to enable config writes.")
        if not confirm:
            raise AgentToolError(code="CONFIRMATION_REQUIRED", message="confirm=true is required for non-dry-run symbol mutations")
    mutated = apply_symbol_mutation_fn(deepcopy_fn(cfg), payload)
    validate_runtime_config(mutated)
    rows = list_symbol_rows_fn(mutated)
    result = {"action": action, "dry_run": dry_run, "symbols": rows, "symbol_count": len(rows)}
    if action != "list" and not dry_run:
        write_json_atomic(config_path, mutated)
    return result, [], {"config_path": mask_path(config_path), "write_applied": (action != "list" and not dry_run)}
