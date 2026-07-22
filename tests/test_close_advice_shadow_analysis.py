from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.application.shadow_replay.close_policy_analysis import (
    analyze_close_policy_rows,
)
from src.application.shadow_replay.analysis import analyze_shadow_replay_dataset
from src.application.shadow_replay.readiness import (
    summarize_close_decision_readiness,
)


_POLICIES = (
    "P0_current",
    "P1_semantic_split",
    "P2_profile_aware",
    "P3_opportunity_required",
)


def _episode(
    index: int,
    *,
    p0: str,
    p1: str,
    p2: str,
    p3: str,
    lot_id: str | None = None,
    replacement: bool = False,
) -> dict[str, Any]:
    observed_day = 23 + index
    episode = {
        "schema_version": "shadow_replay_close_episode.v1",
        "episode_id": f"episode-{index}",
        "account": "lx",
        "position_lot_id": lot_id or f"lot-{index}",
        "source_observation_count": 1,
        "observed_at_utc": f"2026-07-{observed_day:02d}T14:00:00Z",
        "quote_at_utc": f"2026-07-{observed_day:02d}T14:00:00Z",
        "quote_time_basis": "run_anchor",
        "strategy_context_at_utc": f"2026-07-{observed_day:02d}T13:59:00Z",
        "strategy_time_basis": "position_context_as_of_utc",
        "normalized_decision_facts": {
            "tier": "medium" if p0 == "close" else "none",
            "exit_state": "profit_capture" if p0 == "close" else "hold",
            "side": "short",
            "option_type": "put",
            "strategy_family": "sell_put",
            "strategy_profile": "insurance_underwriting",
            "evaluation_status": "priced",
            "fee_calc_status": "schedule_estimate",
            "estimated_pnl_if_close_net": 80.0,
            "thesis_status": "valid",
            "continued_willingness": True,
            "close_calibration_status": "complete",
            "combo_evidence_status": "not_applicable",
        },
        "material_economic_buckets": {
            "capture_ratio": 0.75,
            "remaining_annualized_return": 0.07,
            "dte": 29,
        },
        "threshold_inputs": {
            "capture_ratio": 0.75,
            "remaining_annualized_return": 0.07,
            "dte": 29,
        },
        "decision_economics": {
            "evidence_status": "complete",
            "fee_calc_status": "schedule_estimate",
            "decision_close_fee": 1.5,
            "decision_close_slippage": 1.0,
            "close_now_cost": 22.5,
        },
        "position_identity": {
            "symbol": "NVDA",
            "contract_symbol": "NVDA260821P00100000",
            "option_type": "put",
            "expiration": "2026-08-21",
        },
        "replacement_evidence": {"status": "not_evaluable"},
        "replacement_provenance": {
            "status": "not_applicable",
            "source_run_id": None,
            "source_run_at_utc": None,
        },
        "shadow_policy_results": {
            policy: {"recommendation_state": action}
            for policy, action in zip(_POLICIES, (p0, p1, p2, p3), strict=True)
        },
    }
    if replacement:
        episode["replacement_evidence"] = {
            "status": "review_switch",
            "open_fee": 2.0,
            "entry_slippage": 3.0,
        }
        episode["replacement_provenance"] = {
            "status": "validated_same_decision_run",
            "source_run_id": f"202607{observed_day:02d}T135900Z-run",
            "source_run_at_utc": f"2026-07-{observed_day:02d}T13:59:00Z",
        }
    return episode


def _outcomes(
    episode: dict[str, Any],
    *,
    hold_incremental: float,
    replacement_incremental: float | None = None,
    terminal_alignment: str | None = None,
) -> list[dict[str, Any]]:
    actions = {
        policy: row["recommendation_state"]
        for policy, row in episode["shadow_policy_results"].items()
    }
    rows: list[dict[str, Any]] = []
    for kind in ("horizon_1d", "horizon_3d", "horizon_7d", "horizon_14d", "terminal"):
        usable = kind == "horizon_1d"
        row = {
            "schema_version": "shadow_replay_close_outcome.v1",
            "episode_id": episode["episode_id"],
            "outcome_kind": kind,
            "evidence_status": "usable" if usable else "inconclusive",
            "inconclusive_reason": None if usable else "no_usable_mark_in_window",
            "outcome": "counterfactual_horizon_mark" if usable else "inconclusive",
            "source": "close_decision_mark" if usable else None,
            "marked_at_utc": (
                str(episode["observed_at_utc"]).replace("14:00:00", "15:00:00")
                if usable
                else None
            ),
            "point_in_time_status": "verified_fresh_collection" if usable else None,
            "future_close_fee": 2.5 if usable else None,
            "hold_to_horizon_incremental": hold_incremental if usable else None,
            "hold_vs_close_regret": hold_incremental if usable else None,
            "policy_recommendations": actions,
        }
        if usable and replacement_incremental is not None:
            row.update(
                {
                    "replacement_outcome_status": "usable",
                    "replacement_future_close_fee": 2.0,
                    "replacement_incremental": replacement_incremental,
                    "switch_vs_close_incremental": replacement_incremental,
                    "switch_vs_hold_incremental": (
                        replacement_incremental - hold_incremental
                    ),
                }
            )
        if kind == "terminal" and terminal_alignment is not None:
            row.update(
                {
                    "outcome": "assigned",
                    "source": "canonical_lifecycle_fact",
                    "lifecycle_at_utc": "2026-08-21T14:00:00Z",
                    "willingness_alignment": terminal_alignment,
                }
            )
        rows.append(row)
    return rows


