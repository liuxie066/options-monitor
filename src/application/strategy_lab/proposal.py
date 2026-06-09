from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.application.shadow_replay.common import resolve_output_path, safety_payload, text, utc_now, write_json


PROPOSAL_SCHEMA_VERSION = "strategy_lab_proposal.v1"


def build_strategy_lab_proposal(
    *,
    experiment: str | Path | dict[str, Any],
    output: str | Path | None = None,
    markdown_output: str | Path | None = None,
) -> dict[str, Any]:
    experiment_payload = _load_experiment(experiment)
    best, best_source = _best_variant(experiment_payload)
    evaluation = experiment_payload.get("evaluation") or {}
    variant_payload = _evaluated_variant_for_best(
        experiment_payload=experiment_payload,
        evaluation=evaluation,
        best=best or {},
        best_source=best_source,
    )
    patch_allowed = _dry_run_patch_allowed(experiment_payload=experiment_payload, best=best or {}, best_source=best_source)
    dry_run_patch = (
        _dry_run_patch(experiment_payload=experiment_payload, best=best or {}, variant=variant_payload)
        if patch_allowed
        else {}
    )
    status = _proposal_status(
        experiment_payload=experiment_payload,
        best=best,
        best_source=best_source,
        dry_run_patch=dry_run_patch,
        patch_allowed=patch_allowed,
    )
    limitations = _limitations(
        experiment_payload=experiment_payload,
        best=best or {},
        best_source=best_source,
        dry_run_patch=dry_run_patch,
        patch_allowed=patch_allowed,
    )
    result: dict[str, Any] = {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "generated_at_utc": utc_now(),
        "status": status,
        "strategy_family": (best or {}).get("strategy_family"),
        "recommended_variant": (best or {}).get("variant"),
        "confidence": _confidence(experiment_payload=experiment_payload, dry_run_patch=dry_run_patch),
        "runtime_config_write_allowed": False,
        "production_recommendation_allowed": False,
        "dry_run_patch": dry_run_patch,
        "evidence_summary": _evidence_summary(
            experiment_payload=experiment_payload,
            best=best or {},
            best_source=best_source,
        ),
        "impact": _impact(best=best or {}, variant=variant_payload, best_source=best_source),
        "counterexamples": _counterexamples(variant_payload),
        "group_advisory": _group_advisory(
            experiment_payload=experiment_payload,
            best=best or {},
            best_source=best_source,
        ),
        "risks": _risks(experiment_payload=experiment_payload, limitations=limitations),
        "limitations": limitations,
        "next_action": _next_action(status=status),
        "safety": {
            **safety_payload(writes_local_dataset=False),
            "runtime_config_write_allowed": False,
            "production_recommendation_allowed": False,
        },
    }
    result["proposal_markdown"] = _render_markdown(result)
    if output:
        write_json(resolve_output_path(output), result)
    if markdown_output:
        path = resolve_output_path(markdown_output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result["proposal_markdown"], encoding="utf-8")
    return result


