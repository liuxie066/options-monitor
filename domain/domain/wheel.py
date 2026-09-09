from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
import math
from typing import Any, Mapping, Sequence

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.engine.candidate_engine import build_candidate_rank_key
from domain.domain.ledger import TradeEvent, project_trade_events
from domain.domain.symbol_identity import symbol_market
from domain.domain.trade_execution import futu_order_namespace_issue


WHEEL_EVENT_TYPES_V1 = frozenset(
    {
        "wheel_started",
        "wheel_manual_ended",
        "wheel_called_away",
        "wheel_call_intent_created",
        "wheel_call_intent_cancelled",
        "wheel_call_intent_consumed",
        "wheel_call_linkage_rejected",
        "wheel_event_voided",
    }
)
WHEEL_EVENT_TYPES = WHEEL_EVENT_TYPES_V1 | frozenset(
    {
        "wheel_branch_created",
        "wheel_branch_decided",
        "wheel_put_intent_created",
        "wheel_put_intent_cancelled",
        "wheel_put_intent_consumed",
        "wheel_put_linkage_rejected",
    }
)
WHEEL_EVENT_SCHEMA_V1 = "wheel_event.v1"
WHEEL_EVENT_SCHEMA_V2 = "wheel_event.v2"
WHEEL_EVENT_SCHEMA = WHEEL_EVENT_SCHEMA_V2
WHEEL_PROJECTION_SCHEMA = "wheel_projection.v2"


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _wheel_market(value: Mapping[str, Any] | str) -> str:
    symbol = value.get("symbol") if isinstance(value, Mapping) else value
    market = str(symbol_market(symbol) or "").strip().lower()
    declared = (
        str(value.get("market") or "").strip().lower()
        if isinstance(value, Mapping)
        else ""
    )
    if market not in {"us", "hk"} or declared not in {"", market}:
        raise ValueError("Wheel symbol market is invalid")
    return market


def _wheel_abs_delta_bounds(
    wheel_policy: Mapping[str, Any],
) -> tuple[float | None, float | None]:
    minimum = _finite_float(wheel_policy.get("min_abs_delta", 0.25))
    maximum = _finite_float(wheel_policy.get("max_abs_delta", 0.35))
    if (
        minimum is None
        or maximum is None
        or minimum < 0
        or maximum > 1
        or minimum > maximum
    ):
        return None, None
    return minimum, maximum


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


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"wheel event requires {field}")
    return text


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a positive integer") from None
    if number <= 0 or str(value).strip() not in {str(number), f"{number}.0"}:
        raise ValueError(f"{field} must be a positive integer")
    return number


def wheel_event_payload_hash(event: Mapping[str, Any]) -> str:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("wheel event payload must be an object")
    schema_version = str(
        event.get("event_schema_version") or WHEEL_EVENT_SCHEMA_V1
    ).strip()
    if schema_version not in {WHEEL_EVENT_SCHEMA_V1, WHEEL_EVENT_SCHEMA_V2}:
        raise ValueError(f"unsupported wheel event schema: {schema_version}")
    canonical = {
            "schema_version": schema_version,
            "account": str(event.get("account") or "").strip().lower(),
            "stock_lot_id": str(event.get("stock_lot_id") or "").strip(),
            "event_type": str(event.get("event_type") or "").strip().lower(),
            "occurred_at_ms": int(event.get("occurred_at_ms") or 0),
            "intent_id": str(event.get("intent_id") or "").strip() or None,
            "source_trade_event_id": (
                str(event.get("source_trade_event_id") or "").strip() or None
            ),
            "payload": dict(payload),
        }
    if schema_version == WHEEL_EVENT_SCHEMA_V2:
        canonical["wheel_branch_id"] = str(
            event.get("wheel_branch_id") or ""
        ).strip()
        canonical["stock_lot_id"] = (
            str(event.get("stock_lot_id") or "").strip() or None
        )
    return canonical_sha256(canonical)


