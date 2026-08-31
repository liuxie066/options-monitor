from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.strategy_lab.receipts import (
    StrategyLabReceiptError,
    build_research_receipt,
    publish_receipt,
    read_receipt_artifact,
)
from src.infrastructure.strategy_lab.experiment_store import ExperimentStore


def _experiment() -> dict[str, object]:
    spec = {
        "recipe": {"recipe_id": "sell_put_option_position_concentration"},
        "research_window": {
            "selected_trading_dates": [f"2026-08-{day:02d}" for day in range(1, 21)],
            "sessions": [
                {"trading_date": f"2026-08-{day:02d}", "points": []}
                for day in range(1, 21)
            ],
        },
    }
    manifest = [{"path": "owner.py", "sha256": "a" * 64}]
    return {
        "experiment_id": "experiment-1",
        "spec": spec,
        "spec_sha256": canonical_sha256(spec),
        "source_commit_sha": "b" * 40,
        "behavior_manifest": manifest,
        "evaluator_behavior_sha256": canonical_sha256(manifest),
    }


def _comparison() -> dict[str, object]:
    return {
        "status": "complete",
        "reason_code": None,
        "variant_id": "threshold_0.002",
        "near_return_threshold": 0.002,
        "expected_point_count": 20,
        "effective_point_count": 20,
        "top1_change_count": 10,
        "daily_aggregates": [],
        "mean_daily_annualized_return_delta": 0.01,
        "mean_daily_pnl_delta_cny": 1.0,
        "passed": True,
    }


def _observation(key: str) -> dict[str, object]:
    return {
        "observation_key": key,
        "recommendation_point_id": "point-1",
        "arm_id": "challenger_0.002",
        "kind": "single_result",
        "status": "available",
        "payload": {"economic_pnl_cny": 1.0},
        "artifact_ref": None,
        "artifact_sha256": None,
        "created_at_utc": "2026-08-30T01:00:00Z",
        "updated_at_utc": "2026-08-30T01:00:00Z",
    }


def test_research_receipt_is_deterministic_and_explicitly_provisional() -> None:
    first = build_research_receipt(
        _experiment(),
        [_observation("z"), _observation("a")],
        [_comparison()],
        "2026-08-30T01:00:00Z",
    )
    second = build_research_receipt(
        _experiment(),
        [_observation("a"), _observation("z")],
        [_comparison()],
        "2026-08-30T01:00:00Z",
    )

    assert first == second
    assert first["provisional"] is True
    assert first["fill_declaration"] == "simulated_fill_not_real_trade"
    assert first["conclusion"]["status"] == "leader"
    assert [item["observation_key"] for item in first["observations"]] == ["a", "z"]
    assert "validation" not in first
    assert "adoption" not in first

    failed = {**_comparison(), "mean_daily_annualized_return_delta": 0.0, "passed": False}
    no_leader = build_research_receipt(
        _experiment(),
        [_observation("a")],
        [failed],
        "2026-08-30T01:00:00Z",
    )
    assert no_leader["conclusion"] == {
        "status": "no_leader",
        "reason_code": "no_challenger_passed",
        "leader": None,
        "passing_variant_ids": [],
    }


def test_receipt_publish_is_write_once_readback_verified_and_private(tmp_path: Path) -> None:
    payload = build_research_receipt(
        _experiment(), [_observation("a")], [_comparison()], "2026-08-30T01:00:00Z"
    )

    first = publish_receipt(tmp_path, "experiment-1", "research", payload)
    second = publish_receipt(tmp_path, "experiment-1", "research", payload)

    assert first == second == read_receipt_artifact(
        tmp_path, "experiment-1", "research"
    )
    target = tmp_path / first["receipt_ref"]
    assert first["receipt_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert isinstance(first["receipt"]["comparisons"][0]["near_return_threshold"], float)
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700

    changed = {**payload, "provisional": False}
    with pytest.raises(StrategyLabReceiptError) as raised:
        publish_receipt(tmp_path, "experiment-1", "research", changed)
    assert raised.value.reason_code == "receipt_immutable_conflict"


def test_receipt_rejects_path_escape_and_noncanonical_readback(tmp_path: Path) -> None:
    with pytest.raises(StrategyLabReceiptError) as raised:
        read_receipt_artifact(tmp_path, "../escape", "research")
    assert raised.value.reason_code == "receipt_input_invalid"

    target = tmp_path / "experiments" / "experiment-1" / "receipts" / "research.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"kind": "research"}\n', encoding="utf-8")
    with pytest.raises(StrategyLabReceiptError) as raised:
        read_receipt_artifact(tmp_path, "experiment-1", "research")
    assert raised.value.reason_code == "receipt_artifact_invalid"


def test_published_receipt_is_attached_after_process_restart(tmp_path: Path) -> None:
    binding = _experiment()
    store = ExperimentStore(tmp_path / "experiments.sqlite3")
    store.initialize()
    created = store.create_experiment(
        experiment_id=binding["experiment_id"],
        spec=binding["spec"],
        spec_sha256=binding["spec_sha256"],
        source_commit_sha=binding["source_commit_sha"],
        behavior_manifest=binding["behavior_manifest"],
        evaluator_behavior_sha256=binding["evaluator_behavior_sha256"],
        confirmation_sha256="c" * 64,
        idempotency_key="confirm-1",
        actor="tester",
        occurred_at_utc="2026-08-30T01:00:00Z",
    )
    complete = store.append_event_and_transition(
        created["experiment_id"],
        expected_state="research_running",
        expected_revision=created["revision"],
        new_state="research_complete",
        event_type="research_materialized",
        actor="tester",
        payload={},
        occurred_at_utc="2026-08-30T01:00:00Z",
    )
    payload = build_research_receipt(
        complete, [_observation("a")], [_comparison()], complete["updated_at_utc"]
    )
    published = publish_receipt(
        tmp_path, complete["experiment_id"], "research", payload
    )

    restarted = ExperimentStore(tmp_path / "experiments.sqlite3")
    readback = read_receipt_artifact(tmp_path, "experiment-1", "research")
    attached = restarted.attach_research_receipt_and_transition(
        "experiment-1",
        expected_state="research_complete",
        expected_revision=complete["revision"],
        new_state="awaiting_validation_confirmation",
        receipt_ref=readback["receipt_ref"],
        receipt_sha256=readback["receipt_sha256"],
        leader=payload["conclusion"]["leader"],
        actor="tester",
        occurred_at_utc=complete["updated_at_utc"],
        payload={"status": "leader"},
        idempotency_key="conclude-1",
    )

    assert published == readback
    assert attached["state"] == "awaiting_validation_confirmation"
    assert attached["research_receipt_sha256"] == published["receipt_sha256"]
