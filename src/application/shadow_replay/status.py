from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.application.shadow_replay.analysis import analyze_rows
from src.application.shadow_replay.common import (
    DATASET_FILES,
    read_jsonl,
    resolve_path,
    safety_payload,
    text,
)
from src.application.shadow_replay.settlement import is_usable_mark


STATUS_SCHEMA_VERSION = "shadow_replay_dataset_status.v1"
_PLAN_PRIORITY = {"high": 0, "medium": 1, "low": 2, "none": 3}


def shadow_replay_dataset_status(
    *,
    repo_root: str | Path,
    dataset_root: str | Path | None = None,
    min_sample: int = 30,
    min_mark_points: int = 2,
    mark_stale_hours: int = 24,
    now_utc: str | datetime | None = None,
) -> dict[str, Any]:
    """Summarize local shadow replay datasets without collecting or writing data."""

    base = Path(repo_root).expanduser().resolve()
    now = _coerce_now(now_utc)
    root = (
        resolve_path(dataset_root, base=base)
        if dataset_root is not None and str(dataset_root).strip()
        else (base / "output_shared" / "research" / "shadow_replay" / "datasets").resolve()
    )
    sample_floor = max(1, int(min_sample))
    mark_floor = max(1, int(min_mark_points))
    stale_hours = max(1, int(mark_stale_hours))
    datasets = [
        _dataset_status(
            path,
            min_sample=sample_floor,
            min_mark_points=mark_floor,
            mark_stale_hours=stale_hours,
            now=now,
        )
        for path in _discover_dataset_dirs(root)
    ]
    status_counts = Counter(row["status"] for row in datasets)
    action_counts = Counter(row["next_suggested_action"] for row in datasets)
    plan_rows = _data_plan_rows(datasets)
    review_rows = _review_queue_rows(datasets)
    plan_counts = Counter(row["action"] for row in plan_rows)
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "dataset_root": str(root),
        "generated_at_utc": _format_utc(now),
        "summary": {
            "dataset_count": len(datasets),
            "min_sample": sample_floor,
            "min_mark_points": mark_floor,
            "mark_stale_hours": stale_hours,
            "by_status": dict(sorted(status_counts.items())),
            "next_suggested_actions": dict(sorted(action_counts.items())),
            "data_plan_actions": dict(sorted(plan_counts.items())),
            "ready_for_manual_review_count": status_counts.get("needs_human_review", 0),
            "review_queue_count": len(review_rows),
            "sampling_due_count": sum(1 for row in plan_rows if row["action"] == "collect_marks"),
            "stale_mark_count": sum(1 for row in datasets if row["sampling"]["is_mark_stale"]),
        },
        "datasets": datasets,
        "data_plan": plan_rows,
        "review_queue": review_rows,
        "safety": safety_payload(writes_local_dataset=False),
    }


def _discover_dataset_dirs(root: Path) -> list[Path]:
    if not root.exists() or not root.is_dir():
        return []
    marker_files = set(DATASET_FILES) | {"manifest.json"}
    out: list[Path] = []
    for child in sorted(path for path in root.iterdir() if path.is_dir()):
        if any((child / name).exists() for name in marker_files):
            out.append(child.resolve())
    return out


