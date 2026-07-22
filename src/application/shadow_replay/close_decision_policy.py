from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from domain.domain.close_advice import (
    DECISION_EVIDENCE_COMPLETE,
    DECISION_EVIDENCE_NOT_EVALUABLE,
    DECISION_EVIDENCE_REVIEW_REQUIRED,
    POLICY_VARIANT_P0_CURRENT,
    POLICY_VARIANT_P1_SEMANTIC_SPLIT,
    POLICY_VARIANT_P2_PROFILE_AWARE,
    POLICY_VARIANT_P3_OPPORTUNITY_REQUIRED,
    RECOMMENDATION_CLOSE,
    RECOMMENDATION_HOLD,
    RECOMMENDATION_NOT_EVALUABLE,
    RECOMMENDATION_REVIEW,
    CloseDecisionFacts,
    ClosePolicyResult,
    evaluate_close_policy,
    safe_float,
)


SHADOW_CLOSE_POLICY_VARIANTS = (
    POLICY_VARIANT_P0_CURRENT,
    POLICY_VARIANT_P1_SEMANTIC_SPLIT,
    POLICY_VARIANT_P2_PROFILE_AWARE,
    POLICY_VARIANT_P3_OPPORTUNITY_REQUIRED,
)


@dataclass(frozen=True)
class ReallocationPolicyEvidence:
    status: str
    reason: str = ""


def close_decision_facts_from_row(row: dict[str, Any]) -> CloseDecisionFacts:
    return CloseDecisionFacts(
        tier=_text(row.get("tier")),
        exit_state=_text(row.get("exit_state")),
        side=_text(row.get("position_side") or row.get("side")),
        option_type=_text(row.get("option_type")),
        strategy_family=_text(row.get("strategy_family")),
        strategy_profile=_text(row.get("strategy_profile")),
        evaluation_status=_text(row.get("evaluation_status")),
        fee_calc_status=_text(row.get("fee_calc_status")),
        estimated_pnl_if_close_net=safe_float(row.get("estimated_pnl_if_close_net")),
        thesis_status=_text(row.get("short_vol_thesis_status") or row.get("thesis_status")),
        continued_willingness=_optional_bool(row.get("continued_willingness")),
        close_calibration_status=_text(row.get("close_calibration_status")),
        combo_evidence_status=_combo_evidence_status(row),
    )


def reallocation_evidence_from_row(row: dict[str, Any] | None) -> ReallocationPolicyEvidence:
    source = row if isinstance(row, dict) else {}
    return ReallocationPolicyEvidence(
        status=_text(source.get("reallocation_status")) or "not_evaluable",
        reason=_text(source.get("reallocation_reason")),
    )


def evaluate_shadow_close_policy_variants(
    row: dict[str, Any],
    *,
    reallocation_row: dict[str, Any] | None = None,
) -> dict[str, ClosePolicyResult]:
    facts = close_decision_facts_from_row(row)
    p0 = evaluate_close_policy(facts, POLICY_VARIANT_P0_CURRENT)
    p1 = evaluate_close_policy(facts, POLICY_VARIANT_P1_SEMANTIC_SPLIT)
    p2 = evaluate_close_policy(facts, POLICY_VARIANT_P2_PROFILE_AWARE)
    p3 = compose_opportunity_required_policy(
        p2,
        reallocation_evidence_from_row(reallocation_row),
    )
    return {
        POLICY_VARIANT_P0_CURRENT: p0,
        POLICY_VARIANT_P1_SEMANTIC_SPLIT: p1,
        POLICY_VARIANT_P2_PROFILE_AWARE: p2,
        POLICY_VARIANT_P3_OPPORTUNITY_REQUIRED: p3,
    }


def compose_opportunity_required_policy(
    base_result: ClosePolicyResult,
    evidence: ReallocationPolicyEvidence,
) -> ClosePolicyResult:
    """Compose the offline-only P3 result without feeding it back to runtime."""

    if base_result.policy_version != POLICY_VARIANT_P2_PROFILE_AWARE:
        raise ValueError("P3 composition requires a P2_profile_aware base result")
    if base_result.recommendation_state == RECOMMENDATION_NOT_EVALUABLE:
        return _p3_result(base_result, RECOMMENDATION_NOT_EVALUABLE)

    status = _text(evidence.status)
    if status == "review_switch":
        recommendation = (
            RECOMMENDATION_CLOSE
            if base_result.recommendation_state == RECOMMENDATION_CLOSE
            else RECOMMENDATION_REVIEW
        )
        return _p3_result(
            base_result,
            recommendation,
            "replacement_opportunity_feasible",
            evidence_status=(
                DECISION_EVIDENCE_COMPLETE
                if recommendation == RECOMMENDATION_CLOSE
                else DECISION_EVIDENCE_REVIEW_REQUIRED
            ),
        )
    if base_result.recommendation_state != RECOMMENDATION_CLOSE:
        return _p3_result(base_result, base_result.recommendation_state)
    if status in {
        "hold_more_efficient",
        "no_feasible_replacement",
        "exit_without_replacement",
    }:
        return _p3_result(
            base_result,
            RECOMMENDATION_HOLD,
            "replacement_opportunity_required_not_met",
        )
    return _p3_result(
        base_result,
        RECOMMENDATION_NOT_EVALUABLE,
        "reallocation_evidence_inconclusive",
        evidence_status=DECISION_EVIDENCE_NOT_EVALUABLE,
    )


def _p3_result(
    base: ClosePolicyResult,
    recommendation: str,
    *extra_basis: str,
    evidence_status: str | None = None,
) -> ClosePolicyResult:
    basis = tuple(dict.fromkeys((*base.decision_basis, *extra_basis)))
    return ClosePolicyResult(
        policy_version=POLICY_VARIANT_P3_OPPORTUNITY_REQUIRED,
        recommendation_state=recommendation,
        decision_basis=basis,
        decision_evidence_status=evidence_status or base.decision_evidence_status,
    )


def _combo_evidence_status(row: dict[str, Any]) -> str:
    classification = _text(row.get("combo_group_classification"))
    if not classification:
        return "not_applicable"
    group_status = _text(row.get("combo_group_status"))
    quote_status = _text(row.get("combo_group_quote_status"))
    issues = _text(row.get("combo_group_issues"))
    if (
        classification == "active_combo"
        and group_status == "evaluable"
        and quote_status == "priced"
        and not issues
    ):
        return "complete"
    return "review_required"


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    token = _text(value)
    if token in {"true", "1", "yes"}:
        return True
    if token in {"false", "0", "no"}:
        return False
    return None


def _text(value: Any) -> str:
    return str(value or "").strip().lower()
