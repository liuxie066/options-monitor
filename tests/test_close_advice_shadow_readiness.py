from __future__ import annotations

import json
from pathlib import Path

from src.application.shadow_replay.readiness import summarize_close_decision_readiness


_POLICIES = (
    "P0_current",
    "P1_semantic_split",
    "P2_profile_aware",
    "P3_opportunity_required",
)


def _episode(
    index: int,
    *,
    profile: str = "insurance_underwriting",
    family: str = "sell_put",
    fee_complete: bool = True,
) -> dict[str, object]:
    episode_id = f"episode-{index:03d}"
    return {
        "schema_version": "shadow_replay_close_episode.v1",
        "episode_id": episode_id,
        "account": "lx",
        "position_lot_id": f"lot-{index:03d}",
        "source_observation_count": 2 if index == 0 else 1,
        "observed_at_utc": "2026-07-23T14:00:00Z",
        "quote_at_utc": "2026-07-23T14:00:00Z",
        "quote_time_basis": "run_anchor",
        "strategy_context_at_utc": "2026-07-23T13:59:00Z",
        "strategy_time_basis": "position_context_as_of_utc",
        "normalized_decision_facts": {
            "strategy_profile": profile,
            "strategy_family": family,
        },
        "decision_economics": {
            "evidence_status": "complete" if fee_complete else "incomplete",
            "fee_calc_status": "schedule_estimate" if fee_complete else "unavailable",
            "decision_close_fee": 1.5 if fee_complete else None,
            "close_now_cost": 22.5 if fee_complete else None,
        },
        "replacement_evidence": {"status": "not_evaluable"},
        "shadow_policy_results": {
            policy: {
                "recommendation_state": (
                    "close" if policy == "P0_current" else "hold"
                )
            }
            for policy in _POLICIES
        },
    }


def _mark(episode_id: str, *, verified: bool = True) -> dict[str, object]:
    return {
        "schema_version": "shadow_replay_close_mark.v1",
        "episode_id": episode_id,
        "horizon": "1d",
        "marked_at_utc": "2026-07-24T14:00:00Z",
        "quote_status": "matched",
        "ask": 0.1,
        "future_close_fee": 2.5,
        "point_in_time_status": (
            "verified_fresh_collection" if verified else "unverified_operator_as_of"
        ),
    }


