from __future__ import annotations

from fastapi import HTTPException
from typing import Any

from scripts.account_config import accounts_from_config
from src.application.watchlist_mutations import (
    ensure_symbols_list as _ensure_symbols_list,
    find_symbol_entry as _find_symbol_entry,
)


def patch_close_advice(cfg: dict, payload: dict) -> None:
    close_advice = payload.get("closeAdvice")
    if close_advice is None:
        return
    if not isinstance(close_advice, dict):
        raise HTTPException(status_code=400, detail="closeAdvice must be an object")
    close_cfg = cfg.get("close_advice")
    if close_cfg is None:
        close_cfg = {}
        cfg["close_advice"] = close_cfg
    if not isinstance(close_cfg, dict):
        raise HTTPException(status_code=400, detail="config close_advice must be an object")
    if "enabled" in close_advice:
        close_cfg["enabled"] = bool(close_advice.get("enabled"))
    if "quote_source" in close_advice:
        value = str(close_advice.get("quote_source") or "").strip().lower()
        if value:
            if value not in {"auto", "required_data"}:
                raise HTTPException(status_code=400, detail="closeAdvice.quote_source must be auto or required_data")
            close_cfg["quote_source"] = value
        else:
            close_cfg.pop("quote_source", None)
    if "notify_levels" in close_advice:
        raw_levels = close_advice.get("notify_levels")
        if raw_levels is None:
            close_cfg.pop("notify_levels", None)
        elif isinstance(raw_levels, list):
            levels = [str(x).strip().lower() for x in raw_levels if str(x).strip()]
            if levels:
                close_cfg["notify_levels"] = levels
            else:
                close_cfg.pop("notify_levels", None)
        else:
            raise HTTPException(status_code=400, detail="closeAdvice.notify_levels must be a list or null")
    for field in ("max_items_per_account", "max_spread_ratio", "strong_remaining_annualized_max", "medium_remaining_annualized_max"):
        if field not in close_advice:
            continue
        raw = close_advice.get(field)
        if raw is None or raw == "":
            close_cfg.pop(field, None)
            continue
        caster = int if field == "max_items_per_account" else float
        try:
            value = caster(raw)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"closeAdvice.{field} must be a number") from exc
        if value < 0:
            raise HTTPException(status_code=400, detail=f"closeAdvice.{field} must be >= 0")
        close_cfg[field] = value


def patch_global_strategy(cfg: dict, payload: dict, *, global_strategy_fields: dict[str, type]) -> None:
    strategies = payload.get("strategies")
    if not isinstance(strategies, dict):
        raise HTTPException(status_code=400, detail="strategies must be an object")
    templates = cfg.get("templates")
    if templates is None:
        templates = {}
        cfg["templates"] = templates
    if not isinstance(templates, dict):
        raise HTTPException(status_code=400, detail="templates must be an object")
    targets = {"sell_put": ("put_base", "sell_put"), "sell_call": ("call_base", "sell_call")}
    for side, (template_name, side_key) in targets.items():
        side_payload = strategies.get(side)
        if side_payload is None:
            continue
        if not isinstance(side_payload, dict):
            raise HTTPException(status_code=400, detail=f"strategies.{side} must be an object")
        template = templates.get(template_name)
        if template is None:
            template = {}
            templates[template_name] = template
        if not isinstance(template, dict):
            raise HTTPException(status_code=400, detail=f"templates.{template_name} must be an object")
        side_cfg = template.get(side_key)
        if side_cfg is None:
            side_cfg = {}
            template[side_key] = side_cfg
        if not isinstance(side_cfg, dict):
            raise HTTPException(status_code=400, detail=f"templates.{template_name}.{side_key} must be an object")
        for field, caster in global_strategy_fields.items():
            if field not in side_payload:
                continue
            raw = side_payload.get(field)
            if raw is None or raw == "":
                raise HTTPException(status_code=400, detail=f"{side}.{field} is required")
            try:
                value = caster(raw)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"{side}.{field} must be a number") from exc
            if value < 0:
                raise HTTPException(status_code=400, detail=f"{side}.{field} must be >= 0")
            side_cfg[field] = value