def normalize_wheel_event(event: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise TypeError("wheel event must be an object")
    event_id = _required_text(event.get("event_id"), "event_id")
    account = _required_text(event.get("account"), "account")
    if account != account.lower():
        raise ValueError("wheel event account must be lowercase")
    event_schema_version = str(
        event.get("event_schema_version") or WHEEL_EVENT_SCHEMA_V1
    ).strip()
    if event_schema_version not in {WHEEL_EVENT_SCHEMA_V1, WHEEL_EVENT_SCHEMA_V2}:
        raise ValueError(
            f"unsupported wheel event schema: {event_schema_version}"
        )
    stock_lot_id = str(event.get("stock_lot_id") or "").strip() or None
    if event_schema_version == WHEEL_EVENT_SCHEMA_V1 and stock_lot_id is None:
        raise ValueError("wheel event requires stock_lot_id")
    wheel_branch_id = str(event.get("wheel_branch_id") or "").strip()
    if event_schema_version == WHEEL_EVENT_SCHEMA_V1:
        if wheel_branch_id and wheel_branch_id != stock_lot_id:
            raise ValueError("wheel_event.v1 branch must equal stock_lot_id")
        wheel_branch_id = str(stock_lot_id)
    else:
        wheel_branch_id = _required_text(wheel_branch_id, "wheel_branch_id")
    event_type = _required_text(event.get("event_type"), "event_type").lower()
    allowed_types = (
        WHEEL_EVENT_TYPES_V1
        if event_schema_version == WHEEL_EVENT_SCHEMA_V1
        else WHEEL_EVENT_TYPES
    )
    if event_type not in allowed_types:
        raise ValueError(f"unsupported wheel event type: {event_type}")
    occurred_at_ms = _positive_int(event.get("occurred_at_ms"), "occurred_at_ms")
    recorded_at_ms = _positive_int(event.get("recorded_at_ms"), "recorded_at_ms")
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("wheel event payload must be an object")
    intent_id = str(event.get("intent_id") or "").strip() or None
    source_trade_event_id = (
        str(event.get("source_trade_event_id") or "").strip() or None
    )
    if (
        event_type.startswith("wheel_call_intent_")
        or event_type.startswith("wheel_put_intent_")
    ) and not intent_id:
        raise ValueError(f"{event_type} requires intent_id")
    if event_type == "wheel_branch_created":
        direction = str(payload.get("direction") or "").strip().lower()
        if direction not in {"call", "put"}:
            raise ValueError("wheel_branch_created requires direction=call|put")
    if event_type == "wheel_branch_decided":
        decision = str(payload.get("decision") or "").strip().lower()
        if decision not in {"start", "end"}:
            raise ValueError("wheel_branch_decided requires decision=start|end")
    if event_type == "wheel_event_voided":
        _required_text(payload.get("target_wheel_event_id"), "target_wheel_event_id")
    normalized = {
        "event_id": event_id,
        "event_schema_version": event_schema_version,
        "account": account,
        "wheel_branch_id": wheel_branch_id,
        "stock_lot_id": stock_lot_id,
        "event_type": event_type,
        "occurred_at_ms": occurred_at_ms,
        "recorded_at_ms": recorded_at_ms,
        "intent_id": intent_id,
        "source_trade_event_id": source_trade_event_id,
        "payload": dict(payload),
    }
    payload_hash = wheel_event_payload_hash(normalized)
    supplied_hash = str(event.get("payload_hash") or "").strip()
    if supplied_hash and supplied_hash != payload_hash:
        raise ValueError(f"wheel event payload hash mismatch: event_id={event_id}")
    normalized["payload_hash"] = payload_hash
    return normalized


def build_wheel_event(
    *,
    event_id: str,
    account: str,
    stock_lot_id: str | None,
    wheel_branch_id: str | None = None,
    event_schema_version: str = WHEEL_EVENT_SCHEMA_V2,
    event_type: str,
    occurred_at_ms: int,
    recorded_at_ms: int,
    payload: Mapping[str, Any],
    intent_id: str | None = None,
    source_trade_event_id: str | None = None,
) -> dict[str, Any]:
    return normalize_wheel_event(
        {
            "event_id": event_id,
            "event_schema_version": event_schema_version,
            "account": account,
            "wheel_branch_id": wheel_branch_id or stock_lot_id,
            "stock_lot_id": stock_lot_id,
            "event_type": event_type,
            "occurred_at_ms": occurred_at_ms,
            "recorded_at_ms": recorded_at_ms,
            "intent_id": intent_id,
            "source_trade_event_id": source_trade_event_id,
            "payload": dict(payload),
        }
    )


def deterministic_wheel_branch_id(
    account: str,
    source_assignment_event_id: str,
    direction: str,
) -> str:
    account_value = _required_text(account, "account").lower()
    source_event_id = _required_text(
        source_assignment_event_id,
        "source_assignment_event_id",
    )
    direction_value = _required_text(direction, "direction").lower()
    if direction_value not in {"call", "put"}:
        raise ValueError("Wheel branch direction must be call or put")
    digest = canonical_sha256(
        {
            "schema_version": "wheel_branch_identity.v1",
            "account": account_value,
            "source_assignment_event_id": source_event_id,
            "direction": direction_value,
        }
    )[:32]
    return f"wheel-{direction_value}-{digest}"


def build_wheel_branch_created_event(
    *,
    account: str,
    source_assignment_event_id: str,
    direction: str,
    occurred_at_ms: int,
    recorded_at_ms: int,
    symbol: str,
    contracts: int,
    multiplier: int | None,
    multiplier_source: str,
    multiplier_evidence_hash: str,
    currency: str | None,
    principal_anchor: str | None,
    principal_anchor_reason: str | None = None,
    principal_anchor_fact_ids: Sequence[str] = (),
    stock_lot_id: str | None = None,
    parent_branch_id: str | None = None,
    lifecycle_status: str = "active",
    activation_window: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    direction_value = _required_text(direction, "direction").lower()
    if direction_value not in {"call", "put"}:
        raise ValueError("Wheel branch direction must be call or put")
    status = _required_text(lifecycle_status, "lifecycle_status").lower()
    if status not in {"active", "pending_decision"}:
        raise ValueError("Wheel branch lifecycle_status must be active or pending_decision")
    account_value = _required_text(account, "account").lower()
    source_event_id = _required_text(
        source_assignment_event_id,
        "source_assignment_event_id",
    )
    stock_lot_value = str(stock_lot_id or "").strip() or None
    if direction_value == "call" and stock_lot_value is None:
        raise ValueError("Wheel Call branch requires stock_lot_id")
    branch_id = (
        stock_lot_value
        if direction_value == "call"
        else deterministic_wheel_branch_id(
            account_value,
            source_event_id,
            direction_value,
        )
    )
    symbol_value = _required_text(symbol, "symbol").upper()
    payload = {
        "schema_version": "wheel_branch_created.v1",
        "market": _wheel_market(symbol_value),
        "direction": direction_value,
        "parent_branch_id": str(parent_branch_id or "").strip() or None,
        "source_assignment_event_id": source_event_id,
        "symbol": symbol_value,
        "contracts": _positive_int(contracts, "contracts"),
        "multiplier": (
            _positive_int(multiplier, "multiplier") if multiplier is not None else None
        ),
        "multiplier_source": _required_text(
            multiplier_source,
            "multiplier_source",
        ),
        "multiplier_evidence_hash": _required_text(
            multiplier_evidence_hash,
            "multiplier_evidence_hash",
        ),
        "currency": str(currency or "").strip().upper() or None,
        "principal_anchor": (
            str(principal_anchor).strip() if principal_anchor is not None else None
        ),
        "principal_anchor_reason": (
            str(principal_anchor_reason or "").strip() or None
        ),
        "principal_anchor_fact_ids": sorted(
            {str(value).strip() for value in principal_anchor_fact_ids if str(value).strip()}
        ),
        "initial_lifecycle_status": status,
        "activation_window": dict(activation_window or {}) or None,
    }
    return build_wheel_event(
        event_id=f"wheel-branch-created:{source_event_id}:{direction_value}",
        event_schema_version=WHEEL_EVENT_SCHEMA_V2,
        account=account_value,
        wheel_branch_id=branch_id,
        stock_lot_id=stock_lot_value,
        event_type="wheel_branch_created",
        occurred_at_ms=occurred_at_ms,
        recorded_at_ms=recorded_at_ms,
        source_trade_event_id=source_event_id,
        payload=payload,
    )


def plan_wheel_branch_decision(
    branch: Mapping[str, Any],
    decision: str,
    request_id: str,
    actor: str,
    expected_generation_hash: str,
    *,
    occurred_at_ms: int,
    recorded_at_ms: int,
) -> dict[str, Any]:
    decision_value = _required_text(decision, "decision").lower()
    if decision_value not in {"start", "end"}:
        raise ValueError("Wheel branch decision must be start or end")
    if branch.get("lifecycle_status") != "pending_decision":
        raise ValueError("Wheel branch is not pending a decision")
    expected = _required_text(
        expected_generation_hash,
        "expected_generation_hash",
    )
    if expected != str(branch.get("branch_generation_hash") or ""):
        raise ValueError("Wheel branch generation changed")
    account = _required_text(branch.get("account"), "account").lower()
    branch_id = _required_text(branch.get("wheel_branch_id"), "wheel_branch_id")
    request = _required_text(request_id, "request_id")
    digest = canonical_sha256(
        {
            "schema_version": "wheel_branch_decision_request.v1",
            "account": account,
            "wheel_branch_id": branch_id,
            "request_id": request,
        }
    )[:24]
    return build_wheel_event(
        event_id=f"wheel-branch-decided:{digest}",
        event_schema_version=WHEEL_EVENT_SCHEMA_V2,
        account=account,
        wheel_branch_id=branch_id,
        stock_lot_id=str(branch.get("stock_lot_id") or "").strip() or None,
        event_type="wheel_branch_decided",
        occurred_at_ms=occurred_at_ms,
        recorded_at_ms=recorded_at_ms,
        payload={
            "schema_version": "wheel_branch_decided.v1",
            "market": _wheel_market(branch),
            "decision": decision_value,
            "request_id": request,
            "actor": _required_text(actor, "actor"),
            "expected_generation_hash": expected,
        },
    )


def _trade_event_fact(event: Any) -> dict[str, Any]:
    if isinstance(event, Mapping):
        out = dict(event)
        key = event.get("contract_key")
        key = key if isinstance(key, Mapping) else {}
        for target, source in (
            ("account", "account"),
            ("symbol", "underlying_symbol"),
            ("option_type", "option_type"),
            ("position_side", "position_side"),
            ("strike", "strike"),
            ("expiration_ymd", "expiration_ymd"),
        ):
            out.setdefault(target, key.get(source))
        return out
    contract_key = getattr(event, "contract_key", None)
    key = contract_key.to_dict() if hasattr(contract_key, "to_dict") else {}
    return {
        "event_id": getattr(event, "event_id", None),
        "event_type": getattr(event, "event_type", None),
        "event_time_ms": getattr(event, "event_time_ms", None),
        "account": key.get("account"),
        "symbol": key.get("underlying_symbol"),
        "option_type": key.get("option_type"),
        "position_side": key.get("position_side"),
        "strike": key.get("strike"),
        "expiration_ymd": key.get("expiration_ymd"),
        "contracts": getattr(event, "contracts", None),
        "multiplier": getattr(event, "multiplier", None),
        "currency": getattr(event, "currency", None),
        "target_lot_id": getattr(event, "target_lot_id", None),
        "lot_id": getattr(event, "lot_id", None),
        "raw_payload": dict(getattr(event, "raw_payload", None) or {}),
    }


def _stock_settlement(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event.get("raw_payload")
    payload = payload if isinstance(payload, Mapping) else {}
    stock = payload.get("stock_settlement")
    return dict(stock) if isinstance(stock, Mapping) else {}


def wheel_started_event_from_assignment(
    terminal_event: Any,
    source_put_lot: Mapping[str, Any],
    *,
    recorded_at_ms: int,
) -> dict[str, Any] | None:
    event = _trade_event_fact(terminal_event)
    if _event_type(event) != "assignment":
        return None
    fields = _lot_fields(source_put_lot)
    if (
        str(fields.get("option_type") or "").strip().lower() != "put"
        or str(fields.get("side") or fields.get("position_side") or "").strip().lower()
        != "short"
    ):
        return None
    event_id = _required_text(event.get("event_id"), "source_trade_event_id")
    account = _required_text(
        event.get("account") or fields.get("account"),
        "account",
    ).lower()
    stock = _stock_settlement(event)
    if str(stock.get("side") or "").strip().lower() != "buy":
        raise ValueError("Wheel start requires buy-side Short Put assignment settlement")
    contracts = _positive_int(event.get("contracts"), "assignment contracts")
    try:
        multiplier = int(float(event.get("multiplier") or fields.get("multiplier") or 0))
        shares = int(stock.get("shares") or stock.get("stock_qty") or 0)
        price = float(stock.get("price") if stock.get("price") is not None else stock.get("stock_price"))
    except (TypeError, ValueError):
        raise ValueError("Wheel start assignment settlement is incomplete") from None
    if multiplier <= 0 or shares != contracts * multiplier or price < 0:
        raise ValueError("Wheel start assignment settlement quantity or price is invalid")
    occurred_at_ms = _positive_int(
        stock.get("event_time_ms") or event.get("event_time_ms"),
        "assignment occurred_at_ms",
    )
    stock_lot_id = f"assigned-stock-{event_id}"
    return build_wheel_event(
        event_id=f"wheel-started:{event_id}",
        event_schema_version=WHEEL_EVENT_SCHEMA_V1,
        account=account,
        stock_lot_id=stock_lot_id,
        event_type="wheel_started",
        occurred_at_ms=occurred_at_ms,
        recorded_at_ms=recorded_at_ms,
        source_trade_event_id=event_id,
        payload={
            "schema_version": "wheel_started.v1",
            "source_option_lot_id": str(event.get("target_lot_id") or "").strip(),
            "shares": shares,
            "assignment_price": price,
            "currency": str(stock.get("currency") or event.get("currency") or "").strip().upper(),
        },
    )


def wheel_called_away_event_from_call_assignment(
    terminal_event: Any,
    source_call_lot: Mapping[str, Any],
    stock_lot_before: Mapping[str, Any] | None,
    stock_lot_after: Mapping[str, Any] | None,
    *,
    recorded_at_ms: int,
) -> dict[str, Any] | None:
    event = _trade_event_fact(terminal_event)
    if _event_type(event) != "assignment":
        return None
    fields = _lot_fields(source_call_lot)
    strategy = str(fields.get("strategy") or "").strip().lower()
    leg_role = str(fields.get("leg_role") or "").strip().lower()
    stock_lot_id = str(fields.get("source_stock_lot_id") or "").strip()
    if strategy != "wheel" and leg_role != "wheel_call" and not stock_lot_id:
        return None
    if (
        strategy != "wheel"
        or leg_role != "wheel_call"
        or not stock_lot_id
        or str(fields.get("strategy_group_id") or "").strip()
        or str(fields.get("option_type") or "").strip().lower() != "call"
        or str(fields.get("side") or fields.get("position_side") or "").strip().lower()
        != "short"
    ):
        raise ValueError("Wheel Call assignment has incomplete or conflicting linkage")
    stock = _stock_settlement(event)
    if str(stock.get("side") or "").strip().lower() != "sell":
        raise ValueError("Wheel Call assignment requires sell-side stock settlement")
    contracts = _positive_int(event.get("contracts"), "assignment contracts")
    try:
        multiplier = int(float(event.get("multiplier") or fields.get("multiplier") or 0))
        shares = int(stock.get("shares") or stock.get("stock_qty") or 0)
        before = int((stock_lot_before or {}).get("shares_remaining"))
        after = int((stock_lot_after or {}).get("shares_remaining"))
    except (TypeError, ValueError):
        raise ValueError("Wheel Call assignment stock-lot evidence is incomplete") from None
    if multiplier <= 0 or shares != contracts * multiplier:
        raise ValueError("Wheel Call assignment settlement quantity is invalid")
    if (
        str((stock_lot_before or {}).get("stock_lot_id") or "") != stock_lot_id
        or str((stock_lot_after or {}).get("stock_lot_id") or "") != stock_lot_id
        or before - after != shares
        or after < 0
    ):
        raise ValueError("Wheel Call assignment did not exactly reduce its stock batch")
    if after > 0:
        return None
    source_event_id = _required_text(event.get("event_id"), "source_trade_event_id")
    account = _required_text(
        event.get("account") or fields.get("account"),
        "account",
    ).lower()
    occurred_at_ms = _positive_int(
        stock.get("event_time_ms") or event.get("event_time_ms"),
        "assignment occurred_at_ms",
    )
    return build_wheel_event(
        event_id=f"wheel-called-away:{source_event_id}:{stock_lot_id}",
        event_schema_version=WHEEL_EVENT_SCHEMA_V1,
        account=account,
        stock_lot_id=stock_lot_id,
        event_type="wheel_called_away",
        occurred_at_ms=occurred_at_ms,
        recorded_at_ms=recorded_at_ms,
        source_trade_event_id=source_event_id,
        payload={
            "schema_version": "wheel_called_away.v1",
            "source_call_lot_id": str(event.get("target_lot_id") or "").strip(),
            "shares": shares,
        },
    )


def plan_wheel_manual_end(
    wheel_batch: Mapping[str, Any],
    request_id: str,
    actor: str,
    *,
    occurred_at_ms: int,
    recorded_at_ms: int,
    account: str,
) -> dict[str, Any]:
    stock_lot_id = _required_text(wheel_batch.get("stock_lot_id"), "stock_lot_id")
    if wheel_batch.get("lifecycle_status") != "active":
        raise ValueError("Wheel lifecycle is not active")
    if wheel_batch.get("integrity_status") != "trusted":
        raise ValueError("Wheel lifecycle integrity is not trusted")
    if wheel_batch.get("active_call_lot_ids"):
        raise ValueError("Wheel lifecycle has an active Call")
    if wheel_batch.get("active_intent_ids"):
        raise ValueError("Wheel lifecycle has an active Call intent")
    request = _required_text(request_id, "request_id")
    actor_value = _required_text(actor, "actor")
    account_value = _required_text(account, "account").lower()
    event_digest = canonical_sha256(
        {
            "account": account_value,
            "stock_lot_id": stock_lot_id,
            "request_id": request,
        }
    )[:24]
    return build_wheel_event(
        event_id=f"wheel-manual-ended:{event_digest}",
        event_schema_version=(
            WHEEL_EVENT_SCHEMA_V1
            if wheel_batch.get("legacy_call_adapter")
            else WHEEL_EVENT_SCHEMA_V2
        ),
        account=account_value,
        stock_lot_id=stock_lot_id,
        event_type="wheel_manual_ended",
        occurred_at_ms=occurred_at_ms,
        recorded_at_ms=recorded_at_ms,
        payload={
            "schema_version": "wheel_manual_ended.v1",
            "market": _wheel_market(wheel_batch),
            "request_id": request,
            "actor": actor_value,
            "batch_generation_hash": str(
                wheel_batch.get("batch_generation_hash") or ""
            ),
        },
    )


def _coverage_capacity(
    coverage_fact: Mapping[str, Any],
    *,
    account: str,
    symbol: str,
    contracts: int,
    multiplier: int,
) -> None:
    if not isinstance(coverage_fact, Mapping):
        raise ValueError("Wheel Call requires coverage_fact")
    if str(coverage_fact.get("account") or "").strip().lower() != account:
        raise ValueError("Wheel Call coverage account mismatch")
    if str(coverage_fact.get("symbol") or "").strip().upper() != symbol:
        raise ValueError("Wheel Call coverage symbol mismatch")
    if not str(coverage_fact.get("capacity_identity_hash") or "").strip():
        raise ValueError("Wheel Call coverage identity is unavailable")
    if str(coverage_fact.get("status") or "").strip().lower() != "available":
        raise ValueError("Wheel Call coverage is unavailable")
    try:
        shares_available = int(coverage_fact.get("shares_available_for_cover"))
    except (TypeError, ValueError):
        raise ValueError("Wheel Call available shares are invalid") from None
    if shares_available < contracts * multiplier:
        raise ValueError("Wheel Call coverage is insufficient")


def build_wheel_intent_capacity_binding(
    branch: Mapping[str, Any],
    final_candidate: Mapping[str, Any],
    capacity_fact: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the immutable capacity facts bound into a new Wheel intent."""

    direction = str(branch.get("direction") or "call").strip().lower()
    if direction not in {"call", "put"}:
        raise ValueError("Wheel intent direction must be call or put")
    account = _required_text(branch.get("account"), "account").lower()
    symbol = _required_text(branch.get("symbol"), "symbol").upper()
    branch_id = _required_text(
        branch.get("wheel_branch_id") or branch.get("stock_lot_id"),
        "wheel_branch_id",
    )
    generation_hash = _required_text(
        branch.get("branch_generation_hash") or branch.get("batch_generation_hash"),
        "branch_generation_hash",
    )
    contracts = _positive_int(
        final_candidate.get("granted_contracts"),
        "granted_contracts",
    )
    multiplier = _positive_int(final_candidate.get("multiplier"), "multiplier")
    candidate_branch_id = str(
        final_candidate.get("wheel_branch_id")
        or final_candidate.get("stock_lot_id")
        or ""
    ).strip()
    if candidate_branch_id != branch_id:
        raise ValueError("Wheel candidate branch mismatch")
    if str(final_candidate.get("account") or account).strip().lower() != account:
        raise ValueError("Wheel candidate account mismatch")
    if str(final_candidate.get("symbol") or "").strip().upper() != symbol:
        raise ValueError("Wheel candidate symbol mismatch")
    candidate_generation = str(
        final_candidate.get("branch_generation_hash")
        or final_candidate.get("batch_generation_hash")
        or ""
    ).strip()
    if candidate_generation and candidate_generation != generation_hash:
        raise ValueError("Wheel candidate branch generation mismatch")
    capacity_identity_hash = _required_text(
        capacity_fact.get("capacity_identity_hash"),
        "capacity_identity_hash",
    )
    candidate_capacity_hash = str(
        final_candidate.get("capacity_identity_hash") or ""
    ).strip()
    if candidate_capacity_hash != capacity_identity_hash:
        raise ValueError("Wheel candidate capacity identity mismatch")

    if direction == "call":
        _coverage_capacity(
            capacity_fact,
            account=account,
            symbol=symbol,
            contracts=contracts,
            multiplier=multiplier,
        )
        return {
            "direction": direction,
            "wheel_branch_id": branch_id,
            "branch_generation_hash": generation_hash,
            "capacity_identity_hash": capacity_identity_hash,
            "reserved_amount": contracts * multiplier,
            "reservation_unit": "shares",
            "currency": None,
        }

    if str(capacity_fact.get("account") or "").strip().lower() != account:
        raise ValueError("Wheel Put cash capacity account mismatch")
    if str(capacity_fact.get("allocation_status") or "").strip().lower() != "allocated":
        raise ValueError("Wheel Put cash capacity is unavailable")
    if int(capacity_fact.get("granted_contracts") or 0) < contracts:
        raise ValueError("Wheel Put cash capacity is insufficient")
    strike = _finite_float(final_candidate.get("strike"))
    if strike is None or strike <= 0:
        raise ValueError("Wheel Put candidate strike must be positive")
    currency = _required_text(
        final_candidate.get("cash_reservation_currency")
        or final_candidate.get("currency"),
        "cash_reservation_currency",
    ).upper()
    reserved_amount = round(strike * multiplier * contracts, 6)
    fact_currency = str(
        capacity_fact.get("cash_reservation_currency")
        or capacity_fact.get("currency")
        or ""
    ).strip().upper()
    fact_amount = _finite_float(capacity_fact.get("cash_reservation_amount"))
    if fact_currency != currency or fact_amount != reserved_amount:
        raise ValueError("Wheel Put cash reservation binding mismatch")
    return {
        "direction": direction,
        "wheel_branch_id": branch_id,
        "branch_generation_hash": generation_hash,
        "capacity_identity_hash": capacity_identity_hash,
        "reserved_amount": reserved_amount,
        "reservation_unit": "cash",
        "currency": currency,
    }


def plan_wheel_call_intent_create(
    batch: Mapping[str, Any],
    final_candidate: Mapping[str, Any],
    coverage_fact: Mapping[str, Any],
    expires_at_ms: int,
    request_id: str,
    actor: str,
    *,
    occurred_at_ms: int,
    recorded_at_ms: int,
    broker_order_id: str | None = None,
) -> dict[str, Any]:
    if batch.get("lifecycle_status") != "active":
        raise ValueError("Wheel lifecycle is not active")
    if batch.get("integrity_status") != "trusted":
        raise ValueError("Wheel lifecycle integrity is not trusted")
    if batch.get("active_call_lot_ids") or batch.get("active_intent_ids"):
        raise ValueError("Wheel batch already has an active Call or intent")
    if batch.get("phase") != "ready":
        raise ValueError("Wheel batch is not ready for a Call intent")
    account = _required_text(batch.get("account"), "account").lower()
    symbol = _required_text(batch.get("symbol"), "symbol").upper()
    stock_lot_id = _required_text(batch.get("stock_lot_id"), "stock_lot_id")
    candidate_id = _required_text(
        final_candidate.get("final_candidate_id")
        or final_candidate.get("candidate_id"),
        "final_candidate_id",
    )
    contracts = _positive_int(
        final_candidate.get("granted_contracts"),
        "granted_contracts",
    )
    multiplier = _positive_int(final_candidate.get("multiplier"), "multiplier")
    strike = float(final_candidate.get("strike") or 0)
    expiration_ymd = _required_text(
        final_candidate.get("expiration_ymd") or final_candidate.get("expiration"),
        "expiration_ymd",
    )
    if strike <= 0:
        raise ValueError("Wheel Call candidate strike must be positive")
    if str(final_candidate.get("account") or account).strip().lower() != account:
        raise ValueError("Wheel Call candidate account mismatch")
    if str(final_candidate.get("symbol") or "").strip().upper() != symbol:
        raise ValueError("Wheel Call candidate symbol mismatch")
    if str(final_candidate.get("stock_lot_id") or "").strip() != stock_lot_id:
        raise ValueError("Wheel Call candidate stock batch mismatch")
    if int(batch.get("shares_remaining") or 0) < contracts * multiplier:
        raise ValueError("Wheel batch shares are insufficient")
    now = _positive_int(occurred_at_ms, "occurred_at_ms")
    expiry = _positive_int(expires_at_ms, "expires_at_ms")
    if expiry <= now:
        raise ValueError("Wheel Call intent expiry must be in the future")
    _coverage_capacity(
        coverage_fact,
        account=account,
        symbol=symbol,
        contracts=contracts,
        multiplier=multiplier,
    )
    request = _required_text(request_id, "request_id")
    actor_value = _required_text(actor, "actor")
    digest = canonical_sha256(
        {"account": account, "stock_lot_id": stock_lot_id, "request_id": request}
    )[:24]
    intent_id = f"wheel-call-intent:{digest}"
    return build_wheel_event(
        event_id=f"wheel-call-intent-created:{digest}",
        event_schema_version=(
            WHEEL_EVENT_SCHEMA_V1
            if batch.get("legacy_call_adapter")
            else WHEEL_EVENT_SCHEMA_V2
        ),
        account=account,
        stock_lot_id=stock_lot_id,
        event_type="wheel_call_intent_created",
        occurred_at_ms=now,
        recorded_at_ms=recorded_at_ms,
        intent_id=intent_id,
        payload={
            "schema_version": "wheel_call_intent_created.v1",
            "market": _wheel_market(batch),
            "request_id": request,
            "actor": actor_value,
            "final_candidate_id": candidate_id,
            "snapshot_hash": str(final_candidate.get("snapshot_hash") or "").strip(),
            "batch_generation_hash": str(batch.get("batch_generation_hash") or ""),
            "capacity_identity_hash": str(
                coverage_fact.get("capacity_identity_hash") or ""
            ).strip(),
            "symbol": symbol,
            "strike": strike,
            "expiration_ymd": expiration_ymd,
            "contracts": contracts,
            "multiplier": multiplier,
            "expires_at_ms": expiry,
            "broker_order_id": str(broker_order_id or "").strip() or None,
        },
    )


def plan_wheel_put_intent_create(
    branch: Mapping[str, Any],
    final_candidate: Mapping[str, Any],
    cash_capacity_fact: Mapping[str, Any],
    expires_at_ms: int,
    request_id: str,
    actor: str,
    *,
    occurred_at_ms: int,
    recorded_at_ms: int,
    broker_order_id: str | None = None,
) -> dict[str, Any]:
    if str(branch.get("direction") or "").strip().lower() != "put":
        raise ValueError("Wheel Put intent requires a Put branch")
    if branch.get("lifecycle_status") != "active":
        raise ValueError("Wheel branch is not active")
    if branch.get("integrity_status") != "trusted":
        raise ValueError("Wheel branch integrity is not trusted")
    if branch.get("active_option_lot_ids") or branch.get("active_intent_ids"):
        raise ValueError("Wheel branch already has an active Put or intent")
    if branch.get("phase") != "ready":
        raise ValueError("Wheel branch is not ready for a Put intent")
    account = _required_text(branch.get("account"), "account").lower()
    symbol = _required_text(branch.get("symbol"), "symbol").upper()
    branch_id = _required_text(branch.get("wheel_branch_id"), "wheel_branch_id")
    candidate_id = _required_text(
        final_candidate.get("final_candidate_id")
        or final_candidate.get("candidate_id"),
        "final_candidate_id",
    )
    contracts = _positive_int(
        final_candidate.get("granted_contracts"),
        "granted_contracts",
    )
    multiplier = _positive_int(final_candidate.get("multiplier"), "multiplier")
    strike = _finite_float(final_candidate.get("strike"))
    if strike is None or strike <= 0:
        raise ValueError("Wheel Put candidate strike must be positive")
    expiration_ymd = _required_text(
        final_candidate.get("expiration_ymd") or final_candidate.get("expiration"),
        "expiration_ymd",
    )
    binding = build_wheel_intent_capacity_binding(
        branch,
        final_candidate,
        cash_capacity_fact,
    )
    now = _positive_int(occurred_at_ms, "occurred_at_ms")
    expiry = _positive_int(expires_at_ms, "expires_at_ms")
    if expiry <= now:
        raise ValueError("Wheel Put intent expiry must be in the future")
    request = _required_text(request_id, "request_id")
    actor_value = _required_text(actor, "actor")
    digest = canonical_sha256(
        {"account": account, "wheel_branch_id": branch_id, "request_id": request}
    )[:24]
    intent_id = f"wheel-put-intent:{digest}"
    return build_wheel_event(
        event_id=f"wheel-put-intent-created:{digest}",
        event_schema_version=WHEEL_EVENT_SCHEMA_V2,
        account=account,
        wheel_branch_id=branch_id,
        stock_lot_id=str(branch.get("stock_lot_id") or "").strip() or None,
        event_type="wheel_put_intent_created",
        occurred_at_ms=now,
        recorded_at_ms=recorded_at_ms,
        intent_id=intent_id,
        payload={
            "schema_version": "wheel_put_intent_created.v1",
            "market": _wheel_market(branch),
            "request_id": request,
            "actor": actor_value,
            "final_candidate_id": candidate_id,
            "snapshot_hash": str(final_candidate.get("snapshot_hash") or "").strip(),
            "branch_generation_hash": binding["branch_generation_hash"],
            "capacity_identity_hash": binding["capacity_identity_hash"],
            "symbol": symbol,
            "strike": strike,
            "expiration_ymd": expiration_ymd,
            "contracts": contracts,
            "multiplier": multiplier,
            "cash_reservation_amount": binding["reserved_amount"],
            "cash_reservation_currency": binding["currency"],
            "expires_at_ms": expiry,
            "broker_order_id": str(broker_order_id or "").strip() or None,
        },
    )


def plan_wheel_call_intent_cancel(
    batch: Mapping[str, Any],
    intent: Mapping[str, Any],
    request_id: str,
    actor: str,
    broker_order_inactive_confirmed: bool,
    reason: str,
    *,
    occurred_at_ms: int,
    recorded_at_ms: int,
) -> dict[str, Any] | None:
    if batch.get("lifecycle_status") != "active" or batch.get("integrity_status") != "trusted":
        raise ValueError("Wheel batch is not an active trusted lifecycle")
    if not broker_order_inactive_confirmed:
        raise ValueError("broker_order_inactive_confirmed=true is required")
    if str(intent.get("status") or "") != "active":
        return None
    intent_id = _required_text(intent.get("intent_id"), "intent_id")
    request = _required_text(request_id, "request_id")
    account = _required_text(batch.get("account"), "account").lower()
    stock_lot_id = _required_text(batch.get("stock_lot_id"), "stock_lot_id")
    digest = canonical_sha256(
        {
            "account": account,
            "stock_lot_id": stock_lot_id,
            "intent_id": intent_id,
            "request_id": request,
        }
    )[:24]
    return build_wheel_event(
        event_id=f"wheel-call-intent-cancelled:{digest}",
        event_schema_version=(
            WHEEL_EVENT_SCHEMA_V1
            if batch.get("legacy_call_adapter")
            else WHEEL_EVENT_SCHEMA_V2
        ),
        account=account,
        stock_lot_id=stock_lot_id,
        event_type="wheel_call_intent_cancelled",
        occurred_at_ms=occurred_at_ms,
        recorded_at_ms=recorded_at_ms,
        intent_id=intent_id,
        payload={
            "schema_version": "wheel_call_intent_cancelled.v1",
            "market": _wheel_market(batch),
            "request_id": request,
            "actor": _required_text(actor, "actor"),
            "reason": _required_text(reason, "reason"),
            "broker_order_inactive_confirmed": True,
            "remaining_contracts": int(intent.get("remaining_contracts") or 0),
            "batch_generation_hash": str(batch.get("batch_generation_hash") or ""),
        },
    )


def plan_wheel_call_intent_consume(
    batch: Mapping[str, Any],
    intent: Mapping[str, Any],
    fill: Any,
    coverage_fact: Mapping[str, Any],
    *,
    recorded_at_ms: int,
) -> dict[str, Any]:
    if batch.get("lifecycle_status") != "active" or batch.get("integrity_status") != "trusted":
        raise ValueError("Wheel batch is not an active trusted lifecycle")
    if str(intent.get("status") or "") != "active":
        raise ValueError("Wheel Call intent is not active")
    event = _trade_event_fact(fill)
    if (
        _event_type(event) != "open"
        or _trade_option_type(event) != "call"
        or _trade_position_side(event) != "short"
    ):
        raise ValueError("Wheel Call intent can only consume a Short Call open")
    payload = intent.get("payload")
    payload = payload if isinstance(payload, Mapping) else intent
    event_id = _required_text(event.get("event_id"), "source_trade_event_id")
    contracts = _positive_int(event.get("contracts"), "fill contracts")
    multiplier = _positive_int(event.get("multiplier"), "fill multiplier")
    occurred_at_ms = _positive_int(event.get("event_time_ms"), "fill occurred_at_ms")
    if contracts > int(intent.get("remaining_contracts") or 0):
        raise ValueError("Wheel Call fill exceeds intent remainder")
    if not (
        int(intent.get("created_at_ms") or 0)
        <= occurred_at_ms
        <= int(intent.get("expires_at_ms") or 0)
    ):
        raise ValueError("Wheel Call fill is outside the intent window")
    if (
        _trade_account(event) != str(batch.get("account") or "")
        or _trade_symbol(event) != str(batch.get("symbol") or "")
        or float(event.get("strike") or 0) != float(payload.get("strike") or 0)
        or str(event.get("expiration_ymd") or "")
        != str(payload.get("expiration_ymd") or "")
        or multiplier != int(payload.get("multiplier") or 0)
    ):
        raise ValueError("Wheel Call fill does not match the intent contract")
    bound_order = str(payload.get("broker_order_id") or "").strip()
    fill_payload = event.get("raw_payload") or {}
    if bound_order and (
        str(fill_payload.get("order_id") or "").strip() != bound_order
        or futu_order_namespace_issue(fill_payload) is not None
    ):
        raise ValueError("Wheel Call fill does not match the bound order")
    _coverage_capacity(
        coverage_fact,
        account=str(batch.get("account") or ""),
        symbol=str(batch.get("symbol") or ""),
        contracts=contracts,
        multiplier=multiplier,
    )
    intent_id = _required_text(intent.get("intent_id"), "intent_id")
    return build_wheel_event(
        event_id=f"wheel-call-intent-consumed:{intent_id}:{event_id}",
        event_schema_version=(
            WHEEL_EVENT_SCHEMA_V1
            if batch.get("legacy_call_adapter")
            else WHEEL_EVENT_SCHEMA_V2
        ),
        account=str(batch.get("account") or ""),
        stock_lot_id=str(batch.get("stock_lot_id") or ""),
        event_type="wheel_call_intent_consumed",
        occurred_at_ms=occurred_at_ms,
        recorded_at_ms=recorded_at_ms,
        intent_id=intent_id,
        source_trade_event_id=event_id,
        payload={
            "schema_version": "wheel_call_intent_consumed.v1",
            "market": _wheel_market(batch),
            "contracts": contracts,
            "multiplier": multiplier,
            "call_lot_id": str(event.get("lot_id") or f"lot_{event_id}"),
        },
    )


def _validate_put_intent_reservation(
    intent_payload: Mapping[str, Any],
    cash_capacity_fact: Mapping[str, Any],
    *,
    account: str,
) -> None:
    if str(cash_capacity_fact.get("account") or "").strip().lower() != account:
        raise ValueError("Wheel Put cash capacity account mismatch")
    expected_hash = _required_text(
        intent_payload.get("capacity_identity_hash"),
        "intent capacity_identity_hash",
    )
    if str(cash_capacity_fact.get("capacity_identity_hash") or "").strip() != expected_hash:
        raise ValueError("Wheel Put cash capacity identity changed")
    expected_currency = _required_text(
        intent_payload.get("cash_reservation_currency"),
        "intent cash_reservation_currency",
    ).upper()
    fact_currency = _required_text(
        cash_capacity_fact.get("cash_reservation_currency"),
        "cash_reservation_currency",
    ).upper()
    expected_amount = _finite_float(intent_payload.get("cash_reservation_amount"))
    fact_amount = _finite_float(cash_capacity_fact.get("cash_reservation_amount"))
    if (
        expected_currency != fact_currency
        or expected_amount is None
        or fact_amount is None
        or round(expected_amount, 6) != round(fact_amount, 6)
    ):
        raise ValueError("Wheel Put cash reservation changed")


def plan_wheel_put_intent_cancel(
    branch: Mapping[str, Any],
    intent: Mapping[str, Any],
    cash_capacity_fact: Mapping[str, Any],
    request_id: str,
    actor: str,
    broker_order_inactive_confirmed: bool,
    reason: str,
    *,
    occurred_at_ms: int,
    recorded_at_ms: int,
) -> dict[str, Any] | None:
    if branch.get("lifecycle_status") != "active" or branch.get("integrity_status") != "trusted":
        raise ValueError("Wheel branch is not an active trusted lifecycle")
    if str(branch.get("direction") or "").strip().lower() != "put":
        raise ValueError("Wheel Put intent requires a Put branch")
    if not broker_order_inactive_confirmed:
        raise ValueError("broker_order_inactive_confirmed=true is required")
    if str(intent.get("status") or "") != "active":
        return None
    payload = intent.get("payload")
    payload = payload if isinstance(payload, Mapping) else intent
    account = _required_text(branch.get("account"), "account").lower()
    _validate_put_intent_reservation(
        payload,
        cash_capacity_fact,
        account=account,
    )
    branch_id = _required_text(branch.get("wheel_branch_id"), "wheel_branch_id")
    intent_id = _required_text(intent.get("intent_id"), "intent_id")
    request = _required_text(request_id, "request_id")
    digest = canonical_sha256(
        {
            "account": account,
            "wheel_branch_id": branch_id,
            "intent_id": intent_id,
            "request_id": request,
        }
    )[:24]
    return build_wheel_event(
        event_id=f"wheel-put-intent-cancelled:{digest}",
        event_schema_version=WHEEL_EVENT_SCHEMA_V2,
        account=account,
        wheel_branch_id=branch_id,
        stock_lot_id=str(branch.get("stock_lot_id") or "").strip() or None,
        event_type="wheel_put_intent_cancelled",
        occurred_at_ms=occurred_at_ms,
        recorded_at_ms=recorded_at_ms,
        intent_id=intent_id,
        payload={
            "schema_version": "wheel_put_intent_cancelled.v1",
            "market": _wheel_market(branch),
            "request_id": request,
            "actor": _required_text(actor, "actor"),
            "reason": _required_text(reason, "reason"),
            "broker_order_inactive_confirmed": True,
            "remaining_contracts": int(intent.get("remaining_contracts") or 0),
            "branch_generation_hash": str(
                branch.get("branch_generation_hash") or ""
            ),
            "capacity_identity_hash": payload.get("capacity_identity_hash"),
            "cash_reservation_amount": payload.get("cash_reservation_amount"),
            "cash_reservation_currency": payload.get("cash_reservation_currency"),
        },
    )


def plan_wheel_put_intent_consume(
    branch: Mapping[str, Any],
    intent: Mapping[str, Any],
    fill: Any,
    cash_capacity_fact: Mapping[str, Any],
    *,
    recorded_at_ms: int,
) -> dict[str, Any]:
    if branch.get("lifecycle_status") != "active" or branch.get("integrity_status") != "trusted":
        raise ValueError("Wheel branch is not an active trusted lifecycle")
    if str(branch.get("direction") or "").strip().lower() != "put":
        raise ValueError("Wheel Put intent requires a Put branch")
    if str(intent.get("status") or "") != "active":
        raise ValueError("Wheel Put intent is not active")
    event = _trade_event_fact(fill)
    if (
        _event_type(event) != "open"
        or _trade_option_type(event) != "put"
        or _trade_position_side(event) != "short"
    ):
        raise ValueError("Wheel Put intent can only consume a Short Put open")
    payload = intent.get("payload")
    payload = payload if isinstance(payload, Mapping) else intent
    account = _required_text(branch.get("account"), "account").lower()
    _validate_put_intent_reservation(
        payload,
        cash_capacity_fact,
        account=account,
    )
    event_id = _required_text(event.get("event_id"), "source_trade_event_id")
    contracts = _positive_int(event.get("contracts"), "fill contracts")
    multiplier = _positive_int(event.get("multiplier"), "fill multiplier")
    occurred_at_ms = _positive_int(event.get("event_time_ms"), "fill occurred_at_ms")
    if contracts > int(intent.get("remaining_contracts") or 0):
        raise ValueError("Wheel Put fill exceeds intent remainder")
    if not (
        int(intent.get("created_at_ms") or 0)
        <= occurred_at_ms
        <= int(intent.get("expires_at_ms") or 0)
    ):
        raise ValueError("Wheel Put fill is outside the intent window")
    if (
        _trade_account(event) != account
        or _trade_symbol(event) != str(branch.get("symbol") or "")
        or float(event.get("strike") or 0) != float(payload.get("strike") or 0)
        or str(event.get("expiration_ymd") or "")
        != str(payload.get("expiration_ymd") or "")
        or multiplier != int(payload.get("multiplier") or 0)
    ):
        raise ValueError("Wheel Put fill does not match the intent contract")
    bound_order = str(payload.get("broker_order_id") or "").strip()
    fill_payload = event.get("raw_payload") or {}
    if bound_order and (
        str(fill_payload.get("order_id") or "").strip() != bound_order
        or futu_order_namespace_issue(fill_payload) is not None
    ):
        raise ValueError("Wheel Put fill does not match the bound order")
    intent_id = _required_text(intent.get("intent_id"), "intent_id")
    branch_id = _required_text(branch.get("wheel_branch_id"), "wheel_branch_id")
    return build_wheel_event(
        event_id=f"wheel-put-intent-consumed:{intent_id}:{event_id}",
        event_schema_version=WHEEL_EVENT_SCHEMA_V2,
        account=account,
        wheel_branch_id=branch_id,
        stock_lot_id=str(branch.get("stock_lot_id") or "").strip() or None,
        event_type="wheel_put_intent_consumed",
        occurred_at_ms=occurred_at_ms,
        recorded_at_ms=recorded_at_ms,
        intent_id=intent_id,
        source_trade_event_id=event_id,
        payload={
            "schema_version": "wheel_put_intent_consumed.v1",
            "market": _wheel_market(branch),
            "contracts": contracts,
            "multiplier": multiplier,
            "put_lot_id": str(event.get("lot_id") or f"lot_{event_id}"),
            "capacity_identity_hash": payload.get("capacity_identity_hash"),
            "cash_reservation_amount": round(
                float(payload.get("strike") or 0) * multiplier * contracts,
                6,
            ),
            "cash_reservation_currency": payload.get("cash_reservation_currency"),
        },
    )
def _lot_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    fields = row.get("fields")
    return dict(fields) if isinstance(fields, Mapping) else dict(row)


def _event_type(row: Mapping[str, Any]) -> str:
    payload = row.get("raw_payload")
    payload = payload if isinstance(payload, Mapping) else {}
    return str(
        row.get("event_type") or payload.get("close_type") or ""
    ).strip().lower()


def _trade_account(row: Mapping[str, Any]) -> str:
    key = row.get("contract_key")
    key = key if isinstance(key, Mapping) else {}
    return str(row.get("account") or key.get("account") or "").strip().lower()


def _trade_symbol(row: Mapping[str, Any]) -> str:
    key = row.get("contract_key")
    key = key if isinstance(key, Mapping) else {}
    return str(
        row.get("symbol") or key.get("underlying_symbol") or key.get("symbol") or ""
    ).strip().upper()


def _trade_option_type(row: Mapping[str, Any]) -> str:
    key = row.get("contract_key")
    key = key if isinstance(key, Mapping) else {}
    return str(row.get("option_type") or key.get("option_type") or "").strip().lower()


def _trade_position_side(row: Mapping[str, Any]) -> str:
    key = row.get("contract_key")
    key = key if isinstance(key, Mapping) else {}
    explicit = str(row.get("position_side") or key.get("position_side") or "").strip().lower()
    if explicit:
        return explicit
    side = str(row.get("side") or "").strip().lower()
    effect = str(row.get("position_effect") or "").strip().lower()
    if effect == "open":
        return "short" if side == "sell" else "long" if side == "buy" else ""
    if effect == "close":
        return "short" if side == "buy" else "long" if side == "sell" else ""
    return ""


def _active_trade_events(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    voided = {
        str(row.get("target_event_id") or "").strip()
        for row in rows
        if _event_type(row) == "void" and str(row.get("target_event_id") or "").strip()
    }
    return [
        dict(row)
        for row in rows
        if _event_type(row) != "void"
        and str(row.get("event_id") or "").strip() not in voided
    ]


def _contracts_open(fields: Mapping[str, Any]) -> int:
    try:
        if str(fields.get("status") or "").strip().lower() == "close":
            return 0
        return max(0, int(fields.get("contracts_open", fields.get("contracts", 0)) or 0))
    except (TypeError, ValueError):
        return 0


def _stable_stock_fact(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    fields = (
        "stock_lot_id",
        "source_assignment_event_id",
        "account",
        "broker",
        "symbol",
        "currency",
        "assigned_at_ms",
        "shares_opened",
        "shares_remaining",
        "shares_sold",
        "assignment_price",
        "assignment_fees",
        "stock_cost_basis_total",
        "stock_principal_basis_total",
        "stock_sale_cash_in_net",
        "stock_sale_cash_in_gross",
        "stock_sale_fees",
        "assigned_stock_realized_pnl",
        "sale_event_ids",
    )
    return {field: row.get(field) for field in fields}


def _intent_contracts(payload: Mapping[str, Any]) -> int | None:
    for field in ("contracts", "granted_contracts", "quantity"):
        value = payload.get(field)
        if value in (None, ""):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None
    return None


def _intent_state(
    events: Sequence[Mapping[str, Any]],
    *,
    as_of_ms: int,
    known_trade_event_ids: set[str],
    direction: str = "call",
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    direction_value = _required_text(direction, "direction").lower()
    if direction_value not in {"call", "put"}:
        raise ValueError("Wheel intent direction must be call or put")
    prefix = f"wheel_{direction_value}_intent_"
    by_intent: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    reasons: list[str] = []
    for event in events:
        event_type = str(event.get("event_type") or "")
        if not event_type.startswith(prefix):
            continue
        intent_id = str(event.get("intent_id") or "").strip()
        if not intent_id:
            reasons.append("intent_id_missing")
            continue
        by_intent[intent_id].append(event)
    active: list[str] = []
    summaries: list[dict[str, Any]] = []
    for intent_id in sorted(by_intent):
        intent_events = by_intent[intent_id]
        created = [item for item in intent_events if item["event_type"] == f"{prefix}created"]
        cancelled = [item for item in intent_events if item["event_type"] == f"{prefix}cancelled"]
        consumed = [item for item in intent_events if item["event_type"] == f"{prefix}consumed"]
        if len(created) != 1:
            reasons.append("intent_creation_conflict")
            summaries.append({"intent_id": intent_id, "status": "conflict"})
            continue
        creation = created[0]
        created_contracts = _intent_contracts(creation["payload"])
        try:
            expires_at_ms = int(creation["payload"].get("expires_at_ms") or 0)
        except (TypeError, ValueError):
            expires_at_ms = 0
        if created_contracts is None or expires_at_ms <= int(creation["occurred_at_ms"]):
            reasons.append("intent_contract_invalid")
            summaries.append({"intent_id": intent_id, "status": "conflict"})
            continue
        if len(cancelled) > 1:
            reasons.append("intent_cancellation_conflict")
            summaries.append({"intent_id": intent_id, "status": "conflict"})
            continue
        cancel_at = int(cancelled[0]["occurred_at_ms"]) if cancelled else None
        if cancel_at is not None and cancel_at < int(creation["occurred_at_ms"]):
            reasons.append("intent_causality_conflict")
            summaries.append({"intent_id": intent_id, "status": "conflict"})
            continue
        consumed_contracts = 0
        intent_conflict = False
        for item in consumed:
            quantity = _intent_contracts(item["payload"])
            source_id = str(item.get("source_trade_event_id") or "").strip()
            occurred_at_ms = int(item["occurred_at_ms"])
            if (
                quantity is None
                or not source_id
                or source_id not in known_trade_event_ids
                or occurred_at_ms < int(creation["occurred_at_ms"])
                or occurred_at_ms > expires_at_ms
                or (cancel_at is not None and occurred_at_ms >= cancel_at)
            ):
                intent_conflict = True
                break
            consumed_contracts += quantity
        if intent_conflict or consumed_contracts > created_contracts:
            reasons.append("intent_consumption_conflict")
            summaries.append({"intent_id": intent_id, "status": "conflict"})
            continue
        remaining = created_contracts - consumed_contracts
        status = (
            "cancelled"
            if cancel_at is not None
            else "consumed"
            if remaining == 0
            else "expired"
            if as_of_ms > expires_at_ms
            else "active"
        )
        if status == "active":
            active.append(intent_id)
        summaries.append(
            {
                "intent_id": intent_id,
                "status": status,
                "created_event_id": creation["event_id"],
                "created_at_ms": int(creation["occurred_at_ms"]),
                "expires_at_ms": expires_at_ms,
                "contracts": created_contracts,
                "consumed_contracts": consumed_contracts,
                "remaining_contracts": remaining,
                "payload": dict(creation["payload"]),
            }
        )
    return active, reasons, summaries


def effective_wheel_events(
    wheel_events: Sequence[Mapping[str, Any]],
    *,
    as_of_ms: int | None = None,
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], set[str]]]:
    events_by_id: dict[str, dict[str, Any]] = {}
    invalid_by_group: dict[tuple[str, str], set[str]] = defaultdict(set)
    for raw in wheel_events:
        group = (
            str(raw.get("account") or "").strip().lower(),
            str(
                raw.get("wheel_branch_id")
                or raw.get("stock_lot_id")
                or ""
            ).strip(),
        )
        try:
            event = normalize_wheel_event(raw)
        except (TypeError, ValueError):
            if all(group):
                invalid_by_group[group].add("invalid_wheel_event")
            continue
        if as_of_ms is not None and int(event["occurred_at_ms"]) > int(as_of_ms):
            continue
        previous = events_by_id.get(event["event_id"])
        if previous is not None and previous["payload_hash"] != event["payload_hash"]:
            invalid_by_group[(event["account"], event["wheel_branch_id"])].add(
                "wheel_event_id_conflict"
            )
            invalid_by_group[(previous["account"], previous["wheel_branch_id"])].add(
                "wheel_event_id_conflict"
            )
            continue
        if previous is None or event["recorded_at_ms"] < previous["recorded_at_ms"]:
            events_by_id[event["event_id"]] = event

    voided_ids: set[str] = set()
    valid_void_ids: set[str] = set()
    for event in events_by_id.values():
        if event["event_type"] != "wheel_event_voided":
            continue
        group = (event["account"], event["wheel_branch_id"])
        target_id = str(event["payload"].get("target_wheel_event_id") or "").strip()
        target = events_by_id.get(target_id)
        if (
            target is None
            or target["event_type"] == "wheel_event_voided"
            or (target["account"], target["wheel_branch_id"]) != group
        ):
            invalid_by_group[group].add("wheel_void_target_invalid")
            continue
        voided_ids.add(target_id)
        valid_void_ids.add(event["event_id"])

    return (
        [
            event
            for event in events_by_id.values()
            if event["event_id"] not in voided_ids
            and (
                event["event_type"] != "wheel_event_voided"
                or event["event_id"] in valid_void_ids
            )
        ],
        invalid_by_group,
    )


def project_wheel_intents(
    wheel_events: Sequence[Mapping[str, Any]],
    *,
    account: str,
    wheel_branch_id: str,
    direction: str,
    as_of_ms: int,
    known_trade_event_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    account_value = _required_text(account, "account").lower()
    branch_id = _required_text(wheel_branch_id, "wheel_branch_id")
    direction_value = _required_text(direction, "direction").lower()
    if direction_value not in {"call", "put"}:
        raise ValueError("Wheel intent direction must be call or put")
    instant = _positive_int(as_of_ms, "as_of_ms")
    events, _invalid = effective_wheel_events(
        [
            event
            for event in wheel_events
            if str(event.get("account") or "").strip().lower() == account_value
            and str(
                event.get("wheel_branch_id")
                or event.get("stock_lot_id")
                or ""
            ).strip()
            == branch_id
        ],
        as_of_ms=instant,
    )
    _active, _reasons, summaries = _intent_state(
        events,
        as_of_ms=instant,
        known_trade_event_ids=set(known_trade_event_ids or ()),
        direction=direction_value,
    )
    return summaries


def project_wheel_call_intents(
    wheel_events: Sequence[Mapping[str, Any]],
    *,
    account: str,
    stock_lot_id: str,
    as_of_ms: int,
    known_trade_event_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    account_value = _required_text(account, "account").lower()
    stock_lot_value = _required_text(stock_lot_id, "stock_lot_id")
    instant = _positive_int(as_of_ms, "as_of_ms")
    events, _invalid = effective_wheel_events(
        [
            event
            for event in wheel_events
            if str(event.get("account") or "").strip().lower() == account_value
            and str(
                event.get("wheel_branch_id")
                or event.get("stock_lot_id")
                or ""
            ).strip()
            == stock_lot_value
        ],
        as_of_ms=instant,
    )
    _active, _reasons, summaries = _intent_state(
        events,
        as_of_ms=instant,
        known_trade_event_ids=set(known_trade_event_ids or ()),
    )
    return summaries


def project_wheel_call_linkage_candidates(
    wheel_batches: Sequence[Mapping[str, Any]],
    unlinked_short_call_lots: Sequence[Mapping[str, Any]],
    rejected_linkages: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    effective_linkages, _invalid = effective_wheel_events(rejected_linkages)
    rejected = {
        (
            str((event.get("payload") or {}).get("call_open_event_id") or "").strip(),
            str(event.get("stock_lot_id") or "").strip(),
        )
        for event in effective_linkages
        if str(event.get("event_type") or "").strip()
        == "wheel_call_linkage_rejected"
    }
    candidates: list[dict[str, Any]] = []
    for row in unlinked_short_call_lots:
        fields = _lot_fields(row)
        if (
            str(fields.get("option_type") or "").strip().lower() != "call"
            or str(fields.get("side") or fields.get("position_side") or "").strip().lower()
            != "short"
            or _contracts_open(fields) <= 0
            or any(
                str(fields.get(key) or "").strip()
                for key in (
                    "strategy",
                    "leg_role",
                    "strategy_group_id",
                    "source_stock_lot_id",
                )
            )
        ):
            continue
        call_record_id = _required_text(row.get("record_id"), "call_record_id")
        call_open_event_id = _required_text(
            fields.get("source_event_id"),
            "call_open_event_id",
        )
        account = str(fields.get("account") or "").strip().lower()
        symbol = str(fields.get("symbol") or "").strip().upper()
        for batch in wheel_batches:
            stock_lot_id = str(batch.get("stock_lot_id") or "").strip()
            if (
                batch.get("lifecycle_status") != "active"
                or batch.get("integrity_status") != "trusted"
                or batch.get("active_call_lot_ids")
                or str(batch.get("account") or "").strip().lower() != account
                or str(batch.get("symbol") or "").strip().upper() != symbol
                or (call_open_event_id, stock_lot_id) in rejected
            ):
                continue
            try:
                required_shares = _contracts_open(fields) * int(
                    float(fields.get("multiplier") or 0)
                )
                shares_remaining = int(batch.get("shares_remaining"))
            except (TypeError, ValueError):
                continue
            if required_shares <= 0 or shares_remaining < required_shares:
                continue
            digest = canonical_sha256(
                {
                    "call_open_event_id": call_open_event_id,
                    "stock_lot_id": stock_lot_id,
                }
            )[:24]
            stable_call = {
                key: fields.get(key)
                for key in (
                    "account",
                    "symbol",
                    "option_type",
                    "side",
                    "contracts_open",
                    "strike",
                    "expiration_ymd",
                    "expiration",
                    "multiplier",
                    "source_event_id",
                )
            }
            candidates.append(
                {
                    "linkage_candidate_id": f"wheel-call-linkage:{digest}",
                    "input_snapshot_hash": canonical_sha256(
                        {
                            "call_record_id": call_record_id,
                            "call": stable_call,
                            "stock_lot_id": stock_lot_id,
                            "batch_generation_hash": batch.get(
                                "batch_generation_hash"
                            ),
                        }
                    ),
                    "account": account,
                    "symbol": symbol,
                    "call_record_id": call_record_id,
                    "call_open_event_id": call_open_event_id,
                    "stock_lot_id": stock_lot_id,
                    "contracts": _contracts_open(fields),
                    "multiplier": int(float(fields.get("multiplier") or 0)),
                    "required_shares": required_shares,
                    "batch_generation_hash": batch.get("batch_generation_hash"),
                }
            )
    return sorted(
        candidates,
        key=lambda item: (
            str(item["account"]),
            str(item["symbol"]),
            str(item["call_record_id"]),
            str(item["stock_lot_id"]),
        ),
    )


def project_wheel_linkage_candidates(
    wheel_branches: Sequence[Mapping[str, Any]],
    unlinked_short_option_lots: Sequence[Mapping[str, Any]],
    rejected_linkages: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return deterministic Call and Put linkage choices without guessing."""

    call_branches = [
        {
            **row,
            "batch_generation_hash": row.get("batch_generation_hash")
            or row.get("branch_generation_hash"),
        }
        for row in wheel_branches
        if str(row.get("direction") or "call").strip().lower() == "call"
    ]
    call_branch_by_stock_lot = {
        str(row.get("stock_lot_id") or ""): row for row in call_branches
    }
    call_candidates = [
        {
            **item,
            "direction": "call",
            "wheel_branch_id": str(
                call_branch_by_stock_lot[item["stock_lot_id"]].get(
                    "wheel_branch_id"
                )
                or item["stock_lot_id"]
            ),
            "option_record_id": item["call_record_id"],
            "option_open_event_id": item["call_open_event_id"],
            "branch_generation_hash": item.get("batch_generation_hash"),
        }
        for item in project_wheel_call_linkage_candidates(
            call_branches,
            unlinked_short_option_lots,
            rejected_linkages,
        )
    ]
    effective_linkages, _invalid = effective_wheel_events(rejected_linkages)
    rejected = {
        (
            str(
                (event.get("payload") or {}).get("option_open_event_id")
                or (event.get("payload") or {}).get("put_open_event_id")
                or ""
            ).strip(),
            str(event.get("wheel_branch_id") or "").strip(),
        )
        for event in effective_linkages
        if str(event.get("event_type") or "").strip()
        == "wheel_put_linkage_rejected"
    }
    put_candidates: list[dict[str, Any]] = []
    for row in unlinked_short_option_lots:
        fields = _lot_fields(row)
        if (
            str(fields.get("option_type") or "").strip().lower() != "put"
            or str(
                fields.get("side") or fields.get("position_side") or ""
            ).strip().lower()
            != "short"
            or _contracts_open(fields) <= 0
            or any(
                str(fields.get(key) or "").strip()
                for key in (
                    "strategy",
                    "leg_role",
                    "strategy_group_id",
                    "source_wheel_branch_id",
                )
            )
        ):
            continue
        record_id = _required_text(row.get("record_id"), "option_record_id")
        open_event_id = _required_text(
            fields.get("source_event_id"),
            "option_open_event_id",
        )
        account = str(fields.get("account") or "").strip().lower()
        symbol = str(fields.get("symbol") or "").strip().upper()
        for branch in wheel_branches:
            branch_id = str(branch.get("wheel_branch_id") or "").strip()
            if (
                str(branch.get("direction") or "").strip().lower() != "put"
                or branch.get("lifecycle_status") != "active"
                or branch.get("integrity_status") != "trusted"
                or branch.get("active_option_lot_ids")
                or str(branch.get("account") or "").strip().lower() != account
                or str(branch.get("symbol") or "").strip().upper() != symbol
                or (open_event_id, branch_id) in rejected
            ):
                continue
            try:
                contracts = _contracts_open(fields)
                multiplier = int(float(fields.get("multiplier") or 0))
                remaining = int(branch.get("remaining_contracts") or 0)
                branch_multiplier = int(branch.get("multiplier") or 0)
            except (TypeError, ValueError):
                continue
            if (
                contracts <= 0
                or contracts > remaining
                or multiplier <= 0
                or multiplier != branch_multiplier
            ):
                continue
            digest = canonical_sha256(
                {
                    "direction": "put",
                    "option_open_event_id": open_event_id,
                    "wheel_branch_id": branch_id,
                }
            )[:24]
            generation_hash = str(branch.get("branch_generation_hash") or "")
            put_candidates.append(
                {
                    "linkage_candidate_id": f"wheel-put-linkage:{digest}",
                    "input_snapshot_hash": canonical_sha256(
                        {
                            "option_record_id": record_id,
                            "option": {
                                key: fields.get(key)
                                for key in (
                                    "account",
                                    "symbol",
                                    "option_type",
                                    "side",
                                    "contracts_open",
                                    "strike",
                                    "expiration_ymd",
                                    "expiration",
                                    "multiplier",
                                    "source_event_id",
                                )
                            },
                            "wheel_branch_id": branch_id,
                            "branch_generation_hash": generation_hash,
                        }
                    ),
                    "account": account,
                    "symbol": symbol,
                    "direction": "put",
                    "option_record_id": record_id,
                    "option_open_event_id": open_event_id,
                    "wheel_branch_id": branch_id,
                    "contracts": contracts,
                    "multiplier": multiplier,
                    "cash_reservation_amount": float(
                        fields.get("strike") or 0
                    )
                    * multiplier
                    * contracts,
                    "cash_reservation_currency": str(
                        fields.get("currency") or branch.get("currency") or ""
                    ).strip().upper(),
                    "branch_generation_hash": generation_hash,
                }
            )
    return sorted(
        [*call_candidates, *put_candidates],
        key=lambda item: (
            str(item.get("account") or ""),
            str(item.get("symbol") or ""),
            str(item.get("direction") or ""),
            str(item.get("option_record_id") or ""),
            str(item.get("wheel_branch_id") or ""),
        ),
    )


def project_wheel_lifecycles(
    wheel_events: Sequence[Mapping[str, Any]],
    trade_events: Sequence[Mapping[str, Any]],
    position_lots: Sequence[Mapping[str, Any]],
    assigned_stock_projection: Mapping[str, Any],
    as_of_ms: int,
) -> list[dict[str, Any]]:
    """Rebuild Wheel batches from immutable facts; never guesses a missing link."""

    instant = _positive_int(as_of_ms, "as_of_ms")
    effective_events, invalid_by_group = effective_wheel_events(
        wheel_events,
        as_of_ms=instant,
    )
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in effective_events:
        if event["event_schema_version"] != WHEEL_EVENT_SCHEMA_V1:
            continue
        grouped[(event["account"], event["stock_lot_id"])].append(event)

    active_trade_events = _active_trade_events(trade_events)
    trade_by_id = {
        str(row.get("event_id") or "").strip(): row
        for row in active_trade_events
        if str(row.get("event_id") or "").strip()
    }
    all_stock_rows = assigned_stock_projection.get("_all_assigned_stock_lots")
    if not isinstance(all_stock_rows, Sequence):
        all_stock_rows = assigned_stock_projection.get("assigned_stock_lots") or []
    stock_rows_by_id: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in all_stock_rows:
        if isinstance(row, Mapping) and str(row.get("stock_lot_id") or "").strip():
            stock_rows_by_id[str(row["stock_lot_id"]).strip()].append(row)
    review_rows = [
        row
        for row in assigned_stock_projection.get("assigned_stock_review_rows") or []
        if isinstance(row, Mapping)
    ]

    lots = [(str(row.get("record_id") or "").strip(), _lot_fields(row)) for row in position_lots]
    results: list[dict[str, Any]] = []
    for group in sorted(grouped):
        account, stock_lot_id = group
        batch_events = sorted(
            grouped[group],
            key=lambda item: (int(item["occurred_at_ms"]), str(item["event_id"])),
        )
        starts = [item for item in batch_events if item["event_type"] == "wheel_started"]
        if not starts:
            continue
        reasons = set(invalid_by_group.get(group, set()))
        if len(starts) != 1:
            reasons.add("wheel_start_conflict")
        start = starts[0]
        terminals = [
            item
            for item in batch_events
            if item["event_type"] in {"wheel_called_away", "wheel_manual_ended"}
        ]
        if len(terminals) > 1:
            reasons.add("wheel_terminal_conflict")

        stock_matches = stock_rows_by_id.get(stock_lot_id, [])
        stock_row = stock_matches[0] if len(stock_matches) == 1 else None
        if len(stock_matches) > 1:
            reasons.add("assigned_stock_lot_conflict")
        start_trade_id = str(start.get("source_trade_event_id") or "").strip()
        start_trade = trade_by_id.get(start_trade_id)
        if (
            not start_trade_id
            or start_trade is None
            or _event_type(start_trade) != "assignment"
            or _trade_account(start_trade) != account
            or _trade_option_type(start_trade) != "put"
            or _trade_position_side(start_trade) != "short"
        ):
            reasons.add("wheel_start_source_invalid")
        if stock_row is not None and str(stock_row.get("source_assignment_event_id") or "") != start_trade_id:
            reasons.add("wheel_start_stock_lot_mismatch")

        linked_lots: list[tuple[str, dict[str, Any]]] = []
        for record_id, fields in lots:
            if str(fields.get("account") or "").strip().lower() != account:
                continue
            if str(fields.get("source_stock_lot_id") or "").strip() != stock_lot_id:
                continue
            if (
                str(fields.get("strategy") or "").strip().lower() != "wheel"
                or str(fields.get("leg_role") or "").strip().lower() != "wheel_call"
                or str(fields.get("strategy_group_id") or "").strip()
                or str(fields.get("option_type") or "").strip().lower() != "call"
                or str(fields.get("side") or "").strip().lower() != "short"
            ):
                reasons.add("wheel_call_linkage_conflict")
                continue
            linked_lots.append((record_id, fields))
        active_call_lot_ids = sorted(
            record_id for record_id, fields in linked_lots if _contracts_open(fields) > 0
        )

        assignment_ids = {
            str(row.get("event_id") or "").strip()
            for row in active_trade_events
            if _event_type(row) == "assignment"
            and str(row.get("target_lot_id") or "").strip()
            in {record_id for record_id, _fields in linked_lots}
        }
        called_events = [item for item in terminals if item["event_type"] == "wheel_called_away"]
        manual_events = [item for item in terminals if item["event_type"] == "wheel_manual_ended"]
        if called_events:
            source_id = str(called_events[0].get("source_trade_event_id") or "").strip()
            if source_id not in assignment_ids:
                reasons.add("wheel_called_away_source_invalid")

        active_intent_ids, intent_reasons, intent_summaries = _intent_state(
            batch_events,
            as_of_ms=instant,
            known_trade_event_ids=set(trade_by_id),
        )
        reasons.update(intent_reasons)

        shares_remaining: int | None = None
        if stock_row is not None:
            try:
                shares_remaining = int(stock_row.get("shares_remaining"))
            except (TypeError, ValueError):
                reasons.add("assigned_stock_shares_unavailable")
            if shares_remaining is not None and shares_remaining < 0:
                reasons.add("assigned_stock_shares_conflict")
        multiplier = None
        if start_trade is not None:
            try:
                multiplier = int(float(start_trade.get("multiplier") or 0))
            except (TypeError, ValueError):
                multiplier = None
        if multiplier is None or multiplier <= 0:
            reasons.add("contract_multiplier_unavailable")

        locked_shares = 0
        for _record_id, fields in linked_lots:
            if _contracts_open(fields) <= 0:
                continue
            try:
                locked_shares += _contracts_open(fields) * int(float(fields.get("multiplier") or 0))
            except (TypeError, ValueError):
                reasons.add("wheel_call_multiplier_invalid")
        if shares_remaining is not None and locked_shares > shares_remaining:
            reasons.add("wheel_call_overcovers_batch")
        rejected_call_event_ids = {
            str((item.get("payload") or {}).get("call_open_event_id") or "").strip()
            for item in batch_events
            if item["event_type"] == "wheel_call_linkage_rejected"
        }
        unresolved_lots: list[tuple[str, dict[str, Any]]] = []
        for record_id, fields in lots:
            if (
                str(fields.get("account") or "").strip().lower() != account
                or str(fields.get("symbol") or "").strip().upper()
                != str((stock_row or {}).get("symbol") or _trade_symbol(start_trade or {}))
                or str(fields.get("option_type") or "").strip().lower() != "call"
                or str(fields.get("side") or "").strip().lower() != "short"
                or _contracts_open(fields) <= 0
                or any(
                    str(fields.get(key) or "").strip()
                    for key in (
                        "strategy",
                        "leg_role",
                        "strategy_group_id",
                        "source_stock_lot_id",
                    )
                )
                or str(fields.get("source_event_id") or "").strip()
                in rejected_call_event_ids
            ):
                continue
            try:
                required = _contracts_open(fields) * int(
                    float(fields.get("multiplier") or 0)
                )
            except (TypeError, ValueError):
                continue
            if shares_remaining is not None and 0 < required <= shares_remaining:
                unresolved_lots.append((record_id, fields))
        unresolved_call_lot_ids = sorted(record_id for record_id, _fields in unresolved_lots)
        if manual_events and (active_call_lot_ids or active_intent_ids):
            reasons.add("manual_end_has_active_call_or_intent")
        if called_events and shares_remaining != 0:
            reasons.add("called_away_stock_not_zero")
        if shares_remaining == 0 and assignment_ids and not called_events:
            reasons.add("called_away_event_missing")

        for review in review_rows:
            if str(review.get("stock_lot_id") or "").strip() != stock_lot_id:
                continue
            if str(review.get("status") or "") in {
                "source_conflict",
                "incomplete_inventory_basis",
                "manual_review_required",
                "missing_stock_settlement",
            }:
                reasons.add("assigned_stock_projection_conflict")

        conflict_codes = {
            reason
            for reason in reasons
            if reason.endswith("conflict")
            or reason.endswith("_invalid")
            or reason in {
                "invalid_wheel_event",
                "wheel_start_stock_lot_mismatch",
                "manual_end_has_active_call_or_intent",
                "called_away_stock_not_zero",
                "called_away_event_missing",
            }
        }
        integrity_status = "conflict" if conflict_codes else "trusted"
        terminal = terminals[0] if len(terminals) == 1 else None
        lifecycle_status = (
            "called_away"
            if terminal is not None and terminal["event_type"] == "wheel_called_away"
            else "manual_ended"
            if terminal is not None and terminal["event_type"] == "wheel_manual_ended"
            else "active"
        )
        if integrity_status == "conflict" or lifecycle_status != "active":
            phase = None
        elif unresolved_call_lot_ids:
            phase = "linkage_unresolved"
        elif active_call_lot_ids:
            phase = "call_open"
        elif active_intent_ids:
            phase = "call_pending"
        elif shares_remaining is not None and multiplier is not None and shares_remaining < multiplier:
            phase = "residual_stock"
        elif stock_row is None or shares_remaining is None or multiplier is None:
            phase = "data_unavailable"
        else:
            phase = "ready"

        related_lot_ids = {
            record_id for record_id, _fields in [*linked_lots, *unresolved_lots]
        }
        related_trade_ids = {
            start_trade_id,
            *assignment_ids,
            *{
                str(fields.get("source_event_id") or "").strip()
                for _record_id, fields in linked_lots
            },
            *{
                str(item.get("source_trade_event_id") or "").strip()
                for item in batch_events
            },
        }
        related_trades = [
            row
            for row in active_trade_events
            if str(row.get("event_id") or "").strip() in related_trade_ids
            or str(row.get("target_lot_id") or "").strip() in related_lot_ids
        ]
        generation_payload = {
            "schema_version": WHEEL_PROJECTION_SCHEMA,
            "account": account,
            "stock_lot_id": stock_lot_id,
            "wheel_events": [
                {
                    key: event.get(key)
                    for key in (
                        "event_id",
                        "account",
                        "stock_lot_id",
                        "event_type",
                        "occurred_at_ms",
                        "intent_id",
                        "source_trade_event_id",
                        "payload",
                        "payload_hash",
                    )
                }
                for event in batch_events
            ],
            "position_lots": [
                {"record_id": record_id, "fields": fields}
                for record_id, fields in [*linked_lots, *unresolved_lots]
            ],
            "trade_events": related_trades,
            "assigned_stock": _stable_stock_fact(stock_row),
        }
        batch_generation_hash = canonical_sha256(generation_payload)
        symbol = str(
            (stock_row or {}).get("symbol") or _trade_symbol(start_trade or {})
        ).strip().upper()
        result = {
            "account": account,
            "market": str(symbol_market(symbol) or "").strip().lower() or None,
            "symbol": symbol,
            "stock_lot_id": stock_lot_id,
            "lifecycle_status": lifecycle_status,
            "phase": phase,
            "integrity_status": integrity_status,
            "reason_codes": sorted(reasons),
            "shares_remaining": shares_remaining,
            "batch_generation_hash": batch_generation_hash,
            "start_event_id": start["event_id"],
            "terminal_event_id": terminal["event_id"] if terminal is not None else None,
            "active_call_lot_ids": active_call_lot_ids,
            "unresolved_call_lot_ids": unresolved_call_lot_ids,
            "active_intent_ids": active_intent_ids,
            "active_intent_reserved_shares": sum(
                int(item.get("remaining_contracts") or 0)
                * int((item.get("payload") or {}).get("multiplier") or 0)
                for item in intent_summaries
                if item.get("status") == "active"
            ),
            "candidate": None,
        }
        result["projection_hash"] = canonical_sha256(
            {
                "schema_version": WHEEL_PROJECTION_SCHEMA,
                "batch_generation_hash": batch_generation_hash,
                "as_of_ms": instant,
                "derived": result,
            }
        )
        results.append(result)
    return results


def project_wheel_branches(
    wheel_events: Sequence[Mapping[str, Any]],
    trade_events: Sequence[Mapping[str, Any]],
    position_lots: Sequence[Mapping[str, Any]],
    assigned_stock_projection: Mapping[str, Any],
    as_of_ms: int,
    *,
    monitoring_gate: str = "disabled",
) -> list[dict[str, Any]]:
    """Project v2 branches while adapting legacy Call batches unchanged."""

    instant = _positive_int(as_of_ms, "as_of_ms")
    gate = str(monitoring_gate or "disabled").strip().lower()
    if gate not in {"enabled", "disabled", "config_mismatch"}:
        raise ValueError("invalid Wheel monitoring gate")
    legacy_batches = project_wheel_lifecycles(
        wheel_events,
        trade_events,
        position_lots,
        assigned_stock_projection,
        instant,
    )
    branches: list[dict[str, Any]] = []
    for batch in legacy_batches:
        phase = {
            "call_open": "option_open",
            "call_pending": "intent_pending",
            "residual_stock": "residual_capacity",
        }.get(batch.get("phase"), batch.get("phase"))
        lifecycle_status = (
            "converted"
            if batch.get("lifecycle_status") == "called_away"
            else batch.get("lifecycle_status")
        )
        branches.append(
            {
                **batch,
                "wheel_branch_id": batch["stock_lot_id"],
                "parent_branch_id": None,
                "direction": "call",
                "lifecycle_status": lifecycle_status,
                "phase": phase,
                "monitoring_gate": gate,
                "branch_generation_hash": batch["batch_generation_hash"],
                "legacy_call_adapter": True,
            }
        )

    effective_events, invalid_by_group = effective_wheel_events(
        wheel_events,
        as_of_ms=instant,
    )
    v2_events = [
        event
        for event in effective_events
        if event["event_schema_version"] == WHEEL_EVENT_SCHEMA_V2
    ]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in v2_events:
        grouped[(event["account"], event["wheel_branch_id"])].append(event)
    created_events = [
        event for event in v2_events if event["event_type"] == "wheel_branch_created"
    ]
    children_by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in created_events:
        parent = str(event["payload"].get("parent_branch_id") or "").strip()
        if parent:
            children_by_parent[parent].append(event)

    active_trade_events = _active_trade_events(trade_events)
    trade_ids = {
        str(item.get("event_id") or "").strip()
        for item in active_trade_events
        if str(item.get("event_id") or "").strip()
    }
    allocations_by_lot: dict[str, list[Any]] = defaultdict(list)
    allocation_projection_available = True
    try:
        economic_projection = project_trade_events(
            [TradeEvent.from_dict(dict(item)) for item in trade_events]
        )
    except (TypeError, ValueError, OverflowError):
        allocation_projection_available = False
    else:
        for allocation in economic_projection.allocations:
            allocations_by_lot[allocation.target_lot_id].append(allocation)
    stock_rows = assigned_stock_projection.get("_all_assigned_stock_lots")
    if not isinstance(stock_rows, Sequence):
        stock_rows = assigned_stock_projection.get("assigned_stock_lots") or []
    stock_by_id = {
        str(item.get("stock_lot_id") or "").strip(): item
        for item in stock_rows
        if isinstance(item, Mapping) and str(item.get("stock_lot_id") or "").strip()
    }
    lots = [
        (str(item.get("record_id") or "").strip(), _lot_fields(item))
        for item in position_lots
        if isinstance(item, Mapping)
    ]
    known_legacy_ids = {
        str(item.get("wheel_branch_id") or "") for item in branches
    }
    for group in sorted(grouped):
        account, branch_id = group
        if branch_id in known_legacy_ids:
            continue
        events = sorted(
            grouped[group],
            key=lambda item: (int(item["occurred_at_ms"]), str(item["event_id"])),
        )
        creations = [
            item for item in events if item["event_type"] == "wheel_branch_created"
        ]
        if not creations:
            continue
        reasons = set(invalid_by_group.get(group, set()))
        if len(creations) != 1:
            reasons.add("wheel_branch_creation_conflict")
        created = creations[0]
        payload = created["payload"]
        direction = str(payload.get("direction") or "").strip().lower()
        if direction not in {"call", "put"}:
            reasons.add("wheel_branch_direction_invalid")
        symbol = str(payload.get("symbol") or "").strip().upper()
        try:
            market = _wheel_market(symbol)
        except ValueError:
            market = ""
            reasons.add("wheel_branch_market_invalid")
        if str(payload.get("market") or "").strip().lower() not in {"", market}:
            reasons.add("wheel_branch_market_mismatch")
        if any(
            str((item.get("payload") or {}).get("market") or "").strip().lower()
            not in {"", market}
            for item in events
        ):
            reasons.add("wheel_event_market_mismatch")
        source_assignment_event_id = str(
            payload.get("source_assignment_event_id")
            or created.get("source_trade_event_id")
            or ""
        ).strip()
        if not source_assignment_event_id or source_assignment_event_id not in trade_ids:
            reasons.add("wheel_branch_source_invalid")
        try:
            initial_contracts = _positive_int(payload.get("contracts"), "contracts")
        except ValueError:
            initial_contracts = 0
            reasons.add("wheel_branch_quantity_invalid")
        try:
            multiplier = (
                _positive_int(payload.get("multiplier"), "multiplier")
                if payload.get("multiplier") is not None
                else 0
            )
        except ValueError:
            multiplier = 0
            reasons.add("wheel_branch_quantity_invalid")
        multiplier_source = str(payload.get("multiplier_source") or "").strip()
        if not multiplier_source or not str(
            payload.get("multiplier_evidence_hash") or ""
        ).strip():
            reasons.add("multiplier_unproven")
        elif multiplier_source == "unproven":
            reasons.add("multiplier_unproven")
        elif multiplier_source == "conflict":
            reasons.add("multiplier_conflict")
        if payload.get("principal_anchor") in (None, ""):
            reasons.add(
                str(payload.get("principal_anchor_reason") or "principal_anchor_unavailable")
            )
        if not str(payload.get("currency") or "").strip():
            reasons.add("assignment_currency_unavailable")
        child_events = children_by_parent.get(branch_id, [])
        converted_contracts = 0
        for child in child_events:
            try:
                converted_contracts += _positive_int(
                    child["payload"].get("contracts"),
                    "contracts",
                )
            except ValueError:
                reasons.add("wheel_child_quantity_invalid")
            child_source = str(
                child["payload"].get("source_assignment_event_id")
                or child.get("source_trade_event_id")
                or ""
            ).strip()
            if not child_source or child_source not in trade_ids:
                reasons.add("wheel_child_source_conflict")
        remaining_contracts = initial_contracts - converted_contracts
        if remaining_contracts < 0:
            reasons.add("wheel_branch_conversion_conflict")

        decisions = [
            item for item in events if item["event_type"] == "wheel_branch_decided"
        ]
        manual_ends = [
            item for item in events if item["event_type"] == "wheel_manual_ended"
        ]
        decision_values = {
            str(item["payload"].get("decision") or "").strip().lower()
            for item in decisions
        }
        if len(decisions) > 1 or len(decision_values) > 1 or (
            decisions and manual_ends
        ) or len(manual_ends) > 1:
            reasons.add("wheel_branch_decision_conflict")
        initial_status = str(
            payload.get("initial_lifecycle_status") or "active"
        ).strip().lower()
        lifecycle_status = initial_status
        terminal_event_id = None
        if manual_ends:
            lifecycle_status = "manual_ended"
            terminal_event_id = manual_ends[0]["event_id"]
        elif decisions:
            decision = next(iter(decision_values), "")
            terminal_event_id = decisions[0]["event_id"] if decision == "end" else None
            if initial_status != "pending_decision":
                reasons.add("wheel_branch_decision_conflict")
            elif decision == "start":
                lifecycle_status = "active"
            elif decision == "end":
                lifecycle_status = "manual_ended"
        if lifecycle_status == "active" and initial_contracts > 0 and remaining_contracts == 0:
            lifecycle_status = "converted"

        linked_lots = [
            (record_id, fields)
            for record_id, fields in lots
            if str(fields.get("account") or "").strip().lower() == account
            and str(fields.get("source_wheel_branch_id") or "").strip() == branch_id
        ]
        realized_put_net_pnl: float | None = None
        if direction == "put":
            realized_net = Decimal(0)
            realized_net_available = allocation_projection_available
            for record_id, fields in linked_lots:
                try:
                    closed_contracts = int(fields.get("contracts_closed") or 0)
                except (TypeError, ValueError):
                    realized_net_available = False
                    break
                allocations = allocations_by_lot.get(record_id, [])
                if closed_contracts < 0 or sum(item.contracts for item in allocations) != closed_contracts:
                    realized_net_available = False
                    break
                if any(item.realized_pnl_net is None for item in allocations):
                    realized_net_available = False
                    break
                realized_net += sum(
                    (item.realized_pnl_net for item in allocations),
                    Decimal(0),
                )
            if realized_net_available:
                realized_put_net_pnl = float(realized_net)
            else:
                reasons.add("realized_put_net_pnl_unavailable")
        active_lot_ids = sorted(
            record_id
            for record_id, fields in linked_lots
            if _contracts_open(fields) > 0
        )
        expected_role = f"wheel_{direction}"
        for _record_id, fields in linked_lots:
            if (
                str(fields.get("strategy") or "").strip().lower() != "wheel"
                or str(fields.get("leg_role") or "").strip().lower() != expected_role
                or str(fields.get("strategy_group_id") or "").strip()
            ):
                reasons.add("wheel_option_linkage_conflict")
        active_intent_ids, intent_reasons, intent_summaries = _intent_state(
            events,
            as_of_ms=instant,
            known_trade_event_ids=trade_ids,
            direction=direction if direction in {"call", "put"} else "call",
        )
        reasons.update(intent_reasons)
        active_intent_reservations: list[dict[str, Any]] = []
        if direction == "put":
            for summary in intent_summaries:
                if summary.get("status") != "active":
                    continue
                intent_payload = summary.get("payload")
                intent_payload = (
                    intent_payload if isinstance(intent_payload, Mapping) else {}
                )
                strike = _finite_float(intent_payload.get("strike"))
                intent_multiplier = _finite_float(intent_payload.get("multiplier"))
                currency = str(
                    intent_payload.get("cash_reservation_currency") or ""
                ).strip().upper()
                capacity_hash = str(
                    intent_payload.get("capacity_identity_hash") or ""
                ).strip()
                remaining = int(summary.get("remaining_contracts") or 0)
                if (
                    strike is None
                    or strike <= 0
                    or intent_multiplier is None
                    or intent_multiplier <= 0
                    or not intent_multiplier.is_integer()
                    or remaining <= 0
                    or not currency
                    or not capacity_hash
                ):
                    reasons.add("intent_cash_reservation_invalid")
                    continue
                active_intent_reservations.append(
                    {
                        "account": account,
                        "wheel_branch_id": branch_id,
                        "intent_id": summary["intent_id"],
                        "branch_generation_hash": intent_payload.get(
                            "branch_generation_hash"
                        ),
                        "capacity_identity_hash": capacity_hash,
                        "currency": currency,
                        "cash_reservation_amount": round(
                            strike * int(intent_multiplier) * remaining,
                            6,
                        ),
                        "remaining_contracts": remaining,
                    }
                )
        stock_lot_id = str(created.get("stock_lot_id") or "").strip() or None
        stock_row = stock_by_id.get(stock_lot_id or "")
        if direction == "call" and stock_row is None:
            reasons.add("assigned_stock_lot_unavailable")

        conflict = any(
            reason.endswith("conflict")
            or reason.endswith("_invalid")
            or reason == "invalid_wheel_event"
            for reason in reasons
        )
        integrity_status = "conflict" if conflict else "trusted"
        if lifecycle_status in {"converted", "manual_ended"}:
            phase = lifecycle_status
        elif lifecycle_status == "pending_decision":
            phase = "pending_decision"
        elif conflict:
            phase = "conflict"
        elif active_lot_ids:
            phase = "option_open"
        elif active_intent_ids:
            phase = "intent_pending"
        elif remaining_contracts <= 0 or (
            direction == "call"
            and stock_row is not None
            and int(stock_row.get("shares_remaining") or 0) < multiplier
        ):
            phase = "residual_capacity"
        elif reasons:
            phase = "data_unavailable"
        else:
            phase = "ready"

        generation_payload = {
            "schema_version": WHEEL_PROJECTION_SCHEMA,
            "account": account,
            "wheel_branch_id": branch_id,
            "events": events,
            "children": sorted(
                (
                    {
                        "event_id": item["event_id"],
                        "payload_hash": item["payload_hash"],
                    }
                    for item in child_events
                ),
                key=lambda item: item["event_id"],
            ),
            "position_lots": [
                {"record_id": record_id, "fields": fields}
                for record_id, fields in linked_lots
            ],
            "assigned_stock": _stable_stock_fact(stock_row),
            "realized_put_net_pnl_in_current_stage": realized_put_net_pnl,
        }
        branch_generation_hash = canonical_sha256(generation_payload)
        branch = {
            "account": account,
            "market": market or None,
            "symbol": symbol,
            "wheel_branch_id": branch_id,
            "parent_branch_id": str(payload.get("parent_branch_id") or "").strip() or None,
            "direction": direction,
            "stock_lot_id": stock_lot_id,
            "source_assignment_event_id": source_assignment_event_id,
            "lifecycle_status": lifecycle_status,
            "phase": phase,
            "monitoring_gate": gate,
            "integrity_status": integrity_status,
            "reason_codes": sorted(reasons),
            "initial_contracts": initial_contracts,
            "converted_contracts": converted_contracts,
            "remaining_contracts": max(0, remaining_contracts),
            "multiplier": multiplier or None,
            "shares_opened": (
                int(stock_row.get("shares_opened") or 0) if stock_row is not None else None
            ),
            "shares_remaining": (
                int(stock_row.get("shares_remaining") or 0)
                if stock_row is not None
                else None
            ),
            "principal_anchor": payload.get("principal_anchor"),
            "realized_put_net_pnl_in_current_stage": realized_put_net_pnl,
            "currency": payload.get("currency"),
            "activation_window": payload.get("activation_window"),
            "start_event_id": created["event_id"],
            "terminal_event_id": terminal_event_id,
            "active_option_lot_ids": active_lot_ids,
            "active_intent_ids": active_intent_ids,
            "active_intent_reserved_contracts": sum(
                int(item.get("remaining_contracts") or 0)
                for item in intent_summaries
                if item.get("status") == "active"
            ),
            "active_intent_reservations": active_intent_reservations,
            "branch_generation_hash": branch_generation_hash,
            "legacy_call_adapter": False,
            "candidate": None,
        }
        branch["projection_hash"] = canonical_sha256(
            {
                "schema_version": WHEEL_PROJECTION_SCHEMA,
                "branch_generation_hash": branch_generation_hash,
                "as_of_ms": instant,
                "derived": branch,
            }
        )
        branches.append(branch)
    return sorted(
        branches,
        key=lambda item: (
            str(item.get("account") or ""),
            str(item.get("wheel_branch_id") or ""),
        ),
    )


__all__ = [
    "WHEEL_EVENT_SCHEMA",
    "WHEEL_EVENT_SCHEMA_V1",
    "WHEEL_EVENT_SCHEMA_V2",
    "WHEEL_EVENT_TYPES",
    "WHEEL_EVENT_TYPES_V1",
    "WHEEL_PROJECTION_SCHEMA",
    "build_wheel_intent_capacity_binding",
    "build_wheel_call_rank_key",
    "build_wheel_put_rank_key",
    "build_wheel_event",
    "build_wheel_branch_created_event",
    "deterministic_wheel_branch_id",
    "effective_wheel_events",
    "evaluate_wheel_call_candidate",
    "evaluate_wheel_put_candidate",
    "normalize_wheel_event",
    "plan_wheel_call_intent_cancel",
    "plan_wheel_call_intent_consume",
    "plan_wheel_call_intent_create",
    "plan_wheel_put_intent_cancel",
    "plan_wheel_put_intent_consume",
    "plan_wheel_put_intent_create",
    "plan_wheel_manual_end",
    "plan_wheel_branch_decision",
    "project_wheel_call_linkage_candidates",
    "project_wheel_call_intents",
    "project_wheel_intents",
    "project_wheel_linkage_candidates",
    "project_wheel_lifecycles",
    "project_wheel_branches",
    "wheel_called_away_event_from_call_assignment",
    "wheel_event_payload_hash",
    "wheel_started_event_from_assignment",
]
