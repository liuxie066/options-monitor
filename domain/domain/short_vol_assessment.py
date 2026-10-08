from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Literal

from domain.domain.symbol_identity import canonical_symbol


ShortVolMode = Literal["put", "call"]


@dataclass(frozen=True)
class ShortVolPortfolioContext:
    nav_cny: float | None
    stock_value_cny_by_symbol: dict[str, float]
    short_put_assignment_cny_by_symbol: dict[str, float]
    short_put_assignment_total_cny: float | None
    unavailable_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def portfolio_concentration_fields(
    row: dict[str, Any],
    *,
    mode: ShortVolMode,
    risk_ctx: ShortVolPortfolioContext,
) -> dict[str, Any]:
    """Project candidate concentration facts without imposing an opening gate."""

    symbol = canonical_symbol(row.get("symbol"))
    assignment = _first_float(row, "assignment_notional_cny", "cash_required_cny")
    covered_notional = _first_float(row, "covered_notional_cny", "underlying_notional_cny")
    candidate_notional = assignment if mode == "put" else covered_notional
    nav = risk_ctx.nav_cny
    existing_stock = risk_ctx.stock_value_cny_by_symbol.get(symbol or "", 0.0)
    existing_short_put = risk_ctx.short_put_assignment_cny_by_symbol.get(symbol or "", 0.0)
    existing_total_short_put = risk_ctx.short_put_assignment_total_cny

    concentration_evaluable = bool(
        nav is not None
        and nav > 0
        and candidate_notional is not None
        and candidate_notional > 0
        and not risk_ctx.unavailable_reasons
    )
    if mode == "put":
        concentration_evaluable = bool(concentration_evaluable and existing_total_short_put is not None)

    single_trade = (candidate_notional / nav) if concentration_evaluable and nav else None
    if mode == "put":
        symbol_after = (
            ((existing_stock + existing_short_put + (assignment or 0.0)) / nav)
            if concentration_evaluable and nav
            else None
        )
        total_after = (
            (((existing_total_short_put or 0.0) + (assignment or 0.0)) / nav)
            if concentration_evaluable and nav
            else None
        )
    else:
        symbol_exposure = max(existing_stock, covered_notional or 0.0)
        symbol_after = (symbol_exposure / nav) if concentration_evaluable and nav else None
        total_after = ((existing_total_short_put or 0.0) / nav) if concentration_evaluable and nav else None

    concentration_score = None
    if symbol_after is not None and total_after is not None:
        concentration_score = max(0.0, 1.0 - max(symbol_after, total_after))

    sizing_fields: dict[str, Any] = {}
    if mode == "put":
        # Existing holdings remain meaningful even when this candidate cannot be valued.
        sizing_evaluable = bool(
            symbol
            and nav is not None
            and isfinite(nav)
            and nav > 0
            and isfinite(existing_stock)
            and existing_stock >= 0
            and isfinite(existing_short_put)
            and existing_short_put >= 0
            and existing_total_short_put is not None
            and not risk_ctx.unavailable_reasons
        )
        current = existing_stock / nav if sizing_evaluable else None
        existing_assigned = (existing_stock + existing_short_put) / nav if sizing_evaluable else None
        sizing_fields = {
            "symbol_concentration_current": _round_optional(current) if current is not None and isfinite(current) else None,
            "symbol_concentration_after_existing_puts": (
                _round_optional(existing_assigned)
                if existing_assigned is not None and isfinite(existing_assigned)
                else None
            ),
        }

    return {
        **sizing_fields,
        "portfolio_nav_cny": _round_optional(nav),
        "assignment_notional_cny": _round_optional(assignment),
        "covered_notional_cny": _round_optional(covered_notional),
        "existing_stock_value_cny_symbol": _round_optional(existing_stock),
        "existing_short_put_assignment_cny_symbol": _round_optional(existing_short_put),
        "existing_short_put_assignment_cny_total": _round_optional(existing_total_short_put),
        "single_trade_concentration": _round_optional(single_trade),
        "symbol_concentration_after": _round_optional(symbol_after),
        "total_short_put_concentration_after": _round_optional(total_after),
        "concentration_score": _round_optional(concentration_score),
        "concentration_evaluable": concentration_evaluable,
        "concentration_unavailable_reason": ";".join(risk_ctx.unavailable_reasons) or None,
        "portfolio_risk_warnings": ";".join(risk_ctx.warnings) or None,
    }


def _first_float(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float(row.get(key))
        if value is not None:
            return value
    return None


def _float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        parsed = float(value)
    except Exception:
        return None
    try:
        if parsed != parsed:
            return None
    except Exception:
        pass
    return parsed


def _round_optional(value: Any) -> float | None:
    parsed = _float(value)
    if parsed is None:
        return None
    return round(float(parsed), 6)
