from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from src.application.close_advice_report_manifest import (
    publish_close_advice_report_manifest,
)
from src.application.shadow_replay.capture import build_shadow_replay_dataset
from src.application.shadow_replay.capture import _formal_policy_result
from src.application.shadow_replay.common import (
    CLOSE_DECISION_EPISODE_SCHEMA_VERSION,
    CLOSE_DECISION_MARK_SCHEMA_VERSION,
    CLOSE_DECISION_OUTCOME_SCHEMA_VERSION,
    DATASET_FILES,
    OPTIONAL_CLOSE_DATASET_FILES,
    refresh_dataset_manifest,
)
from src.application.shadow_replay.readiness import (
    summarize_close_decision_readiness,
)
from src.application.shadow_replay.settlement import _horizon_close_outcome
from src.application.shadow_replay.status import shadow_replay_dataset_status


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


_SNAPSHOT_MANIFEST_SHA256 = "a" * 64
_REQUIRED_DATA_PLAN_SHA256 = "b" * 64


def _publish_close_report(
    *,
    close_path: Path,
    context_path: Path,
    run_id: str,
) -> None:
    text_path = close_path.with_name("close_advice.txt")
    text_path.write_text("", encoding="utf-8")
    with close_path.open("r", encoding="utf-8", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    context = json.loads(context_path.read_text(encoding="utf-8"))
    publish_close_advice_report_manifest(
        csv_path=close_path,
        text_path=text_path,
        context_path=context_path,
        context=context,
        rows=rows,
        markets_to_run=["US"],
        run_id=run_id,
        quote_mode="frozen_snapshot",
        required_data_snapshot_manifest_sha256=(
            _SNAPSHOT_MANIFEST_SHA256
        ),
        close_advice_required_data_plan_sha256=(
            _REQUIRED_DATA_PLAN_SHA256
        ),
    )


def _episode(episode_id: str = "episode-1") -> dict[str, object]:
    return {
        "schema_version": CLOSE_DECISION_EPISODE_SCHEMA_VERSION,
        "episode_id": episode_id,
        "account": "lx",
        "position_lot_id": "lot-1",
        "observed_at_utc": "2026-08-10T01:01:00Z",
        "quote_at_utc": "2026-08-10T01:01:00Z",
        "quote_time_basis": "run_anchor",
        "strategy_context_at_utc": "2026-08-10T01:00:30Z",
        "strategy_time_basis": "position_context_as_of_utc",
        "formal_policy_result": {
            "policy_version": "strict_profit_capture.v1",
            "recommendation_state": "close",
            "decision_basis": ["strict_profit_capture_all_gates_passed"],
            "decision_evidence_status": "complete",
        },
        "normalized_decision_facts": {
            "strategy_profile": "strict_profit_capture.v1",
            "strategy_family": "sell_put",
        },
        "decision_economics": {
            "evidence_status": "complete",
            "fee_calc_status": "schedule_estimate",
            "fee_calc_basis": "test_schedule",
            "decision_open_fee": 0.5,
            "decision_close_fee": 0.5,
            "close_now_cost": 8.5,
            "opening_net_credit": 199.5,
            "contracts": 1,
            "multiplier": 100,
            "currency": "USD",
            "broker": "富途",
        },
        "position_identity": {
            "symbol": "NVDA",
            "option_type": "put",
            "side": "short",
            "expiration": "2026-09-18",
            "strike": 100,
        },
    }


def test_close_episode_capture_contains_only_formal_strict_policy(
    tmp_path: Path,
) -> None:
    run_id = "20260810T010000Z-run"
    run_dir = tmp_path / "output_runs" / run_id
    account_dir = run_dir / "accounts" / "lx"
    close_path = account_dir / "close_advice.csv"
    context_path = account_dir / "state" / "option_positions_context.json"
    audit_path = run_dir / "state" / "audit_events.jsonl"
    _write_csv(
        close_path,
        [
            {
                "account": "lx",
                "position_lot_id": "lot-1",
                "symbol": "NVDA",
                "option_type": "put",
                "position_side": "short",
                "expiration": "2026-09-18",
                "strike": 100,
                "contracts_open": 1,
                "multiplier": 100,
                "currency": "USD",
                "bid": 0.07,
                "ask": 0.08,
                "close_mid": 0.075,
                "dte": 39,
                "original_dte": 78,
                "remaining_term_ratio": 0.5,
                "net_capture_ratio": 0.95,
                "close_cost_ratio": 0.00085,
                "spread_ratio": 0.133333,
                "is_otm": True,
                "opening_net_credit": 199.5,
                "all_in_close_cost": 8.5,
                "estimated_open_fee": 0.5,
                "estimated_close_fee": 0.5,
                "fee_calc_status": "schedule_estimate",
                "fee_calc_basis": "test",
                "evaluation_status": "priced",
                "strategy_family": "sell_put",
                "strategy_profile": "strict_profit_capture.v1",
                "policy_version": "strict_profit_capture.v1",
                "recommendation_state": "close",
                "decision_basis": "strict_profit_capture_all_gates_passed",
                "decision_evidence_status": "complete",
                "quote_mode": "frozen_snapshot",
                "required_data_snapshot_manifest_sha256": (
                    _SNAPSHOT_MANIFEST_SHA256
                ),
                "close_advice_required_data_plan_sha256": (
                    _REQUIRED_DATA_PLAN_SHA256
                ),
            }
        ],
    )
    _write_json(
        context_path,
        {
            "context_status": "available",
            "as_of_utc": "2026-08-10T01:00:30Z",
            "open_positions_min": [
                {
                    "record_id": "lot-1",
                    "account": "lx",
                    "symbol": "NVDA",
                    "option_type": "put",
                    "side": "short",
                    "expiration_ymd": "2026-09-18",
                    "strike": 100,
                    "contracts_open": 1,
                    "multiplier": 100,
                    "currency": "USD",
                    "broker": "富途",
                }
            ],
        },
    )
    _write_jsonl(
        audit_path,
        [
            {
                "run_id": run_id,
                "account": "lx",
                "action": "close_advice",
                "status": "ok",
                "event_at_utc": "2026-08-10T01:01:00Z",
            }
        ],
    )
    _publish_close_report(
        close_path=close_path,
        context_path=context_path,
        run_id=run_id,
    )

    manifest = build_shadow_replay_dataset(
        repo_root=tmp_path,
        run_id=run_id,
        include_close_decisions=True,
        dataset_id="strict-close",
    )
    rows = [
        json.loads(line)
        for line in (
            Path(manifest["dataset_dir"]) / "close_decision_episodes.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]

    assert len(rows) == 1
    episode = rows[0]
    assert episode["formal_policy_result"]["policy_version"] == (
        "strict_profit_capture.v1"
    )
    assert episode["formal_policy_result"]["recommendation_state"] == "close"
    assert episode["normalized_decision_facts"]["net_capture_ratio"] == 0.95
    assert "shadow_policy_results" not in episode
    assert "replacement_evidence" not in episode


def test_close_episode_capture_rejects_report_or_context_outside_manifest(
    tmp_path: Path,
) -> None:
    run_id = "20260810T010000Z-run"
    account_dir = tmp_path / "output_runs" / run_id / "accounts" / "lx"
    close_path = account_dir / "close_advice.csv"
    context_path = account_dir / "state" / "option_positions_context.json"
    audit_path = tmp_path / "output_runs" / run_id / "state" / "audit_events.jsonl"
    row = {
        "account": "lx",
        "position_lot_id": "lot-1",
        "symbol": "NVDA",
        "option_type": "put",
        "position_side": "short",
        "expiration": "2026-09-18",
        "strike": 100,
        "evaluation_status": "priced",
        "policy_version": "strict_profit_capture.v1",
        "recommendation_state": "hold",
        "decision_basis": "net_capture_below_threshold",
        "decision_evidence_status": "complete",
        "quote_mode": "frozen_snapshot",
        "required_data_snapshot_manifest_sha256": _SNAPSHOT_MANIFEST_SHA256,
        "close_advice_required_data_plan_sha256": _REQUIRED_DATA_PLAN_SHA256,
    }
    context = {
        "context_status": "available",
        "as_of_utc": "2026-08-10T01:00:30Z",
        "open_positions_min": [
            {
                "record_id": "lot-1",
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "expiration_ymd": "2026-09-18",
                "strike": 100,
                "contracts_open": 1,
                "multiplier": 100,
                "currency": "USD",
                "broker": "富途",
            }
        ],
    }
    _write_csv(close_path, [row])
    _write_json(context_path, context)
    _write_jsonl(
        audit_path,
        [
            {
                "run_id": run_id,
                "account": "lx",
                "action": "close_advice",
                "status": "ok",
                "event_at_utc": "2026-08-10T01:01:00Z",
            }
        ],
    )
    _publish_close_report(
        close_path=close_path,
        context_path=context_path,
        run_id=run_id,
    )

    close_path.write_text("account,symbol\nlx,TSLA\n", encoding="utf-8")
    with pytest.raises(ValueError, match="report integrity validation failed"):
        build_shadow_replay_dataset(
            repo_root=tmp_path,
            run_id=run_id,
            include_close_decisions=True,
            dataset_id="tampered-report",
        )

    _write_csv(close_path, [row])
    _publish_close_report(
        close_path=close_path,
        context_path=context_path,
        run_id=run_id,
    )
    context["as_of_utc"] = "2026-08-10T01:00:31Z"
    _write_json(context_path, context)
    with pytest.raises(ValueError, match="context conflicts with report manifest"):
        build_shadow_replay_dataset(
            repo_root=tmp_path,
            run_id=run_id,
            include_close_decisions=True,
            dataset_id="tampered-context",
        )


def test_close_episode_capture_rejects_non_strict_policy(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported close policy version"):
        _formal_policy_result(
            {
                "policy_version": "legacy_close_policy.v1",
                "recommendation_state": "close",
                "decision_basis": "legacy_rule",
                "decision_evidence_status": "complete",
                "evaluation_status": "priced",
            },
            path=tmp_path / "close_advice.csv",
            row_number=1,
        )


@pytest.mark.parametrize(
    ("recommendation_state", "decision_evidence_status", "evaluation_status", "message"),
    [
        (
            "close",
            "complete",
            "not_evaluable",
            "strict close recommendation is not priced",
        ),
        (
            "not_evaluable",
            "not_evaluable",
            "priced",
            "strict not-evaluable recommendation is marked priced",
        ),
    ],
)
def test_close_episode_capture_rejects_contradictory_strict_decision_state(
    tmp_path: Path,
    recommendation_state: str,
    decision_evidence_status: str,
    evaluation_status: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _formal_policy_result(
            {
                "policy_version": "strict_profit_capture.v1",
                "recommendation_state": recommendation_state,
                "decision_basis": "strict_policy_result",
                "decision_evidence_status": decision_evidence_status,
                "evaluation_status": evaluation_status,
            },
            path=tmp_path / "close_advice.csv",
            row_number=1,
        )


def test_close_readiness_measures_one_strict_policy() -> None:
    episode = _episode()
    horizons = ("1d", "3d", "7d", "14d", "expiry")
    marks = [
        {
            "schema_version": CLOSE_DECISION_MARK_SCHEMA_VERSION,
            "episode_id": "episode-1",
            "horizon": horizon,
            "point_in_time_status": "verified_fresh_collection",
            "quote_status": "matched",
            "ask": 0.05,
            "spot": 120,
            "future_close_fee": 0.5,
        }
        for horizon in horizons
    ]
    outcomes = [
        {
            "schema_version": CLOSE_DECISION_OUTCOME_SCHEMA_VERSION,
            "episode_id": "episode-1",
            "outcome_kind": "terminal" if horizon == "expiry" else f"horizon_{horizon}",
            "evidence_status": "usable",
            "source": "expiration_mark" if horizon == "expiry" else "close_decision_mark",
            "point_in_time_status": "verified_fresh_collection",
            "future_close_fee": 0.0 if horizon == "expiry" else 0.5,
        }
        for horizon in horizons
    ]

    result = summarize_close_decision_readiness(
        episodes=[episode],
        marks=marks,
        outcomes=outcomes,
        min_sample=1,
        min_segment_sample=1,
    )

    assert result["status"] == "ready_for_strict_policy_evaluation"
    assert result["formal_policy_coverage"]["complete_episode_count"] == 1
    assert result["episode_coverage"]["analysis_usable_episode_count"] == 1
    assert result["next_action"] == "evaluate_strict_close_policy"
    assert "paired_policy_coverage" not in result
    assert "production_promotion_allowed" not in result


def test_dataset_status_counts_and_routes_strict_close_evaluation(
    tmp_path: Path,
) -> None:
    dataset = (
        tmp_path
        / "output_shared"
        / "research"
        / "shadow_replay"
        / "datasets"
        / "strict-close"
    )
    dataset.mkdir(parents=True)
    for name in DATASET_FILES:
        _write_jsonl(dataset / name, [])

    episodes = [_episode(f"episode-{index}") for index in range(30)]
    marks: list[dict[str, object]] = []
    outcomes: list[dict[str, object]] = []
    for index in range(30):
        episode_id = f"episode-{index}"
        for horizon in ("1d", "3d", "7d", "14d", "expiry"):
            marks.append(
                {
                    "schema_version": CLOSE_DECISION_MARK_SCHEMA_VERSION,
                    "episode_id": episode_id,
                    "horizon": horizon,
                    "point_in_time_status": "verified_fresh_collection",
                    "quote_status": "matched",
                    "ask": 0.05,
                    "spot": 120,
                    "future_close_fee": 0.5,
                }
            )
            outcomes.append(
                {
                    "schema_version": CLOSE_DECISION_OUTCOME_SCHEMA_VERSION,
                    "episode_id": episode_id,
                    "outcome_kind": (
                        "terminal" if horizon == "expiry" else f"horizon_{horizon}"
                    ),
                    "evidence_status": "usable",
                    "source": (
                        "expiration_mark"
                        if horizon == "expiry"
                        else "close_decision_mark"
                    ),
                    "point_in_time_status": "verified_fresh_collection",
                    "future_close_fee": 0.0 if horizon == "expiry" else 0.5,
                }
            )
    _write_jsonl(dataset / OPTIONAL_CLOSE_DATASET_FILES[0], episodes)
    _write_jsonl(dataset / OPTIONAL_CLOSE_DATASET_FILES[1], marks)
    _write_jsonl(dataset / OPTIONAL_CLOSE_DATASET_FILES[2], outcomes)
    refresh_dataset_manifest(dataset)

    result = shadow_replay_dataset_status(repo_root=tmp_path, min_sample=1)

    assert result["summary"][
        "close_decision_ready_for_strict_evaluation_count"
    ] == 1
    assert "close_decision_ready_for_paired_analysis_count" not in result["summary"]
    close = result["datasets"][0]["close_decision_readiness"]
    assert close["status"] == "ready_for_strict_policy_evaluation"
    assert close["commands"]["suggested_command"].endswith(
        "--dataset " + str(dataset) + " --min-sample 30"
    )


def test_close_readiness_rejects_mismatched_formal_evidence_state() -> None:
    episode = _episode()
    episode["formal_policy_result"]["decision_evidence_status"] = (
        "not_evaluable"
    )

    result = summarize_close_decision_readiness(
        episodes=[episode],
        marks=[],
        outcomes=[],
        min_sample=1,
        min_segment_sample=1,
    )

    assert result["formal_policy_coverage"]["complete_episode_count"] == 0
    assert "formal_policy_evidence_incomplete" in result["blockers"]


def test_close_horizon_outcome_uses_all_in_incremental_cost() -> None:
    outcome = _horizon_close_outcome(
        _episode(),
        horizon="1d",
        mark={
            "ask": 0.05,
            "future_close_fee": 0.5,
            "marked_at_utc": "2026-08-11T01:01:00Z",
            "spot": 120,
            "mark_time_basis": "collection_time",
            "point_in_time_status": "verified_fresh_collection",
        },
    )

    assert outcome["evidence_status"] == "usable"
    assert outcome["future_option_close_cost"] == 5.5
    assert outcome["hold_to_horizon_incremental"] == 3.0
    assert outcome["policy_version"] == "strict_profit_capture.v1"
    assert outcome["recommendation_state"] == "close"
    assert "replacement_outcome_status" not in outcome
