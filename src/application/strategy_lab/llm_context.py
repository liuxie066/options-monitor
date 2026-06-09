from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.application.shadow_replay.common import resolve_output_path, safety_payload, text, utc_now, write_json


LLM_CONTEXT_SCHEMA_VERSION = "strategy_lab_llm_context.v1"

_SECRET_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "webhook",
    "cookie",
    "authorization",
    "api_key",
    "access_key",
    "refresh_token",
    "private_key",
)


def build_strategy_lab_llm_context(
    *,
    experiment: str | Path | dict[str, Any] | None = None,
    proposal: str | Path | dict[str, Any] | None = None,
    output: str | Path | None = None,
    max_rows: int = 8,
    max_samples: int = 5,
) -> dict[str, Any]:
    if experiment is None and proposal is None:
        raise ValueError("strategy lab llm-context requires --experiment or --proposal")

    experiment_payload = _load_json_artifact(
        experiment,
        names=("experiment.json", "strategy_lab_experiment.json"),
        label="strategy lab experiment",
    )
    proposal_payload = _load_json_artifact(
        proposal,
        names=("proposal.json", "strategy_lab_proposal.json"),
        label="strategy lab proposal",
    )

    result: dict[str, Any] = {
        "schema_version": LLM_CONTEXT_SCHEMA_VERSION,
        "generated_at_utc": utc_now(),
        "role": "strategy_research_assistant",
        "source": {
            "experiment": _source_ref(experiment),
            "proposal": _source_ref(proposal),
        },
        "allowed_tasks": [
            "explain_scorecard",
            "challenge_sample_bias",
            "draft_strategy_memo",
            "suggest_next_hypotheses",
            "summarize_experiment_for_human_review",
        ],
        "forbidden_actions": [
            "modify_runtime_config",
            "influence_live_scanning",
            "send_notifications",
            "write_trade_state",
            "write_broker_facing_state",
            "bypass_readiness_gate",
            "claim_optimal_parameters",
            "apply_dry_run_patch",
        ],
        "context": {
            "strategy_family_boundaries": _strategy_family_boundaries(),
            "experiment": _experiment_context(experiment_payload, max_rows=max_rows),
            "proposal": _proposal_context(proposal_payload, max_samples=max_samples),
        },
        "analysis_prompts": [
            "What evidence supports the best observed-universe variant, and what evidence weakens it?",
            "Which blockers make this experiment insufficient for production parameter changes?",
            "Are Sell Put, Covered Call, and Combo Yield being interpreted within their own strategy-family boundaries?",
            "What additional marks, outcomes, holdout windows, or group-level fields should be collected next?",
            "If a dry-run patch exists, what manual review questions must be answered before any shadow rollout?",
        ],
        "redaction": {
            "applied": True,
            "masked_key_patterns": list(_SECRET_KEY_PARTS),
            "max_scorecard_rows": max_rows,
            "max_counterexample_samples": max_samples,
        },
        "safety": {
            **safety_payload(writes_local_dataset=False),
            "runtime_config_write_allowed": False,
            "production_recommendation_allowed": False,
            "online_ai_called": False,
            "llm_can_apply_patch": False,
        },
    }
    result = _redact(result)
    if output:
        write_json(resolve_output_path(output), result)
    return result