def _outcomes(
    episode_id: str,
    *,
    usable: bool,
    verified: bool = True,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for kind in ("horizon_1d", "horizon_3d", "horizon_7d", "horizon_14d", "terminal"):
        is_usable = usable and kind == "horizon_1d"
        rows.append(
            {
                "schema_version": "shadow_replay_close_outcome.v1",
                "episode_id": episode_id,
                "outcome_kind": kind,
                "evidence_status": "usable" if is_usable else "inconclusive",
                "inconclusive_reason": None if is_usable else "no_usable_mark_in_window",
                "source": "close_decision_mark" if is_usable else None,
                "future_close_fee": 2.5 if is_usable else None,
                "policy_recommendations": {
                    policy: "close" if policy == "P0_current" else "hold"
                    for policy in _POLICIES
                },
                "point_in_time_status": (
                    "verified_fresh_collection"
                    if is_usable and verified
                    else "unverified_operator_as_of"
                    if is_usable
                    else None
                ),
            }
        )
    return rows


def _evidence(
    *,
    first_segment_usable: int = 10,
    second_segment_usable: int = 20,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    episodes: list[dict[str, object]] = []
    marks: list[dict[str, object]] = []
    outcomes: list[dict[str, object]] = []
    for index in range(35):
        first_segment = index < 12
        usable = (
            index < first_segment_usable
            if first_segment
            else index - 12 < second_segment_usable
        )
        episode = _episode(
            index,
            profile=("insurance_underwriting" if first_segment else "return_first"),
        )
        episodes.append(episode)
        if usable:
            marks.append(_mark(str(episode["episode_id"])))
        outcomes.extend(_outcomes(str(episode["episode_id"]), usable=usable))
    return episodes, marks, outcomes


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_close_readiness_passes_exact_overall_segment_and_coverage_boundaries() -> None:
    episodes, marks, outcomes = _evidence()

    result = summarize_close_decision_readiness(
        episodes=episodes,
        marks=marks,
        outcomes=outcomes,
    )

    assert result["status"] == "ready_for_paired_policy_analysis"
    assert result["thresholds"] == {
        "min_settled_unique_episodes_overall": 30,
        "min_settled_unique_episodes_per_segment": 10,
        "min_usable_outcome_ratio_per_segment": 0.8,
    }
    assert result["episode_coverage"]["settled_unique_episode_count"] == 30
    assert result["episode_coverage"]["promotion_usable_episode_count"] == 30
    assert result["episode_coverage"]["repeated_tick_observation_count"] == 1
    assert result["episode_coverage"]["episode_grain_deduped"] is True
    assert result["mark_window_coverage"]["by_horizon"]["1d"][
        "verified_usable_episode_count"
    ] == 30
    assert result["terminal_lifecycle_coverage"]["usable_terminal_episode_count"] == 0
    assert result["fee_coverage"]["promotion_fee_complete_episode_count"] == 30
    assert result["paired_policy_coverage"]["all_four_policy_projection_count"] == 35
    assert len(result["analysis_eligibility"]["promotion_usable_episode_ids"]) == 30
    assert len(result["analysis_eligibility"]["promotion_usable_outcome_keys"]) == 30
    assert len(result["promotable_segments"]) == 2
    assert result["policy_quality_judgment"] == "not_evaluated"
    assert result["production_promotion_allowed"] is False


def test_close_readiness_keeps_under_sample_segment_shadow_only() -> None:
    episodes, marks, outcomes = _evidence(first_segment_usable=9, second_segment_usable=21)

    result = summarize_close_decision_readiness(
        episodes=episodes,
        marks=marks,
        outcomes=outcomes,
    )
    by_profile = {row["strategy_profile"]: row for row in result["segments"]}

    assert result["episode_coverage"]["settled_unique_episode_count"] == 30
    assert by_profile["insurance_underwriting"]["promotion_usable_ratio"] == 0.75
    assert by_profile["insurance_underwriting"]["min_sample_pass"] is False
    assert by_profile["insurance_underwriting"]["usable_outcome_ratio_pass"] is False
    assert by_profile["insurance_underwriting"]["mechanically_ready"] is False
    assert by_profile["return_first"]["mechanically_ready"] is True
    assert result["status"] == "ready_for_paired_policy_analysis"
    assert result["promotable_segments"] == [
        {"strategy_profile": "return_first", "strategy_family": "sell_put"}
    ]


def test_close_readiness_fails_closed_on_point_time_fee_and_duplicate_rows() -> None:
    episodes, marks, outcomes = _evidence()
    episodes.append(dict(episodes[0]))
    episodes[1]["decision_economics"] = {
        "evidence_status": "incomplete",
        "fee_calc_status": "unavailable",
        "decision_close_fee": None,
        "close_now_cost": None,
    }
    for mark in marks:
        if mark["episode_id"] == "episode-002":
            mark["point_in_time_status"] = "unverified_operator_as_of"
    for outcome in outcomes:
        if outcome["episode_id"] == "episode-002" and outcome["evidence_status"] == "usable":
            outcome["point_in_time_status"] = "unverified_operator_as_of"

    result = summarize_close_decision_readiness(
        episodes=episodes,
        marks=marks,
        outcomes=outcomes,
    )

    assert result["status"] == "collecting_close_evidence"
    assert "episode_rows_not_deduplicated" in result["blockers"]
    assert result["episode_coverage"]["duplicate_episode_row_count"] == 1
    assert result["episode_coverage"]["promotion_usable_episode_count"] == 28
    assert result["fee_coverage"]["decision_fee_complete_episode_count"] == 34
    assert result["point_in_time_coverage"]["verified_usable_outcome_episode_count"] == 29
    assert result["inconclusive_reasons"]["no_usable_mark_in_window"] > 0


def test_close_readiness_requires_timestamp_provenance_for_p3_replacement() -> None:
    episodes, marks, outcomes = _evidence()
    episodes[0]["replacement_evidence"] = {"status": "review_switch"}

    incomplete = summarize_close_decision_readiness(
        episodes=episodes,
        marks=marks,
        outcomes=outcomes,
    )

    point_time = incomplete["point_in_time_coverage"]
    assert point_time["replacement_timestamp_required_episode_count"] == 1
    assert point_time["replacement_timestamp_complete_episode_count"] == 0
    assert point_time["replacement_timestamp_complete_ratio"] == 0.0
    assert incomplete["episode_coverage"]["promotion_usable_episode_count"] == 29

    episodes[0]["replacement_provenance"] = {
        "status": "validated_same_decision_run",
        "source_run_id": "20260723T135900Z-run",
        "source_run_at_utc": "2026-07-23T13:59:00Z",
    }
    complete = summarize_close_decision_readiness(
        episodes=episodes,
        marks=marks,
        outcomes=outcomes,
    )

    point_time = complete["point_in_time_coverage"]
    assert point_time["replacement_timestamp_required_episode_count"] == 1
    assert point_time["replacement_timestamp_complete_episode_count"] == 1
    assert point_time["replacement_timestamp_complete_ratio"] == 1.0
    assert complete["episode_coverage"]["promotion_usable_episode_count"] == 30


def test_close_readiness_rejects_future_decision_context_and_replacement_times() -> None:
    episodes, marks, outcomes = _evidence()
    episodes[0]["strategy_context_at_utc"] = "2026-07-23T14:00:01Z"
    episodes[1]["quote_at_utc"] = "2026-07-23T14:00:01Z"
    episodes[2]["replacement_evidence"] = {"status": "review_switch"}
    episodes[2]["replacement_provenance"] = {
        "status": "validated_same_decision_run",
        "source_run_id": "20260723T140001Z-run",
        "source_run_at_utc": "2026-07-23T14:00:01Z",
    }

    result = summarize_close_decision_readiness(
        episodes=episodes,
        marks=marks,
        outcomes=outcomes,
    )

    assert result["point_in_time_coverage"][
        "decision_quote_time_complete_episode_count"
    ] == 34
    assert result["point_in_time_coverage"][
        "strategy_time_complete_episode_count"
    ] == 34
    assert result["point_in_time_coverage"][
        "replacement_timestamp_complete_episode_count"
    ] == 0
    assert result["episode_coverage"]["promotion_usable_episode_count"] == 27


def test_dataset_status_adds_close_readiness_only_when_optional_facet_exists(
    tmp_path: Path,
) -> None:
    from src.application.shadow_replay import shadow_replay_dataset_status

    dataset_root = tmp_path / "datasets"
    candidate_only = dataset_root / "candidate-only"
    candidate_only.mkdir(parents=True)
    for name in (
        "candidate_snapshots.jsonl",
        "filter_decisions.jsonl",
        "mark_path_snapshots.jsonl",
        "outcome_facts.jsonl",
    ):
        (candidate_only / name).write_text("", encoding="utf-8")

    first = shadow_replay_dataset_status(repo_root=tmp_path, dataset_root=dataset_root)
    assert "close_decision_dataset_count" not in first["summary"]
    assert "close_decision_readiness" not in first["datasets"][0]

    close_dataset = dataset_root / "with-close"
    close_dataset.mkdir()
    for name in (
        "candidate_snapshots.jsonl",
        "filter_decisions.jsonl",
        "mark_path_snapshots.jsonl",
        "outcome_facts.jsonl",
        "close_decision_marks.jsonl",
        "close_decision_outcomes.jsonl",
    ):
        (close_dataset / name).write_text("", encoding="utf-8")
    _write_jsonl(close_dataset / "close_decision_episodes.jsonl", [_episode(0)])

    second = shadow_replay_dataset_status(repo_root=tmp_path, dataset_root=dataset_root)
    by_id = {row["dataset_id"]: row for row in second["datasets"]}
    assert second["summary"]["close_decision_dataset_count"] == 1
    assert by_id["with-close"]["close_decision_readiness"]["status"] == (
        "collecting_close_evidence"
    )
    assert by_id["with-close"]["close_decision_readiness"]["next_action"] == (
        "collect_fresh_opend_marks"
    )
    assert "--source opend" in by_id["with-close"]["close_decision_readiness"][
        "commands"
    ]["suggested_command"]
    assert "close_decision_readiness" not in by_id["candidate-only"]


def test_data_plan_keeps_receipt_time_separate_from_actual_mark_time(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application.shadow_replay import run_shadow_replay_data_plan
    import src.application.shadow_replay.data_plan as data_plan

    dataset = (
        tmp_path
        / "output_shared"
        / "research"
        / "shadow_replay"
        / "datasets"
        / "case-plan-time"
    )
    _write_jsonl(
        dataset / "candidate_snapshots.jsonl",
        [
            {"contract_symbol": "NVDA260619P00100000", "status": "accepted"},
            {"contract_symbol": "AMD260619P00080000", "status": "rejected"},
        ],
    )
    _write_jsonl(
        dataset / "filter_decisions.jsonl",
        [{"contract_symbol": "AMD260619P00080000", "status": "rejected"}],
    )
    seen: list[dict[str, object]] = []

    def _fake_collect(**kwargs):
        seen.append(kwargs)
        return {
            "schema_version": "shadow_replay_mark_collection.v1",
            "summary": {},
            "safety": {"writes_local_dataset_only": True},
        }

    monkeypatch.setattr(data_plan, "collect_shadow_replay_marks", _fake_collect)
    result = run_shadow_replay_data_plan(
        repo_root=tmp_path,
        source="opend",
        min_sample=2,
        write=True,
        now_utc="2026-07-23T01:00:00Z",
    )

    assert result["generated_at_utc"] == "2026-07-23T01:00:00Z"
    assert seen and seen[0]["as_of"] is None