def _dataset_status(
    dataset_dir: Path,
    *,
    min_sample: int,
    min_mark_points: int,
    mark_stale_hours: int,
    now: datetime,
) -> dict[str, Any]:
    candidate_snapshots = read_jsonl(dataset_dir / "candidate_snapshots.jsonl")
    filter_decisions = read_jsonl(dataset_dir / "filter_decisions.jsonl")
    mark_snapshots = read_jsonl(dataset_dir / "mark_path_snapshots.jsonl")
    outcome_facts = read_jsonl(dataset_dir / "outcome_facts.jsonl")
    analysis = analyze_rows(
        candidate_snapshots=candidate_snapshots,
        filter_decisions=filter_decisions,
        mark_snapshots=mark_snapshots,
        outcome_facts=outcome_facts,
        min_sample=min_sample,
    )
    summary = analysis["summary"]
    evidence_checks = analysis["evidence_checks"]
    has_rejected_universe = summary["counterfactual_candidate_count"] > 0 and summary["filter_decision_count"] > 0
    status, reason = _readiness_status(summary, analysis["outcome_coverage"])
    sampling = _sampling_payload(
        status=status,
        reason=reason,
        dataset_dir=dataset_dir,
        mark_snapshots=mark_snapshots,
        outcome_fact_count=summary["outcome_fact_count"],
        min_sample=summary["min_sample"],
        min_mark_points=min_mark_points,
        mark_stale_hours=mark_stale_hours,
        now=now,
    )
    next_action = sampling["action"]
    return {
        "dataset_id": dataset_dir.name,
        "dataset_dir": str(dataset_dir),
        "status": status,
        "reason": reason,
        "next_suggested_action": next_action,
        "candidate_snapshot_count": summary["candidate_snapshot_count"],
        "filter_decision_count": summary["filter_decision_count"],
        "has_rejected_universe": bool(has_rejected_universe),
        "counterfactual_candidate_count": summary["counterfactual_candidate_count"],
        "mark_path_snapshot_count": summary["mark_path_snapshot_count"],
        "usable_mark_path_snapshot_count": summary["usable_mark_path_snapshot_count"],
        "missing_mark_path_snapshot_count": summary["missing_mark_path_snapshot_count"],
        "last_mark_at": _last_mark_at(mark_snapshots),
        "outcome_fact_count": summary["outcome_fact_count"],
        "missing_outcome_instrument_count": analysis["outcome_coverage"]["missing_outcome_instrument_count"],
        "sampling": sampling,
        "min_sample": summary["min_sample"],
        "evidence_level": summary["evidence_level"],
        "evidence_checks": {
            "has_candidate_universe": evidence_checks["has_candidate_universe"],
            "has_filter_decisions": evidence_checks["has_filter_decisions"],
            "has_rejected_or_post_filtered_samples": evidence_checks["has_rejected_or_post_filtered_samples"],
            "has_mark_path_snapshots": evidence_checks["has_mark_path_snapshots"],
            "has_usable_mark_path_snapshots": evidence_checks["has_usable_mark_path_snapshots"],
            "has_outcome_facts": evidence_checks["has_outcome_facts"],
            "survivorship_bias_risk": evidence_checks["survivorship_bias_risk"],
        },
    }


def _readiness_status(summary: dict[str, Any], outcome_coverage: dict[str, Any]) -> tuple[str, str]:
    reason = str(summary.get("reason") or "")
    status = str(summary.get("status") or "not_ready")
    if reason in {"mark_path_snapshots_missing", "usable_mark_path_snapshots_missing"}:
        return "ready_for_sampling", reason
    if reason == "outcome_facts_missing":
        return "ready_for_settlement", reason
    if status == "needs_human_review" and int(outcome_coverage.get("missing_outcome_instrument_count") or 0) > 0:
        return "ready_for_settlement", "outcome_facts_incomplete"
    return status, reason


def _sampling_payload(
    *,
    status: str,
    reason: str,
    dataset_dir: Path,
    mark_snapshots: list[dict[str, Any]],
    outcome_fact_count: int,
    min_sample: int,
    min_mark_points: int,
    mark_stale_hours: int,
    now: datetime,
) -> dict[str, Any]:
    last_mark_at = _last_mark_at(mark_snapshots)
    mark_age_hours = _mark_age_hours(last_mark_at, now=now)
    usable_mark_point_count = _usable_mark_point_count(mark_snapshots)
    is_stale = bool(mark_age_hours is not None and mark_age_hours > mark_stale_hours)
    base = {
        "min_mark_points": min_mark_points,
        "usable_mark_point_count": usable_mark_point_count,
        "mark_stale_hours": mark_stale_hours,
        "mark_age_hours": mark_age_hours,
        "is_mark_stale": is_stale,
    }
    if status == "ready_for_sampling":
        state = "needs_initial_mark" if reason == "mark_path_snapshots_missing" else "needs_usable_mark"
        return {
            **base,
            "state": state,
            "priority": "high",
            "action": "collect_marks",
            **_commands(dataset_dir, action="collect_marks", min_sample=min_mark_points),
        }
    if status == "ready_for_settlement" and outcome_fact_count <= 0 and usable_mark_point_count < min_mark_points:
        return {
            **base,
            "state": "needs_more_path_samples",
            "priority": "medium",
            "action": "collect_marks",
            **_commands(dataset_dir, action="collect_marks", min_sample=min_mark_points),
        }
    if status == "ready_for_settlement":
        return {
            **base,
            "state": "ready_to_settle",
            "priority": "high",
            "action": "settle",
            **_commands(dataset_dir, action="settle", min_sample=min_mark_points),
        }
    if status == "needs_human_review":
        return {
            **base,
            "state": "ready_to_analyze",
            "priority": "low",
            "action": "analyze",
            **_commands(dataset_dir, action="analyze", min_sample=min_sample),
        }
    return {
        **base,
        "state": "blocked" if status == "evidence_incomplete" else "waiting_for_candidate_evidence",
        "priority": "none",
        "action": "wait",
        "suggested_command": None,
        "suggested_opend_command": None,
    }


