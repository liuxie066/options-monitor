from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from src.application.shadow_replay.analysis import analyze_rows
from src.application.shadow_replay.candidate_impact import (
    _enrich_iv_rv_history_percentiles,
    _evaluate_candidate,
    run_shadow_replay_candidate_impact,
)
from src.application.shadow_replay.common import (
    first_float,
    normal_status,
    resolve_output_path,
    safety_payload,
    text,
    utc_now,
    write_json,
)
from src.application.shadow_replay.parameter_sets import load_parameter_set
from src.application.strategy_lab.combo_evaluator import run_combo_yield_group_experiment
from src.application.strategy_lab.decisions import strategy_family
from src.application.strategy_lab.evidence import load_strategy_lab_evidence
from src.application.strategy_lab.hypotheses import generate_strategy_lab_hypotheses
from src.application.strategy_lab.readiness import analyze_strategy_lab_readiness


EXPERIMENT_SCHEMA_VERSION = "strategy_lab_experiment.v1"
UNDERWRITING_RANKING_SCHEMA_VERSION = "strategy_lab_underwriting_ranking_experiment.v1"
UNDERWRITING_FACTORIAL_SCHEMA_VERSION = "strategy_lab_underwriting_factorial_experiment.v1"
_ACCEPTED_STATUSES = {"accepted", "notified"}
_RANKING_FIELDS = (
    "annualized_return",
    "iv_rv_ratio",
    "iv_minus_rv",
    "safety_margin",
    "spread_ratio",
    "open_interest",
    "net_income",
    "premium_edge_score",
)


def run_strategy_lab_experiment(
    *,
    repo_root: str | Path,
    dataset: str | Path | None = None,
    runs_root: str | Path | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    accounts: list[str] | tuple[str, ...] | None = None,
    market: str | None = None,
    min_sample: int = 30,
    output: str | Path | None = None,
    auto: bool = True,
) -> dict[str, Any]:
    if not _has_input_scope(
        dataset=dataset,
        runs_root=runs_root,
        start_date=start_date,
        end_date=end_date,
        accounts=accounts,
        market=market,
    ):
        raise ValueError("strategy-lab experiment requires --dataset or a run-window selector")
    sample_floor = max(1, int(min_sample))
    readiness = analyze_strategy_lab_readiness(
        repo_root=repo_root,
        dataset=dataset,
        runs_root=runs_root,
        start_date=start_date,
        end_date=end_date,
        accounts=accounts,
        market=market,
        min_sample=sample_floor,
    )
    hypotheses = generate_strategy_lab_hypotheses(
        repo_root=repo_root,
        dataset=dataset,
        runs_root=runs_root,
        start_date=start_date,
        end_date=end_date,
        accounts=accounts,
        market=market,
        min_sample=sample_floor,
    )
    evidence = load_strategy_lab_evidence(
        repo_root=repo_root,
        dataset=dataset,
        runs_root=runs_root,
        start_date=start_date,
        end_date=end_date,
        accounts=accounts,
        market=market,
    )
    combo_group_experiment = run_combo_yield_group_experiment(
        candidate_snapshots=list(evidence["candidate_snapshots"]),
        mark_snapshots=list(evidence["mark_snapshots"]),
        outcome_facts=list(evidence["outcome_facts"]),
        min_sample=sample_floor,
    )
    underwriting_ranking_experiment = _underwriting_ranking_experiment(
        candidate_snapshots=list(evidence["candidate_snapshots"]),
        hypotheses=hypotheses,
    )
    underwriting_factorial_experiment = _underwriting_factorial_experiment(
        candidate_snapshots=list(evidence["candidate_snapshots"]),
        mark_snapshots=list(evidence["mark_snapshots"]),
        outcome_facts=list(evidence["outcome_facts"]),
        hypotheses=hypotheses,
        min_sample=sample_floor,
    )
    parameter_set = hypotheses.get("candidate_impact_parameter_set")
    evaluation: dict[str, Any] | None = None
    if parameter_set:
        evaluation = run_shadow_replay_candidate_impact(
            repo_root=repo_root,
            params=parameter_set,
            dataset=dataset,
            runs_root=runs_root,
            start_date=start_date,
            end_date=end_date,
            accounts=accounts,
            market=market,
            min_sample=sample_floor,
            output_format="json",
        )
    scorecard = _scorecard(evaluation=evaluation, hypotheses=hypotheses)
    status = _experiment_status(
        readiness=readiness,
        hypotheses=hypotheses,
        evaluation=evaluation,
        combo_group_experiment=combo_group_experiment,
    )
    result: dict[str, Any] = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "generated_at_utc": utc_now(),
        "dataset_dir": readiness.get("dataset_dir"),
        "input_scope": {
            "dataset": str(dataset) if dataset is not None and text(dataset) else None,
            "runs_root": str(runs_root) if runs_root is not None and text(runs_root) else None,
            "start_date": text(start_date) or None,
            "end_date": text(end_date) or None,
            "accounts": list(accounts or []),
            "market": text(market).lower() or None,
            "readiness_scope": readiness.get("input_scope") or {},
        },
        "summary": {
            "status": status,
            "auto_generated_hypotheses": bool(auto),
            "min_sample": sample_floor,
            "readiness_status": (readiness.get("summary") or {}).get("status"),
            "hypothesis_status": (hypotheses.get("summary") or {}).get("status"),
            "variant_count": (hypotheses.get("summary") or {}).get("variant_count", 0),
            "candidate_impact_allowed": _candidate_impact_allowed(evaluation),
            "combo_yield_group_evaluator_status": (combo_group_experiment.get("summary") or {}).get("status"),
            "combo_yield_evaluable_group_count": (combo_group_experiment.get("summary") or {}).get(
                "evaluable_group_count", 0
            ),
            "combo_yield_group_experiment_allowed": _combo_group_experiment_allowed(combo_group_experiment),
            "underwriting_ranking_status": (underwriting_ranking_experiment.get("summary") or {}).get("status"),
            "underwriting_ranking_comparable_group_count": (
                underwriting_ranking_experiment.get("summary") or {}
            ).get("comparable_group_count", 0),
            "underwriting_factorial_status": (
                underwriting_factorial_experiment.get("summary") or {}
            ).get("status"),
            "production_recommendation_allowed": False,
        },
        "readiness": readiness,
        "hypotheses": hypotheses,
        "evaluation": evaluation,
        "group_experiments": {
            "combo_yield": combo_group_experiment,
        },
        "ranking_experiments": {
            "underwriting_deduplicated": underwriting_ranking_experiment,
            "underwriting_factorial": underwriting_factorial_experiment,
        },
        "scorecard": scorecard,
        "safety": {
            **safety_payload(writes_local_dataset=False),
            "runtime_config_write_allowed": False,
            "production_recommendation_allowed": False,
        },
    }
    if output:
        write_json(resolve_output_path(output), result)
    return result


