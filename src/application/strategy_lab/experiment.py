from __future__ import annotations

from pathlib import Path
from typing import Any

from src.application.shadow_replay import run_shadow_replay_candidate_impact
from src.application.shadow_replay.common import resolve_output_path, safety_payload, text, utc_now, write_json
from src.application.strategy_lab.combo_optimizer import run_combo_yield_group_experiment
from src.application.strategy_lab.evidence import load_strategy_lab_evidence
from src.application.strategy_lab.hypotheses import generate_strategy_lab_hypotheses
from src.application.strategy_lab.readiness import analyze_strategy_lab_readiness


EXPERIMENT_SCHEMA_VERSION = "strategy_lab_experiment.v1"


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
            "combo_yield_group_optimizer_status": (combo_group_experiment.get("summary") or {}).get("status"),
            "combo_yield_group_variant_count": (combo_group_experiment.get("summary") or {}).get("variant_count", 0),
            "combo_yield_group_experiment_allowed": _combo_group_experiment_allowed(combo_group_experiment),
            "production_recommendation_allowed": False,
        },
        "readiness": readiness,
        "hypotheses": hypotheses,
        "evaluation": evaluation,
        "group_experiments": {
            "combo_yield": combo_group_experiment,
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
            "status": "not_ready",
            "reason": "parameter_set_missing",
            "rows": [],
            "best_variant": None,
            "optimization_claim": "none",
        }
    rows = []
    for variant in evaluation.get("variants") or []:
        newly_accepted = int(variant.get("newly_accepted_count") or 0)
        newly_rejected = int(variant.get("newly_rejected_count") or 0)
        safety_violations = int(variant.get("safety_violation_count") or 0)
        missing_fields = sum(int(value or 0) for value in (variant.get("missing_fields") or {}).values())
        objective_score = round(
            newly_accepted
            - (0.5 * newly_rejected)
            - (100.0 * safety_violations)
            - (0.25 * missing_fields),
            6,
        )
        family = _variant_family(text(variant.get("name")))
        rows.append(
            {
                "variant": variant.get("name"),
                "strategy_family": family,
                "objective_score": objective_score,
                "newly_accepted_count": newly_accepted,
                "newly_rejected_count": newly_rejected,
                "safety_violation_count": safety_violations,
                "missing_field_count": missing_fields,
                "candidate_count": variant.get("candidate_count"),
                "status": "blocked" if safety_violations else "candidate_review",
                "domain_metrics": _domain_metrics(family=family, hypotheses=hypotheses),
            }
        )
    rows.sort(key=lambda row: (row["status"] == "blocked", -float(row["objective_score"]), str(row["variant"])))
    best = next((row for row in rows if row["status"] != "blocked"), None)
    return {
        "status": "ready" if rows else "not_ready",
        "reason": "observed_universe_scorecard" if rows else "variant_evaluation_missing",
        "rows": rows,
        "best_variant": best,
        "optimization_claim": "observed_universe_only",
        "limitations": [
            "scorecard_is_not_production_recommendation",
            "candidate_impact_reuses_observed_run_universe_only",
            "combo_yield_group_experiment_reported_separately",
        ],
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
