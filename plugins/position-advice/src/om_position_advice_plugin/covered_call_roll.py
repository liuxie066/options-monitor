"""Covered-call roll-up policy with positive net cash and conditional sale economics."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .short_put_roll import _evaluate_roll, _old_position_in_roll_scope, _rank_roll_candidates


def old_covered_call_in_roll_scope(*, strike: float, spot: float) -> bool:
    """Include ITM/ATM calls and OTM calls within five percent of current spot."""
    return _old_position_in_roll_scope(strike=strike, spot=spot, option_type="call")


def evaluate_covered_call_roll(
    position: Mapping[str, Any], candidate: Mapping[str, Any], *,
    old_quote: Mapping[str, Any], spot: float, old_dte: int, new_dte: int,
    old_buy_fee: float, new_sell_fee: float, covered_shares: float,
) -> dict[str, Any]:
    """Require positive net cash, a higher strike and no earlier expiration.

    Coverage is verified by the host for the account's complete call allocation.
    Conditional sale improvement remains visible for excluded non-credit candidates;
    it is neither realized profit nor a forecast of assignment at the new strike.
    """
    return _evaluate_roll(position, candidate, old_quote=old_quote, spot=spot,
        old_dte=old_dte, new_dte=new_dte, old_buy_fee=old_buy_fee, new_sell_fee=new_sell_fee,
        option_type="call", covered_shares=covered_shares)


def evaluate_conditional_covered_call_roll(
    position: Mapping[str, Any], candidate: Mapping[str, Any], *,
    old_quote: Mapping[str, Any], spot: float, old_dte: int, new_dte: int,
    old_buy_fee: float, new_sell_fee: float,
) -> dict[str, Any]:
    """Conditional Call roll-up: shared economics without verified stock coverage."""
    result = _evaluate_roll(position, candidate, old_quote=old_quote, spot=spot,
        old_dte=old_dte, new_dte=new_dte, old_buy_fee=old_buy_fee, new_sell_fee=new_sell_fee,
        option_type="call", covered_shares=None, require_broker=False, conditional=True)
    metrics = dict(result.get("metrics") or {})
    contracts = metrics.get("contracts")
    multiplier = metrics.get("multiplier")
    required_shares = (
        int(contracts) * int(multiplier)
        if isinstance(contracts, int) and isinstance(multiplier, int)
        and contracts > 0 and multiplier > 0 else None)
    conditional = {
        "schema_version": "group_query_roll_up_result.v1",
        "analysis_kind": "conditional",
        "account_eligibility": "not_verified",
        "stock_coverage": {
            "verification_status": "not_verified",
            "required_shares": required_shares,
        },
        "unverified_conditions": [
            "actual_share_count_not_verified",
            "all_unclosed_short_call_coverage_not_verified",
            "duplicate_obligation_not_verified",
        ],
        "required_shares": required_shares,
        "reason_codes": list(result.get("reason_codes") or []),
        "metrics": metrics,
    }
    conditional["metrics"].pop("covered_shares", None)
    if conditional["metrics"].get("capital_basis_kind") == "covered_stock_market_value":
        conditional["metrics"]["capital_basis_kind"] = "hypothetical_stock_market_value"
    if "recommendation" in result:
        recommendation = str(result.get("recommendation") or "")
        if recommendation == "excluded":
            conditional["economic_result"] = "excluded"
        elif recommendation == "credit_roll":
            conditional["economic_result"] = "positive_net_credit"
        else:
            conditional["economic_result"] = "non_positive_net_credit"
        conditional["cash_flow_type"] = result.get("cash_flow_type")
    else:
        conditional["economic_result"] = (
            "positive_net_credit" if result.get("accepted")
            else "non_positive_net_credit")
    return conditional


def rank_covered_call_roll_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Use the Put cash-annualized ranking with the Call strike-increase direction."""
    return _rank_roll_candidates(rows, strike_change_field="strike_increase_pct")