def _has_input_scope(
    *,
    dataset: str | Path | None,
    runs_root: str | Path | None,
    start_date: str | None,
    end_date: str | None,
    accounts: list[str] | tuple[str, ...] | None,
    market: str | None,
) -> bool:
    return any(
        (
            dataset is not None and text(dataset),
            runs_root is not None and text(runs_root),
            text(start_date),
            text(end_date),
            bool(accounts),
            text(market),
        )
    )


def _experiment_status(
    *,
    readiness: dict[str, Any],
    hypotheses: dict[str, Any],
    evaluation: dict[str, Any] | None,
    combo_group_experiment: dict[str, Any] | None,
) -> str:
    readiness_status = text((readiness.get("summary") or {}).get("status"))
    if readiness_status == "not_ready":
        return "not_ready"
    if _combo_group_experiment_ready(combo_group_experiment):
        return "ready_for_scorecard_review"
    if not hypotheses.get("parameter_set"):
        return "partial_ready"
    if not evaluation:
        return "partial_ready"
    if not _candidate_impact_allowed(evaluation):
        return "partial_ready"
    return "ready_for_scorecard_review"


def _candidate_impact_allowed(evaluation: dict[str, Any] | None) -> bool:
    if not evaluation:
        return False
    return bool(((evaluation.get("gates") or {}).get("candidate_impact") or {}).get("allowed"))


def _combo_group_experiment_ready(experiment: dict[str, Any] | None) -> bool:
    if not experiment:
        return False
    return text((experiment.get("summary") or {}).get("status")) == "ready"


def _combo_group_experiment_allowed(experiment: dict[str, Any] | None) -> bool:
    if not experiment:
        return False
    return text((experiment.get("summary") or {}).get("status")) in {"ready", "partial_ready"}


