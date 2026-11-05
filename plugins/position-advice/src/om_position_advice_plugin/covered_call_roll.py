"""Covered-call roll-up policy with explicit debit and conditional sale economics."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .short_put_roll import _evaluate_roll, _old_position_in_roll_scope


def old_covered_call_in_roll_scope(*, strike: float, spot: float) -> bool:
    """Include ITM/ATM calls and OTM calls within five percent of current spot."""
    return _old_position_in_roll_scope(strike=strike, spot=spot, option_type="call")


def evaluate_covered_call_roll(
    position: Mapping[str, Any], candidate: Mapping[str, Any], *,
    old_quote: Mapping[str, Any], spot: float, old_dte: int, new_dte: int,
    old_buy_fee: float, new_sell_fee: float, covered_shares: float,
) -> dict[str, Any]:
    """Compare cash plus higher assignment proceeds, conditional on both sales.

    Coverage is verified by the host for the account's complete call allocation.
    A debit below the strike uplift is eligible; this is neither realized profit
    nor a forecast that shares will be called away at the new strike.
    """
    return _evaluate_roll(position, candidate, old_quote=old_quote, spot=spot,
        old_dte=old_dte, new_dte=new_dte, old_buy_fee=old_buy_fee, new_sell_fee=new_sell_fee,
        option_type="call", covered_shares=covered_shares)


def rank_covered_call_roll_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank conditional assignment improvement on current covered stock value."""
    return sorted(rows, key=lambda row: (
        -row["assignment_improvement_annualized_return"],
        -row["assignment_net_improvement"],
        row["extension_days"],
        -row["net_credit"],
        row["expiration"],
        str(row.get("contract") or row.get("contract_symbol") or ""),
    ))