def _last_mark_at(rows: list[dict[str, Any]]) -> str | None:
    values = [_mark_time(row) for row in rows]
    values = [value for value in values if value]
    return max(values) if values else None


def _mark_time(row: dict[str, Any]) -> str:
    return text(row.get("mark_at") or row.get("as_of") or row.get("timestamp") or row.get("time") or row.get("date"))


def _usable_mark_point_count(rows: list[dict[str, Any]]) -> int:
    times: set[str] = set()
    has_unknown_time = False
    for row in rows:
        if not is_usable_mark(row):
            continue
        mark_time = _mark_time(row)
        if mark_time:
            times.add(mark_time)
        else:
            has_unknown_time = True
    return len(times) + (1 if has_unknown_time else 0)


def _mark_age_hours(last_mark_at: str | None, *, now: datetime) -> float | None:
    mark_dt = _parse_datetime(last_mark_at)
    if mark_dt is None:
        return None
    return round(max(0.0, (now - mark_dt).total_seconds() / 3600.0), 2)


def _data_plan_rows(datasets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for dataset in datasets:
        sampling = dataset["sampling"]
        if sampling["action"] not in {"collect_marks", "settle"}:
            continue
        rows.append(
            {
                "dataset_id": dataset["dataset_id"],
                "dataset_dir": dataset["dataset_dir"],
                "status": dataset["status"],
                "reason": dataset["reason"],
                "action": sampling["action"],
                "priority": sampling["priority"],
                "state": sampling["state"],
                "last_mark_at": dataset["last_mark_at"],
                "mark_age_hours": sampling["mark_age_hours"],
                "usable_mark_point_count": sampling["usable_mark_point_count"],
                "suggested_command": sampling["suggested_command"],
                "suggested_opend_command": sampling["suggested_opend_command"],
            }
        )
    return sorted(rows, key=_plan_sort_key)


def _review_queue_rows(datasets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for dataset in datasets:
        sampling = dataset["sampling"]
        if sampling["action"] != "analyze":
            continue
        rows.append(
            {
                "dataset_id": dataset["dataset_id"],
                "dataset_dir": dataset["dataset_dir"],
                "status": dataset["status"],
                "reason": dataset["reason"],
                "action": sampling["action"],
                "priority": sampling["priority"],
                "state": sampling["state"],
                "last_mark_at": dataset["last_mark_at"],
                "mark_age_hours": sampling["mark_age_hours"],
                "usable_mark_point_count": sampling["usable_mark_point_count"],
                "outcome_fact_count": dataset["outcome_fact_count"],
                "missing_outcome_instrument_count": dataset["missing_outcome_instrument_count"],
                "suggested_command": sampling["suggested_command"],
            }
        )
    return sorted(rows, key=_plan_sort_key)


def _plan_sort_key(row: dict[str, Any]) -> tuple[int, float, str]:
    age = row.get("mark_age_hours")
    age_value = -1.0 if age is None else -float(age)
    return (_PLAN_PRIORITY.get(str(row.get("priority") or "none"), 9), age_value, str(row.get("dataset_id") or ""))


def _commands(dataset_dir: Path, *, action: str, min_sample: int) -> dict[str, str | None]:
    dataset = str(dataset_dir)
    if action == "collect_marks":
        return {
            "suggested_command": (
                f"./om research shadow-replay collect-marks --dataset {dataset} "
                "--source local --required-data-root output_shared/required_data --write"
            ),
            "suggested_opend_command": (
                f"./om research shadow-replay collect-marks --dataset {dataset} "
                "--source opend --required-data-root output_shared/required_data --write"
            ),
        }
    if action == "settle":
        return {
            "suggested_command": f"./om research shadow-replay settle --dataset {dataset} --write",
            "suggested_opend_command": None,
        }
    if action == "analyze":
        return {
            "suggested_command": f"./om research shadow-replay analyze --dataset {dataset} --min-sample {min_sample}",
            "suggested_opend_command": None,
        }
    return {"suggested_command": None, "suggested_opend_command": None}


def _coerce_now(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = _parse_datetime(value)
    return parsed or datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    raw = text(value)
    if not raw:
        return None
    if len(raw) == 10 and raw[4:5] == "-" and raw[7:8] == "-":
        raw = f"{raw}T00:00:00+00:00"
    elif raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
