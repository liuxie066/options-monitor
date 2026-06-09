from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from src.application.shadow_replay.analysis import analyze_rows
from src.application.shadow_replay.common import (
    normal_status,
    resolve_output_path,
    safety_payload,
    text,
    utc_now,
    write_json,
)
from src.application.shadow_replay.settlement import is_usable_mark
from src.application.strategy_lab.decisions import build_decision_instances, summarize_decision_instances
from src.application.strategy_lab.evidence import load_strategy_lab_evidence


READINESS_SCHEMA_VERSION = "strategy_lab_readiness.v1"


def analyze_strategy_lab_readiness(
    *,
    dataset: str | Path | None = None,
    repo_root: str | Path | None = None,
    runs_root: str | Path | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    accounts: list[str] | tuple[str, ...] | None = None,
    market: str | None = None,
    min_sample: int = 30,
    output: str | Path | None = None,
) -> dict[str, Any]:
    evidence = load_strategy_lab_evidence(
        repo_root=repo_root,
        dataset=dataset,
        runs_root=runs_root,
        start_date=start_date,
        end_date=end_date,
        accounts=accounts,
        market=market,
    )
    sample_floor = max(1, int(min_sample))
    candidates = list(evidence["candidate_snapshots"])
    decisions = build_decision_instances(candidates)
    decision_summary = summarize_decision_instances(decisions)
    analysis = analyze_rows(
        candidate_snapshots=candidates,
        filter_decisions=list(evidence["filter_decisions"]),
        mark_snapshots=list(evidence["mark_snapshots"]),
        outcome_facts=list(evidence["outcome_facts"]),
        min_sample=sample_floor,
    )
    readiness = _readiness_payload(
        candidate_snapshots=candidates,
        decisions=decisions,
        mark_snapshots=list(evidence["mark_snapshots"]),
        outcome_facts=list(evidence["outcome_facts"]),
        min_sample=sample_floor,
        decision_summary=decision_summary,
    )
    result = {
        "schema_version": READINESS_SCHEMA_VERSION,
        "generated_at_utc": utc_now(),
        "dataset_dir": evidence["dataset_dir"],
        "input_scope": {
            "source": evidence.get("source") or {},
            "coverage": evidence.get("coverage") or {},
            "filters": evidence.get("filters") or {},
        },
        "summary": readiness["summary"],
        "readiness": readiness,
        "decision_instances": {
            "summary": decision_summary,
            "samples": decisions[:50],
        },
        "shadow_replay": {
            "summary": analysis["summary"],
            "review_readiness": analysis["review_readiness"],
            "outcome_coverage": analysis["outcome_coverage"],
        },
        "safety": {
            **safety_payload(writes_local_dataset=False),
            "runtime_config_write_allowed": False,
        },
    }
    if output:
        write_json(resolve_output_path(output), result)
    return result


