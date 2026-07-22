from __future__ import annotations

from pathlib import Path
from typing import Any

from src.application.shadow_replay.candidate_analysis import *  # noqa: F403
from src.application.shadow_replay.candidate_analysis import _evidence_level, analyze_rows
from src.application.shadow_replay.common import (
    ANALYSIS_SCHEMA_VERSION,
    OPTIONAL_CLOSE_DATASET_FILES,
    dataset_dir_from_arg,
    read_jsonl,
    resolve_output_path,
    safety_payload,
    utc_now,
    write_json,
)


def analyze_shadow_replay_dataset(
    *,
    dataset: str | Path,
    min_sample: int = 30,
    output: str | Path | None = None,
) -> dict[str, Any]:
    """Analyze candidate evidence and attach the optional Close Advice facet."""

    dataset_dir = dataset_dir_from_arg(dataset)
    analysis = analyze_rows(
        candidate_snapshots=read_jsonl(dataset_dir / "candidate_snapshots.jsonl"),
        filter_decisions=read_jsonl(dataset_dir / "filter_decisions.jsonl"),
        mark_snapshots=read_jsonl(dataset_dir / "mark_path_snapshots.jsonl"),
        outcome_facts=read_jsonl(dataset_dir / "outcome_facts.jsonl"),
        min_sample=max(1, int(min_sample)),
    )
    analysis.update(
        {
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "dataset_dir": str(dataset_dir),
            "generated_at_utc": utc_now(),
            "safety": safety_payload(writes_local_dataset=False),
        }
    )
    close_episode_path = dataset_dir / OPTIONAL_CLOSE_DATASET_FILES[0]
    if close_episode_path.is_file():
        from src.application.shadow_replay.close_policy_analysis import (
            analyze_close_policy_rows,
        )
        from src.application.shadow_replay.readiness import (
            summarize_close_decision_readiness,
        )

        close_episodes = read_jsonl(close_episode_path)
        close_marks = read_jsonl(dataset_dir / OPTIONAL_CLOSE_DATASET_FILES[1])
        close_outcomes = read_jsonl(dataset_dir / OPTIONAL_CLOSE_DATASET_FILES[2])
        close_readiness = summarize_close_decision_readiness(
            episodes=close_episodes,
            marks=close_marks,
            outcomes=close_outcomes,
            min_sample=30,
        )
        analysis["close_decision_readiness"] = close_readiness
        analysis["close_policy_analysis"] = analyze_close_policy_rows(
            episodes=close_episodes,
            outcomes=close_outcomes,
            readiness=close_readiness,
        )
    if output:
        write_json(resolve_output_path(output), analysis)
    return analysis