def _mark(episode: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "shadow_replay_close_mark.v1",
        "episode_id": episode["episode_id"],
        "horizon": "1d",
        "quote_status": "matched",
        "ask": 0.1,
        "future_close_fee": 2.5,
        "point_in_time_status": "verified_fresh_collection",
    }


def _ready_evidence() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    episodes = [
        _episode(0, p0="close", p1="review", p2="hold", p3="hold", lot_id="lot-repeat"),
        _episode(1, p0="close", p1="review", p2="close", p3="close", replacement=True),
        _episode(2, p0="close", p1="review", p2="hold", p3="hold", lot_id="lot-repeat"),
        _episode(3, p0="hold", p1="hold", p2="hold", p3="hold"),
    ]
    hold_values = (10.0, -20.0, 5.0, -4.0)
    outcomes: list[dict[str, Any]] = []
    for index, (episode, hold) in enumerate(zip(episodes, hold_values, strict=True)):
        outcomes.extend(
            _outcomes(
                episode,
                hold_incremental=hold,
                replacement_incremental=5.0 if index == 1 else None,
                terminal_alignment="aligned" if index == 3 else None,
            )
        )
    marks = [_mark(episode) for episode in episodes]
    readiness = summarize_close_decision_readiness(
        episodes=episodes,
        marks=marks,
        outcomes=outcomes,
        min_sample=4,
        min_segment_sample=4,
        min_usable_outcome_ratio=1.0,
    )
    return episodes, marks, outcomes, readiness


def test_close_policy_analysis_reports_paired_quality_without_selecting_winner() -> None:
    episodes, _marks, outcomes, readiness = _ready_evidence()

    result = analyze_close_policy_rows(
        episodes=episodes,
        outcomes=outcomes,
        readiness=readiness,
    )
    aggregate = result["reports"]["aggregate"]
    p0 = aggregate["policy_metrics"]["P0_current"]
    p2 = aggregate["policy_metrics"]["P2_profile_aware"]
    paired = aggregate["paired_comparisons"]["P2_profile_aware_vs_P0_current"]

    assert result["status"] == "ready_for_ceo_review"
    assert result["automatic_policy_winner"] is None
    assert result["automatic_parameter_recommendation"] is None
    assert aggregate["action_counts"]["P0_current"] == {"close": 3, "hold": 1}
    assert aggregate["action_counts"]["P2_profile_aware"] == {"close": 1, "hold": 3}
    assert p0["premature_close_regret_per_paired_episode"]["count"] == 4
    assert p0["premature_close_regret_per_paired_episode"]["mean"] == 3.75
    assert p0["avoided_loss_benefit_per_paired_episode"]["mean"] == 5.0
    assert p0["close_precision"]["value"] == pytest.approx(1 / 3)
    assert p2["close_precision"]["value"] == 1.0
    assert paired["coverage"]["paired_episode_count"] == 4
    assert paired["premature_close_regret"][
        "paired_delta_proposed_minus_baseline"
    ]["mean"] == -3.75
    assert paired["avoided_loss_benefit"][
        "paired_delta_proposed_minus_baseline"
    ]["mean"] == 0.0