def _scorecard(*, evaluation: dict[str, Any] | None, hypotheses: dict[str, Any]) -> dict[str, Any]:
    if not evaluation:
        return {
            "status": "not_evaluable",
            "reason": "parameter_set_missing",
            "rows": [],
            "best_variant": None,
            "best_variant_basis": None,
            "optimization_claim": "none",
        }
    baseline = evaluation.get("baseline") or {}
    production_gate = (evaluation.get("gates") or {}).get("production_recommendation") or {}
    min_sample = max(1, int(((evaluation.get("gates") or {}).get("sample_size") or {}).get("min_sample") or 1))
    rows = []
    for variant in evaluation.get("variants") or []:
        newly_accepted = int(variant.get("newly_accepted_count") or 0)
        newly_rejected = int(variant.get("newly_rejected_count") or 0)
        safety_violations = int(variant.get("safety_violation_count") or 0)
        missing_fields = sum(int(value or 0) for value in (variant.get("missing_fields") or {}).values())
        comparison_eligible = bool(variant.get("comparison_eligible", True))
        family = _variant_family(text(variant.get("name")))
        outcome_comparison = _outcome_comparison(
            baseline=baseline,
            variant=variant,
            family=family,
            min_sample=min_sample,
        )
        rows.append(
            {
                "variant": variant.get("name"),
                "strategy_family": family,
                "newly_accepted_count": newly_accepted,
                "newly_rejected_count": newly_rejected,
                "safety_violation_count": safety_violations,
                "safety_rejected_count": int(variant.get("safety_rejected_count") or 0),
                "missing_field_count": missing_fields,
                "candidate_count": variant.get("candidate_count"),
                "status": (
                    "blocked"
                    if safety_violations
                    else (outcome_comparison["status"] if comparison_eligible else "insufficient_history")
                ),
                "iv_rv_history_status": variant.get("iv_rv_history_status"),
                "domain_metrics": _domain_metrics(family=family, hypotheses=hypotheses),
                "domain_metrics_status": (
                    "outcome_compared" if outcome_comparison["status"] != "not_evaluable" else "not_evaluable"
                ),
                "score_basis": outcome_comparison.get("basis"),
                "outcome_comparison": outcome_comparison,
            }
        )
    rows.sort(key=lambda row: str(row["variant"]))
    dominant = [row for row in rows if row["status"] == "strictly_dominates_baseline"]
    best = dominant[0] if bool(production_gate.get("allowed")) and len(dominant) == 1 else None
    if not rows:
        status, reason = "not_evaluable", "variant_evaluation_missing"
    elif not bool(production_gate.get("allowed")):
        status, reason = "not_evaluable", text(production_gate.get("reason")) or "outcome_review_not_ready"
    elif len(dominant) > 1:
        status, reason = "ambiguous", "multiple_variants_strictly_dominate_baseline"
    elif best:
        status, reason = "ready", "strict_outcome_dominance"
    elif any(row["status"] == "does_not_strictly_dominate_baseline" for row in rows):
        status, reason = "evaluated_no_change", "no_variant_strictly_dominates_baseline"
    else:
        status, reason = "not_evaluable", "outcome_metrics_not_comparable"
    return {
        "status": status,
        "reason": reason,
        "rows": rows,
        "best_variant": best,
        "best_variant_basis": "strict_outcome_dominance" if best else None,
        "optimization_claim": "strict_outcome_dominance" if best else "none",
        "limitations": [
            "candidate_impact_reuses_observed_run_universe_only",
            "candidate_counts_are_review_context_not_selection_score",
            "assignment_and_callaway_rates_are_descriptive_not_failure_penalties",
            "combo_yield_group_experiment_reported_separately",
        ],
    }


