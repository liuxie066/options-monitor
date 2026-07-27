from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal
from statistics import median
from typing import Any

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.position_advice import decimal_value
from domain.domain.position_advice_authority import PROMOTABLE_STRATEGY_FAMILIES


PROMOTION_EVIDENCE_SCHEMA = "position_advice_promotion_evidence.v1"
PROMOTION_GATE_SCHEMA = "position_advice_promotion_gate.v1"
MIN_DISTINCT_SESSIONS = 10
MIN_ELAPSED_DAYS = 14
MIN_ELIGIBLE_EVALUATIONS = 30
MIN_REPLACEMENT_OPPORTUNITIES = 10
MIN_SELECTED_PROPOSALS = 5
MIN_CAPACITY_DEFERRED_SELECTED = 3
MIN_FAMILY_OPPORTUNITIES = 5
MIN_FAMILY_SELECTED = 1

SAFETY_METRICS = (
    "false_assignment_confirmation",
    "invalid_combo_continuation",
    "stale_or_incomplete_actionable_exposure",
    "allocator_invariant_violation",
    "authority_mixed_exposure",
    "lifecycle_or_identity_conflict_actionable",
)
REQUIRED_CRITICAL_REPLAY_FIXTURES = frozenset(
    {
        "put_release",
        "call_release",
        "capacity_and_invariant_reject",
        "partial_lifecycle",
        "stale_source",
        "authority_cas_conflict",
        "combo_decomposition",
    }
)


def unique_decision_opportunity_key(
    *,
    portfolio_scope_id: str,
    source_position_ids: list[str] | tuple[str, ...],
    candidate_id: str,
    decision_state_fingerprint: str,
    source_manifest_hash: str,
    economic_inputs_hash: str,
) -> str:
    return canonical_sha256(
        {
            "portfolio_scope_id": str(portfolio_scope_id),
            "source_position_ids": sorted(str(item) for item in source_position_ids),
            "candidate_id": str(candidate_id),
            "decision_state_fingerprint": str(decision_state_fingerprint),
            "source_manifest_hash": str(source_manifest_hash),
            "economic_inputs_hash": str(economic_inputs_hash),
        }
    )


def _opportunity_gate_signature(item: dict[str, Any]) -> str:
    return canonical_sha256(
        {
            "eligible": item.get("eligible"),
            "replacement_opportunity": item.get("replacement_opportunity"),
            "selected": item.get("selected"),
            "replacement_eligibility": item.get("replacement_eligibility"),
            "receipt_complete": item.get("receipt_complete"),
            "fresh": item.get("fresh"),
            "authority_mode": item.get("authority_mode"),
            "strategy_family": item.get("strategy_family"),
            "pool_efficiency_improvement": item.get("pool_efficiency_improvement"),
            "outcome_reason": item.get("outcome_reason"),
        }
    )


