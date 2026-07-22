from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from src.application.shadow_replay.candidate_analysis import analyze_rows
from src.application.shadow_replay.capture import (
    accepted_candidate_snapshots,
    candidate_snapshots_from_filter_decisions,
    dedupe_snapshots,
    filter_decision_rows,
    read_replay_rows,
)
from src.application.shadow_replay.common import (
    CLOSE_DECISION_EPISODE_SCHEMA_VERSION,
    CLOSE_DECISION_MARK_SCHEMA_VERSION,
    CLOSE_DECISION_OUTCOME_SCHEMA_VERSION,
    MARK_PATH_SCHEMA_VERSION,
    OPTIONAL_CLOSE_DATASET_FILES,
    OUTCOME_FACT_SCHEMA_VERSION,
    READINESS_SCHEMA_VERSION,
    read_jsonl,
    resolve_many,
    safe_rel,
    safety_payload,
    text,
    unique,
)


CLOSE_READINESS_SCHEMA_VERSION = "shadow_replay_close_readiness.v1"
_CLOSE_POLICIES = (
    "P0_current",
    "P1_semantic_split",
    "P2_profile_aware",
    "P3_opportunity_required",
)
_CLOSE_HORIZONS = ("1d", "3d", "7d", "14d", "expiry")
_CLOSE_OUTCOME_KINDS = (
    "horizon_1d",
    "horizon_3d",
    "horizon_7d",
    "horizon_14d",
    "terminal",
)
_FEE_USABLE_STATUSES = {"schedule_estimate", "conservative_estimate"}
_RECOMMENDATION_STATES = {"close", "review", "hold", "not_evaluable"}
_EPISODE_QUOTE_TIME_BASES = {
    "run_anchor",
    "quote_as_of_utc",
    "quote_timestamp_utc",
    "quote_timestamp",
    "quote_time_utc",
    "quote_time",
}


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
    result = {
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
    close_paths = _close_facet_paths(
        resolved_candidates
        + resolved_traces
        + resolved_reject_logs
        + resolved_marks
        + resolved_outcomes
    )
    if close_paths:
        result["source"]["close_decision_paths"] = {
            name: [safe_rel(path, base=root) for path in paths]
            for name, paths in close_paths.items()
        }
        result["close_decision_readiness"] = summarize_close_decision_readiness(
            episodes=_read_close_rows(
                close_paths[OPTIONAL_CLOSE_DATASET_FILES[0]],
            ),
            marks=_read_close_rows(
                close_paths[OPTIONAL_CLOSE_DATASET_FILES[1]],
            ),
            outcomes=_read_close_rows(
                close_paths[OPTIONAL_CLOSE_DATASET_FILES[2]],
            ),
            min_sample=30,
        )
    return result


def summarize_close_decision_readiness(
    *,
    episodes: list[dict[str, Any]],
    marks: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    min_sample: int = 30,
    min_segment_sample: int = 10,
    min_usable_outcome_ratio: float = 0.8,
) -> dict[str, Any]:
    """Return mechanical Close Advice evidence coverage without judging policy quality."""

    overall_floor = max(1, int(min_sample))
    segment_floor = max(1, int(min_segment_sample))
    ratio_floor = min(1.0, max(0.0, float(min_usable_outcome_ratio)))
    invalid_episode_schema_count = sum(
        text(row.get("schema_version")) != CLOSE_DECISION_EPISODE_SCHEMA_VERSION
        for row in episodes
    )
    invalid_mark_schema_count = sum(
        text(row.get("schema_version")) != CLOSE_DECISION_MARK_SCHEMA_VERSION
        for row in marks
    )
    invalid_outcome_schema_count = sum(
        text(row.get("schema_version")) != CLOSE_DECISION_OUTCOME_SCHEMA_VERSION
        for row in outcomes
    )
    valid_episodes = [
        row
        for row in episodes
        if text(row.get("schema_version")) == CLOSE_DECISION_EPISODE_SCHEMA_VERSION
    ]
    valid_marks = [
        row
        for row in marks
        if text(row.get("schema_version")) == CLOSE_DECISION_MARK_SCHEMA_VERSION
    ]
    valid_outcomes = [
        row
        for row in outcomes
        if text(row.get("schema_version")) == CLOSE_DECISION_OUTCOME_SCHEMA_VERSION
    ]
    dedupe = _dedupe_close_episodes(valid_episodes)
    unique_episodes = dedupe["episodes"]
    episode_by_id = {
        text(row.get("episode_id")): row
        for row in unique_episodes
        if text(row.get("episode_id"))
    }
    marks_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    orphan_mark_count = 0
    invalid_mark_horizon_count = 0
    for mark in valid_marks:
        episode_id = text(mark.get("episode_id"))
        if episode_id not in episode_by_id:
            orphan_mark_count += 1
            continue
        if text(mark.get("horizon")).lower() not in _CLOSE_HORIZONS:
            invalid_mark_horizon_count += 1
            continue
        marks_by_episode[episode_id].append(mark)
    outcome_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    orphan_outcome_count = 0
    invalid_outcome_kind_count = 0
    for outcome in valid_outcomes:
        episode_id = text(outcome.get("episode_id"))
        outcome_kind = text(outcome.get("outcome_kind")).lower()
        if episode_id not in episode_by_id:
            orphan_outcome_count += 1
            continue
        if outcome_kind not in _CLOSE_OUTCOME_KINDS:
            invalid_outcome_kind_count += 1
            continue
        outcome_groups[(episode_id, outcome_kind)].append(outcome)
    outcome_by_key = {
        key: _preferred_close_outcome(rows)
        for key, rows in outcome_groups.items()
    }
    outcome_duplicate_count = sum(max(0, len(rows) - 1) for rows in outcome_groups.values())
    missing_outcome_key_count = sum(
        (episode_id, kind) not in outcome_groups
        for episode_id in episode_by_id
        for kind in _CLOSE_OUTCOME_KINDS
    )

    policy_complete_ids = {
        episode_id
        for episode_id, episode in episode_by_id.items()
        if _paired_policy_complete(episode)
    }
    outcome_policy_pair_complete_ids = {
        episode_id
        for episode_id, episode in episode_by_id.items()
        if all(
            (
                outcome := outcome_by_key.get((episode_id, kind))
            ) is not None
            and _outcome_policy_pair_complete(outcome, episode=episode)
            for kind in _CLOSE_OUTCOME_KINDS
        )
    }
    strategy_complete_ids = {
        episode_id
        for episode_id, episode in episode_by_id.items()
        if _segment_key(episode) is not None
    }
    strategy_time_complete_ids = {
        episode_id
        for episode_id, episode in episode_by_id.items()
        if _strategy_point_in_time_complete(episode)
    }
    replacement_required_ids = {
        episode_id
        for episode_id, episode in episode_by_id.items()
        if text((episode.get("replacement_evidence") or {}).get("status")).lower()
        == "review_switch"
    }
    replacement_time_complete_ids = {
        episode_id
        for episode_id, episode in episode_by_id.items()
        if _replacement_point_in_time_complete(episode)
    }
    decision_fee_complete_ids = {
        episode_id
        for episode_id, episode in episode_by_id.items()
        if _decision_fee_complete(episode)
    }
    episode_time_complete_ids = {
        episode_id
        for episode_id, episode in episode_by_id.items()
        if _episode_point_in_time_complete(episode)
    }
    usable_outcomes_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    promotion_outcomes_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (episode_id, _kind), outcome in outcome_by_key.items():
        if text(outcome.get("evidence_status")).lower() != "usable":
            continue
        usable_outcomes_by_episode[episode_id].append(outcome)
        if (
            episode_id in policy_complete_ids
            and episode_id in outcome_policy_pair_complete_ids
            and episode_id in strategy_complete_ids
            and episode_id in strategy_time_complete_ids
            and (
                episode_id not in replacement_required_ids
                or episode_id in replacement_time_complete_ids
            )
            and episode_id in decision_fee_complete_ids
            and episode_id in episode_time_complete_ids
            and _outcome_point_in_time_complete(outcome)
            and _outcome_fee_complete(outcome)
        ):
            promotion_outcomes_by_episode[episode_id].append(outcome)

    settled_ids = set(usable_outcomes_by_episode)
    promotion_usable_ids = set(promotion_outcomes_by_episode)
    mark_coverage = _close_mark_window_coverage(
        episode_ids=set(episode_by_id),
        marks_by_episode=marks_by_episode,
    )
    terminal_coverage = _close_terminal_coverage(
        episode_ids=set(episode_by_id),
        outcome_by_key=outcome_by_key,
    )
    fee_coverage = _close_fee_coverage(
        episode_ids=set(episode_by_id),
        decision_fee_complete_ids=decision_fee_complete_ids,
        usable_outcomes_by_episode=usable_outcomes_by_episode,
        promotion_outcomes_by_episode=promotion_outcomes_by_episode,
    )
    paired_coverage = _close_paired_policy_coverage(
        episode_by_id=episode_by_id,
        policy_complete_ids=policy_complete_ids,
        outcome_policy_pair_complete_ids=outcome_policy_pair_complete_ids,
        promotion_outcomes_by_episode=promotion_outcomes_by_episode,
    )
    point_in_time_coverage = _close_point_in_time_coverage(
        episode_ids=set(episode_by_id),
        episode_time_complete_ids=episode_time_complete_ids,
        strategy_time_complete_ids=strategy_time_complete_ids,
        replacement_required_ids=replacement_required_ids,
        replacement_time_complete_ids=replacement_time_complete_ids,
        marks_by_episode=marks_by_episode,
        outcome_by_key=outcome_by_key,
    )
    segments = _close_segment_readiness(
        episode_by_id=episode_by_id,
        settled_ids=settled_ids,
        promotion_usable_ids=promotion_usable_ids,
        min_segment_sample=segment_floor,
        min_usable_outcome_ratio=ratio_floor,
    )
    promotable_segments = [
        {
            "strategy_profile": row["strategy_profile"],
            "strategy_family": row["strategy_family"],
        }
        for row in segments
        if row["mechanically_ready"]
    ]
    inconclusive_reasons = Counter(
        text(outcome.get("inconclusive_reason")).lower() or "unspecified"
        for outcome in outcome_by_key.values()
        if text(outcome.get("evidence_status")).lower() == "inconclusive"
    )
    blockers: list[str] = []
    if len(settled_ids) < overall_floor:
        blockers.append("settled_unique_episodes_below_minimum")
    if dedupe["missing_episode_id_count"]:
        blockers.append("episode_id_missing")
    if dedupe["duplicate_episode_row_count"]:
        blockers.append("episode_rows_not_deduplicated")
    if dedupe["conflicting_episode_id_count"]:
        blockers.append("episode_id_conflict")
    if outcome_duplicate_count:
        blockers.append("duplicate_outcome_keys")
    if missing_outcome_key_count:
        blockers.append("close_outcome_matrix_incomplete")
    if orphan_mark_count or orphan_outcome_count:
        blockers.append("close_facet_orphan_rows")
    if invalid_mark_horizon_count or invalid_outcome_kind_count:
        blockers.append("close_facet_enum_invalid")
    if invalid_episode_schema_count or invalid_mark_schema_count or invalid_outcome_schema_count:
        blockers.append("close_facet_schema_mismatch")
    if not promotable_segments:
        blockers.append("no_profile_family_segment_meets_sample_and_coverage")
    status = "ready_for_paired_policy_analysis" if not blockers else "collecting_close_evidence"
    reason = "mechanical_readiness_passed" if not blockers else blockers[0]
    episode_count = len(episode_by_id)
    return {
        "schema_version": CLOSE_READINESS_SCHEMA_VERSION,
        "status": status,
        "reason": reason,
        "thresholds": {
            "min_settled_unique_episodes_overall": overall_floor,
            "min_settled_unique_episodes_per_segment": segment_floor,
            "min_usable_outcome_ratio_per_segment": ratio_floor,
        },
        "episode_coverage": {
            "raw_episode_row_count": len(episodes),
            "valid_episode_row_count": len(valid_episodes),
            "unique_episode_count": episode_count,
            "unique_lot_count": len(
                {
                    (text(row.get("account")).lower(), text(row.get("position_lot_id")))
                    for row in unique_episodes
                    if text(row.get("account")) and text(row.get("position_lot_id"))
                }
            ),
            "settled_unique_episode_count": len(settled_ids),
            "promotion_usable_episode_count": len(promotion_usable_ids),
            "promotion_usable_episode_ratio": _ratio(len(promotion_usable_ids), episode_count),
            "repeated_tick_observation_count": dedupe["repeated_tick_observation_count"],
            "duplicate_episode_row_count": dedupe["duplicate_episode_row_count"],
            "conflicting_episode_id_count": dedupe["conflicting_episode_id_count"],
            "missing_episode_id_count": dedupe["missing_episode_id_count"],
            "episode_grain_deduped": not (
                dedupe["duplicate_episode_row_count"]
                or dedupe["conflicting_episode_id_count"]
                or dedupe["missing_episode_id_count"]
            ),
        },
        "mark_window_coverage": mark_coverage,
        "terminal_lifecycle_coverage": terminal_coverage,
        "fee_coverage": fee_coverage,
        "strategy_coverage": {
            "episode_count": episode_count,
            "profile_family_complete_episode_count": len(strategy_complete_ids),
            "profile_family_complete_ratio": _ratio(
                len(strategy_complete_ids), episode_count
            ),
            "strategy_time_complete_episode_count": len(strategy_time_complete_ids),
            "strategy_time_complete_ratio": _ratio(
                len(strategy_time_complete_ids), episode_count
            ),
        },
        "paired_policy_coverage": paired_coverage,
        "point_in_time_coverage": point_in_time_coverage,
        "segments": segments,
        "promotable_segments": promotable_segments,
        "analysis_eligibility": {
            "promotion_usable_episode_ids": sorted(promotion_usable_ids),
            "promotion_usable_outcome_keys": [
                {"episode_id": episode_id, "outcome_kind": text(row.get("outcome_kind")).lower()}
                for episode_id, rows in sorted(promotion_outcomes_by_episode.items())
                for row in sorted(rows, key=lambda item: text(item.get("outcome_kind")).lower())
            ],
        },
        "inconclusive_reasons": dict(sorted(inconclusive_reasons.items())),
        "outcome_duplicate_key_count": outcome_duplicate_count,
        "outcome_matrix": {
            "expected_outcome_key_count": len(episode_by_id) * len(_CLOSE_OUTCOME_KINDS),
            "present_outcome_key_count": len(outcome_by_key),
            "missing_outcome_key_count": missing_outcome_key_count,
            "complete": missing_outcome_key_count == 0,
        },
        "join_integrity": {
            "orphan_mark_count": orphan_mark_count,
            "orphan_outcome_count": orphan_outcome_count,
            "invalid_mark_horizon_count": invalid_mark_horizon_count,
            "invalid_outcome_kind_count": invalid_outcome_kind_count,
            "passed": not (
                orphan_mark_count
                or orphan_outcome_count
                or invalid_mark_horizon_count
                or invalid_outcome_kind_count
            ),
        },
        "schema_checks": {
            "invalid_episode_schema_count": invalid_episode_schema_count,
            "invalid_mark_schema_count": invalid_mark_schema_count,
            "invalid_outcome_schema_count": invalid_outcome_schema_count,
            "passed": not (
                invalid_episode_schema_count
                or invalid_mark_schema_count
                or invalid_outcome_schema_count
            ),
        },
        "blockers": blockers,
        "next_action": _close_readiness_next_action(
            episode_count=episode_count,
            mark_coverage=mark_coverage,
            outcome_count=len(outcome_by_key),
            blockers=blockers,
        ),
        "policy_quality_judgment": "not_evaluated",
        "production_promotion_allowed": False,
        "safety": safety_payload(writes_local_dataset=False),
    }


def _dedupe_close_episodes(episodes: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    missing = 0
    for episode in episodes:
        episode_id = text(episode.get("episode_id"))
        if not episode_id:
            missing += 1
            continue
        grouped[episode_id].append(episode)
    unique_rows: list[dict[str, Any]] = []
    conflicts = 0
    duplicate_rows = 0
    for episode_id in sorted(grouped):
        rows = grouped[episode_id]
        duplicate_rows += max(0, len(rows) - 1)
        canonical = {_canonical_json(row) for row in rows}
        if len(canonical) > 1:
            conflicts += 1
        unique_rows.append(rows[0])
    return {
        "episodes": unique_rows,
        "missing_episode_id_count": missing,
        "duplicate_episode_row_count": duplicate_rows,
        "conflicting_episode_id_count": conflicts,
        "repeated_tick_observation_count": sum(
            max(0, int(row.get("source_observation_count") or 1) - 1)
            for row in unique_rows
        ),
    }


def _close_mark_window_coverage(
    *,
    episode_ids: set[str],
    marks_by_episode: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    by_horizon: dict[str, Any] = {}
    for horizon in _CLOSE_HORIZONS:
        episodes_with_mark: set[str] = set()
        verified_episodes: set[str] = set()
        unverified_episodes: set[str] = set()
        for episode_id in episode_ids:
            horizon_marks = [
                row
                for row in marks_by_episode.get(episode_id, [])
                if text(row.get("horizon")).lower() == horizon
            ]
            if not horizon_marks:
                continue
            episodes_with_mark.add(episode_id)
            if any(_verified_close_mark(row, horizon=horizon) for row in horizon_marks):
                verified_episodes.add(episode_id)
            elif any(text(row.get("point_in_time_status")) for row in horizon_marks):
                unverified_episodes.add(episode_id)
        by_horizon[horizon] = {
            "episode_with_mark_count": len(episodes_with_mark),
            "verified_usable_episode_count": len(verified_episodes),
            "unverified_episode_count": len(unverified_episodes),
            "missing_episode_count": max(0, len(episode_ids) - len(episodes_with_mark)),
            "verified_usable_ratio": _ratio(len(verified_episodes), len(episode_ids)),
        }
    return {
        "episode_count": len(episode_ids),
        "mark_row_count": sum(len(rows) for rows in marks_by_episode.values()),
        "by_horizon": by_horizon,
        "episode_with_any_verified_mark_count": len(
            {
                episode_id
                for episode_id, rows in marks_by_episode.items()
                if any(
                    _verified_close_mark(row, horizon=text(row.get("horizon")).lower())
                    for row in rows
                )
            }
        ),
    }


def _close_terminal_coverage(
    *,
    episode_ids: set[str],
    outcome_by_key: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    terminals = {
        episode_id: outcome_by_key.get((episode_id, "terminal"))
        for episode_id in episode_ids
    }
    usable = {
        episode_id
        for episode_id, row in terminals.items()
        if isinstance(row, dict) and text(row.get("evidence_status")).lower() == "usable"
    }
    canonical = {
        episode_id
        for episode_id, row in terminals.items()
        if isinstance(row, dict) and text(row.get("source")).lower() == "canonical_lifecycle_fact"
    }
    expiry = {
        episode_id
        for episode_id, row in terminals.items()
        if isinstance(row, dict) and text(row.get("source")).lower() == "expiration_mark"
    }
    reasons = Counter(
        text(row.get("inconclusive_reason")).lower() or "unspecified"
        for row in terminals.values()
        if isinstance(row, dict) and text(row.get("evidence_status")).lower() == "inconclusive"
    )
    return {
        "episode_count": len(episode_ids),
        "terminal_outcome_count": sum(isinstance(row, dict) for row in terminals.values()),
        "usable_terminal_episode_count": len(usable),
        "usable_terminal_ratio": _ratio(len(usable), len(episode_ids)),
        "canonical_lifecycle_episode_count": len(canonical),
        "expiration_mark_episode_count": len(expiry),
        "inconclusive_terminal_reasons": dict(sorted(reasons.items())),
    }


def _close_fee_coverage(
    *,
    episode_ids: set[str],
    decision_fee_complete_ids: set[str],
    usable_outcomes_by_episode: dict[str, list[dict[str, Any]]],
    promotion_outcomes_by_episode: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    outcome_fee_complete_ids = {
        episode_id
        for episode_id, rows in usable_outcomes_by_episode.items()
        if any(_outcome_fee_complete(row) for row in rows)
    }
    return {
        "episode_count": len(episode_ids),
        "decision_fee_complete_episode_count": len(decision_fee_complete_ids),
        "decision_fee_complete_ratio": _ratio(len(decision_fee_complete_ids), len(episode_ids)),
        "usable_outcome_fee_complete_episode_count": len(outcome_fee_complete_ids),
        "promotion_fee_complete_episode_count": len(promotion_outcomes_by_episode),
        "promotion_fee_complete_ratio": _ratio(len(promotion_outcomes_by_episode), len(episode_ids)),
    }


def _close_paired_policy_coverage(
    *,
    episode_by_id: dict[str, dict[str, Any]],
    policy_complete_ids: set[str],
    outcome_policy_pair_complete_ids: set[str],
    promotion_outcomes_by_episode: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    p3_opportunity_ids = {
        episode_id
        for episode_id, episode in episode_by_id.items()
        if text((episode.get("replacement_evidence") or {}).get("status")).lower()
        == "review_switch"
    }
    p3_same_horizon_ids = {
        episode_id
        for episode_id, rows in promotion_outcomes_by_episode.items()
        if any(text(row.get("replacement_outcome_status")).lower() == "usable" for row in rows)
    }
    return {
        "episode_count": len(episode_by_id),
        "all_four_policy_projection_count": len(policy_complete_ids),
        "all_four_policy_projection_ratio": _ratio(len(policy_complete_ids), len(episode_by_id)),
        "complete_outcome_policy_pair_episode_count": len(
            outcome_policy_pair_complete_ids
        ),
        "complete_outcome_policy_pair_ratio": _ratio(
            len(outcome_policy_pair_complete_ids), len(episode_by_id)
        ),
        "paired_policy_with_promotion_outcome_count": len(
            policy_complete_ids & set(promotion_outcomes_by_episode)
        ),
        "p3_opportunity_episode_count": len(p3_opportunity_ids),
        "p3_same_horizon_usable_episode_count": len(p3_opportunity_ids & p3_same_horizon_ids),
        "p3_same_horizon_usable_ratio": _ratio(
            len(p3_opportunity_ids & p3_same_horizon_ids),
            len(p3_opportunity_ids),
        ),
    }


def _close_point_in_time_coverage(
    *,
    episode_ids: set[str],
    episode_time_complete_ids: set[str],
    strategy_time_complete_ids: set[str],
    replacement_required_ids: set[str],
    replacement_time_complete_ids: set[str],
    marks_by_episode: dict[str, list[dict[str, Any]]],
    outcome_by_key: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    verified_mark_ids = {
        episode_id
        for episode_id, rows in marks_by_episode.items()
        if any(
            text(row.get("point_in_time_status")).lower() == "verified_fresh_collection"
            for row in rows
        )
    }
    verified_outcome_ids = {
        episode_id
        for (episode_id, _kind), row in outcome_by_key.items()
        if text(row.get("evidence_status")).lower() == "usable"
        and _outcome_point_in_time_complete(row)
    }
    return {
        "episode_count": len(episode_ids),
        "decision_quote_time_complete_episode_count": len(episode_time_complete_ids),
        "decision_quote_time_complete_ratio": _ratio(
            len(episode_time_complete_ids), len(episode_ids)
        ),
        "strategy_time_complete_episode_count": len(strategy_time_complete_ids),
        "strategy_time_complete_ratio": _ratio(
            len(strategy_time_complete_ids), len(episode_ids)
        ),
        "replacement_timestamp_required_episode_count": len(replacement_required_ids),
        "replacement_timestamp_complete_episode_count": len(
            replacement_required_ids & replacement_time_complete_ids
        ),
        "replacement_timestamp_complete_ratio": _ratio(
            len(replacement_required_ids & replacement_time_complete_ids),
            len(replacement_required_ids),
        ),
        "verified_mark_episode_count": len(verified_mark_ids),
        "verified_mark_episode_ratio": _ratio(len(verified_mark_ids), len(episode_ids)),
        "verified_usable_outcome_episode_count": len(verified_outcome_ids),
        "verified_usable_outcome_ratio": _ratio(len(verified_outcome_ids), len(episode_ids)),
    }


def _close_segment_readiness(
    *,
    episode_by_id: dict[str, dict[str, Any]],
    settled_ids: set[str],
    promotion_usable_ids: set[str],
    min_segment_sample: int,
    min_usable_outcome_ratio: float,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    for episode_id, episode in episode_by_id.items():
        segment = _segment_key(episode)
        if segment is not None:
            groups[segment].add(episode_id)
    out: list[dict[str, Any]] = []
    for (profile, family), ids in sorted(groups.items()):
        segment_settled = ids & settled_ids
        segment_usable = ids & promotion_usable_ids
        coverage = _ratio(len(segment_usable), len(ids))
        sample_pass = len(segment_usable) >= min_segment_sample
        coverage_pass = coverage >= min_usable_outcome_ratio
        out.append(
            {
                "strategy_profile": profile,
                "strategy_family": family,
                "episode_count": len(ids),
                "settled_unique_episode_count": len(segment_settled),
                "promotion_usable_episode_count": len(segment_usable),
                "promotion_usable_ratio": coverage,
                "min_sample_pass": sample_pass,
                "usable_outcome_ratio_pass": coverage_pass,
                "mechanically_ready": sample_pass and coverage_pass,
            }
        )
    return out


def _paired_policy_complete(episode: dict[str, Any]) -> bool:
    projections = episode.get("shadow_policy_results")
    if not isinstance(projections, dict):
        return False
    for policy in _CLOSE_POLICIES:
        row = projections.get(policy)
        if not isinstance(row, dict):
            return False
        if text(row.get("recommendation_state")).lower() not in _RECOMMENDATION_STATES:
            return False
    return True


def _outcome_policy_pair_complete(
    outcome: dict[str, Any],
    *,
    episode: dict[str, Any],
) -> bool:
    actual = outcome.get("policy_recommendations")
    projections = episode.get("shadow_policy_results")
    if not isinstance(actual, dict) or not isinstance(projections, dict):
        return False
    expected = {
        policy: text((projections.get(policy) or {}).get("recommendation_state")).lower()
        for policy in _CLOSE_POLICIES
    }
    return {
        policy: text(actual.get(policy)).lower()
        for policy in _CLOSE_POLICIES
    } == expected


def _segment_key(episode: dict[str, Any]) -> tuple[str, str] | None:
    facts = episode.get("normalized_decision_facts")
    facts = facts if isinstance(facts, dict) else {}
    profile = text(facts.get("strategy_profile")).lower()
    family = text(facts.get("strategy_family")).lower()
    return (profile, family) if profile and family else None


def _decision_fee_complete(episode: dict[str, Any]) -> bool:
    economics = episode.get("decision_economics")
    economics = economics if isinstance(economics, dict) else {}
    return (
        text(economics.get("evidence_status")).lower() == "complete"
        and text(economics.get("fee_calc_status")).lower() in _FEE_USABLE_STATUSES
        and economics.get("decision_close_fee") is not None
        and economics.get("close_now_cost") is not None
    )


def _episode_point_in_time_complete(episode: dict[str, Any]) -> bool:
    observed_at = _utc_datetime(episode.get("observed_at_utc"))
    quote_at = _utc_datetime(episode.get("quote_at_utc"))
    return bool(
        observed_at is not None
        and quote_at is not None
        and quote_at <= observed_at
        and text(episode.get("quote_time_basis")).lower() in _EPISODE_QUOTE_TIME_BASES
    )


def _strategy_point_in_time_complete(episode: dict[str, Any]) -> bool:
    observed_at = _utc_datetime(episode.get("observed_at_utc"))
    strategy_at = _utc_datetime(episode.get("strategy_context_at_utc"))
    return bool(
        observed_at is not None
        and strategy_at is not None
        and strategy_at <= observed_at
        and text(episode.get("strategy_time_basis")).lower()
        == "position_context_as_of_utc"
    )


def _replacement_point_in_time_complete(episode: dict[str, Any]) -> bool:
    evidence = episode.get("replacement_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    if text(evidence.get("status")).lower() != "review_switch":
        return True
    provenance = episode.get("replacement_provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    observed_at = _utc_datetime(episode.get("observed_at_utc"))
    source_run_at = _utc_datetime(provenance.get("source_run_at_utc"))
    return bool(
        text(provenance.get("status")).lower() == "validated_same_decision_run"
        and text(provenance.get("source_run_id"))
        and observed_at is not None
        and source_run_at is not None
        and source_run_at <= observed_at
    )


def _outcome_point_in_time_complete(outcome: dict[str, Any]) -> bool:
    source = text(outcome.get("source")).lower()
    if source == "canonical_lifecycle_fact":
        return bool(text(outcome.get("lifecycle_at_utc")))
    return text(outcome.get("point_in_time_status")).lower() == "verified_fresh_collection"


def _outcome_fee_complete(outcome: dict[str, Any]) -> bool:
    source = text(outcome.get("source")).lower()
    if source in {"close_decision_mark", "expiration_mark"}:
        return outcome.get("future_close_fee") is not None
    if source == "canonical_lifecycle_fact":
        outcome_name = text(outcome.get("outcome")).lower()
        return outcome_name in {"closed_later", "expired_worthless"} and outcome.get(
            "future_close_fee"
        ) is not None
    return False


def _verified_close_mark(mark: dict[str, Any], *, horizon: str) -> bool:
    if text(mark.get("point_in_time_status")).lower() != "verified_fresh_collection":
        return False
    if text(mark.get("quote_status")).lower() != "matched":
        return False
    if horizon == "expiry":
        return mark.get("spot") is not None or mark.get("ask") is not None
    return mark.get("ask") is not None and mark.get("future_close_fee") is not None


def _preferred_close_outcome(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(
        rows,
        key=lambda row: (
            text(row.get("evidence_status")).lower() == "usable",
            text(row.get("marked_at_utc") or row.get("lifecycle_at_utc")),
        ),
        reverse=True,
    )[0]


def _close_readiness_next_action(
    *,
    episode_count: int,
    mark_coverage: dict[str, Any],
    outcome_count: int,
    blockers: list[str],
) -> str:
    if episode_count <= 0:
        return "capture_close_decision_episodes"
    if int(mark_coverage.get("episode_with_any_verified_mark_count") or 0) <= 0:
        return "collect_fresh_opend_marks"
    if outcome_count < episode_count * 5:
        return "settle_close_decision_outcomes"
    if blockers:
        return "collect_more_close_decision_evidence"
    return "run_paired_policy_analysis"


def _close_facet_paths(paths: list[Path]) -> dict[str, list[Path]]:
    directories = unique(path.parent for path in paths if path.exists())
    out: dict[str, list[Path]] = {name: [] for name in OPTIONAL_CLOSE_DATASET_FILES}
    for directory in directories:
        for name in OPTIONAL_CLOSE_DATASET_FILES:
            path = directory / name
            if path.is_file():
                out[name].append(path.resolve())
    if not out[OPTIONAL_CLOSE_DATASET_FILES[0]]:
        return {}
    return out


def _read_close_rows(paths: list[Path]) -> list[dict[str, Any]]:
    return [row for path in paths for row in read_jsonl(path)]


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_datetime(value: Any) -> datetime | None:
    raw = text(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator > 0 else 0.0