def _readiness_payload(
    *,
    candidate_snapshots: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    mark_snapshots: list[dict[str, Any]],
    outcome_facts: list[dict[str, Any]],
    min_sample: int,
    decision_summary: dict[str, Any],
) -> dict[str, Any]:
    candidate_count = len(candidate_snapshots)
    decision_count = len(decisions)
    status_counts = Counter(normal_status(row.get("status")) for row in candidate_snapshots)
    rejected_count = sum(status_counts.get(status, 0) for status in ("rejected", "post_filtered", "ranked_below"))
    usable_mark_count = sum(1 for row in mark_snapshots if is_usable_mark(row))
    outcome_count = len(outcome_facts)
    data_mode = _data_mode(mark_snapshots, outcome_facts)
    blockers: list[str] = []
    limitations: list[str] = []
    if candidate_count <= 0:
        blockers.append("candidate_universe_missing")
    if decision_count <= 0:
        blockers.append("decision_instance_missing")
    if candidate_count < min_sample:
        blockers.append("sample_size_below_min_sample")
    if rejected_count <= 0:
        limitations.append("rejected_universe_missing")
    if data_mode == "filter_only":
        limitations.append("mark_path_and_outcome_missing")
    elif data_mode == "path_only":
        limitations.append("outcome_fact_missing")
    blocker_counts = decision_summary.get("blocker_counts") if isinstance(decision_summary, dict) else {}
    for blocker in blocker_counts or {}:
        blocker_text = text(blocker)
        if blocker_text and blocker_text not in blockers:
            blockers.append(blocker_text)
    domain_readiness = _domain_readiness(decisions, min_sample=min_sample)
    has_supported_domain = any(
        bool(item.get("ready"))
        for key, item in domain_readiness.items()
        if key in {"sell_put", "covered_call", "combo_yield"}
    )
    status = _status(
        blockers=blockers,
        candidate_count=candidate_count,
        min_sample=min_sample,
        data_mode=data_mode,
        has_supported_domain=has_supported_domain,
    )
    return {
        "summary": {
            "status": status,
            "data_mode": data_mode,
            "candidate_snapshot_count": candidate_count,
            "decision_instance_count": decision_count,
            "min_sample": min_sample,
            "sample_floor_met": candidate_count >= min_sample,
            "rejected_count": rejected_count,
            "usable_mark_path_snapshot_count": usable_mark_count,
            "outcome_fact_count": outcome_count,
            "ready_for_experiment": status in {"ready_for_experiment", "ready_for_proposal"},
            "ready_for_proposal": status == "ready_for_proposal",
        },
        "blockers": blockers,
        "limitations": limitations,
        "domain_readiness": domain_readiness,
        "next_action": _next_action(status=status, data_mode=data_mode, blockers=blockers),
    }


def _domain_readiness(decisions: list[dict[str, Any]], *, min_sample: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for family in ("sell_put", "covered_call", "combo_yield"):
        scoped = [row for row in decisions if row.get("strategy_family") == family]
        blocker_counts = Counter()
        for row in scoped:
            blocker_counts.update(str(blocker) for blocker in row.get("blockers") or [] if str(blocker))
        single_leg_ready = sum(1 for row in scoped if bool(row.get("ready_for_single_leg_experiment")))
        group_ready = sum(1 for row in scoped if bool(row.get("ready_for_group_experiment")))
        if family == "combo_yield":
            ready = group_ready >= min_sample
            supported_scope = "group_readiness_only" if not ready else "group_experiment_ready"
        else:
            ready = single_leg_ready >= min_sample
            supported_scope = "single_leg_experiment" if ready else "insufficient_single_leg_samples"
        out[family] = {
            "decision_instance_count": len(scoped),
            "single_leg_ready_count": single_leg_ready,
            "group_ready_count": group_ready,
            "min_sample": min_sample,
            "ready": ready,
            "supported_scope": supported_scope,
            "blockers": dict(blocker_counts.most_common(20)),
        }
    return out


def _data_mode(marks: list[dict[str, Any]], outcomes: list[dict[str, Any]]) -> str:
    if outcomes and any(is_usable_mark(row) for row in marks):
        return "closed_replay"
    if any(is_usable_mark(row) for row in marks):
        return "path_only"
    return "filter_only"


def _status(
    *,
    blockers: list[str],
    candidate_count: int,
    min_sample: int,
    data_mode: str,
    has_supported_domain: bool,
) -> str:
    fatal = {"candidate_universe_missing", "decision_instance_missing"}
    if any(blocker in fatal for blocker in blockers):
        return "not_ready"
    if candidate_count < min_sample or not has_supported_domain:
        return "partial_ready"
    if data_mode == "closed_replay":
        return "ready_for_proposal"
    return "ready_for_experiment"


def _next_action(*, status: str, data_mode: str, blockers: list[str]) -> str:
    if "candidate_universe_missing" in blockers:
        return "build_shadow_replay_dataset"
    if "decision_instance_missing" in blockers:
        return "collect_strategy_family_evidence"
    if status == "partial_ready":
        return "collect_more_strategy_lab_evidence"
    if data_mode == "filter_only":
        return "run_candidate_impact_or_collect_mark_paths_before_proposal"
    if data_mode == "path_only":
        return "collect_outcome_facts_before_proposal"
    return "run_strategy_lab_experiment"
