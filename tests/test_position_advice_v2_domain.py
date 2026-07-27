from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from domain.domain.combo_identity import (
    build_combo_identity,
    build_combo_identity_intent,
    classify_combo_structure,
    identity_from_intent,
)
from domain.domain.decision_state_fingerprint import (
    DecisionStateNormalizationError,
    build_decision_state_fingerprint,
)
from domain.domain.lifecycle_allocation import (
    allocation_id_for,
    plan_evidence_allocation,
    resolve_allocations,
    terminal_event_id_for,
)
from domain.domain.option_lifecycle import (
    derive_lifecycle_read_model,
    expiration_observation_start_ms,
)
from domain.domain.position_advice import (
    ShortOptionCarryInput,
    evaluate_short_option_switch,
    long_call_observable_facts,
)
from domain.domain.position_advice_allocator import allocate_position_advice
from domain.domain.position_advice_authority import (
    build_authority_policy,
    capacity_pool_authority_id,
    portfolio_account_identity_hash,
    resolve_authority,
    scope_for,
    validate_first_use_uniqueness,
)
from domain.domain.position_advice_promotion import (
    SAFETY_METRICS,
    evaluate_promotion_gate,
    unique_decision_opportunity_key,
)


def _short(
    *,
    option_type: str = "put",
    price: str,
    dte: int,
    capacity: str,
    currency: str = "USD",
    spot: str = "100",
    strike: str = "100",
) -> ShortOptionCarryInput:
    return ShortOptionCarryInput(
        option_type=option_type,
        spot=Decimal(spot),
        strike=Decimal(strike),
        price=Decimal(price),
        dte=dte,
        multiplier=Decimal("100"),
        contracts=1,
        capacity=Decimal(capacity),
        currency=currency,
    )


def _combo_legs() -> tuple[dict, dict]:
    put = {
        "strategy_group_id": "combo:lx:1",
        "strategy": "combo_yield",
        "account": "lx",
        "symbol": "NVDA",
        "leg_role": "funding_put",
        "contracts": 2,
        "open_event_id": "event-put",
        "record_id": "lot-put",
        "contract_key": {"option_type": "put", "strike": 100},
    }
    call = {
        "strategy_group_id": "combo:lx:1",
        "strategy": "combo_yield",
        "account": "lx",
        "symbol": "NVDA",
        "leg_role": "participation_call",
        "contracts": 2,
        "open_event_id": "event-call",
        "record_id": "lot-call",
        "contract_key": {"option_type": "call", "strike": 110},
    }
    return put, call


def test_decision_fingerprint_is_deterministic_and_fail_closed() -> None:
    left = {
        "account_position_lots": [
            {
                "record_id": "lot-1",
                "fields": {
                    "contracts_open": Decimal("1.00"),
                    "cash_secured": 10000,
                    "display_label": "old",
                    "updated_at_ms": 1,
                },
            }
        ],
        "account_lifecycle_allocations": [],
    }
    right = {
        "account_lifecycle_allocations": [],
        "account_position_lots": [
            {
                "fields": {
                    "updated_at_ms": 999,
                    "display_label": "new",
                    "cash_secured": Decimal("10000.0"),
                    "contracts_open": 1.0,
                },
                "record_id": "lot-1",
            }
        ],
    }

    assert build_decision_state_fingerprint(left) == build_decision_state_fingerprint(right)
    changed = {
        **right,
        "account_position_lots": [
            {
                **right["account_position_lots"][0],
                "fields": {**right["account_position_lots"][0]["fields"], "future_business_field": "changed"},
            }
        ],
    }
    assert build_decision_state_fingerprint(left) != build_decision_state_fingerprint(changed)
    with pytest.raises(DecisionStateNormalizationError):
        build_decision_state_fingerprint({"value": float("nan")})


