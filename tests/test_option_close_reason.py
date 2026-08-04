from __future__ import annotations

import pytest

from domain.domain.lifecycle_allocation import (
    allocation_id_for,
    terminal_event_id_for,
)
from domain.domain.option_close_reason import (
    CloseReasonEvidenceBundle,
    CloseReasonTarget,
    LifecycleTimingPolicy,
    resolve_close_reason,
)
from domain.domain.option_lifecycle import (
    expiration_observation_start_ms,
    derive_lifecycle_read_model,
)


def _target(**overrides) -> CloseReasonTarget:
    values = {
        "account": "lx",
        "futu_account_id": "REAL_1",
        "position_side": "short",
        "option_type": "put",
        "expiration_ymd": "2026-08-21",
        "target_contracts_by_lot": {"lot-1": 2},
        "frozen_preterminal_remaining_by_lot": {"lot-1": 2},
        "reservation_exclusive": True,
        "competing_effective_consumption": False,
    }
    values.update(overrides)
    return CloseReasonTarget(**values)


def _timing(**overrides) -> LifecycleTimingPolicy:
    values = {
        "pairing_until_ms": 200,
        "settlement_deadline_ms": 300,
        "last_trade_cutoff_ms": 150,
        "settlement_style": "physical",
    }
    values.update(overrides)
    return LifecycleTimingPolicy(**values)


def _zero_evidence(**overrides) -> CloseReasonEvidenceBundle:
    values = {
        "evidence_ids": ("zero-1",),
        "option_close_present": True,
        "option_close_price": 0,
        "option_execution_time_ms": 100,
        "option_execution_local_ymd": "2026-08-21",
        "stock_match_status": "none",
    }
    values.update(overrides)
    return CloseReasonEvidenceBundle(**values)


def test_close_reason_requires_complete_target_identity() -> None:
    decision = resolve_close_reason(
        _target(futu_account_id=""),
        _zero_evidence(),
        _timing(),
        100,
    )

    assert decision.status == "needs_review"
    assert decision.reason_codes == ("target_identity_invalid",)


def test_close_reason_not_started_without_close_evidence() -> None:
    decision = resolve_close_reason(
        _target(),
        CloseReasonEvidenceBundle(),
        _timing(),
        100,
    )

    assert decision.status == "not_started"
    assert decision.public_transition is None


def test_exact_nonzero_close_before_expiration_is_trade_close() -> None:
    evidence = CloseReasonEvidenceBundle(
        evidence_ids=("deal-1", "order-1"),
        option_close_present=True,
        option_close_price="1.25",
        option_execution_time_ms=100,
        option_execution_local_ymd="2026-08-20",
        exact_normal_order=True,
        exact_normal_close_deal=True,
    )

    decision = resolve_close_reason(_target(), evidence, None, 100)

    assert decision.status == "resolved"
    assert decision.close_reason == "trade_close"
    assert decision.contracts_resolved == 2


def test_nonzero_close_after_last_trade_cutoff_is_conflict() -> None:
    evidence = CloseReasonEvidenceBundle(
        evidence_ids=("deal-1", "order-1"),
        option_close_present=True,
        option_close_price=1,
        option_execution_time_ms=151,
        option_execution_local_ymd="2026-08-21",
        exact_normal_order=True,
        exact_normal_close_deal=True,
    )

    decision = resolve_close_reason(_target(), evidence, _timing(), 151)

    assert decision.status == "conflict"
    assert decision.reason_codes == ("nonzero_close_after_last_trade_cutoff",)


@pytest.mark.parametrize(
    ("position_side", "stock_status", "expected_status", "expected_reason"),
    (
        ("short", "partial", "partially_resolved", "assignment"),
        ("short", "full", "resolved", "assignment"),
        ("long", "partial", "partially_resolved", "exercise"),
        ("long", "full", "resolved", "exercise"),
    ),
)
def test_zero_close_stock_match_maps_side_to_assignment_or_exercise(
    position_side: str,
    stock_status: str,
    expected_status: str,
    expected_reason: str,
) -> None:
    decision = resolve_close_reason(
        _target(position_side=position_side),
        _zero_evidence(
            stock_match_status=stock_status,
            stock_contracts=(
                2 if stock_status == "full" else 1
            ),
        ),
        _timing(),
        100,
    )

    assert decision.status == expected_status
    assert decision.close_reason == expected_reason


def test_stock_match_status_cannot_self_attest_full_quantity() -> None:
    decision = resolve_close_reason(
        _target(position_side="short"),
        _zero_evidence(
            stock_match_status="full",
            stock_contracts=1,
        ),
        _timing(),
        100,
    )

    assert decision.status == "conflict"
    assert decision.reason_codes == (
        "stock_match_status_quantity_mismatch",
    )


def test_cash_settlement_is_review_only() -> None:
    decision = resolve_close_reason(
        _target(),
        _zero_evidence(),
        _timing(settlement_style="cash"),
        100,
    )

    assert decision.status == "needs_review"
    assert decision.close_reason == "cash_settlement"
    assert decision.reason_codes == ("cash_settlement_unsupported_v1",)


