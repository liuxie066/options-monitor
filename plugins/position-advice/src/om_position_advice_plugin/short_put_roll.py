"""Pure same-quantity short-put roll eligibility, cash economics and ranking.

OM owns standard-contract, account, market-date and current-quote evidence.
These policies consume normalized observations and never fetch or place orders.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from math import isfinite
from typing import Any

from domain.domain.money import to_decimal
from domain.domain.option_position_identity import normalize_currency
from domain.domain.symbol_identity import canonical_symbol
from domain.domain.trade_contract_identity import (
    contract_share_quantity,
    normalize_contract_expiration,
)

from .economics import remaining_max_annualized_return


def old_position_in_roll_scope(*, strike: float, spot: float) -> bool:
    """Include all ITM/ATM puts and OTM puts up to and including five percent."""
    strike_value = _positive_decimal("strike", strike)
    spot_value = _positive_decimal("spot", spot)
    # Compare monetary values directly so 95/100 and decimal-price equivalents
    # share the exact inclusive boundary without float subtraction artifacts.
    return spot_value - strike_value <= spot_value * Decimal("0.05")


def evaluate_short_put_roll(
    position: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    old_quote: Mapping[str, Any],
    spot: float,
    old_dte: int,
    new_dte: int,
    old_buy_fee: float,
    new_sell_fee: float,
) -> dict[str, Any]:
    """Evaluate a two-order bid/ask cash estimate for one complete old lot.

    Missing/invalid required observations raise ``ValueError``. Confirmed
    identity or strategy exclusions return reason codes. No opening premium,
    Greeks, IV/RV, or minimum annualized-return gate participates in this policy.
    """
    for name, observation in (("position", position), ("candidate", candidate), ("old_quote", old_quote)):
        if not isinstance(observation, Mapping):
            raise ValueError(f"{name} must be a mapping")
    old_symbol = _symbol(position.get("symbol"))
    new_symbol = _symbol(candidate.get("symbol"))
    old_currency = normalize_currency(_required_text("currency", position.get("currency")), strict=True)
    new_currency = normalize_currency(_required_text("currency", candidate.get("currency")), strict=True)
    _required_text("broker", position.get("broker"))
    side = _required_text("side", position.get("side"))
    old_option_type = _required_text("option_type", position.get("option_type"))
    new_option_type = _required_text("option_type", candidate.get("option_type"))
    old_expiration = _expiration(position.get("expiration"))
    new_expiration = _expiration(candidate.get("expiration"))
    old_strike = _positive_decimal("old_strike", position.get("strike"))
    new_strike = _positive_decimal("new_strike", candidate.get("strike"))
    spot_value = _positive_decimal("spot", spot)
    contracts = _positive_integer("contracts_open", position.get("contracts_open"))
    new_contracts = _positive_integer("contracts", candidate.get("contracts"))
    multiplier = _positive_integer("multiplier", position.get("multiplier"))
    new_multiplier = _positive_integer("multiplier", candidate.get("multiplier"))
    shares = contract_share_quantity(contracts, multiplier)
    old_days = _positive_integer("old_dte", old_dte)
    new_days = _positive_integer("new_dte", new_dte)
    extension_days = (new_expiration - old_expiration).days
    if new_days - old_days != extension_days:
        raise ValueError("old_dte and new_dte must use the same market date as their expirations")

    old_ask = _positive_decimal("old_ask", old_quote.get("ask"))
    if "bid" in old_quote:
        old_bid = _nonnegative_decimal("old_bid", old_quote["bid"])
        if old_bid > old_ask:
            raise ValueError("old_bid must be <= old_ask")
    new_bid = _positive_decimal("new_bid", candidate.get("bid"))
    new_ask = _positive_decimal("new_ask", candidate.get("ask"))
    if new_bid > new_ask:
        raise ValueError("new_bid must be <= new_ask")
    buy_fee = _nonnegative_decimal("old_buy_fee", old_buy_fee)
    sell_fee = _nonnegative_decimal("new_sell_fee", new_sell_fee)

    reasons: list[str] = []
    if side != "short" or old_option_type != "put":
        reasons.append("unsupported_position")
    if new_option_type != "put":
        reasons.append("unsupported_candidate")
    if old_symbol != new_symbol:
        reasons.append("symbol_mismatch")
    if old_currency != new_currency:
        reasons.append("currency_mismatch")
    if multiplier != new_multiplier:
        reasons.append("multiplier_mismatch")
    if contracts != new_contracts:
        reasons.append("contracts_mismatch")
    if not old_position_in_roll_scope(strike=old_strike, spot=spot_value):
        reasons.append("old_position_outside_roll_scope")
    if new_strike >= old_strike:
        reasons.append("new_strike_not_lower")
    if new_strike >= spot_value:
        reasons.append("new_put_not_otm")
    if extension_days < 0:
        reasons.append("new_expiration_before_old")
    if reasons:
        return {"accepted": False, "reason_codes": reasons, "metrics": {}}

    old_close_cost = old_ask * shares + buy_fee
    new_net_premium = new_bid * shares - sell_fee
    net_credit = new_net_premium - old_close_cost
    old_capital_basis = old_strike * shares
    new_capital_basis = new_strike * shares
    old_annualized = remaining_max_annualized_return(
        remaining_cash=float(old_close_cost), capital_basis=float(old_capital_basis), dte=old_days,
    )
    new_annualized = remaining_max_annualized_return(
        remaining_cash=float(new_net_premium), capital_basis=float(new_capital_basis), dte=new_days,
    )
    metrics = {
        "old_otm_pct": float((spot_value - old_strike) / spot_value),
        "new_otm_pct": float((spot_value - new_strike) / spot_value),
        "strike_drop_pct": float((old_strike - new_strike) / old_strike),
        "notional_obligation_reduction": float(old_capital_basis - new_capital_basis),
        "contracts": contracts,
        "multiplier": multiplier,
        "old_close_cost": float(old_close_cost),
        "new_net_premium": float(new_net_premium),
        "net_credit": float(net_credit),
        "old_capital_basis": float(old_capital_basis),
        "new_capital_basis": float(new_capital_basis),
        "old_dte": old_days,
        "new_dte": new_days,
        "extension_days": extension_days,
        "incremental_period_return": float(net_credit / new_capital_basis),
        "incremental_annualized_return": remaining_max_annualized_return(
            remaining_cash=float(net_credit), capital_basis=float(new_capital_basis), dte=new_days,
        ),
        "new_remaining_max_annualized_return": new_annualized,
        "old_remaining_max_annualized_return": old_annualized,
        "remaining_annualized_change": new_annualized - old_annualized,
        "net_credit_per_extension_day": float(net_credit / extension_days) if extension_days > 0 else None,
        "extension_annualized_return": remaining_max_annualized_return(
            remaining_cash=float(net_credit), capital_basis=float(new_capital_basis), dte=extension_days,
        ) if extension_days > 0 else None,
    }
    if not all(value is None or isfinite(value) for value in metrics.values()):
        raise ValueError("roll economics must be finite")
    return {
        "accepted": net_credit > 0,
        "reason_codes": [] if net_credit > 0 else ["non_positive_net_credit"],
        "metrics": metrics,
    }


def rank_short_put_roll_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank a complete eligible set using full-precision flattened metrics."""
    return sorted(rows, key=lambda row: (
        -row["incremental_annualized_return"],
        -row["strike_drop_pct"],
        row["extension_days"],
        -row["net_credit"],
        str(row.get("contract_symbol") or row.get("contract") or ""),
        str(row.get("expiration") or ""),
    ))


def _positive_decimal(name: str, value: Any) -> Decimal:
    result = _nonnegative_decimal(name, value)
    if result <= 0:
        raise ValueError(f"{name} must be > 0")
    return result


def _nonnegative_decimal(name: str, value: Any) -> Decimal:
    result = to_decimal(value, field_name=name)
    if result < 0 or not isfinite(float(result)):
        raise ValueError(f"{name} must be finite and >= 0")
    return result


def _positive_integer(name: str, value: Any) -> int:
    result = _positive_decimal(name, value)
    if result != result.to_integral_value():
        raise ValueError(f"{name} must be a positive integer")
    return int(result)


def _required_text(name: str, value: Any) -> str:
    if value is None or isinstance(value, bool) or not str(value).strip():
        raise ValueError(f"{name} is required")
    return str(value).strip()


def _symbol(value: Any) -> str:
    symbol = canonical_symbol(_required_text("symbol", value))
    if not symbol:
        raise ValueError("symbol must be a recognized underlying")
    return symbol


def _expiration(value: Any) -> date:
    normalized = normalize_contract_expiration(_required_text("expiration", value))
    if not normalized:
        raise ValueError("expiration must be a valid date")
    return date.fromisoformat(normalized)