def test_lifecycle_uses_market_boundary_and_never_infers_terminal_from_silence() -> None:
    observation_start = expiration_observation_start_ms("2026-07-24", "US")
    assert observation_start is not None
    before = derive_lifecycle_read_model(
        expiration_ymd="2026-07-24",
        market="US",
        target_contracts_by_lot={"lot-1": 2},
        now_ms=observation_start - 1,
    )
    pending = derive_lifecycle_read_model(
        expiration_ymd="2026-07-24",
        market="US",
        target_contracts_by_lot={"lot-1": 2},
        now_ms=observation_start,
    )
    review = derive_lifecycle_read_model(
        expiration_ymd="2026-07-24",
        market="US",
        target_contracts_by_lot={"lot-1": 2},
        now_ms=observation_start + 72 * 3600 * 1000,
    )

    assert before.lifecycle_state == "open"
    assert pending.lifecycle_state == "settlement_pending"
    assert review.lifecycle_state == "needs_review"
    assert "expired_unassigned" not in {pending.lifecycle_state, review.lifecycle_state}
    unknown = derive_lifecycle_read_model(
        expiration_ymd="2026-07-24",
        market="SG",
        target_contracts_by_lot={"lot-1": 1},
    )
    assert unknown.lifecycle_state == "needs_review"
    assert unknown.lifecycle_reason_codes == ("market_expiration_policy_missing",)


def test_lifecycle_partial_mixed_and_allocation_conflicts() -> None:
    observation_start = expiration_observation_start_ms("2026-07-24", "HK")
    assert observation_start is not None
    first = {
        "allocation_id": allocation_id_for(
            case_id="c1",
            evidence_id="e1",
            target_lot_id="lot-1",
        ),
        "case_id": "c1",
        "evidence_id": "e1",
        "target_lot_id": "lot-1",
        "terminal_type": "assignment",
        "contracts_allocated": 1,
        "canonical_terminal_event_id": terminal_event_id_for(
            case_id="c1",
            evidence_id="e1",
            target_lot_id="lot-1",
            terminal_type="assignment",
            contracts_allocated=1,
        ),
    }
    partial = derive_lifecycle_read_model(
        expiration_ymd="2026-07-24",
        market="HK",
        target_contracts_by_lot={"lot-1": 2},
        allocations=[first],
        now_ms=observation_start + 1,
    )
    assert partial.lifecycle_state == "partially_resolved"
    assert partial.remaining_contracts_by_lot == {"lot-1": 1}
    assert partial.actionable is False

    second = {
        **first,
        "allocation_id": allocation_id_for(
            case_id="c1",
            evidence_id="e2",
            target_lot_id="lot-1",
        ),
        "evidence_id": "e2",
        "terminal_type": "expire_close",
        "canonical_terminal_event_id": terminal_event_id_for(
            case_id="c1",
            evidence_id="e2",
            target_lot_id="lot-1",
            terminal_type="expire_close",
            contracts_allocated=1,
        ),
    }
    complete = derive_lifecycle_read_model(
        expiration_ymd="2026-07-24",
        market="HK",
        target_contracts_by_lot={"lot-1": 2},
        allocations=[first, second],
        now_ms=observation_start + 1,
    )
    assert complete.lifecycle_state == "resolved_mixed"
    assert complete.remaining_contracts_by_lot == {"lot-1": 0}

    over = resolve_allocations(
        {"lot-1": 1},
        [
            {
                **first,
                "contracts_allocated": 2,
                "canonical_terminal_event_id": terminal_event_id_for(
                    case_id="c1",
                    evidence_id="e1",
                    target_lot_id="lot-1",
                    terminal_type="assignment",
                    contracts_allocated=2,
                ),
            }
        ],
    )
    assert over.status == "conflict"
    assert "allocation_exceeds_target" in over.reason_codes
    orphan = derive_lifecycle_read_model(
        expiration_ymd="2026-07-24",
        market="HK",
        target_contracts_by_lot={"lot-1": 1},
        orphan_evidence=True,
        now_ms=observation_start + 1,
    )
    assert orphan.lifecycle_state == "needs_review"