def test_zero_close_waits_silently_until_pairing_then_emits_interim_transition() -> None:
    before = resolve_close_reason(_target(), _zero_evidence(), _timing(), 199)
    after = resolve_close_reason(_target(), _zero_evidence(), _timing(), 200)

    assert before.status == "cause_pending"
    assert before.public_transition is None
    assert after.status == "cause_pending"
    assert after.public_transition == "option_leg_closed"


def test_complete_no_settlement_observation_resolves_expiration() -> None:
    evidence = _zero_evidence(
        observation_complete=True,
        broker_option_position_absent=True,
        projection_matches_frozen_remaining=True,
        no_stock_settlement=True,
        no_normal_order=True,
    )

    decision = resolve_close_reason(_target(), evidence, _timing(), 300)

    assert decision.status == "resolved"
    assert decision.close_reason == "expiration_no_settlement"
    assert decision.contracts_resolved == 2


def test_incomplete_no_settlement_observation_fails_closed() -> None:
    evidence = _zero_evidence(
        observation_complete=True,
        broker_option_position_absent=True,
        projection_matches_frozen_remaining=False,
        no_stock_settlement=True,
        no_normal_order=True,
    )

    decision = resolve_close_reason(_target(), evidence, _timing(), 300)

    assert decision.status == "needs_review"
    assert decision.reason_codes == ("settlement_observation_incomplete",)


def test_terminal_fact_conflict_precedes_reason_resolution() -> None:
    decision = resolve_close_reason(
        _target(),
        _zero_evidence(mutually_exclusive_terminal_facts=True),
        _timing(),
        100,
    )

    assert decision.status == "conflict"
    assert decision.reason_codes == ("mutually_exclusive_terminal_facts",)


def _allocation(*, terminal_type: str = "assignment") -> dict:
    case_id = "case-1"
    evidence_id = "evidence-1"
    lot_id = "lot-1"
    contracts = 2
    return {
        "allocation_id": allocation_id_for(
            case_id=case_id,
            evidence_id=evidence_id,
            target_lot_id=lot_id,
        ),
        "case_id": case_id,
        "evidence_id": evidence_id,
        "target_lot_id": lot_id,
        "terminal_type": terminal_type,
        "contracts_allocated": contracts,
        "canonical_terminal_event_id": terminal_event_id_for(
            case_id=case_id,
            evidence_id=evidence_id,
            target_lot_id=lot_id,
            terminal_type=terminal_type,
            contracts_allocated=contracts,
        ),
    }


def test_pending_reservation_is_nonactionable_without_changing_remaining() -> None:
    observation_start = expiration_observation_start_ms("2026-08-21", "US")
    assert observation_start is not None

    model = derive_lifecycle_read_model(
        expiration_ymd="2026-08-21",
        market="US",
        target_contracts_by_lot={"lot-1": 2},
        accepted_option_close_contracts_by_lot={"lot-1": 2},
        now_ms=observation_start - 1,
    )

    assert model.remaining_contracts_by_lot == {"lot-1": 2}
    assert model.reserved_contracts_by_lot == {"lot-1": 2}
    assert model.closure_fact == "option_leg_closed"
    assert model.reason_state == "cause_pending"
    assert model.actionable is False


def test_voided_terminal_allocation_becomes_pending_reservation() -> None:
    allocation = _allocation()
    terminal_event_id = allocation["canonical_terminal_event_id"]
    resolved = derive_lifecycle_read_model(
        expiration_ymd="2026-08-21",
        market="US",
        target_contracts_by_lot={"lot-1": 2},
        allocations=[allocation],
        now_ms=expiration_observation_start_ms("2026-08-21", "US"),
    )
    corrected = derive_lifecycle_read_model(
        expiration_ymd="2026-08-21",
        market="US",
        target_contracts_by_lot={"lot-1": 2},
        allocations=[allocation],
        void_event_ids=(terminal_event_id,),
        accepted_option_close_contracts_by_lot={"lot-1": 2},
        now_ms=expiration_observation_start_ms("2026-08-21", "US"),
    )

    assert resolved.reason_state == "resolved"
    assert resolved.close_reason == "assignment"
    assert resolved.remaining_contracts_by_lot == {"lot-1": 0}
    assert corrected.reason_state == "cause_pending"
    assert corrected.remaining_contracts_by_lot == {"lot-1": 2}
    assert corrected.reserved_contracts_by_lot == {"lot-1": 2}
    assert corrected.actionable is False


def test_close_terminal_allocation_maps_to_trade_close() -> None:
    model = derive_lifecycle_read_model(
        expiration_ymd="2026-08-21",
        market="US",
        target_contracts_by_lot={"lot-1": 2},
        allocations=[_allocation(terminal_type="close")],
        now_ms=expiration_observation_start_ms("2026-08-21", "US"),
    )

    assert model.lifecycle_state == "closed"
    assert model.reason_state == "resolved"
    assert model.close_reason == "trade_close"