def _outcome_comparison(
    *,
    baseline: dict[str, Any],
    variant: dict[str, Any],
    family: str,
    min_sample: int,
) -> dict[str, Any]:
    baseline_metrics = _accepted_family_metrics(baseline, family=family)
    variant_metrics = _accepted_family_metrics(variant, family=family)
    blockers: list[str] = []
    if not bool((baseline.get("analysis_summary") or {}).get("manual_strategy_review_ready")):
        blockers.append("baseline_outcome_review_not_ready")
    if not bool((variant.get("analysis_summary") or {}).get("manual_strategy_review_ready")):
        blockers.append("variant_outcome_review_not_ready")

    metric_specs = (
        ("return_on_capital_avg", "return_on_capital_observation_count"),
        ("max_adverse_return_on_capital_worst", "max_adverse_return_on_capital_observation_count"),
    )
    comparisons: list[dict[str, Any]] = []
    for metric, count_field in metric_specs:
        baseline_value = first_float(baseline_metrics, metric)
        variant_value = first_float(variant_metrics, metric)
        if int(baseline_metrics.get(count_field) or 0) < min_sample:
            blockers.append(f"baseline_{metric}_sample_below_minimum")
        if int(variant_metrics.get(count_field) or 0) < min_sample:
            blockers.append(f"variant_{metric}_sample_below_minimum")
        if baseline_value is None or variant_value is None:
            blockers.append(f"{metric}_missing")
        else:
            comparisons.append(_metric_comparison(metric, baseline_value, variant_value))

    for label, metrics in (("baseline", baseline_metrics), ("variant", variant_metrics)):
        tail = metrics.get("tail_risk") or {}
        if text(tail.get("status")) != "evaluable" or first_float(tail, "cvar_90") is None:
            blockers.append(f"{label}_cvar_90_not_evaluable")
    baseline_cvar = first_float(baseline_metrics.get("tail_risk") or {}, "cvar_90")
    variant_cvar = first_float(variant_metrics.get("tail_risk") or {}, "cvar_90")
    if baseline_cvar is not None and variant_cvar is not None:
        comparisons.append(_metric_comparison("cvar_90", baseline_cvar, variant_cvar))

    for label, metrics in (("baseline", baseline_metrics), ("variant", variant_metrics)):
        transition_count = int(metrics.get("lifecycle_transition_count") or 0)
        observation_count = int(metrics.get("lifecycle_return_on_capital_observation_count") or 0)
        if transition_count <= 0 or observation_count != transition_count:
            blockers.append(f"{label}_lifecycle_return_not_evaluable")
    baseline_lifecycle = first_float(baseline_metrics, "lifecycle_return_on_capital_avg")
    variant_lifecycle = first_float(variant_metrics, "lifecycle_return_on_capital_avg")
    if baseline_lifecycle is not None and variant_lifecycle is not None:
        comparisons.append(
            _metric_comparison("lifecycle_return_on_capital_avg", baseline_lifecycle, variant_lifecycle)
        )

    if blockers:
        return {
            "status": "not_evaluable",
            "basis": None,
            "blockers": sorted(set(blockers)),
            "metrics": comparisons,
            "descriptive_transitions": _descriptive_transitions(
                baseline_metrics=baseline_metrics,
                variant_metrics=variant_metrics,
                family=family,
            ),
        }
    strictly_dominates = all(row["relation"] in {"better", "equal"} for row in comparisons) and any(
        row["relation"] == "better" for row in comparisons
    )
    return {
        "status": (
            "strictly_dominates_baseline" if strictly_dominates else "does_not_strictly_dominate_baseline"
        ),
        "basis": "strict_outcome_dominance",
        "blockers": [],
        "metrics": comparisons,
        "descriptive_transitions": _descriptive_transitions(
            baseline_metrics=baseline_metrics,
            variant_metrics=variant_metrics,
            family=family,
        ),
    }


def _accepted_family_metrics(payload: dict[str, Any], *, family: str) -> dict[str, Any]:
    mode = {"sell_put": "put", "covered_call": "call"}.get(family)
    if not mode:
        return {}
    by_mode_status = (payload.get("insurance_metrics") or {}).get("by_mode_status") or {}
    return ((by_mode_status.get(mode) or {}).get("accepted") or {})


def _metric_comparison(metric: str, baseline: float, variant: float) -> dict[str, Any]:
    tolerance = 1e-12
    if variant > baseline + tolerance:
        relation = "better"
    elif variant < baseline - tolerance:
        relation = "worse"
    else:
        relation = "equal"
    return {
        "metric": metric,
        "direction": "higher_is_better",
        "baseline": baseline,
        "variant": variant,
        "relation": relation,
    }


def _descriptive_transitions(
    *,
    baseline_metrics: dict[str, Any],
    variant_metrics: dict[str, Any],
    family: str,
) -> dict[str, Any]:
    metric = "assignment_rate" if family == "sell_put" else "called_away_rate"
    return {
        "metric": metric,
        "baseline": baseline_metrics.get(metric),
        "variant": variant_metrics.get(metric),
        "used_as_failure_penalty": False,
    }


def _variant_family(name: str) -> str:
    for family in ("covered_call", "combo_yield", "sell_put"):
        if name.startswith(f"{family}_"):
            return family
    return "unknown"


def _domain_metrics(*, family: str, hypotheses: dict[str, Any]) -> list[str]:
    for item in hypotheses.get("domain_hypotheses") or []:
        if item.get("strategy_family") == family:
            adapter = item.get("adapter") or {}
            return list(adapter.get("scorecard_metrics") or [])
    return []


