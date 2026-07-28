from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from domain.domain.combo_identity import (
    build_combo_identity,
    build_combo_identity_intent,
    classify_combo_structure,
    identity_from_intent,
    validate_combo_identity_intent,
)
from domain.domain.decision_state_fingerprint import (
    DecisionStateNormalizationError,
    build_decision_state_fingerprint,
    canonical_sha256,
)
from domain.domain.lifecycle_allocation import (
    plan_evidence_allocation,
    resolve_allocations,
)
from domain.domain.option_lifecycle import (
    PENDING_ELAPSED_HOURS,
    build_lifecycle_case,
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
    portfolio_account_identity_hash,
    resolve_authority,
    scope_for,
    validate_authority_policy,
    validate_first_use_uniqueness,
)
from domain.domain.position_advice_promotion import (
    PROMOTION_EVIDENCE_SCHEMA,
    REQUIRED_CRITICAL_REPLAY_FIXTURES,
    SAFETY_METRICS,
    evaluate_promotion_gate,
)


def _sha(char: str) -> str:
    return char * 64


def _combo_legs() -> tuple[dict[str, object], dict[str, object]]:
    return (
        {
            "strategy_group_id": "combo-1",
            "strategy": "combo_yield",
            "account": "lx",
            "symbol": "NVDA",
            "leg_role": "funding_put",
            "contracts": 1,
            "open_event_id": "event-put",
            "record_id": "lot-put",
            "contract_key": {"option_type": "put", "strike": "100", "expiration": "2026-09-18"},
        },
        {
            "strategy_group_id": "combo-1",
            "strategy": "combo_yield",
            "account": "lx",
            "symbol": "NVDA",
            "leg_role": "participation_call",
            "contracts": 1,
            "open_event_id": "event-call",
            "record_id": "lot-call",
            "contract_key": {"option_type": "call", "strike": "110", "expiration": "2026-12-18"},
        },
    )


def _combo_identity() -> dict[str, object]:
    put_leg, call_leg = _combo_legs()
    return build_combo_identity(
        {
            "group_id": "combo-1",
            "strategy": "combo_yield",
            "account": "lx",
            "symbol": "NVDA",
            "funding_put_record_id": put_leg["record_id"],
            "funding_put_open_event_id": put_leg["open_event_id"],
            "funding_put_contract_key": put_leg["contract_key"],
            "participation_call_record_id": call_leg["record_id"],
            "participation_call_open_event_id": call_leg["open_event_id"],
            "participation_call_contract_key": call_leg["contract_key"],
            "original_contracts": 1,
        }
    )


def _short_option(
    *,
    strike: str,
    price: str,
    dte: int,
    capacity: str,
    currency: str = "USD",
) -> ShortOptionCarryInput:
    return ShortOptionCarryInput(
        option_type="put",
        spot=Decimal("100"),
        strike=Decimal(strike),
        price=Decimal(price),
        dte=dte,
        multiplier=Decimal("100"),
        contracts=1,
        capacity=Decimal(capacity),
        currency=currency,
    )


def _cash_proposal(
    *,
    proposal_id: str,
    source_id: str,
    candidate_id: str,
    released: str,
    required: str,
    efficiency: str = "0.10",
    improvement: str = "100",
) -> dict[str, object]:
    return {
        "proposal_id": proposal_id,
        "source_position_ids": [source_id],
        "candidate_id": candidate_id,
        "candidate_contracts": 1,
        "replacement_eligibility": "capacity_deferred_to_allocator",
        "pool_efficiency_improvement": efficiency,
        "net_carry_improvement_H": improvement,
        "allocation_rank": 1,
        "resource_deltas": [
            {
                "resource_kind": "cash_base_cny",
                "pool_key": "cash:scope:authority",
                "unit": "CNY",
                "released": released,
                "required": required,
            }
        ],
    }