def test_close_policy_analysis_reports_cost_path_alignment_and_repeats() -> None:
    episodes, _marks, outcomes, readiness = _ready_evidence()

    result = analyze_close_policy_rows(
        episodes=episodes,
        outcomes=outcomes,
        readiness=readiness,
    )
    aggregate = result["reports"]["aggregate"]

    assert aggregate["policy_metrics"]["P0_current"]["close_transaction_cost"] == {
        "definition": "sum of explicitly captured fee and slippage components",
        "total": 7.5,
        "per_episode": {
            "population_count": 3,
            "count": 3,
            "inconclusive_count": 0,
            "mean": 2.5,
            "median": 2.5,
            "p5": 2.5,
            "p95": 2.5,
        },
    }
    assert aggregate["policy_metrics"]["P3_opportunity_required"][
        "switch_transaction_cost"
    ]["total"] == 9.5
    assert aggregate["p3_switch_opportunity"]["switch_vs_hold_incremental"][
        "mean"
    ] == 25.0
    assert aggregate["outcome_path_risk"]["maximum_adverse_excursion"] == 20.0
    assert aggregate["terminal_willingness_alignment"]["by_alignment"]["aligned"] == 1
    assert aggregate["operational"]["P0_current"][
        "repeated_actionable_reminder_count"
    ] == 1
    assert aggregate["unique_lot_rollup"]["unique_lot_count"] == 3


def test_close_policy_analysis_segments_market_account_and_bounded_sensitivity() -> None:
    episodes, _marks, outcomes, readiness = _ready_evidence()

    result = analyze_close_policy_rows(
        episodes=episodes,
        outcomes=outcomes,
        readiness=readiness,
    )

    assert result["reports"]["by_profile_family"][0]["strategy_profile"] == (
        "insurance_underwriting"
    )
    assert result["reports"]["by_market"][0]["market"] == "us"
    assert result["reports"]["by_account"][0]["account"] == "lx"
    sensitivity = result["bounded_threshold_sensitivity"]
    assert sensitivity["status"] == "descriptive_only"
    assert sensitivity["scenario_count"] == 9
    assert sensitivity["automatic_winner"] is None
    assert sensitivity["automatic_parameter_recommendation"] is None
    assert all(row["one_factor_at_a_time"] for row in sensitivity["scenarios"])


def test_close_policy_analysis_stops_when_mechanical_readiness_is_not_met() -> None:
    episodes, _marks, outcomes, readiness = _ready_evidence()
    readiness["status"] = "collecting_close_evidence"
    readiness["reason"] = "settled_unique_episodes_below_minimum"

    result = analyze_close_policy_rows(
        episodes=episodes,
        outcomes=outcomes,
        readiness=readiness,
    )

    assert result["status"] == "blocked_mechanical_readiness"
    assert result["reports"] is None
    assert result["automatic_policy_winner"] is None


def test_threshold_sensitivity_fails_closed_without_decision_time_dte() -> None:
    episodes, _marks, outcomes, readiness = _ready_evidence()
    episodes[0].pop("threshold_inputs")

    result = analyze_close_policy_rows(
        episodes=episodes,
        outcomes=outcomes,
        readiness=readiness,
    )

    scenarios = result["bounded_threshold_sensitivity"]["scenarios"]
    assert all(row["input_coverage"]["evaluable_episode_count"] == 3 for row in scenarios)
    assert all(row["input_coverage"]["inconclusive_episode_count"] == 1 for row in scenarios)


def test_dataset_analysis_adds_close_report_only_for_optional_close_facet(
    tmp_path: Path,
) -> None:
    candidate_only = tmp_path / "candidate-only"
    candidate_only.mkdir()
    for name in (
        "candidate_snapshots.jsonl",
        "filter_decisions.jsonl",
        "mark_path_snapshots.jsonl",
        "outcome_facts.jsonl",
    ):
        (candidate_only / name).write_text("", encoding="utf-8")

    first = analyze_shadow_replay_dataset(dataset=candidate_only, min_sample=1)
    assert "close_decision_readiness" not in first
    assert "close_policy_analysis" not in first

    episodes, marks, outcomes, _readiness = _ready_evidence()
    with_close = tmp_path / "with-close"
    with_close.mkdir()
    for name in (
        "candidate_snapshots.jsonl",
        "filter_decisions.jsonl",
        "mark_path_snapshots.jsonl",
        "outcome_facts.jsonl",
    ):
        (with_close / name).write_text("", encoding="utf-8")
    for name, rows in (
        ("close_decision_episodes.jsonl", episodes),
        ("close_decision_marks.jsonl", marks),
        ("close_decision_outcomes.jsonl", outcomes),
    ):
        (with_close / name).write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )

    second = analyze_shadow_replay_dataset(dataset=with_close, min_sample=1)
    assert second["close_decision_readiness"]["status"] == (
        "collecting_close_evidence"
    )
    assert second["close_policy_analysis"]["status"] == (
        "blocked_mechanical_readiness"
    )