def _underwriting_ranking_experiment(
    *,
    candidate_snapshots: list[dict[str, Any]],
    hypotheses: dict[str, Any],
    top_n: int = 3,
) -> dict[str, Any]:
    scoped = [
        dict(row)
        for row in candidate_snapshots
        if strategy_family(row) in {"sell_put", "covered_call"}
        and normal_status(row.get("status")) in _ACCEPTED_STATUSES
        and text(row.get("strategy_profile")).lower() == "insurance_underwriting"
    ]
    baselines = _underwriting_baselines(hypotheses)
    coverage = _ranking_field_coverage(scoped)
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for index, row in enumerate(scoped, start=1):
        row["_observed_index"] = index
        grouped[_ranking_group_key(row)].append(row)

    groups = [
        _ranking_group_payload(key=key, rows=rows, baseline=baselines.get(key[0]) or {}, top_n=top_n)
        for key, rows in sorted(grouped.items())
    ]
    comparable = [group for group in groups if group["status"] == "ready"]
    changed = [group for group in comparable if group["top_n"]["changed"]]
    rank_change_count = sum(len(group["rank_changes"]) for group in comparable)
    if comparable:
        status = "ready"
        reason = "observed_vs_deduplicated_ranking_available"
    elif scoped:
        status = "partial_ready"
        reason = "ranking_fields_or_peer_candidates_missing"
    else:
        status = "not_ready"
        reason = "insurance_underwriting_accepted_candidates_missing"
    return {
        "schema_version": UNDERWRITING_RANKING_SCHEMA_VERSION,
        "generated_at_utc": utc_now(),
        "summary": {
            "status": status,
            "reason": reason,
            "top_n": top_n,
            "underwriting_candidate_snapshot_count": len(scoped),
            "group_count": len(groups),
            "comparable_group_count": len(comparable),
            "changed_top_n_group_count": len(changed),
            "candidate_rank_change_count": rank_change_count,
            "score_basis": "candidate_ranking_only",
            "quality_claim": "none",
            "runtime_config_write_allowed": False,
            "production_recommendation_allowed": False,
        },
        "policy": {
            "name": "safety_margin_then_deduplicated_compensation",
            "primary_order": [
                "safety_margin_desc",
                "deduplicated_compensation_desc",
                "spread_ratio_asc",
                "open_interest_desc",
                "net_income_desc_tiebreak_only",
            ],
            "compensation": "mean(return_edge, min(iv_rv_edge, iv_minus_rv_edge))",
            "net_income_in_primary_score": False,
            "net_income_ranking_role": "final_tiebreak_only",
            "threshold_source": "strategy_lab_empirical_baseline",
        },
        "field_coverage": coverage,
        "groups": groups[:50],
        "limitations": [
            "ranking_experiment_does_not_change_production_sorting",
            "ranking_comparison_requires_persisted_underwriting_margin_fields",
            "ranking_only_cannot_claim_return_drawdown_or_cvar_improvement",
            "outcome_evidence_required_before_production_switch",
        ],
        "safety": {
            **safety_payload(writes_local_dataset=False),
            "runtime_config_write_allowed": False,
            "production_recommendation_allowed": False,
        },
    }