def test_evidence_allocation_requires_unique_lot_binding_and_is_deterministic() -> None:
    ambiguous = plan_evidence_allocation(
        case_id="case",
        evidence_id="evidence",
        terminal_type="assignment",
        contracts=1,
        remaining_contracts_by_lot={"lot-a": 1, "lot-b": 1},
    )
    assert ambiguous.status == "conflict"
    assert ambiguous.reason_codes == ("ambiguous_quantity_binding",)

    planned = plan_evidence_allocation(
        case_id="case",
        evidence_id="evidence",
        terminal_type="assignment",
        contracts=1,
        remaining_contracts_by_lot={"lot-a": 1, "lot-b": 1},
        target_lot_id="lot-b",
    )
    assert planned.status == "planned"
    assert planned.allocations[0]["allocation_id"] == allocation_id_for(
        case_id="case",
        evidence_id="evidence",
        target_lot_id="lot-b",
    )


def test_combo_identity_is_explicit_immutable_and_partial_terminal_never_revives() -> None:
    put, call = _combo_legs()
    intent = build_combo_identity_intent(first_leg=put, second_leg=call)
    identity = identity_from_intent(intent, first_leg=put, second_leg=call)
    assert identity == build_combo_identity(identity)
    assert classify_combo_structure(
        identity=identity,
        funding_put_contracts_open=2,
        participation_call_contracts_open=2,
    ) == "active_combo"
    assert classify_combo_structure(
        identity=identity,
        funding_put_contracts_open=2,
        participation_call_contracts_open=1,
        participation_call_terminal_allocated=1,
    ) == "partially_decomposed"
    assert classify_combo_structure(
        identity=identity,
        funding_put_contracts_open=2,
        participation_call_contracts_open=0,
        participation_call_terminal_allocated=2,
    ) == "decomposed_residual_funding_put"
    assert classify_combo_structure(
        identity=None,
        funding_put_contracts_open=2,
        participation_call_contracts_open=2,
    ) == "identity_unverified"


def test_observable_carry_excludes_intrinsic_and_requires_two_times_friction() -> None:
    # Current put ask contains 10 intrinsic + 1 extrinsic. Only the 1 extrinsic carries.
    current = _short(price="11", dte=10, capacity="10000", spot="90", strike="100")
    candidate = _short(price="2.5", dte=10, capacity="10000", spot="100", strike="100")
    result = evaluate_short_option_switch(
        current=current,
        candidate=candidate,
        close_fee="20",
        open_fee="20",
        proposed_action="roll",
        replacement_eligible=True,
        allocator_selected=True,
    )
    assert result.current_extrinsic == Decimal("100")
    assert result.candidate_extrinsic == Decimal("250")
    assert result.gross_carry_improvement_h == Decimal("150")
    assert result.net_carry_improvement_h == Decimal("110")
    assert result.actionable is True

    below_floor = evaluate_short_option_switch(
        current=current,
        candidate=_short(price="1.5", dte=10, capacity="10000"),
        close_fee="30",
        open_fee="20",
        proposed_action="roll",
        replacement_eligible=True,
        allocator_selected=True,
    )
    assert below_floor.gross_carry_improvement_h == Decimal("50")
    assert below_floor.actionable is False
    assert "improvement_below_two_times_friction" in below_floor.reason_codes


def test_observable_carry_cross_currency_and_long_call_boundaries() -> None:
    result = evaluate_short_option_switch(
        current=_short(price="1", dte=10, capacity="10000", currency="USD"),
        candidate=_short(price="20", dte=10, capacity="80000", currency="HKD"),
        close_fee="1",
        open_fee="10",
        proposed_action="reallocate",
        replacement_eligible=True,
        fx_to_cny={"USD": "7.2", "HKD": "0.92"},
    )
    assert result.comparison_currency == "CNY"
    stale = evaluate_short_option_switch(
        current=_short(price="1", dte=10, capacity="10000", currency="USD"),
        candidate=_short(price="20", dte=10, capacity="80000", currency="HKD"),
        close_fee="1",
        open_fee="10",
        proposed_action="reallocate",
        replacement_eligible=True,
        fx_to_cny={"USD": "7.2", "HKD": "0.92"},
        fx_fresh=False,
    )
    assert stale.recommendation == "not_evaluable"
    facts = long_call_observable_facts(
        spot=120,
        strike=100,
        bid=23,
        contracts=1,
        multiplier=100,
        dte=30,
    )
    assert facts["actionable"] is False
    assert facts["reason"] == "long_call_forward_model_not_approved"