def _promotion_evidence() -> dict[str, object]:
    opportunities: list[dict[str, object]] = []
    for index in range(30):
        selected = index < 5
        opportunities.append(
            {
                "opportunity_key": f"opportunity-{index}",
                "authority_mode": "v2_shadow",
                "fresh": True,
                "eligible": True,
                "replacement_opportunity": index < 10,
                "selected": selected,
                "replacement_eligibility": (
                    "capacity_deferred_to_allocator" if index < 3 else "accepted_opening"
                ),
                "receipt_complete": True,
                "strategy_family": "short_put",
                "pool_efficiency_improvement": "0.01",
                "outcome_reason": "selected" if selected else "not_selected",
            }
        )
    return _with_automatic_reports({
        "schema_version": PROMOTION_EVIDENCE_SCHEMA,
        "authority_mode": "v2_shadow",
        "safety": {metric: 0 for metric in SAFETY_METRICS},
        "market_session_ids": [f"US:2026-07-{day:02d}" for day in range(1, 11)],
        "first_opportunity_at": "2026-07-01T00:00:00+00:00",
        "last_opportunity_at": "2026-07-16T00:00:00+00:00",
        "opportunities": opportunities,
        "covered_strategy_families": ["short_put"],
        "critical_replay_fixtures": {
            fixture: True for fixture in REQUIRED_CRITICAL_REPLAY_FIXTURES
        },
        "economic": {
            "modeled_daily_carry_uplift_base_cny": "100",
            "aggregate_net_carry_improvement_H_base_cny": "1000",
            "pool_efficiencies": [
                {
                    "pool_key": "cash:scope:authority",
                    "before": "0.01",
                    "after": "0.02",
                    "resource_units_before": "100000",
                    "resource_units_after": "90000",
                }
            ],
        },
    })


def _with_automatic_reports(
    evidence: dict[str, object],
) -> dict[str, object]:
    evidence["source_plan_hashes"] = ["9" * 64]
    safety_report: dict[str, object] = {
        "schema_version": "position_advice_promotion_checks.v1",
        "evaluator_version": "position_advice_promotion_checks.v1",
        "source_plan_hashes": ["9" * 64],
        "safety": evidence["safety"],
        "violations": [],
    }
    safety_report["artifact_hash"] = canonical_sha256(safety_report)
    replay_report: dict[str, object] = {
        "schema_version": "position_advice_critical_replay.v1",
        "fixture_results": evidence["critical_replay_fixtures"],
        "details": {},
    }
    replay_report["artifact_hash"] = canonical_sha256(replay_report)
    evidence["automatic_safety_evaluation"] = safety_report
    evidence["automatic_critical_replay"] = replay_report
    return evidence


def test_decision_fingerprint_normalizes_decimal_forms_and_set_like_rows() -> None:
    first = {
        "account_position_lots": [
            {"record_id": "b", "contracts_open": "1.00", "cost_basis": Decimal("2.500")},
            {"record_id": "a", "contracts_open": 2, "cost_basis": 3},
        ]
    }
    second = {
        "account_position_lots": [
            {"cost_basis": "3.0", "contracts_open": Decimal("2.00"), "record_id": "a"},
            {"cost_basis": "2.5", "contracts_open": 1.0, "record_id": "b"},
        ]
    }

    assert build_decision_state_fingerprint(first) == build_decision_state_fingerprint(second)


def test_decision_fingerprint_excludes_only_operational_fields_and_preserves_ordered_lists() -> None:
    base = {
        "account_position_lots": [
            {
                "record_id": "lot-1",
                "contracts_open": 1,
                "updated_at": "old",
                "display_label": "old",
                "new_business_field": "A",
            }
        ],
        "operations": [{"sequence": 1, "type": "close"}, {"sequence": 2, "type": "open"}],
    }
    metadata_change = deepcopy(base)
    metadata_change["account_position_lots"][0]["updated_at"] = "new"
    metadata_change["account_position_lots"][0]["display_label"] = "new"
    assert build_decision_state_fingerprint(base) == build_decision_state_fingerprint(metadata_change)

    business_change = deepcopy(base)
    business_change["account_position_lots"][0]["new_business_field"] = "B"
    assert build_decision_state_fingerprint(base) != build_decision_state_fingerprint(business_change)

    reordered_operations = deepcopy(base)
    reordered_operations["operations"].reverse()
    assert build_decision_state_fingerprint(base) != build_decision_state_fingerprint(reordered_operations)


