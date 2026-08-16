from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from src.application.shadow_replay.common import (
    attach_artifact_provenance,
    dataset_dir_from_arg,
    resolve_output_path,
    safety_payload,
    text,
    utc_now,
    validate_artifact_provenance,
    validate_dataset_integrity,
    write_json,
    write_text_artifact,
)
from src.application.shadow_replay.parameter_sets import EXPERIMENT_ONLY_PARAMETERS
from src.application.strategy_lab.experiment import (
    EXPERIMENT_SCHEMA_VERSION,
    run_strategy_lab_experiment,
)


PROPOSAL_SCHEMA_VERSION = "strategy_lab_proposal.v1"


def build_strategy_lab_proposal(
    *,
    experiment: str | Path | dict[str, Any],
    output: str | Path | None = None,
    markdown_output: str | Path | None = None,
) -> dict[str, Any]:
    experiment_payload = _load_experiment(experiment)
    experiment_validation = validate_artifact_provenance(
        experiment_payload,
        artifact_kind="strategy_lab_experiment",
        schema_version=EXPERIMENT_SCHEMA_VERSION,
    )
    source_errors = _experiment_source_errors(
        experiment,
        validation=experiment_validation,
    )
    if source_errors:
        experiment_validation["errors"] = list(experiment_validation["errors"]) + source_errors
        experiment_validation["trusted"] = False
    best, best_source = (
        _best_variant(experiment_payload)
        if experiment_validation["trusted"]
        else (None, "untrusted_experiment")
    )
    evaluation = experiment_payload.get("evaluation") or {}
    variant_payload = _evaluated_variant_for_best(
        evaluation=evaluation,
        best=best or {},
    )
    patch_allowed = _dry_run_patch_allowed(
        experiment_payload=experiment_payload,
        best=best or {},
        best_source=best_source,
        variant=variant_payload,
    )
    if patch_allowed:
        semantic_errors = _experiment_semantic_errors(
            experiment_payload,
            validation=experiment_validation,
        )
        if semantic_errors:
            experiment_validation["errors"] = (
                list(experiment_validation["errors"]) + semantic_errors
            )
            experiment_validation["trusted"] = False
            best = None
            best_source = "untrusted_experiment"
            variant_payload = None
            patch_allowed = False
    dry_run_patch = (
        _dry_run_patch(experiment_payload=experiment_payload, best=best or {}, variant=variant_payload)
        if patch_allowed
        else {}
    )
    status = (
        _proposal_status(
        experiment_payload=experiment_payload,
        best=best,
        best_source=best_source,
        dry_run_patch=dry_run_patch,
        patch_allowed=patch_allowed,
        )
        if experiment_validation["trusted"]
        else "display_only_untrusted"
    )
    limitations = _limitations(
        experiment_payload=experiment_payload,
        best=best or {},
        best_source=best_source,
        dry_run_patch=dry_run_patch,
        patch_allowed=patch_allowed,
        variant=variant_payload,
    )
    result: dict[str, Any] = {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "generated_at_utc": utc_now(),
        "status": status,
        "strategy_family": (
            (best or {}).get("strategy_family")
            or ("combo_yield" if best_source == "combo_yield_group" else None)
        ),
        "recommended_variant": (best or {}).get("variant"),
        "confidence": _confidence(experiment_payload=experiment_payload, dry_run_patch=dry_run_patch),
        "runtime_config_write_allowed": False,
        "production_recommendation_allowed": False,
        "dry_run_patch": dry_run_patch,
        "artifact_validation": {
            "experiment": experiment_validation,
        },
        "source_artifacts": {
            "experiment": {
                "artifact_id": experiment_validation.get("artifact_id"),
                "content_sha256": experiment_validation.get("content_sha256"),
            }
        },
        "evidence_summary": _evidence_summary(
            experiment_payload=experiment_payload,
            best_source=best_source,
        ),
        "impact": _impact(best=best or {}, variant=variant_payload),
        "counterexamples": _counterexamples(variant_payload),
        "group_advisory": _group_advisory(
            experiment_payload=experiment_payload,
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
    attach_artifact_provenance(
        result,
        artifact_kind="strategy_lab_proposal",
        source_generation=experiment_validation.get("source_generation") or {},
    )
    if output:
        write_json(resolve_output_path(output), result)
    if markdown_output:
        path = resolve_output_path(markdown_output)
        write_text_artifact(path, result["proposal_markdown"])
    return result


def _best_variant(experiment_payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    scorecard = experiment_payload.get("scorecard") or {}
    if isinstance(scorecard, dict) and text(scorecard.get("best_variant_basis")) == "strict_outcome_dominance":
        best = scorecard.get("best_variant")
        if isinstance(best, dict):
            return best, "single_leg"
    combo_summary = (experiment_payload.get("group_experiments") or {}).get("combo_yield") or {}
    single_leg_rows = (scorecard.get("rows") or []) if isinstance(scorecard, dict) else []
    if not single_leg_rows and int((combo_summary.get("summary") or {}).get("group_count") or 0) > 0:
        return None, "combo_yield_group"
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


def _experiment_source_errors(
    experiment: str | Path | dict[str, Any],
    *,
    validation: dict[str, Any],
) -> list[str]:
    if isinstance(experiment, dict):
        return ["inline_experiment_is_display_only"]
    source_generation = validation.get("source_generation")
    source_generation = (
        source_generation if isinstance(source_generation, dict) else {}
    )
    dataset_value = source_generation.get("dataset_dir")
    if not dataset_value:
        return ["source_dataset_missing"]
    generation_ref = source_generation.get("generation_ref")
    if isinstance(generation_ref, dict):
        from src.application.shadow_replay.generations import (
            ResearchGenerationError,
            resolve_dataset_generation,
        )

        try:
            resolved = resolve_dataset_generation(
                dataset_dir_from_arg(dataset_value),
                generation_ref,
            )
        except (OSError, ValueError, ResearchGenerationError):
            return ["source_dataset_generation_unavailable"]
        if resolved.get("generation_id") != source_generation.get("generation_id"):
            return ["source_dataset_generation_mismatch"]
        return []
    try:
        current = validate_dataset_integrity(dataset_dir_from_arg(dataset_value))
    except ValueError:
        return ["source_dataset_integrity_invalid"]
    if current.get("generation_id") != source_generation.get("generation_id"):
        return ["source_dataset_generation_mismatch"]
    if current.get("revision") != source_generation.get("revision"):
        return ["source_dataset_revision_mismatch"]
    return []


def _experiment_semantic_errors(
    payload: dict[str, Any],
    *,
    validation: dict[str, Any],
) -> list[str]:
    source_generation = validation.get("source_generation")
    source_generation = (
        source_generation if isinstance(source_generation, dict) else {}
    )
    dataset_value = text(source_generation.get("dataset_dir"))
    repo_root_value = text(source_generation.get("repo_root"))
    if not dataset_value:
        return ["source_dataset_missing_for_gate_recompute"]
    if not repo_root_value:
        return ["source_repo_root_missing_for_gate_recompute"]
    summary = payload.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    try:
        min_sample = max(1, int(summary.get("min_sample") or 30))
    except (TypeError, ValueError):
        return ["experiment_min_sample_invalid"]
    try:
        generation_ref = source_generation.get("generation_ref")
        if isinstance(generation_ref, dict):
            from src.application.shadow_replay.generations import (
                materialized_dataset_generation,
            )

            source_context = materialized_dataset_generation(
                dataset_dir_from_arg(dataset_value),
                generation_ref,
            )
        else:
            source_context = nullcontext(dataset_value)
        with source_context as bound_dataset:
            recomputed = run_strategy_lab_experiment(
                repo_root=repo_root_value,
                dataset=bound_dataset,
                min_sample=min_sample,
                auto=bool(summary.get("auto_generated_hypotheses", True)),
            )
    except Exception:
        return ["experiment_gate_recompute_failed"]
    if _promotion_semantics(payload) != _promotion_semantics(recomputed):
        return ["experiment_gate_recompute_mismatch"]
    return []


def _promotion_semantics(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    return _stable_semantics(
        {
            "summary": {
                key: summary.get(key)
                for key in (
                    "status",
                    "min_sample",
                    "readiness_status",
                    "hypothesis_status",
                    "candidate_impact_allowed",
                )
            },
            "hypotheses": payload.get("hypotheses"),
            "evaluation": payload.get("evaluation"),
            "scorecard": payload.get("scorecard"),
        }
    )


def _stable_semantics(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _stable_semantics(item)
            for key, item in sorted(value.items())
            if key not in {"artifact_provenance", "generated_at_utc"}
        }
    if isinstance(value, list):
        return [_stable_semantics(item) for item in value]
    return value


def _proposal_status(
    *,
    experiment_payload: dict[str, Any],
    best: Any,
    best_source: str,
    dry_run_patch: dict[str, Any],
    patch_allowed: bool,
) -> str:
    experiment_status = text((experiment_payload.get("summary") or {}).get("status"))
    if best_source == "combo_yield_group":
        return "data_gap_only"
    if not best or not isinstance(best, dict):
        return "needs_more_evidence" if experiment_payload.get("evaluation") else "not_ready"
    if experiment_status not in {"ready_for_scorecard_review", "ready_for_proposal"}:
        return "needs_more_evidence"
    if not patch_allowed:
        return "needs_more_evidence"
    if not dry_run_patch:
        return "data_gap_only"
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


def _dry_run_patch_allowed(
    *,
    experiment_payload: dict[str, Any],
    best: dict[str, Any],
    best_source: str,
    variant: dict[str, Any] | None,
) -> bool:
    if best_source != "single_leg":
        return False
    if text((experiment_payload.get("scorecard") or {}).get("best_variant_basis")) != "strict_outcome_dominance":
        return False
    family = text(best.get("strategy_family"))
    if family not in {"sell_put", "covered_call"}:
        return False
    if _has_experiment_only_parameters(variant):
        return False
    evaluation = experiment_payload.get("evaluation") or {}
    if text(evaluation.get("data_mode")) != "closed_replay":
        return False
    production_gate = (evaluation.get("gates") or {}).get("production_recommendation") or {}
    variant_name = text((variant or {}).get("name") or best.get("variant"))
    if not bool(production_gate.get("allowed")):
        return False
    if variant_name not in set(production_gate.get("ready_variants") or []):
        return False
    receipt = (production_gate.get("variant_eligibility") or {}).get(variant_name)
    if not isinstance(receipt, dict) or not bool(receipt.get("allowed")):
        return False
    if text(receipt.get("strategy_family")) != family:
        return False
    if text((variant or {}).get("strategy_family")) != family:
        return False
    return bool((variant or {}).get("production_closed_replay_eligible"))


def _has_experiment_only_parameters(variant: dict[str, Any] | None) -> bool:
    parameters = (variant or {}).get("parameters") or {}
    profile_params = parameters.get("insurance_underwriting") if isinstance(parameters, dict) else None
    return isinstance(profile_params, dict) and bool(EXPERIMENT_ONLY_PARAMETERS & set(profile_params))


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
    evaluation: dict[str, Any],
    best: dict[str, Any],
) -> dict[str, Any] | None:
    return _evaluated_variant(evaluation, text(best.get("variant")))


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
        "best_variant_basis": (experiment_payload.get("scorecard") or {}).get("best_variant_basis"),
        "best_source": best_source,
        "optimization_claim": (experiment_payload.get("scorecard") or {}).get("optimization_claim"),
        "combo_yield_group_evaluator_status": combo_summary.get("status"),
        "combo_yield_ready_group_count": combo_summary.get("ready_group_count"),
        "combo_yield_evaluable_group_count": combo_summary.get("evaluable_group_count"),
    }


def _impact(*, best: dict[str, Any], variant: dict[str, Any] | None) -> dict[str, Any]:
    if not best:
        return {}
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
        "ready_group_count": summary.get("ready_group_count"),
        "evaluable_group_count": summary.get("evaluable_group_count"),
        "scorecard_status": scorecard.get("status"),
        "limitations": scorecard.get("limitations") or [],
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


def _patch_blocker(
    *,
    experiment_payload: dict[str, Any],
    best: dict[str, Any],
    best_source: str,
    variant: dict[str, Any] | None,
) -> str | None:
    if best_source == "combo_yield_group":
        return "combo_yield_group_evaluator_does_not_emit_single_leg_patch"
    if not best or text((experiment_payload.get("scorecard") or {}).get("best_variant_basis")) != "strict_outcome_dominance":
        return "strict_outcome_dominance_required_for_patch"
    if text(best.get("strategy_family")) not in {"sell_put", "covered_call"}:
        return "unsupported_strategy_family_for_patch"
    if _has_experiment_only_parameters(variant):
        return "offline_only_variant_not_patchable"
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
    variant: dict[str, Any] | None,
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
        blocker = _patch_blocker(
            experiment_payload=experiment_payload,
            best=best,
            best_source=best_source,
            variant=variant,
        )
        if blocker:
            limitations.append(blocker)
    if text((experiment_payload.get("evaluation") or {}).get("data_mode")) != "closed_replay":
        limitations.append("closed_replay_outcome_required_for_patch")
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