def test_typed_allocator_prevents_double_use_and_tracks_release_dependency() -> None:
    proposals = [
        {
            "proposal_id": "p-release",
            "source_position_ids": ["lot-1"],
            "candidate_id": "candidate-a",
            "candidate_contracts": 1,
            "replacement_eligibility": "capacity_deferred_to_allocator",
            "pool_efficiency_improvement": "0.10",
            "net_carry_improvement_H": "100",
            "allocation_rank": 1,
            "resource_deltas": [
                {
                    "resource_kind": "cash_base_cny",
                    "pool_key": "cash:scope:authority",
                    "unit": "CNY",
                    "released": "10000",
                    "required": "7000",
                }
            ],
        },
        {
            "proposal_id": "p-dependent",
            "source_position_ids": ["lot-2"],
            "candidate_id": "candidate-b",
            "candidate_contracts": 1,
            "replacement_eligibility": "accepted_opening",
            "pool_efficiency_improvement": "0.09",
            "net_carry_improvement_H": "90",
            "allocation_rank": 2,
            "resource_deltas": [
                {
                    "resource_kind": "cash_base_cny",
                    "pool_key": "cash:scope:authority",
                    "unit": "CNY",
                    "released": "0",
                    "required": "2000",
                }
            ],
        },
        {
            "proposal_id": "p-double-source",
            "source_position_ids": ["lot-1"],
            "candidate_id": "candidate-c",
            "candidate_contracts": 1,
            "replacement_eligibility": "accepted_opening",
            "pool_efficiency_improvement": "0.08",
            "net_carry_improvement_H": "80",
            "allocation_rank": 3,
            "resource_deltas": [
                {
                    "resource_kind": "cash_base_cny",
                    "pool_key": "cash:scope:authority",
                    "unit": "CNY",
                    "released": "0",
                    "required": "1",
                }
            ],
        },
    ]
    result = allocate_position_advice(
        proposals=proposals,
        resource_pools={
            "cash:scope:authority": {
                "resource_kind": "cash_base_cny",
                "unit": "CNY",
                "available": "0",
            },
            "shares:futu:lx:NVDA": {
                "resource_kind": "covered_shares",
                "unit": "shares",
                "available": "0",
            },
        },
        candidate_quantities={"candidate-a": 1, "candidate-b": 1, "candidate-c": 1},
    )
    assert [item["proposal_id"] for item in result.selected] == ["p-release", "p-dependent"]
    assert result.selected[1]["depends_on"] == ["p-release"]
    assert result.resource_pools_after["cash:scope:authority"]["available"] == "1000"
    assert result.alternatives[0]["allocator_reason"] == "source_position_already_allocated"


def test_authority_scope_is_label_only_and_identity_drift_fails_closed() -> None:
    scope = scope_for(" LX ")
    assert scope == scope_for("lx")
    identity = portfolio_account_identity_hash(
        normalized_portfolio_source="futu",
        broker_account_identifiers=["123", "456"],
    )
    policy = build_authority_policy(
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash_value=identity,
        mode="v2_shadow",
        generation=1,
        updated_at="2026-07-27T00:00:00+00:00",
        change_receipt_hash="a" * 64,
        covered_strategy_families=["short_put"],
    )
    resolved = resolve_authority(
        normalized_account_label="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash_value=identity,
        policy=policy,
    )
    assert resolved.portfolio_scope_id == scope
    assert resolved.resolution_status == "resolved"
    assert resolved.mode == "v2_shadow"

    drift = resolve_authority(
        normalized_account_label="lx",
        normalized_portfolio_source="external_holdings",
        portfolio_account_identity_hash_value="different",
        policy=policy,
    )
    assert drift.portfolio_scope_id == scope
    assert drift.resolution_status == "authority_conflict"
    assert drift.notifications_allowed is False
    missing_with_history = resolve_authority(
        normalized_account_label="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash_value=identity,
        policy=None,
        historical_authority_state_exists=True,
    )
    assert missing_with_history.resolution_status == "authority_conflict"