def _underwriting_factorial_experiment(
    *,
    candidate_snapshots: list[dict[str, Any]],
    mark_snapshots: list[dict[str, Any]],
    outcome_facts: list[dict[str, Any]],
    hypotheses: dict[str, Any],
    min_sample: int,
    top_n: int = 3,
) -> dict[str, Any]:
    parameter_payload = hypotheses.get("candidate_impact_parameter_set")
    if not parameter_payload:
        return {
            "schema_version": UNDERWRITING_FACTORIAL_SCHEMA_VERSION,
            "generated_at_utc": utc_now(),
            "summary": {
                "status": "not_ready",
                "reason": "parameter_set_missing",
                "production_recommendation_allowed": False,
            },
            "families": [],
            "safety": {
                **safety_payload(writes_local_dataset=False),
                "production_recommendation_allowed": False,
            },
        }

    parameter_set = load_parameter_set(parameter_payload)
    historical_variants = {
        text(variant.strategy_family): variant
        for variant in parameter_set.variants
        if variant.name.endswith("_historical_iv_rv_percentile") and text(variant.strategy_family)
    }
    scoped = [
        dict(row)
        for row in candidate_snapshots
        if strategy_family(row) in {"sell_put", "covered_call"}
        and text(row.get("strategy_profile")).lower() == "insurance_underwriting"
    ]
    enriched, history = _enrich_iv_rv_history_percentiles(scoped)
    baselines = _underwriting_baselines(hypotheses)
    families: list[dict[str, Any]] = []
    for family in ("sell_put", "covered_call"):
        rows = [row for row in enriched if strategy_family(row) == family]
        variant = historical_variants.get(family)
        if not rows or variant is None:
            families.append(
                {
                    "strategy_family": family,
                    "status": "not_ready",
                    "reason": "candidate_or_historical_variant_missing",
                    "cells": {},
                }
            )
            continue

        fixed_rows = [row for row in rows if normal_status(row.get("status")) in _ACCEPTED_STATUSES]
        historical_rows: list[dict[str, Any]] = []
        history_modes: Counter[str] = Counter()
        for row in rows:
            evaluation = _evaluate_candidate(row, variant=variant)
            history_modes.update([text(evaluation.get("iv_rv_history_mode")) or "unknown"])
            if evaluation["status"] != "accepted":
                continue
            candidate = dict(row)
            candidate["status"] = "accepted"
            candidate["variant_name"] = variant.name
            candidate["variant_reasons"] = evaluation["reasons"]
            historical_rows.append(candidate)

        cell_inputs = (
            ("fixed_iv_rv__production_observed", "fixed_iv_rv", "production_observed", fixed_rows),
            (
                "historical_iv_rv_percentile__production_observed",
                "historical_iv_rv_percentile",
                "production_observed",
                historical_rows,
            ),
            ("fixed_iv_rv__deduplicated", "fixed_iv_rv", "deduplicated", fixed_rows),
            (
                "historical_iv_rv_percentile__deduplicated",
                "historical_iv_rv_percentile",
                "deduplicated",
                historical_rows,
            ),
        )
        cells = {
            name: _underwriting_factorial_cell(
                rows=cell_rows,
                family=family,
                filter_policy=filter_policy,
                rank_policy=rank_policy,
                baseline=baselines.get(family) or {},
                mark_snapshots=mark_snapshots,
                outcome_facts=outcome_facts,
                min_sample=min_sample,
                top_n=top_n,
            )
            for name, filter_policy, rank_policy, cell_rows in cell_inputs
        }
        baseline_cell = cells["fixed_iv_rv__production_observed"]
        for name, cell in cells.items():
            if name == "fixed_iv_rv__production_observed":
                cell["outcome_comparison"] = {
                    "status": "baseline",
                    "basis": None,
                    "blockers": [],
                    "metrics": [],
                }
                continue
            cell["outcome_comparison"] = _outcome_comparison(
                baseline=baseline_cell,
                variant=cell,
                family=family,
                min_sample=min_sample,
            )

        history_evaluated = int(history_modes.get("evaluated") or 0)
        partial_cells = [name for name, cell in cells.items() if cell["status"] != "ready"]
        if history_evaluated <= 0:
            status, reason = "partial_ready", "historical_iv_rv_samples_insufficient"
        elif partial_cells:
            status, reason = "partial_ready", "ranking_fields_incomplete"
        else:
            status, reason = "ready", "four_cell_comparison_available"
        families.append(
            {
                "strategy_family": family,
                "status": status,
                "reason": reason,
                "historical_variant": variant.name,
                "history_modes": dict(sorted(history_modes.items())),
                "partial_cells": partial_cells,
                "cells": cells,
            }
        )

    statuses = {text(row.get("status")) for row in families}
    if "ready" in statuses:
        status, reason = "ready", "at_least_one_family_four_cell_comparison_available"
    elif "partial_ready" in statuses:
        status, reason = "partial_ready", "historical_or_ranking_evidence_incomplete"
    else:
        status, reason = "not_ready", "underwriting_factorial_inputs_missing"
    return {
        "schema_version": UNDERWRITING_FACTORIAL_SCHEMA_VERSION,
        "generated_at_utc": utc_now(),
        "summary": {
            "status": status,
            "reason": reason,
            "top_n": top_n,
            "family_count": len(families),
            "history": history,
            "production_recommendation_allowed": False,
        },
        "families": families,
        "limitations": [
            "observed_run_universe_only",
            "historical_percentile_uses_prior_runs_only",
            "outcome_claim_requires_complete_marks_and_lifecycle_outcomes",
            "four_cell_result_does_not_change_production_filtering_or_ranking",
        ],
        "safety": {
            **safety_payload(writes_local_dataset=False),
            "production_recommendation_allowed": False,
        },
    }