def test_decision_fingerprint_rejects_non_finite_values() -> None:
    with pytest.raises(DecisionStateNormalizationError):
        build_decision_state_fingerprint({"account_position_lots": [{"record_id": "x", "premium": float("nan")}]})


def test_combo_intent_is_bound_to_all_payload_fields() -> None:
    put_leg, call_leg = _combo_legs()
    intent = build_combo_identity_intent(first_leg=put_leg, second_leg=call_leg)
    assert validate_combo_identity_intent(intent).status == "valid"
    assert identity_from_intent(intent, first_leg=put_leg, second_leg=call_leg)["group_id"] == "combo-1"

    tampered = {**intent, "account": "sy"}
    assert validate_combo_identity_intent(tampered).reason_codes == ("intent_hash_mismatch",)
    with pytest.raises(ValueError, match="invalid combo identity intent"):
        identity_from_intent(tampered, first_leg=put_leg, second_leg=call_leg)


def test_combo_structure_classification_never_guesses_missing_or_partial_legs() -> None:
    identity = _combo_identity()
    assert (
        classify_combo_structure(
            identity=identity,
            funding_put_contracts_open=1,
            participation_call_contracts_open=1,
        )
        == "active_combo"
    )
    assert (
        classify_combo_structure(
            identity=identity,
            funding_put_contracts_open=1,
            participation_call_contracts_open=0,
        )
        == "opening_incomplete"
    )
    assert (
        classify_combo_structure(
            identity=identity,
            funding_put_contracts_open=1,
            participation_call_terminal_allocated=1,
        )
        == "decomposed_residual_funding_put"
    )
    assert (
        classify_combo_structure(
            identity=identity,
            funding_put_contracts_open=1,
            participation_call_contracts_open=1,
            funding_put_terminal_allocated=1,
        )
        == "review_required"
    )


def test_lifecycle_allocation_is_deterministic_and_tamper_evident() -> None:
    plan = plan_evidence_allocation(
        case_id="case-1",
        evidence_id="evidence-1",
        terminal_type="assignment",
        contracts=1,
        remaining_contracts_by_lot={"lot-1": 2},
    )
    assert plan.status == "planned"
    partial = resolve_allocations({"lot-1": 2}, plan.allocations)
    assert partial.status == "ok"
    assert partial.remaining_contracts_by_lot == {"lot-1": 1}

    tampered = [{**plan.allocations[0], "canonical_terminal_event_id": "wrong"}]
    conflict = resolve_allocations({"lot-1": 2}, tampered)
    assert conflict.status == "conflict"
    assert conflict.reason_codes == ("terminal_event_id_mismatch",)


def test_lifecycle_allocation_refuses_ambiguous_quantity_binding() -> None:
    plan = plan_evidence_allocation(
        case_id="case-1",
        evidence_id="evidence-1",
        terminal_type="expire_close",
        contracts=1,
        remaining_contracts_by_lot={"lot-a": 1, "lot-b": 1},
    )
    assert plan.status == "conflict"
    assert plan.reason_codes == ("ambiguous_quantity_binding",)


