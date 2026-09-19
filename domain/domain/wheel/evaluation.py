"""Wheel candidate economics: Call/Put acceptance and rank keys."""

from __future__ import annotations

from typing import Any, Mapping

from domain.domain.engine.candidate_engine import build_candidate_rank_key

from ._common import _finite_float, _wheel_abs_delta_bounds
from .events import _positive_int


def evaluate_wheel_call_candidate(
    batch: Mapping[str, Any],
    normalized_candidate: Mapping[str, Any],
    wheel_policy: Mapping[str, Any],
    stock_exit_fee_fact: Mapping[str, Any],
    contracts: int,
) -> dict[str, Any]:
    """Apply only Wheel batch economics after common Call policy acceptance."""

    result = dict(normalized_candidate)
    reasons: list[str] = []
    unavailable: list[str] = []
    try:
        contract_count = _positive_int(contracts, "contracts")
    except ValueError:
        contract_count = 0
        unavailable.append("contracts_unavailable")
    multiplier = _finite_float(result.get("multiplier"))
    delta = _finite_float(result.get("delta"))
    strike = _finite_float(result.get("strike"))
    spot = _finite_float(result.get("spot"))
    net_premium_per_contract = _finite_float(
        result.get("net_premium", result.get("net_income"))
    )
    shares_remaining = _finite_float(batch.get("shares_remaining"))
    remaining_basis = _finite_float(batch.get("remaining_stock_cost_basis"))
    realized_put = _finite_float(batch.get("realized_sell_put_net_pnl"))
    realized_calls = _finite_float(batch.get("realized_prior_call_net_pnl"))
    realized_stock = _finite_float(batch.get("realized_prior_stock_sale_net_pnl"))
    fee_amount = _finite_float(stock_exit_fee_fact.get("amount"))
    fee_basis = str(stock_exit_fee_fact.get("basis") or "").strip().lower()
    min_abs_delta, max_abs_delta = _wheel_abs_delta_bounds(wheel_policy)
    for field, value in (
        ("multiplier_unavailable", multiplier),
        ("delta_unavailable", delta),
        ("strike_unavailable", strike),
        ("spot_unavailable", spot),
        ("candidate_net_premium_unavailable", net_premium_per_contract),
        ("shares_remaining_unavailable", shares_remaining),
        ("remaining_stock_cost_basis_unavailable", remaining_basis),
        ("realized_sell_put_net_pnl_unavailable", realized_put),
        ("realized_prior_call_net_pnl_unavailable", realized_calls),
        ("realized_prior_stock_sale_net_pnl_unavailable", realized_stock),
        ("stock_exit_fee_unavailable", fee_amount),
        ("wheel_abs_delta_bounds_unavailable", min_abs_delta),
        ("wheel_abs_delta_bounds_unavailable", max_abs_delta),
    ):
        if value is None:
            unavailable.append(field)
    if fee_basis not in {"actual", "estimated"}:
        unavailable.append("stock_exit_fee_unavailable")
    if unavailable:
        return {
            **result,
            "accepted": False,
            "wheel_candidate_status": "data_unavailable",
            "reason_codes": sorted(set(unavailable)),
        }

    assert multiplier is not None
    assert delta is not None
    assert strike is not None
    assert spot is not None
    assert net_premium_per_contract is not None
    assert shares_remaining is not None
    assert remaining_basis is not None
    assert realized_put is not None
    assert realized_calls is not None
    assert realized_stock is not None
    assert fee_amount is not None
    assert min_abs_delta is not None
    assert max_abs_delta is not None
    multiplier_int = int(multiplier)
    if multiplier_int <= 0 or multiplier != multiplier_int:
        unavailable.append("multiplier_unavailable")
    covered_shares = contract_count * multiplier_int
    if covered_shares <= 0 or covered_shares > int(shares_remaining):
        reasons.append("wheel_batch_capacity_insufficient")
    absolute_delta = abs(delta)
    if absolute_delta < min_abs_delta:
        reasons.append("wheel_call_abs_delta_below_min")
    if absolute_delta > max_abs_delta:
        reasons.append("wheel_call_abs_delta_above_max")
    if strike < spot:
        reasons.append("wheel_call_strike_below_spot")
    if shares_remaining <= 0 or remaining_basis < 0 or spot <= 0:
        unavailable.append("remaining_stock_cost_basis_unavailable")
    if unavailable:
        return {
            **result,
            "accepted": False,
            "wheel_candidate_status": "data_unavailable",
            "reason_codes": sorted(set(unavailable)),
        }

    allocated_basis = remaining_basis * covered_shares / shares_remaining
    sale_net = strike * covered_shares - fee_amount
    if sale_net < allocated_basis:
        reasons.append("wheel_call_strike_below_cost_floor")
    candidate_premium = net_premium_per_contract * contract_count
    projected_stock_pnl = sale_net - allocated_basis
    projected_lifecycle_pnl = (
        realized_put
        + realized_calls
        + realized_stock
        + candidate_premium
        + projected_stock_pnl
    )
    covered_market_value = spot * covered_shares
    result.update(
        {
            "accepted": not reasons,
            "wheel_candidate_status": "accepted" if not reasons else "rejected",
            "reason_codes": sorted(set(reasons)),
            "contracts": contract_count,
            "candidate_covered_shares": covered_shares,
            "allocated_remaining_stock_cost_basis": round(allocated_basis, 6),
            "estimated_stock_exit_fees": round(fee_amount, 6),
            "stock_exit_fee_basis": fee_basis,
            "candidate_call_net_premium": round(candidate_premium, 6),
            "projected_remaining_stock_sale_net_pnl_at_strike": round(projected_stock_pnl, 6),
            "projected_lifecycle_net_pnl_if_called": round(projected_lifecycle_pnl, 6),
            "projected_lifecycle_return_if_called": round(
                projected_lifecycle_pnl / covered_market_value,
                10,
            ),
            "projected_lifecycle_pnl_scope": (
                "final_total_if_called"
                if covered_shares == int(shares_remaining)
                else "cumulative_after_this_call"
            ),
        }
    )
    return result


