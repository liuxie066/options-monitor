from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from domain.domain.engine import rank_candidate_rows
from domain.domain.short_vol_assessment import calculate_option_market_concentration_after
from domain.domain.symbol_identity import OPTION_CODE_RE, resolve_symbol_identity
from src.application.opening_candidate_snapshot import ranked_opening_candidate_decisions
from src.application.strategy_lab.contracts import (
    ACCOUNT,
    MARKET,
    NEAR_RETURN_THRESHOLDS,
    RECIPE_ID,
    RESEARCH_SESSIONS,
    canonical_sha256,
)


class StrategyLabRecipeError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def describe_recipe(recipe_id: str) -> dict[str, Any]:
    if recipe_id != RECIPE_ID:
        raise StrategyLabRecipeError("recipe_unsupported", "Recipe is not supported")
    return {
        "recipe_id": RECIPE_ID,
        "name": "CSP option-position concentration",
        "question": "降低近收益候选的期权持仓市值集中度，能否改善单推荐结果？",
        "market": MARKET,
        "account": ACCOUNT,
        "strategy": "sell_put",
        "research_sessions": RESEARCH_SESSIONS,
        "near_return_thresholds": list(NEAR_RETURN_THRESHOLDS),
        "evidence": [
            "formal_corpus",
            "sealed_opening_candidate_snapshot",
            "prepared_option_position_evidence",
            "targeted_history_k_readiness",
            "terminal_fx",
            "account_fee_plan",
        ],
    }


def variant_preference(recipe_id: str) -> list[dict[str, Any]]:
    if recipe_id != RECIPE_ID:
        raise StrategyLabRecipeError("recipe_unsupported", "Recipe is not supported")
    return [
        {
            "variant_id": f"challenger_{threshold:.3f}",
            "near_return_threshold": threshold,
        }
        for threshold in NEAR_RETURN_THRESHOLDS
    ]


def _canonical_hk_put_candidate(candidate: Mapping[str, Any]) -> None:
    contract_value = candidate.get("contract_symbol")
    if not isinstance(contract_value, str):
        raise ValueError("contract_symbol is unavailable")
    contract = contract_value.strip().upper()
    match = OPTION_CODE_RE.fullmatch(contract)
    contract_identity = resolve_symbol_identity(contract)
    symbol_identity = resolve_symbol_identity(candidate.get("symbol"))
    if (
        contract_value != contract
        or match is None
        or match.group("market") != "HK"
        or match.group("cp") != "P"
        or contract_identity is None
        or contract_identity.market != "HK"
        or contract_identity.currency != "HKD"
        or symbol_identity is None
        or symbol_identity.canonical != contract_identity.canonical
        or candidate.get("symbol") != contract_identity.canonical
        or candidate.get("option_type") != "put"
        or candidate.get("currency") != "HKD"
    ):
        raise ValueError("candidate is not one canonical HK Put contract")
    try:
        code_expiration = date(
            2000 + int(match.group("yy")),
            int(match.group("mm")),
            int(match.group("dd")),
        ).isoformat()
        code_strike = Decimal(match.group("strike")) / Decimal("1000")
        candidate_strike = Decimal(str(candidate["strike"]))
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise ValueError("candidate contract identity is incomplete") from exc
    if candidate.get("expiration") != code_expiration or candidate_strike != code_strike:
        raise ValueError("candidate contract fields do not match contract_symbol")