def _underwriting_factorial_cell(
    *,
    rows: list[dict[str, Any]],
    family: str,
    filter_policy: str,
    rank_policy: str,
    baseline: dict[str, float],
    mark_snapshots: list[dict[str, Any]],
    outcome_facts: list[dict[str, Any]],
    min_sample: int,
    top_n: int,
) -> dict[str, Any]:
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for index, row in enumerate(rows, start=1):
        candidate = dict(row)
        candidate["_observed_index"] = index
        grouped[_ranking_group_key(candidate)].append(candidate)

    selected: list[dict[str, Any]] = []
    group_payloads: list[dict[str, Any]] = []
    incomplete_groups = 0
    for key, group_rows in sorted(grouped.items()):
        if rank_policy == "deduplicated":
            scored = [(row, _ranking_candidate_payload(row, family=family, baseline=baseline)) for row in group_rows]
            missing = sorted(
                {
                    field
                    for _, score in scored
                    for field in score["missing_primary_fields"]
                }
            )
            if missing:
                incomplete_groups += 1
                group_payloads.append(
                    {
                        "group_id": "|".join(part or "-" for part in key),
                        "status": "incomplete_fields",
                        "missing_primary_fields": missing,
                        "selected_contracts": [],
                    }
                )
                continue
            ranked = [row for row, score in sorted(scored, key=lambda item: _deduplicated_rank_key(item[1]))]
        else:
            ranked = sorted(group_rows, key=_observed_rank_key)
        chosen = ranked[:top_n]
        for row in chosen:
            candidate = dict(row)
            candidate.pop("_observed_index", None)
            candidate["status"] = "accepted"
            candidate["factorial_filter_policy"] = filter_policy
            candidate["factorial_rank_policy"] = rank_policy
            selected.append(candidate)
        group_payloads.append(
            {
                "group_id": "|".join(part or "-" for part in key),
                "status": "ready",
                "candidate_count": len(group_rows),
                "selected_contracts": [text(row.get("contract_symbol")) or None for row in chosen],
            }
        )

    analysis = analyze_rows(
        candidate_snapshots=selected,
        filter_decisions=[],
        mark_snapshots=mark_snapshots,
        outcome_facts=outcome_facts,
        min_sample=min_sample,
    )
    return {
        "status": "partial_ready" if incomplete_groups else "ready",
        "filter_policy": filter_policy,
        "rank_policy": rank_policy,
        "candidate_count": len(rows),
        "selected_candidate_count": len(selected),
        "group_count": len(grouped),
        "incomplete_group_count": incomplete_groups,
        "groups": group_payloads[:50],
        "analysis_summary": analysis["summary"],
        "insurance_metrics": analysis["insurance_metrics"],
        "family_metrics": _accepted_family_metrics(analysis, family=family),
    }