def _best_variant(experiment_payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    scorecard = experiment_payload.get("scorecard") or {}
    if isinstance(scorecard, dict):
        best = scorecard.get("best_variant")
        if isinstance(best, dict):
            return best, "single_leg"
    combo = (((experiment_payload.get("group_experiments") or {}).get("combo_yield") or {}).get("scorecard") or {})
    if isinstance(combo, dict):
        best = combo.get("best_variant")
        if isinstance(best, dict):
            return best, "combo_yield_group"
    return None, "none"


def _load_experiment(experiment: str | Path | dict[str, Any]) -> dict[str, Any]:
    if isinstance(experiment, dict):
        return experiment
    path = Path(experiment).expanduser()
    if path.is_dir():
        for name in ("experiment.json", "strategy_lab_experiment.json"):
            candidate = path / name
            if candidate.exists():
                path = candidate
                break
    if not path.exists() or not path.is_file():
        raise ValueError(f"strategy lab experiment not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("strategy lab experiment must be a JSON object")
    return payload


def _proposal_status(
    *,
    experiment_payload: dict[str, Any],
    best: Any,
    best_source: str,
    dry_run_patch: dict[str, Any],
    patch_allowed: bool,
) -> str:
    experiment_status = text((experiment_payload.get("summary") or {}).get("status"))
    if not best or not isinstance(best, dict):
        return "not_ready"
    if experiment_status not in {"ready_for_scorecard_review", "ready_for_proposal"}:
        return "needs_more_evidence"
    if best_source == "combo_yield_group":
        return "data_gap_only"
    if not patch_allowed:
        return "needs_more_evidence"
    if not dry_run_patch:
        return "data_gap_only"
    score = float(best.get("objective_score") or 0.0)
    if score <= 0:
        return "no_change_recommended"
    return "shadow_rollout_candidate"


def _dry_run_patch(
    *,
    experiment_payload: dict[str, Any],
    best: dict[str, Any],
    variant: dict[str, Any] | None,
) -> dict[str, Any]:
    family = text(best.get("strategy_family"))
    if family not in {"sell_put", "covered_call"} or not variant:
        return {}
    baseline = _baseline_parameters(experiment_payload, family=family)
    parameters = variant.get("parameters") or {}
    if not isinstance(parameters, dict):
        return {}
    profile_params = parameters.get("insurance_underwriting")
    if not isinstance(profile_params, dict):
        return {}
    patch: dict[str, Any] = {}
    for key, value in sorted(profile_params.items()):
        baseline_value = baseline.get(key)
        if baseline_value is not None and _same_number(value, baseline_value):
            continue
        patch[f"{family}.insurance_underwriting.{key}"] = value
    return patch


def _dry_run_patch_allowed(*, experiment_payload: dict[str, Any], best: dict[str, Any], best_source: str) -> bool:
    if best_source != "single_leg":
        return False
    family = text(best.get("strategy_family"))
    if family not in {"sell_put", "covered_call"}:
        return False
    evaluation = experiment_payload.get("evaluation") or {}
    if text(evaluation.get("data_mode")) != "closed_replay":
        return False
    production_gate = (evaluation.get("gates") or {}).get("production_recommendation") or {}
    return bool(production_gate.get("allowed"))


def _baseline_parameters(experiment_payload: dict[str, Any], *, family: str) -> dict[str, Any]:
    hypotheses = experiment_payload.get("hypotheses") or {}
    for item in hypotheses.get("domain_hypotheses") or []:
        if item.get("strategy_family") == family:
            params = item.get("baseline_parameters")
            return params if isinstance(params, dict) else {}
    return {}


def _same_number(left: Any, right: Any) -> bool:
    try:
        return abs(float(left) - float(right)) < 0.000001
    except Exception:
        return left == right


def _evaluated_variant(evaluation: dict[str, Any], name: str) -> dict[str, Any] | None:
    if not name:
        return None
    for variant in evaluation.get("variants") or []:
        if variant.get("name") == name:
            return variant
    return None


def _evaluated_variant_for_best(
    *,
    experiment_payload: dict[str, Any],
    evaluation: dict[str, Any],
    best: dict[str, Any],
    best_source: str,
) -> dict[str, Any] | None:
    name = text(best.get("variant"))
    if best_source == "combo_yield_group":
        combo = (experiment_payload.get("group_experiments") or {}).get("combo_yield") or {}
        for variant in combo.get("variants") or []:
            if variant.get("name") == name:
                return variant
        return None
    return _evaluated_variant(evaluation, name)


def _confidence(*, experiment_payload: dict[str, Any], dry_run_patch: dict[str, Any]) -> str:
    if not dry_run_patch:
        return "low"
    evaluation = experiment_payload.get("evaluation") or {}
    data_mode = text(evaluation.get("data_mode"))
    gate_allowed = bool(((evaluation.get("gates") or {}).get("candidate_impact") or {}).get("allowed"))
    if data_mode == "closed_replay" and gate_allowed:
        return "medium"
    if gate_allowed:
        return "low"
    return "low"


def _evidence_summary(
    *,
    experiment_payload: dict[str, Any],
    best: dict[str, Any],
    best_source: str,
) -> dict[str, Any]:
    summary = experiment_payload.get("summary") or {}
    evaluation = experiment_payload.get("evaluation") or {}
    combo = (experiment_payload.get("group_experiments") or {}).get("combo_yield") or {}
    combo_summary = combo.get("summary") or {}
    return {
        "experiment_status": summary.get("status"),
        "readiness_status": summary.get("readiness_status"),
        "hypothesis_status": summary.get("hypothesis_status"),
        "data_mode": evaluation.get("data_mode"),
        "universe_scope": evaluation.get("universe_scope"),
        "variant_count": summary.get("variant_count"),
        "objective_score": best.get("objective_score"),
        "best_source": best_source,
        "optimization_claim": _optimization_claim(experiment_payload=experiment_payload, best_source=best_source),
        "combo_yield_group_optimizer_status": combo_summary.get("status"),
        "combo_yield_ready_group_count": combo_summary.get("ready_group_count"),
        "combo_yield_group_variant_count": combo_summary.get("variant_count"),
    }


def _impact(*, best: dict[str, Any], variant: dict[str, Any] | None, best_source: str) -> dict[str, Any]:
    if best_source == "combo_yield_group":
        return {
            "accepted_group_count": best.get("accepted_group_count"),
            "newly_accepted_group_count": best.get("newly_accepted_group_count"),
            "newly_rejected_group_count": best.get("newly_rejected_group_count"),
            "safety_violation_count": best.get("safety_violation_count"),
            "missing_metric_count": best.get("missing_metric_count"),
            "accepted_group_samples": list((variant or {}).get("accepted_group_samples") or [])[:10],
            "newly_accepted_group_ids": list((variant or {}).get("newly_accepted_group_ids") or [])[:20],
            "newly_rejected_group_ids": list((variant or {}).get("newly_rejected_group_ids") or [])[:20],
        }
    return {
        "candidate_count": best.get("candidate_count"),
        "newly_accepted_count": best.get("newly_accepted_count"),
        "newly_rejected_count": best.get("newly_rejected_count"),
        "safety_violation_count": best.get("safety_violation_count"),
        "missing_field_count": best.get("missing_field_count"),
        "top_reasons": (variant or {}).get("top_reasons") or {},
        "safety_reasons": (variant or {}).get("safety_reasons") or {},
    }


def _group_advisory(
    *,
    experiment_payload: dict[str, Any],
    best: dict[str, Any],
    best_source: str,
) -> dict[str, Any] | None:
    if best_source != "combo_yield_group":
        return None
    combo = (experiment_payload.get("group_experiments") or {}).get("combo_yield") or {}
    summary = combo.get("summary") or {}
    scorecard = combo.get("scorecard") or {}
    return {
        "strategy_family": "combo_yield",
        "status": summary.get("status"),
        "recommended_variant": best.get("variant"),
        "optimization_claim": summary.get("optimization_claim"),
        "ready_group_count": summary.get("ready_group_count"),
        "variant_count": summary.get("variant_count"),
        "scorecard_status": scorecard.get("status"),
        "limitations": scorecard.get("limitations") or [],
        "dry_run_patch_allowed": False,
    }


def _counterexamples(variant: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "newly_rejected_samples": list((variant or {}).get("newly_rejected_samples") or [])[:10],
        "newly_accepted_samples": list((variant or {}).get("newly_accepted_samples") or [])[:10],
    }


def _risks(*, experiment_payload: dict[str, Any], limitations: list[str]) -> list[str]:
    risks = [
        "manual_review_required_before_shadow_rollout",
        "runtime_config_not_modified",
    ]
    evaluation = experiment_payload.get("evaluation") or {}
    data_mode = text(evaluation.get("data_mode"))
    if data_mode != "closed_replay":
        risks.append("closed_replay_outcome_missing")
    risks.extend(limitations)
    out: list[str] = []
    for risk in risks:
        if risk and risk not in out:
            out.append(risk)
    return out


def _optimization_claim(*, experiment_payload: dict[str, Any], best_source: str) -> Any:
    if best_source == "combo_yield_group":
        combo = (experiment_payload.get("group_experiments") or {}).get("combo_yield") or {}
        return (combo.get("scorecard") or {}).get("optimization_claim") or (combo.get("summary") or {}).get(
            "optimization_claim"
        )
    return (experiment_payload.get("scorecard") or {}).get("optimization_claim")


def _patch_blocker(*, experiment_payload: dict[str, Any], best: dict[str, Any], best_source: str) -> str | None:
    if best_source == "combo_yield_group":
        return "combo_yield_group_optimizer_does_not_emit_single_leg_patch"
    if text(best.get("strategy_family")) not in {"sell_put", "covered_call"}:
        return "unsupported_strategy_family_for_patch"
    evaluation = experiment_payload.get("evaluation") or {}
    if text(evaluation.get("data_mode")) != "closed_replay":
        return "closed_replay_outcome_required_for_patch"
    production_gate = (evaluation.get("gates") or {}).get("production_recommendation") or {}
    if not bool(production_gate.get("allowed")):
        return text(production_gate.get("reason")) or "production_recommendation_gate_not_ready"
    return None


def _limitations(
    *,
    experiment_payload: dict[str, Any],
    best: dict[str, Any],
    best_source: str,
    dry_run_patch: dict[str, Any],
    patch_allowed: bool,
) -> list[str]:
    scorecard = experiment_payload.get("scorecard") or {}
    limitations = list(scorecard.get("limitations") or [])
    if best_source == "combo_yield_group":
        combo = (experiment_payload.get("group_experiments") or {}).get("combo_yield") or {}
        limitations.extend((combo.get("scorecard") or {}).get("limitations") or [])
        limitations.append("combo_yield_group_advisory_only")
    limitations.extend(
        [
            "proposal_is_advisory_only",
            "dry_run_patch_not_applied",
            "observed_universe_only",
        ]
    )
    if not patch_allowed:
        blocker = _patch_blocker(experiment_payload=experiment_payload, best=best, best_source=best_source)
        if blocker:
            limitations.append(blocker)
    if not dry_run_patch:
        limitations.append("no_supported_single_leg_patch")
    out: list[str] = []
    for item in limitations:
        item_text = text(item)
        if item_text and item_text not in out:
            out.append(item_text)
    return out


def _next_action(*, status: str) -> str:
    if status == "shadow_rollout_candidate":
        return "human_review_then_optional_shadow_rollout"
    if status == "no_change_recommended":
        return "keep_current_parameters_and_collect_more_evidence"
    if status == "needs_more_evidence":
        return "collect_mark_outcomes_before_proposal"
    if status == "data_gap_only":
        return "collect_group_or_parameter_evidence_before_proposal"
    return "run_strategy_lab_experiment_after_readiness"


def _render_markdown(proposal: dict[str, Any]) -> str:
    impact = proposal.get("impact") or {}
    dry_run_patch = proposal.get("dry_run_patch") or {}
    lines = [
        "# Strategy Lab Proposal",
        "",
        f"- Status: {proposal.get('status')}",
        f"- Strategy family: {proposal.get('strategy_family')}",
        f"- Variant: {proposal.get('recommended_variant')}",
        f"- Confidence: {proposal.get('confidence')}",
        f"- Runtime config write allowed: {proposal.get('runtime_config_write_allowed')}",
        "",
        "## Impact",
        "",
        f"- Newly accepted: {impact.get('newly_accepted_count', impact.get('newly_accepted_group_count'))}",
        f"- Newly rejected: {impact.get('newly_rejected_count', impact.get('newly_rejected_group_count'))}",
        f"- Safety violations: {impact.get('safety_violation_count')}",
        "",
        "## Dry-run Patch",
        "",
    ]
    if dry_run_patch:
        lines.extend(f"- `{key}` = `{value}`" for key, value in sorted(dry_run_patch.items()))
    else:
        lines.append("- No supported dry-run patch.")
    lines.extend(
        [
            "",
            "## Limitations",
            "",
            *[f"- {item}" for item in proposal.get("limitations") or []],
            "",
            f"Next action: {proposal.get('next_action')}",
            "",
        ]
    )
    return "\n".join(lines)