def _candidate_rows(formal_point: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    recommendation = formal_point.get("recommendation_point")
    opening = formal_point.get("opening_snapshot")
    evidence = formal_point.get("option_position_evidence_binding")
    if not all(isinstance(value, Mapping) for value in (recommendation, opening, evidence)):
        raise StrategyLabRecipeError("recipe_evidence_incomplete", "formal point owners are incomplete")
    assert isinstance(recommendation, Mapping)
    assert isinstance(opening, Mapping)
    assert isinstance(evidence, Mapping)
    if recommendation.get("opening_snapshot_sha256") != opening.get("content_sha256"):
        raise StrategyLabRecipeError("recipe_evidence_incomplete", "formal point opening snapshot binding changed")
    accepted_ids = recommendation.get("producer_accepted_candidate_ids")
    if not isinstance(accepted_ids, list) or not accepted_ids:
        raise StrategyLabRecipeError("recipe_evidence_incomplete", "formal point has no accepted CSP candidate")
    decisions = [
        item
        for item in ranked_opening_candidate_decisions(opening)
        if item.get("strategy_mode") == "put" and (item.get("opening_decision") or {}).get("accepted") is True
    ]
    decision_ids = [item.get("candidate_id") for item in decisions]
    if len(decision_ids) != len(set(decision_ids)) or set(decision_ids) != set(accepted_ids):
        raise StrategyLabRecipeError("recipe_evidence_incomplete", "sealed accepted candidate set changed")
    rows: list[dict[str, Any]] = []
    try:
        for decision in decisions:
            normalized = decision.get("normalized_input")
            if not isinstance(normalized, Mapping):
                raise ValueError("candidate normalized input is unavailable")
            candidate = {"candidate_id": decision["candidate_id"], **dict(normalized)}
            _canonical_hk_put_candidate(candidate)
            metric = calculate_option_market_concentration_after(
                candidate=candidate,
                open_option_positions=[dict(row) for row in evidence["open_option_positions"]],
                valuation_mark_facts=[dict(row) for row in evidence["valuation_mark_facts"]],
                fx_rate_facts=[dict(row) for row in evidence["fx_rate_facts"]],
            )
            rows.append(
                {
                    **candidate,
                    "opening_snapshot_rank": decision["opening_snapshot_rank"],
                    "option_market_concentration_after": metric["option_market_concentration_after"],
                    "option_market_value_cny": metric["option_market_value_cny"],
                    "option_market_concentration_metric_version": metric["metric_version"],
                    "option_market_evidence_refs": {
                        "prepared_context_manifest_ref": recommendation["prepared_context_manifest_ref"],
                        "prepared_context_manifest_sha256": recommendation["prepared_context_manifest_sha256"],
                        "prepared_context_payload_sha256": recommendation["prepared_context_payload_sha256"],
                        **metric["evidence_refs"],
                    },
                }
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise StrategyLabRecipeError("recipe_evidence_incomplete", f"candidate evidence is incomplete: {exc}") from exc
    baseline = [row for row in rows if row["opening_snapshot_rank"] == 1]
    if len(baseline) != 1 or baseline[0]["candidate_id"] not in accepted_ids:
        raise StrategyLabRecipeError("recipe_evidence_incomplete", "sealed production rank-1 candidate is unavailable")
    return baseline[0], rows


def _arm(kind: str, candidate: Mapping[str, Any], threshold: float | None) -> dict[str, Any]:
    return {
        "arm_id": kind if threshold is None else f"{kind}_{threshold:.3f}",
        "kind": kind,
        "near_return_threshold": threshold,
        "candidate_id": candidate["candidate_id"],
        "candidate": dict(candidate),
    }


def _opening_fx_binding(formal_point: Mapping[str, Any]) -> dict[str, Any]:
    evidence = formal_point.get("option_position_evidence_binding")
    rows = evidence.get("fx_rate_facts") if isinstance(evidence, Mapping) else None
    matches = [
        dict(row)
        for row in rows or []
        if isinstance(row, Mapping) and row.get("base_currency") == "HKD" and row.get("quote_currency") == "CNY"
    ]
    if len(matches) != 1:
        raise StrategyLabRecipeError(
            "recipe_evidence_incomplete",
            "formal point must bind exactly one HKD/CNY opening FX fact",
        )
    fact = matches[0]
    fact_id = fact.get("fact_id")
    source_hash = fact.get("source_fact_sha256")
    if not isinstance(fact_id, str) or not fact_id or not isinstance(source_hash, str) or len(source_hash) != 64:
        raise StrategyLabRecipeError(
            "recipe_evidence_incomplete",
            "formal point opening FX identity is incomplete",
        )
    return {
        "fact": fact,
        "fact_ref": {"kind": "formal_point_fx_rate", "fact_id": fact_id},
        "fact_sha256": canonical_sha256(fact),
        "source_fact_sha256": source_hash,
    }


def _recommendation_available_at_utc(formal_point: Mapping[str, Any]) -> str:
    try:
        recommendation = formal_point["recommendation_point"]
        opening = formal_point["opening_snapshot"]
        coherence = recommendation["formal_point_time_coherence"]
        values = (
            formal_point["captured_at_utc"],
            formal_point["source_binding"]["scheduled_scan_target_market"],
            recommendation["scheduled_scan_target_market"],
            recommendation["decision_at_utc"],
            opening["sealed_at_utc"],
            coherence["maximum_observed_at_utc"],
        )
        parsed = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in values]
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise StrategyLabRecipeError(
            "recipe_evidence_incomplete", "formal point availability time is incomplete"
        ) from exc
    if any(value.tzinfo is None for value in parsed):
        raise StrategyLabRecipeError("recipe_evidence_incomplete", "formal point availability time lacks a timezone")
    return max(value.astimezone(timezone.utc) for value in parsed).isoformat().replace("+00:00", "Z")


def build_concentration_arms(
    formal_point: Mapping[str, Any],
    parameters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    parameters = dict(parameters or {})
    baseline, candidates = _candidate_rows(formal_point)
    arms = [_arm("baseline", baseline, None)]
    candidate_ids = {str(row["candidate_id"]) for row in candidates}
    for threshold in NEAR_RETURN_THRESHOLDS:
        ranked = rank_candidate_rows(
            candidates,
            mode="put",
            sell_put_ranking_profile="option_market_concentration",
            near_return_threshold=threshold,
        )
        if {str(row["candidate_id"]) for row in ranked} != candidate_ids:
            raise StrategyLabRecipeError("recipe_evidence_incomplete", "Recipe reranking changed the accepted set")
        arms.append(_arm("challenger", ranked[0], threshold))
    return {
        "recommendation_point_id": formal_point["recommendation_point_id"],
        "scheduled_scan_target_market": formal_point["recommendation_point"]["scheduled_scan_target_market"],
        "recommendation_available_at_utc": _recommendation_available_at_utc(formal_point),
        "formal_point_ref": parameters.get("formal_point_ref"),
        "formal_point_content_sha256": formal_point["content_sha256"],
        "formal_point_file_sha256": parameters.get("formal_point_file_sha256"),
        "source_commit_sha": formal_point["recommendation_point"]["source_commit_sha"],
        "opening_fx_binding": _opening_fx_binding(formal_point),
        "accepted_candidate_ids": sorted(candidate_ids),
        "arms": arms,
    }


def validate_recipe_leader(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StrategyLabRecipeError("validation_plan_invalid", "research leader is unavailable")
    leader = dict(value)
    threshold = leader.get("near_return_threshold")
    if (
        threshold not in NEAR_RETURN_THRESHOLDS
        or leader.get("variant_id") != f"challenger_{float(threshold):.3f}"
        or not isinstance(leader.get("comparison_sha256"), str)
        or len(leader["comparison_sha256"]) != 64
        or bool(set(leader["comparison_sha256"]) - set("0123456789abcdef"))
    ):
        raise StrategyLabRecipeError("validation_plan_invalid", "research leader is invalid")
    return leader


def project_validation_arms(formal_point: Mapping[str, Any], leader: Mapping[str, Any]) -> dict[str, Any]:
    """Project the frozen baseline and one confirmed challenger from a formal point."""

    projected = build_concentration_arms(formal_point)
    frozen_leader = validate_recipe_leader(leader)
    baseline = [arm for arm in projected["arms"] if arm["kind"] == "baseline"]
    challenger = [
        arm
        for arm in projected["arms"]
        if arm["kind"] == "challenger" and arm["near_return_threshold"] == frozen_leader["near_return_threshold"]
    ]
    if len(baseline) != 1 or len(challenger) != 1:
        raise StrategyLabRecipeError("validation_plan_invalid", "confirmed leader cannot be projected")
    return {
        key: projected[key]
        for key in (
            "recommendation_point_id",
            "scheduled_scan_target_market",
            "recommendation_available_at_utc",
            "formal_point_ref",
            "formal_point_content_sha256",
            "formal_point_file_sha256",
            "source_commit_sha",
            "opening_fx_binding",
            "accepted_candidate_ids",
        )
    } | {"arms": [baseline[0], challenger[0]]}


__all__ = [
    "StrategyLabRecipeError",
    "build_concentration_arms",
    "describe_recipe",
    "project_validation_arms",
    "validate_recipe_leader",
    "variant_preference",
]
