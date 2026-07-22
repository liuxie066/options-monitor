from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import inspect

import pytest

from domain.domain.close_advice import (
    POLICY_VARIANT_P0_CURRENT,
    POLICY_VARIANT_P1_SEMANTIC_SPLIT,
    POLICY_VARIANT_P2_PROFILE_AWARE,
    POLICY_VARIANT_P3_OPPORTUNITY_REQUIRED,
    CloseDecisionFacts,
    evaluate_close_policy,
)
from src.application import close_advice_runner
from src.application.shadow_replay.close_decision_policy import (
    ReallocationPolicyEvidence,
    close_decision_facts_from_row,
    compose_opportunity_required_policy,
    evaluate_shadow_close_policy_variants,
)


def _facts(**overrides: object) -> CloseDecisionFacts:
    values: dict[str, object] = {
        "tier": "strong",
        "exit_state": "profit_capture",
        "side": "short",
        "option_type": "put",
        "strategy_family": "sell_put",
        "strategy_profile": "return_first",
        "evaluation_status": "priced",
        "fee_calc_status": "schedule_estimate",
        "estimated_pnl_if_close_net": 80.0,
        "thesis_status": "valid",
        "continued_willingness": True,
        "close_calibration_status": "complete",
        "combo_evidence_status": "not_applicable",
    }
    values.update(overrides)
    return CloseDecisionFacts(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("tier", "exit_state", "expected"),
    [
        ("strong", "profit_capture", "close"),
        ("medium", "profit_capture", "close"),
        ("weak", "profit_capture", "close"),
        ("optional", "profit_capture", "close"),
        ("strong", "risk_exit", "hold"),
        ("none", "hold", "hold"),
        ("not_evaluable", "not_evaluable", "not_evaluable"),
    ],
)
def test_p0_policy_preserves_current_exit_action_semantics(
    tier: str,
    exit_state: str,
    expected: str,
) -> None:
    result = evaluate_close_policy(
        _facts(tier=tier, exit_state=exit_state),
        POLICY_VARIANT_P0_CURRENT,
    )

    assert result.recommendation_state == expected


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        ("strong", "close"),
        ("medium", "review"),
        ("weak", "hold"),
        ("optional", "hold"),
        ("none", "hold"),
    ],
)
def test_p1_splits_medium_from_close_and_holds_lower_tiers(tier: str, expected: str) -> None:
    exit_state = "hold" if tier == "none" else "profit_capture"

    result = evaluate_close_policy(
        _facts(tier=tier, exit_state=exit_state),
        POLICY_VARIANT_P1_SEMANTIC_SPLIT,
    )

    assert result.recommendation_state == expected


@pytest.mark.parametrize("family,option_type", [("sell_put", "put"), ("sell_call", "call")])
def test_p2_underwriting_medium_seven_percent_holds_when_thesis_and_willingness_remain_valid(
    family: str,
    option_type: str,
) -> None:
    facts = _facts(
        tier="medium",
        strategy_family=family,
        option_type=option_type,
        strategy_profile="insurance_underwriting",
        thesis_status="valid",
        continued_willingness=True,
    )

    result = evaluate_close_policy(facts, POLICY_VARIANT_P2_PROFILE_AWARE)

    assert result.recommendation_state == "hold"
    assert result.decision_basis == (
        "profit_capture_medium",
        "underwriting_thesis_valid",
        "continued_willingness_accepted",
    )