def test_lifecycle_observation_boundary_and_silence_fail_closed() -> None:
    expected = datetime(2026, 7, 28, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    observation_start = expiration_observation_start_ms("2026-07-27", "US")
    assert observation_start == int(expected.timestamp() * 1000)

    lifecycle_case = build_lifecycle_case(
        account="lx",
        broker="futu",
        contract_key="NVDA:put:100:2026-07-27",
        position_side="short",
        expiration_ymd="2026-07-27",
        market="US",
        target_contracts_by_lot={"lot-1": "2.00"},
    )
    assert lifecycle_case["pending_until_ms"] == observation_start + PENDING_ELAPSED_HOURS * 3600 * 1000
    assert lifecycle_case["target_contracts_by_lot"] == {"lot-1": 2}

    pending = derive_lifecycle_read_model(
        expiration_ymd="2026-07-27",
        market="US",
        target_contracts_by_lot={"lot-1": 2},
        now_ms=observation_start + 1,
    )
    assert pending.lifecycle_state == "settlement_pending"
    assert pending.actionable is False

    review = derive_lifecycle_read_model(
        expiration_ymd="2026-07-27",
        market="US",
        target_contracts_by_lot={"lot-1": 2},
        now_ms=lifecycle_case["pending_until_ms"],
    )
    assert review.lifecycle_state == "needs_review"
    assert review.resolved_contracts_by_terminal_type == {}


def test_lifecycle_partial_and_terminal_states_are_not_option_actions() -> None:
    observation_start = expiration_observation_start_ms("2026-07-27", "US")
    first = plan_evidence_allocation(
        case_id="case-1",
        evidence_id="evidence-1",
        terminal_type="assignment",
        contracts=1,
        remaining_contracts_by_lot={"lot-1": 2},
    )
    partial = derive_lifecycle_read_model(
        expiration_ymd="2026-07-27",
        market="US",
        target_contracts_by_lot={"lot-1": 2},
        allocations=first.allocations,
        now_ms=observation_start + 1,
    )
    assert partial.lifecycle_state == "partially_resolved"
    assert partial.actionable is False

    second = plan_evidence_allocation(
        case_id="case-1",
        evidence_id="evidence-2",
        terminal_type="assignment",
        contracts=1,
        remaining_contracts_by_lot={"lot-1": 1},
    )
    terminal = derive_lifecycle_read_model(
        expiration_ymd="2026-07-27",
        market="US",
        target_contracts_by_lot={"lot-1": 2},
        allocations=first.allocations + second.allocations,
        now_ms=observation_start + 1,
    )
    assert terminal.lifecycle_state == "assigned"
    assert terminal.actionable is False


def test_unknown_market_and_invalid_expiration_have_explicit_review_reasons() -> None:
    unknown_market = derive_lifecycle_read_model(
        expiration_ymd="2026-07-27",
        market="EU",
        target_contracts_by_lot={"lot-1": 1},
    )
    assert unknown_market.lifecycle_reason_codes == ("market_expiration_policy_missing",)

    invalid_date = derive_lifecycle_read_model(
        expiration_ymd="not-a-date",
        market="US",
        target_contracts_by_lot={"lot-1": 1},
    )
    assert invalid_date.lifecycle_reason_codes == ("expiration_date_invalid",)


def test_observable_carry_requires_allocator_selection_for_formal_action() -> None:
    current = _short_option(strike="105", price="6", dte=10, capacity="10500")
    candidate = _short_option(strike="100", price="3", dte=20, capacity="10000")

    proposal = evaluate_short_option_switch(
        current=current,
        candidate=candidate,
        close_fee="10",
        open_fee="10",
        proposed_action="roll",
        replacement_eligible=True,
    )
    assert proposal.recommendation == "hold"
    assert proposal.reason_codes == ("portfolio_allocator_not_selected",)

    selected = evaluate_short_option_switch(
        current=current,
        candidate=candidate,
        close_fee="10",
        open_fee="10",
        proposed_action="roll",
        replacement_eligible=True,
        allocator_selected=True,
    )
    assert selected.recommendation == "roll"
    assert selected.actionable is True
    assert selected.current_extrinsic == Decimal("100")
    assert selected.candidate_extrinsic == Decimal("300")
    assert selected.net_carry_improvement_h == Decimal("30")
    assert selected.payback_days == Decimal("4")


def test_observable_carry_rejects_bad_economic_or_fx_inputs() -> None:
    current = _short_option(strike="105", price="-1", dte=10, capacity="10500")
    candidate = _short_option(strike="100", price="3", dte=20, capacity="10000")
    invalid = evaluate_short_option_switch(
        current=current,
        candidate=candidate,
        close_fee=0,
        open_fee=0,
        proposed_action="roll",
        replacement_eligible=True,
        allocator_selected=True,
    )
    assert invalid.reason_codes == ("economic_input_invalid",)

    cross_currency = evaluate_short_option_switch(
        current=_short_option(strike="105", price="6", dte=10, capacity="10500", currency="USD"),
        candidate=_short_option(strike="100", price="3", dte=20, capacity="10000", currency="HKD"),
        close_fee=0,
        open_fee=0,
        proposed_action="reallocate",
        replacement_eligible=True,
        allocator_selected=True,
    )
    assert cross_currency.reason_codes == ("fx_missing_or_stale",)


def test_long_call_remains_informational_only() -> None:
    facts = long_call_observable_facts(
        spot="120",
        strike="100",
        bid="25",
        contracts=1,
        multiplier=100,
        dte=30,
        fee=5,
    )
    assert facts["recommendation"] == "not_evaluable"
    assert facts["reason"] == "long_call_forward_model_not_approved"
    assert facts["actionable"] is False
    with pytest.raises(ValueError, match="contracts must be a positive integer"):
        long_call_observable_facts(
            spot="120",
            strike="100",
            bid="25",
            contracts=0,
            multiplier=100,
            dte=30,
        )


def test_allocator_uses_released_cash_without_double_allocating_candidate() -> None:
    result = allocate_position_advice(
        proposals=[
            _cash_proposal(
                proposal_id="p1",
                source_id="lot-1",
                candidate_id="candidate-1",
                released="1000",
                required="1200",
                efficiency="0.2",
            ),
            _cash_proposal(
                proposal_id="p2",
                source_id="lot-2",
                candidate_id="candidate-1",
                released="1000",
                required="1100",
                efficiency="0.1",
            ),
        ],
        resource_pools={
            "cash:scope:authority": {
                "resource_kind": "cash_base_cny",
                "unit": "CNY",
                "available": "500",
            }
        },
        candidate_quantities={"candidate-1": 1},
    )
    assert [item["proposal_id"] for item in result.selected] == ["p1"]
    assert result.selected[0]["resource_deltas"][0]["net_after"] == "300"
    assert result.alternatives[0]["allocator_reason"] == "candidate_quantity_exhausted"


def test_allocator_records_dependency_on_prior_net_release() -> None:
    result = allocate_position_advice(
        proposals=[
            _cash_proposal(
                proposal_id="release",
                source_id="lot-release",
                candidate_id="candidate-release",
                released="1000",
                required="500",
                efficiency="0.2",
            ),
            _cash_proposal(
                proposal_id="consume",
                source_id="lot-consume",
                candidate_id="candidate-consume",
                released="0",
                required="400",
                efficiency="0.1",
            ),
        ],
        resource_pools={
            "cash:scope:authority": {
                "resource_kind": "cash_base_cny",
                "unit": "CNY",
                "available": "0",
            }
        },
        candidate_quantities={"candidate-release": 1, "candidate-consume": 1},
    )
    assert [item["proposal_id"] for item in result.selected] == ["release", "consume"]
    assert result.selected[1]["depends_on"] == ["release"]


def test_allocator_rejects_invalid_pool_contract_and_duplicate_proposal_ids() -> None:
    with pytest.raises(ValueError, match="resource pool contract is invalid"):
        allocate_position_advice(
            proposals=[],
            resource_pools={
                "shares:futu:lx:NVDA": {
                    "resource_kind": "cash_base_cny",
                    "unit": "CNY",
                    "available": "100",
                }
            },
            candidate_quantities={},
        )

    proposal = _cash_proposal(
        proposal_id="duplicate",
        source_id="lot-1",
        candidate_id="candidate-1",
        released="1000",
        required="500",
    )
    result = allocate_position_advice(
        proposals=[proposal, {**proposal, "source_position_ids": ["lot-2"]}],
        resource_pools={
            "cash:scope:authority": {
                "resource_kind": "cash_base_cny",
                "unit": "CNY",
                "available": "0",
            }
        },
        candidate_quantities={"candidate-1": 2},
    )
    assert result.selected == ()
    assert {item["allocator_reason"] for item in result.alternatives} == {"proposal_id_conflict"}


def test_authority_scope_is_label_only_and_first_use_v1_allows_notifications() -> None:
    assert scope_for(" LX ") == scope_for("lx")
    first_use = resolve_authority(
        normalized_account_label="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash_value=_sha("a"),
        policy=None,
    )
    assert first_use.mode == "v1"
    assert first_use.resolution_status == "first_use_default_v1"
    assert first_use.notifications_allowed is True


def test_authority_policy_fails_closed_on_identity_drift_or_tamper() -> None:
    identity = portfolio_account_identity_hash(
        normalized_portfolio_source="futu",
        broker_account_identifiers=["12345"],
    )
    policy = build_authority_policy(
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash_value=identity,
        mode="v1",
        generation=1,
        updated_at="2026-07-27T10:00:00+00:00",
        change_receipt_hash=_sha("b"),
    )
    assert validate_authority_policy(policy) == ()
    resolved = resolve_authority(
        normalized_account_label="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash_value=identity,
        policy=policy,
    )
    assert resolved.resolution_status == "resolved"

    drift = resolve_authority(
        normalized_account_label="lx",
        normalized_portfolio_source="external_holdings",
        portfolio_account_identity_hash_value=_sha("c"),
        policy=policy,
    )
    assert drift.resolution_status == "authority_conflict"
    assert drift.notifications_allowed is False
    assert "portfolio_source_identity_conflict" in drift.reason_codes

    tampered = {**policy, "mode": "v2"}
    reasons = validate_authority_policy(tampered)
    assert "authority_policy_hash_mismatch" in reasons
    assert "authority_v2_promotion_evidence_missing" in reasons


def test_v2_authority_requires_promotion_evidence_and_covered_family() -> None:
    with pytest.raises(ValueError, match="v2 authority requires promotion evidence"):
        build_authority_policy(
            normalized_account="lx",
            normalized_portfolio_source="futu",
            portfolio_account_identity_hash_value=_sha("a"),
            mode="v2",
            generation=2,
            updated_at="2026-07-27T10:00:00+00:00",
            change_receipt_hash=_sha("b"),
        )


def test_first_use_uniqueness_rejects_same_portfolio_identity_in_another_scope() -> None:
    existing = build_authority_policy(
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash_value=_sha("a"),
        mode="v1",
        generation=1,
        updated_at="2026-07-27T10:00:00+00:00",
        change_receipt_hash=_sha("b"),
    )
    reasons = validate_first_use_uniqueness(
        target_scope_id=scope_for("sy"),
        target_identity_hash=_sha("a"),
        existing_policies=[existing],
    )
    assert reasons == ("portfolio_identity_already_bound_to_other_scope",)


def test_promotion_gate_passes_only_complete_nonempty_shadow_evidence() -> None:
    gate = evaluate_promotion_gate(_promotion_evidence())
    assert gate["status"] == "pass"
    assert gate["reason_codes"] == []
    assert gate["unique_opportunity_count"] == 30
    assert gate["selected_proposal_count"] == 5


def test_promotion_gate_fails_closed_on_empty_or_malformed_evidence() -> None:
    empty = evaluate_promotion_gate({})
    assert empty["status"] == "insufficient_evidence"
    assert "promotion_evidence_schema_invalid" in empty["reason_codes"]
    assert "selected_population_empty" in empty["reason_codes"]

    malformed = _promotion_evidence()
    malformed["safety"]["false_assignment_confirmation"] = "zero"
    gate = evaluate_promotion_gate(malformed)
    assert "safety_invalid:false_assignment_confirmation" in gate["reason_codes"]

    caller_claimed = _promotion_evidence()
    caller_claimed.pop("automatic_safety_evaluation")
    caller_claimed.pop("automatic_critical_replay")
    claimed_gate = evaluate_promotion_gate(caller_claimed)
    assert claimed_gate["status"] == "insufficient_evidence"
    assert "automatic_safety_evidence_missing" in claimed_gate["reason_codes"]
    assert "automatic_critical_replay_missing" in claimed_gate["reason_codes"]


def test_promotion_gate_rejects_conflicting_duplicate_opportunity() -> None:
    evidence = _promotion_evidence()
    duplicate = deepcopy(evidence["opportunities"][0])
    duplicate["selected"] = False
    evidence["opportunities"].append(duplicate)
    gate = evaluate_promotion_gate(evidence)
    assert gate["status"] == "insufficient_evidence"
    assert "duplicate_opportunity_conflict" in gate["reason_codes"]


def test_opportunity_key_hash_is_stable_for_ordered_source_identity() -> None:
    first = canonical_sha256({"source_position_ids": sorted(["lot-b", "lot-a"])})
    second = canonical_sha256({"source_position_ids": sorted(["lot-a", "lot-b"])})
    assert first == second