def _underwriting_baselines(hypotheses: dict[str, Any]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for item in hypotheses.get("domain_hypotheses") or []:
        family = text(item.get("strategy_family"))
        if family not in {"sell_put", "covered_call"}:
            continue
        raw = item.get("baseline_parameters") or {}
        out[family] = {
            key: float(value)
            for key in ("min_annualized_return", "min_iv_rv_ratio", "min_iv_minus_rv")
            if (value := first_float(raw, key)) is not None
        }
    return out


def _ranking_group_key(row: dict[str, Any]) -> tuple[str, ...]:
    return (
        strategy_family(row),
        text(row.get("run_id")),
        text(row.get("account")).lower(),
        text(row.get("symbol")).upper(),
        text(row.get("source_path")),
    )


def _ranking_group_payload(
    *,
    key: tuple[str, ...],
    rows: list[dict[str, Any]],
    baseline: dict[str, float],
    top_n: int,
) -> dict[str, Any]:
    family, run_id, account, symbol, source_path = key
    observed = sorted(rows, key=_observed_rank_key)
    scored = [_ranking_candidate_payload(row, family=family, baseline=baseline) for row in observed]
    missing_primary = Counter(
        field
        for row in scored
        for field in row["missing_primary_fields"]
    )
    threshold_missing = [
        key
        for key in ("min_annualized_return", "min_iv_rv_ratio", "min_iv_minus_rv")
        if first_float(baseline, key) is None or float(baseline[key]) <= 0
    ]
    if len(scored) < 2:
        status = "insufficient_candidates"
    elif missing_primary or threshold_missing:
        status = "incomplete_fields"
    else:
        status = "ready"

    proposed = sorted(scored, key=_deduplicated_rank_key) if status == "ready" else []
    observed_ids = [row["contract_symbol"] for row in scored]
    proposed_ids = [row["contract_symbol"] for row in proposed]
    observed_ranks = {contract: idx for idx, contract in enumerate(observed_ids, start=1)}
    proposed_ranks = {contract: idx for idx, contract in enumerate(proposed_ids, start=1)}
    changes = [
        {
            "contract_symbol": contract,
            "observed_rank": observed_ranks[contract],
            "deduplicated_rank": proposed_ranks[contract],
            "rank_delta": observed_ranks[contract] - proposed_ranks[contract],
        }
        for contract in proposed_ids
        if observed_ranks.get(contract) != proposed_ranks.get(contract)
    ]
    observed_top = observed_ids[:top_n]
    proposed_top = proposed_ids[:top_n]
    return {
        "group_id": "|".join(part or "-" for part in key),
        "strategy_family": family,
        "run_id": run_id or None,
        "account": account or None,
        "symbol": symbol or None,
        "source_path": source_path or None,
        "candidate_count": len(scored),
        "status": status,
        "baseline_parameters": baseline,
        "missing_primary_fields": dict(missing_primary.most_common()),
        "missing_thresholds": threshold_missing,
        "production_observed": scored,
        "deduplicated": proposed,
        "rank_changes": changes,
        "top_n": {
            "production_observed": observed_top,
            "deduplicated": proposed_top,
            "overlap_count": len(set(observed_top) & set(proposed_top)),
            "changed": bool(proposed_top) and observed_top != proposed_top,
        },
    }


def _observed_rank_key(row: dict[str, Any]) -> tuple[float, int]:
    source_rank = first_float(row, "source_row_number")
    return (source_rank if source_rank is not None else float("inf"), int(row.get("_observed_index") or 0))


def _ranking_candidate_payload(
    row: dict[str, Any],
    *,
    family: str,
    baseline: dict[str, float],
) -> dict[str, Any]:
    annualized = first_float(row, "annualized_return")
    ratio = first_float(row, "iv_rv_ratio")
    spread = first_float(row, "iv_minus_rv")
    margin = _underwriting_margin(row, family=family)
    return_edge = _edge_score(annualized, first_float(baseline, "min_annualized_return"))
    ratio_edge = _edge_score(ratio, first_float(baseline, "min_iv_rv_ratio"))
    spread_edge = _edge_score(spread, first_float(baseline, "min_iv_minus_rv"))
    vol_edge = min(ratio_edge, spread_edge) if ratio_edge is not None and spread_edge is not None else None
    compensation = (
        round((return_edge + vol_edge) / 2.0, 6)
        if return_edge is not None and vol_edge is not None
        else None
    )
    missing_primary = [
        field
        for field, value in (
            ("contract_symbol", text(row.get("contract_symbol")) or None),
            ("annualized_return", annualized),
            ("iv_rv_ratio", ratio),
            ("iv_minus_rv", spread),
            ("safety_margin", margin),
        )
        if value is None
    ]
    return {
        "contract_symbol": text(row.get("contract_symbol")) or None,
        "source_row_number": first_float(row, "source_row_number"),
        "premium_edge_score": first_float(row, "premium_edge_score"),
        "safety_margin": _round_or_none(margin),
        "return_edge_score": _round_or_none(return_edge),
        "iv_rv_edge_score": _round_or_none(ratio_edge),
        "iv_minus_rv_edge_score": _round_or_none(spread_edge),
        "vol_edge_score": _round_or_none(vol_edge),
        "deduplicated_compensation_score": _round_or_none(compensation),
        "spread_ratio": first_float(row, "spread_ratio"),
        "open_interest": first_float(row, "open_interest"),
        "net_income": first_float(row, "net_income_cny", "net_income"),
        "missing_primary_fields": missing_primary,
    }


def _underwriting_margin(row: dict[str, Any], *, family: str) -> float | None:
    if family == "sell_put":
        persisted = first_float(row, "strike_safety_margin_pct")
        if persisted is not None:
            return persisted
        strike = first_float(row, "strike")
        boundary = first_float(row, "max_strike")
        if strike is not None and boundary is not None and boundary > 0:
            return (boundary - strike) / boundary
        return None
    persisted = first_float(row, "strike_upside_margin_pct")
    if persisted is not None:
        return persisted
    strike = first_float(row, "strike")
    boundary = first_float(row, "effective_min_strike", "min_strike")
    if strike is not None and boundary is not None and boundary > 0:
        return (strike - boundary) / boundary
    return None


def _edge_score(value: float | None, threshold: float | None, *, cap: float = 1.5) -> float | None:
    if value is None or threshold is None or threshold <= 0:
        return None
    return min(max(value / threshold, 0.0), cap)


def _deduplicated_rank_key(row: dict[str, Any]) -> tuple[float, ...]:
    return (
        -_rank_number(row.get("safety_margin")),
        -_rank_number(row.get("deduplicated_compensation_score")),
        _rank_number(row.get("spread_ratio"), missing=float("inf")),
        -_rank_number(row.get("open_interest")),
        -_rank_number(row.get("net_income")),
    )


def _rank_number(value: Any, *, missing: float = 0.0) -> float:
    try:
        parsed = float(value)
    except Exception:
        return missing
    return parsed if parsed == parsed else missing


def _ranking_field_coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter()
    for row in rows:
        family = strategy_family(row)
        values = {
            "annualized_return": first_float(row, "annualized_return"),
            "iv_rv_ratio": first_float(row, "iv_rv_ratio"),
            "iv_minus_rv": first_float(row, "iv_minus_rv"),
            "safety_margin": _underwriting_margin(row, family=family),
            "spread_ratio": first_float(row, "spread_ratio"),
            "open_interest": first_float(row, "open_interest"),
            "net_income": first_float(row, "net_income_cny", "net_income"),
            "premium_edge_score": first_float(row, "premium_edge_score"),
        }
        counts.update(key for key, value in values.items() if value is not None)
    total = len(rows)
    return {
        field: {
            "available_count": counts[field],
            "missing_count": total - counts[field],
            "coverage_ratio": round(counts[field] / total, 6) if total else 0.0,
        }
        for field in _RANKING_FIELDS
    }


def _round_or_none(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None