@pytest.mark.parametrize(
    ("tier", "thesis", "willingness", "expected", "evidence_status"),
    [
        ("strong", "valid", True, "close", "complete"),
        ("strong", "observe", True, "review", "review_required"),
        ("strong", "not_evaluable", True, "review", "partial"),
        ("strong", "valid", False, "review", "review_required"),
        ("strong", "valid", None, "review", "partial"),
        ("medium", "valid", True, "hold", "complete"),
        ("medium", "observe", True, "review", "review_required"),
        ("medium", "not_evaluable", True, "review", "partial"),
        ("medium", "valid", False, "review", "review_required"),
        ("medium", "valid", None, "review", "partial"),
        ("weak", "valid", True, "hold", "complete"),
        ("weak", "observe", True, "review", "review_required"),
        ("weak", "not_evaluable", True, "hold", "partial"),
        ("weak", "valid", False, "review", "review_required"),
    ],
)
def test_p2_underwriting_truth_table(
    tier: str,
    thesis: str,
    willingness: bool | None,
    expected: str,
    evidence_status: str,
) -> None:
    result = evaluate_close_policy(
        _facts(
            tier=tier,
            exit_state="hold" if tier == "none" else "profit_capture",
            strategy_profile="insurance_underwriting",
            thesis_status=thesis,
            continued_willingness=willingness,
        ),
        POLICY_VARIANT_P2_PROFILE_AWARE,
    )

    assert result.recommendation_state == expected
    assert result.decision_evidence_status == evidence_status


def test_p2_accepts_legacy_short_vol_as_underwriting_profile() -> None:
    result = evaluate_close_policy(
        _facts(tier="medium", strategy_profile="short_vol"),
        POLICY_VARIANT_P2_PROFILE_AWARE,
    )

    assert result.recommendation_state == "hold"


@pytest.mark.parametrize(
    ("field", "value", "expected", "basis"),
    [
        ("fee_calc_status", "unavailable", "not_evaluable", "fee_evidence_unusable"),
        ("estimated_pnl_if_close_net", None, "not_evaluable", "net_close_pnl_missing"),
        ("estimated_pnl_if_close_net", 0.0, "hold", "net_close_pnl_non_positive"),
        ("evaluation_status", "quote_unusable", "not_evaluable", "execution_evidence_not_evaluable"),
    ],
)
def test_common_gates_fail_closed(field: str, value: object, expected: str, basis: str) -> None:
    result = evaluate_close_policy(
        replace(_facts(), **{field: value}),
        POLICY_VARIANT_P2_PROFILE_AWARE,
    )

    assert result.recommendation_state == expected
    assert basis in result.decision_basis


def test_combo_incomplete_evidence_downgrades_only_close_to_review() -> None:
    close_result = evaluate_close_policy(
        _facts(
            strategy_profile="insurance_underwriting",
            combo_evidence_status="review_required",
        ),
        POLICY_VARIANT_P2_PROFILE_AWARE,
    )
    hold_result = evaluate_close_policy(
        _facts(
            tier="medium",
            strategy_profile="insurance_underwriting",
            combo_evidence_status="review_required",
        ),
        POLICY_VARIANT_P2_PROFILE_AWARE,
    )

    assert close_result.recommendation_state == "review"
    assert "combo_evidence_incomplete" in close_result.decision_basis
    assert hold_result.recommendation_state == "hold"


def test_p2_rejects_mismatched_strategy_family() -> None:
    result = evaluate_close_policy(
        _facts(strategy_family="sell_call", option_type="put"),
        POLICY_VARIANT_P2_PROFILE_AWARE,
    )

    assert result.recommendation_state == "not_evaluable"
    assert result.decision_basis == ("strategy_family_mismatch",)


@pytest.mark.parametrize(
    ("exit_state", "tier", "net_pnl", "expected"),
    [
        ("take_profit", "strong", 50.0, "close"),
        ("hold", "none", -20.0, "hold"),
        ("salvage", "optional", -20.0, "close"),
        ("let_expire", "none", -20.0, "hold"),
    ],
)
def test_long_call_behavior_is_unchanged_across_shadow_variants(
    exit_state: str,
    tier: str,
    net_pnl: float,
    expected: str,
) -> None:
    facts = _facts(
        side="long",
        option_type="call",
        strategy_family="combo_yield",
        strategy_profile="vol_convexity_enhancement",
        exit_state=exit_state,
        tier=tier,
        estimated_pnl_if_close_net=net_pnl,
    )

    assert {
        evaluate_close_policy(facts, variant).recommendation_state
        for variant in (
            POLICY_VARIANT_P0_CURRENT,
            POLICY_VARIANT_P1_SEMANTIC_SPLIT,
            POLICY_VARIANT_P2_PROFILE_AWARE,
        )
    } == {expected}