def patch_notifications(cfg: dict, payload: dict, *, notification_numeric_fields: dict[str, type]) -> None:
    notifications = payload.get("notifications")
    if notifications is None:
        return
    if not isinstance(notifications, dict):
        raise HTTPException(status_code=400, detail="notifications must be an object")
    notif_cfg = cfg.get("notifications")
    if notif_cfg is None:
        notif_cfg = {}
        cfg["notifications"] = notif_cfg
    if not isinstance(notif_cfg, dict):
        raise HTTPException(status_code=400, detail="config notifications must be an object")
    for key in ("enabled", "include_cash_footer"):
        if key in notifications:
            value = notifications.get(key)
            if value is None:
                notif_cfg.pop(key, None)
            else:
                notif_cfg[key] = bool(value)
    for key in ("channel", "target"):
        if key in notifications:
            value = str(notifications.get(key) or "").strip()
            if value:
                notif_cfg[key] = value
            else:
                notif_cfg.pop(key, None)
    if "cash_footer_accounts" in notifications:
        raw_accounts = notifications.get("cash_footer_accounts")
        if raw_accounts is None:
            notif_cfg.pop("cash_footer_accounts", None)
        elif isinstance(raw_accounts, list):
            values = [str(x).strip().lower() for x in raw_accounts if str(x).strip()]
            default_accounts = accounts_from_config(cfg)
            if values and values != default_accounts:
                notif_cfg["cash_footer_accounts"] = values
            else:
                notif_cfg.pop("cash_footer_accounts", None)
        else:
            raise HTTPException(status_code=400, detail="notifications.cash_footer_accounts must be a list or null")
    if "quiet_hours_beijing" in notifications:
        raw_quiet = notifications.get("quiet_hours_beijing")
        if raw_quiet is None:
            notif_cfg.pop("quiet_hours_beijing", None)
        elif not isinstance(raw_quiet, dict):
            raise HTTPException(status_code=400, detail="notifications.quiet_hours_beijing must be an object or null")
        else:
            start = str(raw_quiet.get("start") or "").strip()
            end = str(raw_quiet.get("end") or "").strip()
            if start or end:
                if not start or not end:
                    raise HTTPException(status_code=400, detail="notifications.quiet_hours_beijing.start/end must be set together")
                notif_cfg["quiet_hours_beijing"] = {"start": start, "end": end}
            else:
                notif_cfg.pop("quiet_hours_beijing", None)
    for field, caster in notification_numeric_fields.items():
        if field not in notifications:
            continue
        raw = notifications.get(field)
        if raw is None or raw == "":
            notif_cfg.pop(field, None)
            continue
        try:
            value = caster(raw)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"notifications.{field} must be a number") from exc
        if value < 0:
            raise HTTPException(status_code=400, detail=f"notifications.{field} must be >= 0")
        notif_cfg[field] = value


def find_symbol(cfg: dict, symbol: str) -> tuple[int | None, dict | None]:
    return _find_symbol_entry(
        cfg,
        symbol,
        resolve_watchlist_config=lambda data: [item for item in _ensure_symbols_list(data, error_factory=ValueError) if isinstance(item, dict)],
    )


def ensure_symbols_list(cfg: dict) -> list:
    return _ensure_symbols_list(cfg, error_factory=lambda message: HTTPException(status_code=400, detail=message))


def clean_symbol_level_strategy_fields(cfg: dict, *, forbidden_fields: list[str] | set[str] | tuple[str, ...]) -> None:
    symbols = cfg.get("symbols")
    if not isinstance(symbols, list):
        return
    for item in symbols:
        if not isinstance(item, dict):
            continue
        for side in ("sell_put", "sell_call"):
            side_cfg = item.get(side)
            if not isinstance(side_cfg, dict):
                continue
            for field in forbidden_fields:
                side_cfg.pop(field, None)


def patch_entry(entry: dict, payload: dict, *, forbidden_fields: list[str] | set[str] | tuple[str, ...]) -> None:
    if "broker" in payload:
        entry["broker"] = payload.get("broker")
    if "accounts" in payload:
        accounts = payload.get("accounts")
        if accounts is None:
            entry.pop("accounts", None)
        elif isinstance(accounts, list):
            entry["accounts"] = [str(a).strip().lower() for a in accounts if str(a).strip()]
        else:
            raise HTTPException(status_code=400, detail="accounts must be list or null")
    if "limit_expirations" in payload:
        le = payload.get("limit_expirations")
        entry.setdefault("fetch", {})
        if not isinstance(entry.get("fetch"), dict):
            entry["fetch"] = {}
        if le is None:
            entry["fetch"].pop("limit_expirations", None)
        else:
            entry["fetch"]["limit_expirations"] = int(le)
    sp = entry.get("sell_put")
    if not isinstance(sp, dict):
        sp = {}
        entry["sell_put"] = sp
    for field in forbidden_fields:
        sp.pop(field, None)
    mapping_sp = {
        "sell_put_enabled": ("enabled", bool),
        "sell_put_min_dte": ("min_dte", int),
        "sell_put_max_dte": ("max_dte", int),
        "sell_put_min_strike": ("min_strike", float),
        "sell_put_max_strike": ("max_strike", float),
    }
    for key, (field, caster) in mapping_sp.items():
        if key in payload:
            value = payload.get(key)
            if value is None:
                sp.pop(field, None)
            else:
                sp[field] = caster(value)
    sc = entry.get("sell_call")
    if not isinstance(sc, dict):
        sc = {}
        entry["sell_call"] = sc
    for field in forbidden_fields:
        sc.pop(field, None)
    mapping_sc = {
        "sell_call_enabled": ("enabled", bool),
        "sell_call_min_dte": ("min_dte", int),
        "sell_call_max_dte": ("max_dte", int),
        "sell_call_min_strike": ("min_strike", float),
        "sell_call_max_strike": ("max_strike", float),
    }
    for key, (field, caster) in mapping_sc.items():
        if key in payload:
            value = payload.get(key)
            if value is None:
                sc.pop(field, None)
            else:
                sc[field] = caster(value)
