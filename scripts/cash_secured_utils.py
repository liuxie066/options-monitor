"""Shared helpers for cash-secured option usage payloads."""

from __future__ import annotations

from typing import Any, Callable


def normalize_symbol(symbol: Any) -> str:
    return str(symbol or "").strip().upper()


def normalize_cash_secured_by_symbol_by_ccy(option_ctx: dict | None) -> dict[str, dict[str, float]]:
    norm: dict[str, dict[str, float]] = {}
    ctx = option_ctx if isinstance(option_ctx, dict) else {}

    by_ccy = ctx.get("cash_secured_by_symbol_by_ccy") or {}
    if isinstance(by_ccy, dict) and by_ccy:
        for sym, ccy_map in by_ccy.items():
            if not isinstance(ccy_map, dict):
                continue
            sym_u = normalize_symbol(sym)
            if not sym_u:
                continue
            for ccy, amount in ccy_map.items():
                try:
                    fv = float(amount)
                except Exception:
                    continue
                if not fv:
                    continue
                ccy_u = normalize_symbol(ccy) or "USD"
                norm.setdefault(sym_u, {})
                norm[sym_u][ccy_u] = norm[sym_u].get(ccy_u, 0.0) + fv
        return norm

    old_map = ctx.get("cash_secured_by_symbol") or {}
    if not isinstance(old_map, dict):
        return norm
    for sym, amount in old_map.items():
        try:
            fv = float(amount)
        except Exception:
            continue
        if not fv:
            continue
        sym_u = normalize_symbol(sym)
        if not sym_u:
            continue
        norm[sym_u] = {"USD": fv}
    return norm


def normalize_cash_secured_total_by_ccy(
    option_ctx: dict | None,
    *,
    by_symbol_by_ccy: dict[str, dict[str, float]] | None = None,
) -> dict[str, float]:
    ctx = option_ctx if isinstance(option_ctx, dict) else {}
    total = ctx.get("cash_secured_total_by_ccy") or {}
    norm: dict[str, float] = {}
    if isinstance(total, dict):
        for ccy, amount in total.items():
            try:
                fv = float(amount)
            except Exception:
                continue
            if not fv:
                continue
            ccy_u = normalize_symbol(ccy)
            if not ccy_u:
                continue
            norm[ccy_u] = fv
    if norm:
        return norm

    by_sym = by_symbol_by_ccy if isinstance(by_symbol_by_ccy, dict) else normalize_cash_secured_by_symbol_by_ccy(ctx)
    for ccy_map in by_sym.values():
        if not isinstance(ccy_map, dict):
            continue
        for ccy, amount in ccy_map.items():
            try:
                fv = float(amount)
            except Exception:
                continue
            if not fv:
                continue
            ccy_u = normalize_symbol(ccy)
            if not ccy_u:
                continue
            norm[ccy_u] = norm.get(ccy_u, 0.0) + fv
    return norm


def read_cash_secured_total_cny(option_ctx: dict | None) -> float | None:
    ctx = option_ctx if isinstance(option_ctx, dict) else {}
    v = ctx.get("cash_secured_total_cny")
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def cash_secured_symbol_by_ccy(
    option_ctx: dict | None,
    symbol: str,
    *,
    by_symbol_by_ccy: dict[str, dict[str, float]] | None = None,
) -> dict[str, float]:
    by_sym = by_symbol_by_ccy if isinstance(by_symbol_by_ccy, dict) else normalize_cash_secured_by_symbol_by_ccy(option_ctx)
    sym_u = normalize_symbol(symbol)
    return by_sym.get(sym_u) or {}


def cash_secured_symbol_cny(
    option_ctx: dict | None,
    symbol: str,
    *,
    by_symbol_by_ccy: dict[str, dict[str, float]] | None = None,
    native_to_cny: Callable[[float, str], float | None] | None = None,
) -> float | None:
    ctx = option_ctx if isinstance(option_ctx, dict) else {}
    sym_u = normalize_symbol(symbol)

    m_cny = ctx.get("cash_secured_by_symbol_cny") or {}
    if isinstance(m_cny, dict):
        v = m_cny.get(sym_u)
        if v is None:
            v = m_cny.get(symbol)
        if v is not None:
            try:
                return float(v)
            except Exception:
                return None

    sym_by_ccy = cash_secured_symbol_by_ccy(ctx, symbol, by_symbol_by_ccy=by_symbol_by_ccy)
    if not isinstance(sym_by_ccy, dict) or not sym_by_ccy:
        return None

    total = 0.0
    has_any = False
    for ccy, amount in sym_by_ccy.items():
        try:
            fv = float(amount)
        except Exception:
            continue
        ccy_u = normalize_symbol(ccy)
        if ccy_u == "CNY":
            total += fv
            has_any = True
            continue
        if native_to_cny is None:
            return None
        v_cny = native_to_cny(fv, ccy_u)
        if v_cny is None:
            return None
        total += float(v_cny)
        has_any = True
    return total if has_any else None