def test_authority_ids_are_snapshot_independent_and_first_use_is_unique() -> None:
    identity = portfolio_account_identity_hash(
        normalized_portfolio_source="futu",
        broker_account_identifiers=["123"],
    )
    authority = capacity_pool_authority_id(
        normalized_portfolio_source="futu",
        broker_account_identifiers=["123"],
        cash_scope_semantics_version="uncommitted_headroom.v1",
    )
    assert authority == capacity_pool_authority_id(
        normalized_portfolio_source="futu",
        broker_account_identifiers=["123"],
        cash_scope_semantics_version="uncommitted_headroom.v1",
    )
    existing = build_authority_policy(
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash_value=identity,
        mode="v1",
        generation=1,
        updated_at="2026-07-27T00:00:00+00:00",
        change_receipt_hash="b" * 64,
    )
    reasons = validate_first_use_uniqueness(
        target_scope_id=scope_for("other"),
        target_identity_hash=identity,
        existing_policies=[existing],
    )
    assert reasons == ("portfolio_identity_already_bound_to_other_scope",)


def _promotion_evidence() -> dict:
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    opportunities = []
    for index in range(30):
        selected = index < 5
        opportunities.append(
            {
                "opportunity_key": f"op-{index}",
                "eligible": True,
                "replacement_opportunity": index < 10,
                "selected": selected,
                "replacement_eligibility": (
                    "capacity_deferred_to_allocator" if index < 3 else "accepted_opening"
                ),
                "strategy_family": "short_put",
                "receipt_complete": True,
                "fresh": True,
                "authority_mode": "v2_shadow",
                "pool_efficiency_improvement": "0.01",
                "outcome_reason": "selected" if selected else "eligible",
            }
        )
    return {
        "schema_version": "position_advice_promotion_evidence.v1",
        "authority_mode": "v2_shadow",
        "safety": {metric: 0 for metric in SAFETY_METRICS},
        "market_session_ids": [f"US:2026-07-{day:02d}" for day in range(1, 11)],
        "first_opportunity_at": start.isoformat(),
        "last_opportunity_at": (start + timedelta(days=14)).isoformat(),
        "covered_strategy_families": ["short_put"],
        "critical_replay_fixtures": {
            "put_release": True,
            "call_release": True,
            "capacity_and_invariant_reject": True,
            "partial_lifecycle": True,
            "stale_source": True,
            "authority_cas_conflict": True,
            "combo_decomposition": True,
        },
        "economic": {
            "modeled_daily_carry_uplift_base_cny": "10",
            "aggregate_net_carry_improvement_H_base_cny": "100",
            "pool_efficiencies": [
                {
                    "pool_key": "cash:one",
                    "before": "0.01",
                    "after": "0.02",
                    "resource_units_before": "100",
                    "resource_units_after": "100",
                },
            ],
        },
        "opportunities": opportunities,
    }


def test_promotion_gate_is_non_vacuous_deduplicated_and_economic() -> None:
    evidence = _promotion_evidence()
    gate = evaluate_promotion_gate(evidence)
    assert gate["status"] == "pass"
    evidence["opportunities"].append(dict(evidence["opportunities"][0]))
    assert evaluate_promotion_gate(evidence)["unique_opportunity_count"] == 30
    evidence["opportunities"] = []
    empty = evaluate_promotion_gate(evidence)
    assert empty["status"] == "insufficient_evidence"
    assert "selected_population_empty" in empty["reason_codes"]

    key = unique_decision_opportunity_key(
        portfolio_scope_id="scope",
        source_position_ids=["b", "a"],
        candidate_id="candidate",
        decision_state_fingerprint="decision",
        source_manifest_hash="source",
        economic_inputs_hash="economic",
    )
    assert key == unique_decision_opportunity_key(
        portfolio_scope_id="scope",
        source_position_ids=["a", "b"],
        candidate_id="candidate",
        decision_state_fingerprint="decision",
        source_manifest_hash="source",
        economic_inputs_hash="economic",
    )