def test_p3_is_application_only_and_never_upgrades_p2_hold_to_close() -> None:
    p2_close = evaluate_close_policy(
        _facts(strategy_profile="insurance_underwriting"),
        POLICY_VARIANT_P2_PROFILE_AWARE,
    )
    p2_hold = evaluate_close_policy(
        _facts(tier="medium", strategy_profile="insurance_underwriting"),
        POLICY_VARIANT_P2_PROFILE_AWARE,
    )

    close_with_switch = compose_opportunity_required_policy(
        p2_close,
        ReallocationPolicyEvidence(status="review_switch"),
    )
    close_without_switch = compose_opportunity_required_policy(
        p2_close,
        ReallocationPolicyEvidence(status="hold_more_efficient"),
    )
    hold_with_switch = compose_opportunity_required_policy(
        p2_hold,
        ReallocationPolicyEvidence(status="review_switch"),
    )
    close_without_evidence = compose_opportunity_required_policy(
        p2_close,
        ReallocationPolicyEvidence(status="not_evaluable"),
    )

    assert close_with_switch.recommendation_state == "close"
    assert close_without_switch.recommendation_state == "hold"
    assert hold_with_switch.recommendation_state == "review"
    assert close_without_evidence.recommendation_state == "not_evaluable"
    assert all(
        result.policy_version == POLICY_VARIANT_P3_OPPORTUNITY_REQUIRED
        for result in (close_with_switch, close_without_switch, hold_with_switch)
    )
    with pytest.raises(ValueError, match="unsupported domain close policy variant"):
        evaluate_close_policy(_facts(), POLICY_VARIANT_P3_OPPORTUNITY_REQUIRED)


def test_shadow_adapter_normalizes_facts_and_emits_all_named_variants() -> None:
    row = {
        "tier": "medium",
        "exit_state": "profit_capture",
        "position_side": "short",
        "option_type": "put",
        "strategy_family": "sell_put",
        "strategy_profile": "insurance_underwriting",
        "evaluation_status": "priced",
        "fee_calc_status": "schedule_estimate",
        "estimated_pnl_if_close_net": "80",
        "short_vol_thesis_status": "valid",
        "continued_willingness": "true",
        "close_calibration_status": "partial",
        "remaining_annualized_return": 0.07,
    }

    facts = close_decision_facts_from_row(row)
    variants = evaluate_shadow_close_policy_variants(
        row,
        reallocation_row={"reallocation_status": "review_switch"},
    )

    assert facts.estimated_pnl_if_close_net == 80.0
    assert facts.continued_willingness is True
    assert variants[POLICY_VARIANT_P0_CURRENT].recommendation_state == "close"
    assert variants[POLICY_VARIANT_P1_SEMANTIC_SPLIT].recommendation_state == "review"
    assert variants[POLICY_VARIANT_P2_PROFILE_AWARE].recommendation_state == "hold"
    assert variants[POLICY_VARIANT_P3_OPPORTUNITY_REQUIRED].recommendation_state == "review"


def test_shadow_policy_dataclasses_are_immutable_and_runtime_has_no_variant_selector() -> None:
    facts = _facts()
    with pytest.raises(FrozenInstanceError):
        facts.tier = "none"  # type: ignore[misc]

    source = inspect.getsource(close_advice_runner)
    assert "close_decision_policy" not in source
    assert "P1_semantic_split" not in source
    assert "P2_profile_aware" not in source
    assert "P3_opportunity_required" not in source