def evaluate_promotion_gate(evidence: dict[str, Any]) -> dict[str, Any]:
    payload = dict(evidence or {})
    reasons: list[str] = []
    if payload.get("schema_version") != PROMOTION_EVIDENCE_SCHEMA:
        reasons.append("promotion_evidence_schema_invalid")
    if payload.get("authority_mode") != "v2_shadow":
        reasons.append("promotion_evidence_not_v2_shadow")
    safety = dict(payload.get("safety") or {})
    for metric in SAFETY_METRICS:
        value = safety.get(metric)
        if value is None:
            reasons.append(f"safety_unknown:{metric}")
            continue
        try:
            if isinstance(value, bool) or int(value) != value or int(value) < 0:
                raise ValueError
            if int(value) != 0:
                reasons.append(f"safety_nonzero:{metric}")
        except (TypeError, ValueError, OverflowError):
            reasons.append(f"safety_invalid:{metric}")

    sessions = {str(item) for item in payload.get("market_session_ids") or [] if str(item)}
    if len(sessions) < MIN_DISTINCT_SESSIONS:
        reasons.append("distinct_sessions_insufficient")
    try:
        first = datetime.fromisoformat(str(payload.get("first_opportunity_at") or ""))
        last = datetime.fromisoformat(str(payload.get("last_opportunity_at") or ""))
        if first.tzinfo is None or last.tzinfo is None:
            raise ValueError
        if (last - first).total_seconds() < MIN_ELAPSED_DAYS * 86400:
            reasons.append("elapsed_days_insufficient")
    except (TypeError, ValueError, OverflowError):
        reasons.append("observation_window_unknown")

    unique: dict[str, dict[str, Any]] = {}
    signatures: dict[str, str] = {}
    for item in payload.get("opportunities") or []:
        if not isinstance(item, dict):
            reasons.append("opportunity_invalid")
            continue
        key = str(item.get("opportunity_key") or "").strip()
        if not key:
            reasons.append("opportunity_key_missing")
            continue
        signature = _opportunity_gate_signature(item)
        if key in signatures and signatures[key] != signature:
            reasons.append("duplicate_opportunity_conflict")
            continue
        signatures[key] = signature
        unique.setdefault(key, dict(item))
    opportunities = list(unique.values())
    if any(item.get("authority_mode") != "v2_shadow" for item in opportunities):
        reasons.append("opportunity_not_v2_shadow")
    if any(item.get("fresh") is not True for item in opportunities):
        reasons.append("opportunity_not_fresh")
    eligible = [item for item in opportunities if item.get("eligible") is True]
    replacements = [item for item in opportunities if item.get("replacement_opportunity") is True]
    selected = [item for item in opportunities if item.get("selected") is True]
    deferred_selected = [
        item
        for item in selected
        if item.get("replacement_eligibility") == "capacity_deferred_to_allocator"
    ]
    if len(eligible) < MIN_ELIGIBLE_EVALUATIONS:
        reasons.append("eligible_evaluations_insufficient")
    if len(replacements) < MIN_REPLACEMENT_OPPORTUNITIES:
        reasons.append("replacement_opportunities_insufficient")
    if len(selected) < MIN_SELECTED_PROPOSALS:
        reasons.append("selected_proposals_insufficient")
    if len(deferred_selected) < MIN_CAPACITY_DEFERRED_SELECTED:
        reasons.append("capacity_deferred_selected_insufficient")
    if selected and any(item.get("receipt_complete") is not True for item in selected):
        reasons.append("producer_receipt_completeness_failed")
    if not selected:
        reasons.append("selected_population_empty")

    covered_families = {
        str(item) for item in payload.get("covered_strategy_families") or [] if str(item)
    }
    if not covered_families:
        reasons.append("covered_strategy_families_empty")
    if any(item not in PROMOTABLE_STRATEGY_FAMILIES for item in covered_families):
        reasons.append("covered_strategy_family_unsupported")
    if any(item.get("strategy_family") not in covered_families for item in selected):
        reasons.append("selected_strategy_family_uncovered")
    for family in sorted(covered_families):
        family_opportunities = [item for item in opportunities if item.get("strategy_family") == family]
        family_selected = [item for item in family_opportunities if item.get("selected")]
        if len(family_opportunities) < MIN_FAMILY_OPPORTUNITIES:
            reasons.append(f"strategy_family_opportunities_insufficient:{family}")
        if len(family_selected) < MIN_FAMILY_SELECTED:
            reasons.append(f"strategy_family_selected_insufficient:{family}")

    critical_replay = dict(payload.get("critical_replay_fixtures") or {})
    if any(critical_replay.get(name) is not True for name in REQUIRED_CRITICAL_REPLAY_FIXTURES):
        reasons.append("critical_replay_incomplete")

    economic = dict(payload.get("economic") or {})
    for field in (
        "modeled_daily_carry_uplift_base_cny",
        "aggregate_net_carry_improvement_H_base_cny",
    ):
        try:
            if decimal_value(economic.get(field), field=field) <= 0:
                reasons.append(f"economic_nonpositive:{field}")
        except ValueError:
            reasons.append(f"economic_unknown:{field}")
    pool_efficiencies = list(economic.get("pool_efficiencies") or [])
    if not pool_efficiencies:
        reasons.append("pool_efficiency_population_empty")
    for item in pool_efficiencies:
        try:
            before = decimal_value(item.get("before"), field="pool efficiency before")
            after = decimal_value(item.get("after"), field="pool efficiency after")
            units_before = decimal_value(
                item.get("resource_units_before"),
                field="pool resource units before",
                positive=True,
            )
            units_after = decimal_value(
                item.get("resource_units_after"),
                field="pool resource units after",
                positive=True,
            )
            if units_before <= 0 or units_after <= 0:
                raise ValueError
            if after <= before:
                reasons.append(f"pool_efficiency_not_improved:{item.get('pool_key')}")
        except (AttributeError, ValueError):
            reasons.append("pool_efficiency_unknown")
    improvements: list[Decimal] = []
    for item in selected:
        try:
            improvements.append(
                decimal_value(item.get("pool_efficiency_improvement"), field="pool efficiency improvement")
            )
        except ValueError:
            reasons.append("selected_efficiency_unknown")
    if not improvements:
        reasons.append("selected_efficiency_population_empty")
    elif median(improvements) <= 0:
        reasons.append("selected_median_efficiency_not_positive")

    reason_distribution = Counter(
        str(item.get("outcome_reason") or "unknown") for item in opportunities
    )
    unique_reasons = sorted(set(reasons))
    status = "pass" if not unique_reasons else "insufficient_evidence"
    gate = {
        "schema_version": PROMOTION_GATE_SCHEMA,
        "status": status,
        "reason_codes": unique_reasons,
        "unique_opportunity_count": len(opportunities),
        "eligible_evaluation_count": len(eligible),
        "replacement_opportunity_count": len(replacements),
        "selected_proposal_count": len(selected),
        "capacity_deferred_selected_count": len(deferred_selected),
        "distinct_market_session_count": len(sessions),
        "covered_strategy_families": sorted(covered_families),
        "reason_distribution": dict(sorted(reason_distribution.items())),
    }
    return {**gate, "gate_hash": canonical_sha256(gate)}


__all__ = [
    "MIN_CAPACITY_DEFERRED_SELECTED",
    "MIN_DISTINCT_SESSIONS",
    "MIN_ELAPSED_DAYS",
    "MIN_ELIGIBLE_EVALUATIONS",
    "MIN_FAMILY_OPPORTUNITIES",
    "MIN_FAMILY_SELECTED",
    "MIN_REPLACEMENT_OPPORTUNITIES",
    "MIN_SELECTED_PROPOSALS",
    "PROMOTION_EVIDENCE_SCHEMA",
    "PROMOTION_GATE_SCHEMA",
    "REQUIRED_CRITICAL_REPLAY_FIXTURES",
    "SAFETY_METRICS",
    "evaluate_promotion_gate",
    "unique_decision_opportunity_key",
]
