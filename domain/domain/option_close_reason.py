from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any


CLOSE_REASON_STATUSES = frozenset(
    {
        "not_started",
        "cause_pending",
        "partially_resolved",
        "resolved",
        "needs_review",
        "conflict",
    }
)
CLOSE_REASONS = frozenset(
    {
        "trade_close",
        "assignment",
        "exercise",
        "expiration_no_settlement",
        "cash_settlement",
    }
)
STOCK_MATCH_STATUSES = frozenset({"none", "partial", "full", "conflict"})


@dataclass(frozen=True)
class CloseReasonTarget:
    account: str
    futu_account_id: str
    position_side: str
    option_type: str
    expiration_ymd: str
    target_contracts_by_lot: dict[str, int]
    frozen_preterminal_remaining_by_lot: dict[str, int]
    reservation_exclusive: bool = True
    competing_effective_consumption: bool = False


@dataclass(frozen=True)
class EffectiveLifecycleTiming:
    pairing_until_ms: int
    settlement_deadline_ms: int
    last_trade_cutoff_ms: int
    settlement_style: str


# Compatibility alias for callers written against the pre-freeze draft.
LifecycleTimingPolicy = EffectiveLifecycleTiming


@dataclass(frozen=True)
class CloseReasonEvidenceBundle:
    evidence_ids: tuple[str, ...] = ()
    option_close_present: bool = False
    option_close_price: Any = None
    option_execution_time_ms: int | None = None
    option_execution_local_ymd: str | None = None
    exact_normal_order: bool = False
    exact_normal_close_deal: bool = False
    stock_match_status: str = "none"
    stock_contracts: int = 0
    proposed_allocations: tuple[dict[str, Any], ...] = ()
    mutually_exclusive_terminal_facts: bool = False
    duplicate_source_consumption: bool = False
    over_allocation: bool = False
    projection_drift: bool = False
    observation_complete: bool = False
    broker_option_position_absent: bool = False
    projection_matches_frozen_remaining: bool = False
    no_stock_settlement: bool = False
    no_normal_order: bool = False


@dataclass(frozen=True)
class CloseReasonDecision:
    status: str
    close_reason: str | None = None
    contracts_resolved: int = 0
    proposed_allocations: tuple[dict[str, Any], ...] = ()
    evidence_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    public_transition: str | None = None


def _decision(
    *,
    status: str,
    evidence: CloseReasonEvidenceBundle,
    close_reason: str | None = None,
    contracts_resolved: int = 0,
    proposed_allocations: tuple[dict[str, Any], ...] = (),
    reason_codes: tuple[str, ...] = (),
    public_transition: str | None = None,
) -> CloseReasonDecision:
    if status not in CLOSE_REASON_STATUSES:
        raise ValueError(f"unsupported close reason status: {status}")
    if close_reason is not None and close_reason not in CLOSE_REASONS:
        raise ValueError(f"unsupported close reason: {close_reason}")
    return CloseReasonDecision(
        status=status,
        close_reason=close_reason,
        contracts_resolved=max(0, int(contracts_resolved)),
        proposed_allocations=tuple(dict(item) for item in proposed_allocations),
        evidence_ids=tuple(
            sorted({str(item).strip() for item in evidence.evidence_ids if str(item).strip()})
        ),
        reason_codes=tuple(sorted({str(item).strip() for item in reason_codes if str(item).strip()})),
        public_transition=public_transition,
    )


def _positive_target_quantity(values: dict[str, Any]) -> int | None:
    if not isinstance(values, dict) or not values:
        return None
    total = 0
    for raw_lot_id, raw_quantity in values.items():
        if not str(raw_lot_id or "").strip() or isinstance(raw_quantity, bool):
            return None
        try:
            numeric = Decimal(str(raw_quantity))
            quantity = int(numeric)
        except (InvalidOperation, TypeError, ValueError, OverflowError):
            return None
        if not numeric.is_finite() or numeric != quantity or quantity <= 0:
            return None
        total += quantity
    return total if total > 0 else None


