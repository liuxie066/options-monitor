from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from domain.domain.symbol_identity import canonical_symbol, symbol_currency
from domain.domain.portfolio_assignment_scenario import POSITION_SIZING_BASIS, project_non_option_assignment_assets


ShortVolMode = Literal["put", "call"]


@dataclass(frozen=True)
class ShortVolPortfolioContext:
    nav_cny: float | None
    stock_value_cny_by_symbol: dict[str, float]
    short_put_assignment_cny_by_symbol: dict[str, float]
    short_put_assignment_total_cny: float | None
    unavailable_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    account: str | None = None
    portfolio_evidence: dict[str, Any] | None = None
    assignment_positions: tuple[dict[str, Any], ...] | None = None


def portfolio_concentration_fields(
    row: dict[str, Any],
    *,
    mode: ShortVolMode,
    risk_ctx: ShortVolPortfolioContext,
) -> dict[str, Any]:
    """Project candidate concentration facts without imposing an opening gate."""

    symbol = canonical_symbol(row.get("symbol"))
    account = risk_ctx.account
    evidence = risk_ctx.portfolio_evidence or {}
    accounts = [account] if account else []
    baseline = project_non_option_assignment_assets(accounts=accounts, portfolio_evidence=evidence, option_positions=[])
    positions = [
        position
        for position in risk_ctx.assignment_positions or ()
        if canonical_symbol(position.get("symbol")) == symbol
    ]
    existing = project_non_option_assignment_assets(
        accounts=accounts, portfolio_evidence=evidence, option_positions=positions
    )
    candidate = {
        "account": account,
        "broker": row.get("broker") or "富途",
        "symbol": symbol,
        "option_type": mode,
        "side": "short",
        "status": "open",
        "contracts_open": 1,
        "multiplier": row.get("multiplier"),
        "strike": row.get("strike"),
        "currency": row.get("currency") or row.get("option_ccy") or symbol_currency(symbol),
    }
    after = project_non_option_assignment_assets(
        accounts=accounts,
        portfolio_evidence=evidence,
        option_positions=[*positions, candidate],
        candidate_net_premium_cny=row.get("net_income_cny"),
    )
    options_known = risk_ctx.assignment_positions is not None

    def weight(result: dict[str, Any]) -> float | None:
        if result.get("unavailable_reasons"):
            return None
        # A complete empty symbol position is a trusted zero.
        return _float(result["weight_of_net_assets_by_symbol"].get(symbol, "0"))

    current = weight(baseline) if symbol and account else None
    existing_weight = weight(existing) if options_known and symbol and account else None
    after_weight = weight(after) if options_known and symbol and account else None
    reasons = list(after.get("unavailable_reasons") or [])
    if not options_known:
        reasons.append("assignment_positions_unavailable")
    assignment = _first_float(row, "assignment_notional_cny", "cash_required_cny")
    covered = _first_float(row, "covered_notional_cny", "underlying_notional_cny")
    nav = _float(baseline.get("net_assets_cny"))
    return {
        "position_sizing_basis": POSITION_SIZING_BASIS,
        "symbol_concentration_current": current,
        "symbol_concentration_after_existing_assignments": existing_weight,
        "symbol_concentration_after": after_weight,
        "portfolio_nav_cny": nav,
        "portfolio_nav_after_existing_assignments_cny": _float(existing.get("net_assets_cny"))
        if options_known
        else None,
        "portfolio_nav_after_candidate_assignment_cny": _float(after.get("net_assets_cny")) if options_known else None,
        "assignment_notional_cny": assignment,
        "covered_notional_cny": covered,
        "existing_stock_value_cny_symbol": _float(baseline.get("stock_value_cny_by_symbol", {}).get(symbol, "0"))
        if current is not None
        else None,
        "existing_short_put_assignment_cny_symbol": risk_ctx.short_put_assignment_cny_by_symbol.get(symbol),
        "existing_short_put_assignment_cny_total": risk_ctx.short_put_assignment_total_cny,
        "single_trade_concentration": assignment / nav
        if assignment is not None and nav is not None and nav > 0
        else None,
        "total_short_put_concentration_after": (
            (
                (
                    risk_ctx.short_put_assignment_total_cny + (assignment or 0)
                    if mode == "put"
                    else risk_ctx.short_put_assignment_total_cny
                )
                / nav
            )
            if risk_ctx.short_put_assignment_total_cny is not None and nav is not None and nav > 0
            else None
        ),
        "concentration_score": None,
        "concentration_evaluable": after_weight is not None,
        "concentration_unavailable_reason": ";".join(reasons) or None,
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