def _load_json_artifact(
    value: str | Path | dict[str, Any] | None,
    *,
    names: tuple[str, ...],
    label: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    path = Path(value).expanduser()
    if path.is_dir():
        for name in names:
            candidate = path / name
            if candidate.exists():
                path = candidate
                break
    if not path.exists() or not path.is_file():
        raise ValueError(f"{label} not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _source_ref(value: str | Path | dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return {"type": "inline"}
    return {"type": "path", "path": str(Path(value).expanduser())}


def _experiment_context(payload: dict[str, Any] | None, *, max_rows: int) -> dict[str, Any] | None:
    if not payload:
        return None
    summary = payload.get("summary") or {}
    readiness = payload.get("readiness") or {}
    evaluation = payload.get("evaluation") or {}
    group_experiments = payload.get("group_experiments") or {}
    scorecard = payload.get("scorecard") or {}
    return {
        "schema_version": payload.get("schema_version"),
        "dataset_dir": payload.get("dataset_dir"),
        "summary": {
            "status": summary.get("status"),
            "min_sample": summary.get("min_sample"),
            "readiness_status": summary.get("readiness_status"),
            "hypothesis_status": summary.get("hypothesis_status"),
            "variant_count": summary.get("variant_count"),
            "candidate_impact_allowed": summary.get("candidate_impact_allowed"),
            "production_recommendation_allowed": summary.get("production_recommendation_allowed"),
        },
        "readiness": {
            "status": (readiness.get("summary") or {}).get("status"),
            "data_mode": (readiness.get("summary") or {}).get("data_mode"),
            "decision_instance_counts": ((readiness.get("decision_instances") or {}).get("summary") or {}).get(
                "strategy_family_counts"
            ),
            "blockers": (readiness.get("readiness") or {}).get("blockers") or [],
            "domain_readiness": (readiness.get("readiness") or {}).get("domain_readiness") or {},
        },
        "evaluation": {
            "schema_version": evaluation.get("schema_version"),
            "data_mode": evaluation.get("data_mode"),
            "universe_scope": evaluation.get("universe_scope"),
            "candidate_impact_allowed": bool(((evaluation.get("gates") or {}).get("candidate_impact") or {}).get("allowed")),
        },
        "group_experiments": {
            "combo_yield": _combo_group_experiment_context(group_experiments.get("combo_yield")),
        },
        "scorecard": {
            "status": scorecard.get("status"),
            "reason": scorecard.get("reason"),
            "optimization_claim": scorecard.get("optimization_claim"),
            "best_variant": scorecard.get("best_variant"),
            "rows": list(scorecard.get("rows") or [])[: max(0, int(max_rows))],
            "limitations": scorecard.get("limitations") or [],
        },
    }


def _proposal_context(payload: dict[str, Any] | None, *, max_samples: int) -> dict[str, Any] | None:
    if not payload:
        return None
    counterexamples = payload.get("counterexamples") or {}
    return {
        "schema_version": payload.get("schema_version"),
        "status": payload.get("status"),
        "strategy_family": payload.get("strategy_family"),
        "recommended_variant": payload.get("recommended_variant"),
        "confidence": payload.get("confidence"),
        "runtime_config_write_allowed": payload.get("runtime_config_write_allowed"),
        "production_recommendation_allowed": payload.get("production_recommendation_allowed"),
        "dry_run_patch": payload.get("dry_run_patch") or {},
        "evidence_summary": payload.get("evidence_summary") or {},
        "impact": payload.get("impact") or {},
        "counterexamples": {
            "newly_accepted_samples": list(counterexamples.get("newly_accepted_samples") or [])[
                : max(0, int(max_samples))
            ],
            "newly_rejected_samples": list(counterexamples.get("newly_rejected_samples") or [])[
                : max(0, int(max_samples))
            ],
        },
        "risks": payload.get("risks") or [],
        "limitations": payload.get("limitations") or [],
        "next_action": payload.get("next_action"),
        "safety": payload.get("safety") or {},
    }


def _strategy_family_boundaries() -> dict[str, Any]:
    return {
        "sell_put": {
            "decision_unit": "single_short_put_candidate",
            "allowed_first_stage_experiment": "single_leg_candidate_impact",
            "must_not_ignore": ["assignment_risk", "cash_efficiency", "downside_stress"],
        },
        "covered_call": {
            "decision_unit": "covered_short_call_candidate_with_holding_context",
            "allowed_first_stage_experiment": "single_leg_candidate_impact_with_coverage_context",
            "must_not_ignore": ["covered_share_availability", "cost_basis", "call_away_or_missed_upside"],
        },
        "combo_yield": {
            "decision_unit": "group_level_multi_leg_candidate",
            "allowed_first_stage_experiment": "group_level_observed_universe_optimizer",
            "must_not_ignore": ["strategy_group_id", "leg_role", "group_payoff", "funding_quality"],
            "single_leg_parameter_patch_allowed": False,
        },
    }


def _combo_group_experiment_context(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    summary = payload.get("summary") or {}
    scorecard = payload.get("scorecard") or {}
    return {
        "schema_version": payload.get("schema_version"),
        "summary": {
            "status": summary.get("status"),
            "ready_group_count": summary.get("ready_group_count"),
            "variant_count": summary.get("variant_count"),
            "optimization_claim": summary.get("optimization_claim"),
            "production_recommendation_allowed": summary.get("production_recommendation_allowed"),
        },
        "scorecard": {
            "status": scorecard.get("status"),
            "best_variant": scorecard.get("best_variant"),
            "rows": list(scorecard.get("rows") or [])[:5],
            "limitations": scorecard.get("limitations") or [],
        },
    }


def _redact(value: Any, *, key: str = "") -> Any:
    key_lower = key.lower()
    if any(part in key_lower for part in _SECRET_KEY_PARTS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _redact(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [_redact(item, key=key) for item in value]
    if isinstance(value, str):
        stripped = text(value)
        if len(stripped) > 1200:
            return f"{stripped[:1200]}...[truncated]"
        return stripped
    return value