def _nonnegative_remaining(
    values: dict[str, Any],
    *,
    target: dict[str, Any],
) -> int | None:
    if not isinstance(values, dict) or set(values) != set(target):
        return None
    total = 0
    for lot_id, raw_quantity in values.items():
        if isinstance(raw_quantity, bool):
            return None
        try:
            numeric = Decimal(str(raw_quantity))
            quantity = int(numeric)
            target_quantity = int(Decimal(str(target[lot_id])))
        except (InvalidOperation, TypeError, ValueError, OverflowError):
            return None
        if (
            not numeric.is_finite()
            or numeric != quantity
            or quantity < 0
            or quantity > target_quantity
        ):
            return None
        total += quantity
    return total


def _price(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _target_validation_reason(target: CloseReasonTarget) -> str | None:
    if (
        not str(target.account or "").strip()
        or not str(target.futu_account_id or "").strip()
        or str(target.position_side or "").strip().lower() not in {"short", "long"}
        or str(target.option_type or "").strip().lower() not in {"put", "call"}
        or not str(target.expiration_ymd or "").strip()
    ):
        return "target_identity_invalid"
    target_quantity = _positive_target_quantity(target.target_contracts_by_lot)
    remaining_quantity = _nonnegative_remaining(
        target.frozen_preterminal_remaining_by_lot,
        target=target.target_contracts_by_lot,
    )
    if target_quantity is None or remaining_quantity is None or remaining_quantity <= 0:
        return "target_quantity_invalid"
    return None


def _stock_reason(position_side: str) -> str | None:
    side = str(position_side or "").strip().lower()
    if side == "short":
        return "assignment"
    if side == "long":
        return "exercise"
    return None


def resolve_close_reason(
    target: CloseReasonTarget,
    evidence: CloseReasonEvidenceBundle,
    timing: EffectiveLifecycleTiming | None,
    now_ms: int,
) -> CloseReasonDecision:
    """Resolve an option close reason from already-normalized immutable facts."""

    target_reason = _target_validation_reason(target)
    if target_reason:
        return _decision(
            status="needs_review",
            evidence=evidence,
            reason_codes=(target_reason,),
            public_transition="needs_review",
        )

    stock_match_status = str(evidence.stock_match_status or "").strip().lower()
    if stock_match_status not in STOCK_MATCH_STATUSES:
        return _decision(
            status="needs_review",
            evidence=evidence,
            reason_codes=("stock_match_status_invalid",),
            public_transition="needs_review",
        )
    target_contracts = sum(
        int(value)
        for value in target.frozen_preterminal_remaining_by_lot.values()
    )
    stock_contracts = int(evidence.stock_contracts)
    if stock_contracts < 0 or stock_contracts > target_contracts:
        return _decision(
            status="conflict",
            evidence=evidence,
            reason_codes=("stock_settlement_quantity_conflict",),
            public_transition="conflict",
        )
    derived_stock_match_status = (
        "none"
        if stock_contracts == 0
        else "full"
        if stock_contracts == target_contracts
        else "partial"
    )
    if stock_match_status != derived_stock_match_status:
        return _decision(
            status="conflict",
            evidence=evidence,
            reason_codes=(
                "stock_match_status_quantity_mismatch",
            ),
            public_transition="conflict",
        )
    stock_match_status = derived_stock_match_status

    conflict_codes: list[str] = []
    for matched, code in (
        (evidence.mutually_exclusive_terminal_facts, "mutually_exclusive_terminal_facts"),
        (evidence.duplicate_source_consumption, "duplicate_source_consumption"),
        (evidence.over_allocation, "allocation_exceeds_target"),
        (evidence.projection_drift, "target_projection_drift"),
        (stock_match_status == "conflict", "stock_settlement_match_conflict"),
    ):
        if matched:
            conflict_codes.append(code)
    if conflict_codes:
        return _decision(
            status="conflict",
            evidence=evidence,
            reason_codes=tuple(conflict_codes),
            public_transition="conflict",
        )

    if not evidence.option_close_present and not evidence.exact_normal_close_deal:
        return _decision(status="not_started", evidence=evidence)

    price = _price(evidence.option_close_price)
    if (
        evidence.exact_normal_order
        and evidence.exact_normal_close_deal
        and price is not None
        and price > 0
    ):
        execution_ymd = str(evidence.option_execution_local_ymd or "").strip()
        expiration_ymd = str(target.expiration_ymd or "").strip()
        if execution_ymd and execution_ymd < expiration_ymd:
            return _decision(
                status="resolved",
                evidence=evidence,
                close_reason="trade_close",
                contracts_resolved=sum(target.frozen_preterminal_remaining_by_lot.values()),
                proposed_allocations=evidence.proposed_allocations,
                public_transition="resolution_confirmed",
            )
        if timing is None:
            return _decision(
                status="needs_review",
                evidence=evidence,
                reason_codes=("last_trade_cutoff_unavailable",),
                public_transition="needs_review",
            )
        execution_time_ms = evidence.option_execution_time_ms
        if execution_time_ms is None:
            return _decision(
                status="needs_review",
                evidence=evidence,
                reason_codes=("option_execution_time_missing",),
                public_transition="needs_review",
            )
        if int(execution_time_ms) <= int(timing.last_trade_cutoff_ms):
            return _decision(
                status="resolved",
                evidence=evidence,
                close_reason="trade_close",
                contracts_resolved=sum(target.frozen_preterminal_remaining_by_lot.values()),
                proposed_allocations=evidence.proposed_allocations,
                public_transition="resolution_confirmed",
            )
        return _decision(
            status="conflict",
            evidence=evidence,
            reason_codes=("nonzero_close_after_last_trade_cutoff",),
            public_transition="conflict",
        )

    if price is None or price < 0:
        return _decision(
            status="needs_review",
            evidence=evidence,
            reason_codes=("option_close_price_invalid",),
            public_transition="needs_review",
        )
    if price > 0:
        return _decision(
            status="needs_review",
            evidence=evidence,
            reason_codes=("nonzero_close_order_evidence_missing",),
            public_transition="needs_review",
        )

    settlement_style = (
        str(timing.settlement_style or "").strip().lower()
        if timing is not None
        else ""
    )
    if settlement_style == "cash":
        return _decision(
            status="needs_review",
            evidence=evidence,
            close_reason="cash_settlement",
            reason_codes=("cash_settlement_unsupported_v1",),
            public_transition="needs_review",
        )

    if stock_match_status in {"partial", "full"}:
        close_reason = _stock_reason(target.position_side)
        if close_reason is None:
            return _decision(
                status="needs_review",
                evidence=evidence,
                reason_codes=("position_side_missing",),
                public_transition="needs_review",
            )
        status = "resolved" if stock_match_status == "full" else "partially_resolved"
        return _decision(
            status=status,
            evidence=evidence,
            close_reason=close_reason,
            contracts_resolved=stock_contracts,
            proposed_allocations=evidence.proposed_allocations,
            public_transition=(
                "resolution_confirmed" if status == "resolved" else "option_leg_closed"
            ),
        )

    if timing is None:
        return _decision(
            status="needs_review",
            evidence=evidence,
            reason_codes=("lifecycle_timing_policy_unavailable",),
            public_transition="needs_review",
        )
    if settlement_style != "physical":
        return _decision(
            status="needs_review",
            evidence=evidence,
            reason_codes=("physical_settlement_not_proven",),
            public_transition="needs_review",
        )
    if int(now_ms) < int(timing.pairing_until_ms):
        return _decision(
            status="cause_pending",
            evidence=evidence,
            reason_codes=("awaiting_out_of_order_pair",),
        )
    if int(now_ms) < int(timing.settlement_deadline_ms):
        return _decision(
            status="cause_pending",
            evidence=evidence,
            reason_codes=("awaiting_settlement_evidence",),
            public_transition="option_leg_closed",
        )
    if (
        evidence.observation_complete
        and evidence.broker_option_position_absent
        and evidence.projection_matches_frozen_remaining
        and target.reservation_exclusive
        and not target.competing_effective_consumption
        and evidence.no_stock_settlement
        and evidence.no_normal_order
    ):
        return _decision(
            status="resolved",
            evidence=evidence,
            close_reason="expiration_no_settlement",
            contracts_resolved=sum(target.frozen_preterminal_remaining_by_lot.values()),
            proposed_allocations=evidence.proposed_allocations,
            public_transition="resolution_confirmed",
        )
    return _decision(
        status="needs_review",
        evidence=evidence,
        reason_codes=("settlement_observation_incomplete",),
        public_transition="needs_review",
    )


__all__ = [
    "CLOSE_REASONS",
    "CLOSE_REASON_STATUSES",
    "CloseReasonDecision",
    "CloseReasonEvidenceBundle",
    "CloseReasonTarget",
    "EffectiveLifecycleTiming",
    "LifecycleTimingPolicy",
    "resolve_close_reason",
]
