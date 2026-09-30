"""Shared helpers for cash-secured option usage payloads."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from domain.domain.option_position_identity import normalize_currency
from domain.domain.symbol_identity import canonical_symbol


def normalize_symbol(symbol: Any) -> str:
    raw = str(symbol or "").strip()
    return canonical_symbol(raw) or raw.upper()


def cash_secured_unavailable_for_cash_snapshot(
    option_ctx: dict | None,
    portfolio_ctx: dict | None,
) -> Any:
    """Release a fully closed Put only after a newer direct broker cash observation."""
    unavailable = option_ctx.get("cash_secured_unavailable_by_symbol") if isinstance(option_ctx, dict) else None
    if not isinstance(unavailable, dict) or not unavailable or not isinstance(portfolio_ctx, dict):
        return unavailable
    if (
        portfolio_ctx.get("context_source") != "futu_direct"
        or portfolio_ctx.get("cash_balance_reliable") is False
        or portfolio_ctx.get("cash_source_observation_status") not in (None, "trusted")
    ):
        return unavailable
    observed = portfolio_ctx.get("cash_source_observed_at") or portfolio_ctx.get("source_observed_at")
    try:
        cash_as_of = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
        if cash_as_of.tzinfo is None:
            return unavailable
        cash_as_of_ms = cash_as_of.timestamp() * 1000
    except (TypeError, ValueError, OverflowError):
        return unavailable
    rows = option_ctx.get("open_positions_min") if isinstance(option_ctx, dict) else None
    if not isinstance(rows, list):
        return unavailable
    remaining = dict(unavailable)
    for symbol, reason in unavailable.items():
        if reason != "option_close_settlement_pending":
            continue
        reserved_rows = [
            row for row in rows
            if isinstance(row, dict)
            and normalize_symbol(row.get("symbol")) == normalize_symbol(symbol)
            and row.get("side") == "short"
            and row.get("option_type") == "put"
            and row.get("closure_fact") in {"option_leg_closed", "partial_close_observed"}
            and isinstance(row.get("reserved_contracts_by_lot"), dict)
            and type(row["reserved_contracts_by_lot"].get(row.get("lot_id"))) is int
            and row["reserved_contracts_by_lot"][row["lot_id"]] > 0
        ]
        if reserved_rows and all(
            row.get("closure_fact") == "option_leg_closed"
            and row.get("lifecycle_state") != "conflict"
            and row.get("reason_state") != "conflict"
            and type(row.get("last_option_close_received_at_ms")) is int
            and 0 < row["last_option_close_received_at_ms"] <= cash_as_of_ms
            for row in reserved_rows
        ):
            remaining.pop(symbol)
    return remaining


def _normalize_currency(value: Any) -> str:
    return normalize_currency(value)


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
                ccy_u = _normalize_currency(ccy) or "USD"
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
            ccy_u = _normalize_currency(ccy)
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
            ccy_u = _normalize_currency(ccy)
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
        for key, v in m_cny.items():
            if normalize_symbol(key) != sym_u and key != symbol:
                continue
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
        ccy_u = _normalize_currency(ccy)
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