def evaluate_wheel_put_candidate(
    branch: Mapping[str, Any],
    normalized_candidate: Mapping[str, Any],
    wheel_policy: Mapping[str, Any],
    stock_assignment_fee_fact: Mapping[str, Any],
    contracts: int,
) -> dict[str, Any]:
    """Apply only Wheel Put replenishment economics after common Put policy."""

    result = dict(normalized_candidate)
    reasons: list[str] = []
    unavailable: list[str] = []
    try:
        contract_count = _positive_int(contracts, "contracts")
    except ValueError:
        contract_count = 0
        unavailable.append("contracts_unavailable")
    multiplier = _finite_float(result.get("multiplier"))
    delta = _finite_float(result.get("delta"))
    strike = _finite_float(result.get("strike"))
    spot = _finite_float(result.get("spot"))
    net_premium_per_contract = _finite_float(
        result.get("net_premium", result.get("net_income"))
    )
    principal_anchor_total = _finite_float(
        branch.get(
            "allocated_prior_stock_sale_net_proceeds",
            branch.get("principal_anchor"),
        )
    )
    remaining_contracts = _finite_float(branch.get("remaining_contracts"))
    branch_multiplier = _finite_float(branch.get("multiplier"))
    realized_put = _finite_float(
        branch.get("realized_put_net_pnl_in_current_stage")
    )
    fee_amount = _finite_float(stock_assignment_fee_fact.get("amount"))
    fee_basis = str(stock_assignment_fee_fact.get("basis") or "").strip().lower()
    candidate_currency = str(result.get("currency") or "").strip().upper()
    branch_currency = str(branch.get("currency") or "").strip().upper()
    min_abs_delta, max_abs_delta = _wheel_abs_delta_bounds(wheel_policy)
    for field, value in (
        ("multiplier_unavailable", multiplier),
        ("delta_unavailable", delta),
        ("strike_unavailable", strike),
        ("spot_unavailable", spot),
        ("candidate_net_premium_unavailable", net_premium_per_contract),
        ("principal_anchor_unavailable", principal_anchor_total),
        ("remaining_contracts_unavailable", remaining_contracts),
        ("branch_multiplier_unavailable", branch_multiplier),
        ("realized_put_net_pnl_unavailable", realized_put),
        ("stock_assignment_fee_unavailable", fee_amount),
        ("wheel_abs_delta_bounds_unavailable", min_abs_delta),
        ("wheel_abs_delta_bounds_unavailable", max_abs_delta),
    ):
        if value is None:
            unavailable.append(field)
    if fee_basis not in {"actual", "estimated"}:
        unavailable.append("stock_assignment_fee_unavailable")
    if not candidate_currency or not branch_currency:
        unavailable.append("currency_unavailable")
    elif candidate_currency != branch_currency:
        unavailable.append("currency_mismatch")
    if unavailable:
        return {
            **result,
            "accepted": False,
            "wheel_candidate_status": "data_unavailable",
            "reason_codes": sorted(set(unavailable)),
        }

    assert multiplier is not None
    assert delta is not None
    assert strike is not None
    assert spot is not None
    assert net_premium_per_contract is not None
    assert principal_anchor_total is not None
    assert remaining_contracts is not None
    assert branch_multiplier is not None
    assert realized_put is not None
    assert fee_amount is not None
    assert min_abs_delta is not None
    assert max_abs_delta is not None
    multiplier_int = int(multiplier)
    if (
        multiplier_int <= 0
        or multiplier != multiplier_int
        or branch_multiplier != multiplier
    ):
        unavailable.append("multiplier_unavailable")
    remaining_contracts_int = int(remaining_contracts)
    if (
        remaining_contracts_int <= 0
        or remaining_contracts != remaining_contracts_int
        or contract_count > remaining_contracts_int
    ):
        unavailable.append("remaining_contracts_unavailable")
    if strike <= 0 or spot <= 0 or principal_anchor_total < 0 or fee_amount < 0:
        unavailable.append("wheel_put_economics_invalid")
    assignment_shares = contract_count * multiplier_int
    if assignment_shares <= 0:
        unavailable.append("contracts_unavailable")
    if unavailable:
        return {
            **result,
            "accepted": False,
            "wheel_candidate_status": "data_unavailable",
            "reason_codes": sorted(set(unavailable)),
        }

    absolute_delta = abs(delta)
    if absolute_delta < min_abs_delta:
        reasons.append("wheel_put_abs_delta_below_min")
    if absolute_delta > max_abs_delta:
        reasons.append("wheel_put_abs_delta_above_max")
    if strike > spot:
        reasons.append("wheel_put_strike_above_spot")
    allocated_principal_anchor = (
        principal_anchor_total * contract_count / remaining_contracts_int
    )
    projected_assignment_total = strike * assignment_shares + fee_amount
    if projected_assignment_total > allocated_principal_anchor:
        reasons.append("wheel_put_principal_anchor_exceeded")
    candidate_premium = net_premium_per_contract * contract_count
    replenishment_cash_remainder = (
        allocated_principal_anchor
        - projected_assignment_total
        + realized_put
        + candidate_premium
    )
    result.update(
        {
            "accepted": not reasons,
            "wheel_candidate_status": "accepted" if not reasons else "rejected",
            "reason_codes": sorted(set(reasons)),
            "contracts": contract_count,
            "candidate_assignment_shares": assignment_shares,
            "allocated_prior_stock_sale_net_proceeds": round(
                allocated_principal_anchor,
                6,
            ),
            "estimated_stock_assignment_fees": round(fee_amount, 6),
            "stock_assignment_fee_basis": fee_basis,
            "projected_assignment_total": round(projected_assignment_total, 6),
            "realized_put_net_pnl_in_current_stage": round(realized_put, 6),
            "candidate_put_net_premium": round(candidate_premium, 6),
            "replenishment_cash_remainder": round(
                replenishment_cash_remainder,
                6,
            ),
            "cash_reservation_amount": round(
                strike * multiplier_int * contract_count,
                6,
            ),
            "cash_reservation_currency": candidate_currency,
        }
    )
    return result


def build_wheel_call_rank_key(evaluated_candidate: Mapping[str, Any]) -> dict[str, Any]:
    lifecycle_pnl = _finite_float(
        evaluated_candidate.get("projected_lifecycle_net_pnl_if_called")
    )
    call_key = build_candidate_rank_key(dict(evaluated_candidate), mode="call")
    return {
        "projected_lifecycle_net_pnl_if_called": lifecycle_pnl,
        "covered_call_rank_key": call_key,
        "sort_tuple": (
            lifecycle_pnl is None,
            -float(lifecycle_pnl or 0.0),
            *tuple(call_key["sort_tuple"]),
        ),
    }


def build_wheel_put_rank_key(evaluated_candidate: Mapping[str, Any]) -> dict[str, Any]:
    remainder = _finite_float(evaluated_candidate.get("replenishment_cash_remainder"))
    put_key = build_candidate_rank_key(dict(evaluated_candidate), mode="put")
    return {
        "replenishment_cash_remainder": remainder,
        "sell_put_rank_key": put_key,
        "sort_tuple": (
            remainder is None,
            -float(remainder or 0.0),
            *tuple(put_key["sort_tuple"]),
        ),
    }
