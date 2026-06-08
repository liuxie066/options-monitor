from __future__ import annotations

from pathlib import Path
from typing import Any

from src.application.shadow_replay.analysis import analyze_rows
from src.application.shadow_replay.capture import (
    accepted_candidate_snapshots,
    candidate_snapshots_from_filter_decisions,
    dedupe_snapshots,
    filter_decision_rows,
    read_replay_rows,
)
from src.application.shadow_replay.common import (
    MARK_PATH_SCHEMA_VERSION,
    OUTCOME_FACT_SCHEMA_VERSION,
    READINESS_SCHEMA_VERSION,
    resolve_many,
    safe_rel,
    safety_payload,
    unique,
)


def summarize_shadow_replay_readiness(
    *,
    candidate_paths: list[str | Path] | tuple[str | Path, ...],
    trace_paths: list[str | Path] | tuple[str | Path, ...],
    base: Path,
    reject_log_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
    mark_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
    outcome_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
    min_sample: int = 30,
) -> dict[str, Any]:
    """Return a read-only replay-readiness profile for Research bundles."""

    root = base.resolve()
    resolved_candidates = unique(resolve_many(candidate_paths, base=root))
    resolved_traces = unique(resolve_many(trace_paths, base=root))
    resolved_reject_logs = unique(resolve_many(reject_log_paths, base=root))
    resolved_marks = unique(resolve_many(mark_paths, base=root))
    resolved_outcomes = unique(resolve_many(outcome_paths, base=root))

    filter_decisions = filter_decision_rows(
        [path for path in resolved_traces if path.exists()],
        [path for path in resolved_reject_logs if path.exists()],
        base=root,
    )
    candidate_snapshots = dedupe_snapshots(
        accepted_candidate_snapshots([path for path in resolved_candidates if path.exists()], base=root)
        + candidate_snapshots_from_filter_decisions(filter_decisions)
    )
    analysis = analyze_rows(
        candidate_snapshots=candidate_snapshots,
        filter_decisions=filter_decisions,
        mark_snapshots=read_replay_rows([path for path in resolved_marks if path.exists()], schema_version=MARK_PATH_SCHEMA_VERSION, base=root),
        outcome_facts=read_replay_rows([path for path in resolved_outcomes if path.exists()], schema_version=OUTCOME_FACT_SCHEMA_VERSION, base=root),
        min_sample=max(1, int(min_sample)),
    )
    return {
        "schema_version": READINESS_SCHEMA_VERSION,
        "source": {
            "candidate_paths": [safe_rel(path, base=root) for path in resolved_candidates],
            "trace_paths": [safe_rel(path, base=root) for path in resolved_traces],
            "reject_log_paths": [safe_rel(path, base=root) for path in resolved_reject_logs],
            "mark_paths": [safe_rel(path, base=root) for path in resolved_marks],
            "outcome_paths": [safe_rel(path, base=root) for path in resolved_outcomes],
        },
        "summary": analysis["summary"],
        "evidence_checks": analysis["evidence_checks"],
        "bucket_stats": analysis["bucket_stats"],
        "filter_decisions": analysis["filter_decisions"],
        "outcome_coverage": analysis["outcome_coverage"],
        "path_risk": analysis["path_risk"],
        "outcome_stats": analysis["outcome_stats"],
        "insurance_metrics": analysis["insurance_metrics"],
        "outcome_by_bucket": analysis["outcome_by_bucket"],
        "decision_quality": analysis["decision_quality"],
        "review_readiness": analysis["review_readiness"],
        "parameter_advice_gate": analysis["parameter_advice_gate"],
        "recommendations": analysis["recommendations"],
        "safety": safety_payload(writes_local_dataset=False),
    }
