from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.opening_candidate_snapshot import OPENING_CANDIDATE_SNAPSHOT_FILE
from src.application.recommendation_point import (
    RECOMMENDATION_POINT_FILE,
    capture_scheduled_recommendation_point,
)
from src.application.scan_scheduler import scheduled_scan_targets_for_date
from src.application.strategy_lab.top1.corpus import (
    CORPUS_COMMAND_RESULT_SCHEMA,
    CorpusError,
    capture_recommendation_point,
    seal_day_expectation,
)
from src.application.strategy_lab.top1.lifecycle import set_account_opt_in
from src.infrastructure.strategy_lab.experiment_store import ExperimentStore
from tests.candidate_evidence_helpers import seal_opening_candidate_fixture


AVAILABLE = {"OM_STRATEGY_LAB_TOP1_AVAILABLE": "1"}
CALENDAR_HASH = "a" * 64
SOURCE_SHA = "c" * 40


def _schedule(*, start_plus_min: int = 10, enabled: bool = True) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "timezone": "Asia/Hong_Kong",
        "run_window": {"start": "09:50", "end": "10:10"},
        "run_points": {"start_plus_min": start_plus_min},
    }


def _candidate(symbol: str = "0700.HK") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "contract_symbol": f"{symbol.replace('.', '')}260821P00400000",
        "expiration": "2026-08-21",
        "strike": 400,
        "spot": 450,
        "currency": "HKD",
        "open_interest": 500,
        "period_net_return_on_cash_basis": 0.012,
        "net_assignment_discount_pct": 0.10,
        "symbol_concentration_after": 0.20,
        "sell_limit": 5.10,
        "net_premium": 505.0,
        "net_cash_basis": 39_495.0,
        "net_income": 505.0,
        "net_income_cny": 465.0,
        "spread_ratio": 0.10,
        "stock_owner": "none",
        "fee_schedule_version": "fixture.v1",
        "fee_basis": "fixture",
        "fee_schedule_url": "https://example.test/fees",
    }


def _store(tmp_path: Path) -> ExperimentStore:
    store = ExperimentStore(tmp_path / "strategy-lab.sqlite3")
    store.migrate(migrated_at_utc="2026-07-20T00:00:00Z")
    return store


def _enable(store: ExperimentStore, artifact_root: Path) -> None:
    set_account_opt_in(
        store,
        market="HK",
        account="lx",
        enabled=True,
        actor="human",
        occurred_at_utc="2026-07-20T00:00:00Z",
        idempotency_key="enable-corpus",
        artifact_root=artifact_root,
        environ=AVAILABLE,
    )


def _target_for(day: str, *, minute: int = 0) -> str:
    return f"{day}T10:{minute:02d}:00+08:00"


def _scheduler(day: str, *, minute: int = 0) -> dict[str, Any]:
    target = datetime.fromisoformat(_target_for(day, minute=minute))
    now_utc = target.astimezone(timezone.utc) + timedelta(seconds=30)
    return {
        "should_run_scan": True,
        "scheduled_scan_target_market": target.isoformat(),
        "now_utc": now_utc.isoformat().replace("+00:00", "Z"),
    }


def _publish_source_point(
    source_root: Path,
    *,
    run_id: str,
    day: str,
    minute: int = 0,
    accepted: bool = True,
    rejected: bool = False,
) -> tuple[str, dict[str, Any]]:
    seal_opening_candidate_fixture(
        source_root,
        run_id=run_id,
        market="HK",
        accepted_rows=[_candidate()] if accepted else [],
        rejected_rows=[_candidate("3690.HK")] if rejected else [],
    )
    publication, point = capture_scheduled_recommendation_point(
        source_root,
        run_id,
        "lx",
        _scheduler(day, minute=minute),
        source_commit_sha=SOURCE_SHA,
    )
    assert publication == "published"
    return (
        f"output_runs/{run_id}/accounts/lx/state/{RECOMMENDATION_POINT_FILE}",
        point,
    )


def _seal(
    store: ExperimentStore,
    artifact_root: Path,
    *,
    day: str,
    sealed_at: str | None = None,
    schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return seal_day_expectation(
        store,
        artifact_root,
        market="HK",
        account="lx",
        schedule=schedule or _schedule(),
        trading_date=day,
        market_calendar_version="hk-calendar.fixture.v1",
        market_calendar_sha256=CALENDAR_HASH,
        sealed_at_utc=sealed_at or f"{day}T01:00:00Z",
        environ=AVAILABLE,
    )


def test_target_wrapper_and_feature_off_are_side_effect_free(tmp_path: Path) -> None:
    assert [
        target.isoformat()
        for target in scheduled_scan_targets_for_date(_schedule(), "2026-07-21")
    ] == ["2026-07-21T10:00:00+08:00"]
    assert scheduled_scan_targets_for_date(_schedule(), "2026-07-19") == []
    with pytest.raises(ValueError, match="canonical ISO date"):
        scheduled_scan_targets_for_date(_schedule(), "20260721")
    with pytest.raises(ValueError, match="timezone"):
        scheduled_scan_targets_for_date(
            {**_schedule(), "timezone": "Not/A_Zone"}, "2026-07-21"
        )
    with pytest.raises(ValueError, match="gate timezone"):
        scheduled_scan_targets_for_date(
            {
                **_schedule(),
                "gates": [
                    {"type": "before", "timezone": "Not/A_Zone", "time": "12:00"}
                ],
            },
            "2026-07-21",
        )

    store = _store(tmp_path)
    artifact_root = tmp_path / "artifacts"
    result = _seal(store, artifact_root, day="2026-07-21")
    assert result == {
        "schema_version": CORPUS_COMMAND_RESULT_SCHEMA,
        "operation": "seal_day_expectation",
        "status": "not_evaluable",
        "reason_code": "feature_disabled",
        "market": "HK",
        "account": "lx",
        "trading_date": "2026-07-21",
        "recommendation_point_id": None,
        "artifact_ref": None,
        "artifact_sha256": None,
        "artifact_content_sha256": None,
        "expected_point_count": None,
    }
    assert store.corpus_days("HK", "lx") == []
    assert not (artifact_root / "strategy_lab/top1/corpus").exists()

    source_root = tmp_path / "source"
    point_ref, point = _publish_source_point(
        source_root,
        run_id="feature-off-point",
        day="2026-07-21",
    )
    capture = capture_recommendation_point(
        store,
        source_root,
        artifact_root,
        point_ref=point_ref,
        trading_date="2026-07-21",
        captured_at_utc="2026-07-21T02:01:00Z",
        environ=AVAILABLE,
    )
    assert (capture["status"], capture["reason_code"]) == (
        "not_evaluable",
        "feature_disabled",
    )
    assert store.corpus_point("HK", "lx", point["recommendation_point_id"]) is None
    with pytest.raises(CorpusError) as raised:
        seal_day_expectation(
            store,
            artifact_root,
            market="US",
            account="lx",
            schedule=_schedule(),
            trading_date="2026-07-21",
            market_calendar_version="us-calendar.fixture.v1",
            market_calendar_sha256=CALENDAR_HASH,
            sealed_at_utc="2026-07-21T01:00:00Z",
            environ=AVAILABLE,
        )
    assert raised.value.reason_code == "corpus_input_invalid"


def test_expectation_is_immutable_idempotent_and_conflict_marked(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    artifact_root = tmp_path / "artifacts"
    _enable(store, artifact_root)

    with pytest.raises(CorpusError) as invalid_schedule:
        _seal(
            store,
            artifact_root,
            day="2026-07-20",
            schedule={**_schedule(), "timezone": "Not/A_Zone"},
        )
    assert invalid_schedule.value.reason_code == "corpus_input_invalid"
    assert store.corpus_day("HK", "lx", "2026-07-20") is None

    published = _seal(store, artifact_root, day="2026-07-21")
    assert published["status"] == "published"
    assert published["reason_code"] is None
    assert published["expected_point_count"] == 1
    first_bytes = (artifact_root / str(published["artifact_ref"])).read_bytes()

    retried = _seal(
        store,
        artifact_root,
        day="2026-07-21",
        sealed_at="2026-07-21T01:30:00Z",
    )
    assert retried["status"] == "idempotent"
    assert retried["artifact_sha256"] == published["artifact_sha256"]
    assert (artifact_root / str(published["artifact_ref"])).read_bytes() == first_bytes

    conflict = _seal(
        store,
        artifact_root,
        day="2026-07-21",
        schedule=_schedule(start_plus_min=5),
    )
    assert (conflict["status"], conflict["reason_code"]) == (
        "conflict",
        "research_corpus_conflict",
    )
    assert store.corpus_day("HK", "lx", "2026-07-21")["conflict_status"] == "conflict"
    repeated_conflict = _seal(
        store,
        artifact_root,
        day="2026-07-21",
        schedule=_schedule(start_plus_min=7),
    )
    assert repeated_conflict["status"] == "conflict"
    assert repeated_conflict["artifact_ref"] is None
    assert repeated_conflict["artifact_sha256"] is None
    assert repeated_conflict["artifact_content_sha256"] is None

    late = _seal(
        store,
        artifact_root,
        day="2026-07-22",
        sealed_at="2026-07-22T02:00:00Z",
    )
    assert (late["status"], late["reason_code"]) == (
        "not_evaluable",
        "corpus_day_expectation_late",
    )
    empty = _seal(
        store,
        artifact_root,
        day="2026-07-23",
        schedule=_schedule(enabled=False),
    )
    assert (empty["status"], empty["reason_code"], empty["expected_point_count"]) == (
        "not_evaluable",
        "corpus_day_expectation_empty",
        0,
    )


def test_clean_point_capture_copies_only_the_rankable_projection(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    artifact_root = tmp_path / "artifacts"
    source_root = tmp_path / "source"
    _enable(store, artifact_root)
    day = "2026-07-21"
    assert _seal(store, artifact_root, day=day)["status"] == "published"
    point_ref, point = _publish_source_point(
        source_root,
        run_id="clean-corpus-point",
        day=day,
        rejected=True,
    )

    captured = capture_recommendation_point(
        store,
        source_root,
        artifact_root,
        point_ref=point_ref,
        trading_date=day,
        captured_at_utc="2026-07-21T02:01:00Z",
        environ=AVAILABLE,
    )
    assert captured["status"] == "published"
    assert captured["recommendation_point_id"] == point["recommendation_point_id"]
    projection = json.loads(
        (artifact_root / str(captured["artifact_ref"])).read_text(encoding="utf-8")
    )
    assert projection["producer_accepted_candidate_ids"] == point[
        "producer_accepted_candidate_ids"
    ]
    assert len(projection["candidates"]) == 1
    assert "candidate_decisions" not in projection
    assert "3690.HK" not in json.dumps(projection)
    assert store.corpus_point(
        "HK", "lx", point["recommendation_point_id"]
    )["capture_status"] == "captured"

    retried = capture_recommendation_point(
        store,
        source_root,
        artifact_root,
        point_ref=point_ref,
        trading_date=day,
        captured_at_utc="2026-07-21T02:02:00Z",
        environ=AVAILABLE,
    )
    assert retried["status"] == "idempotent"
    assert retried["artifact_sha256"] == captured["artifact_sha256"]

    (artifact_root / str(captured["artifact_ref"])).write_text("{}\n", encoding="utf-8")
    conflicted = capture_recommendation_point(
        store,
        source_root,
        artifact_root,
        point_ref=point_ref,
        trading_date=day,
        captured_at_utc="2026-07-21T02:03:00Z",
        environ=AVAILABLE,
    )
    assert (conflicted["status"], conflicted["reason_code"]) == (
        "conflict",
        "research_corpus_conflict",
    )
    assert store.corpus_point(
        "HK", "lx", point["recommendation_point_id"]
    )["conflict_status"] == "conflict"


def test_capture_rejects_missing_late_and_unexpected_denominators(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    artifact_root = tmp_path / "artifacts"
    source_root = tmp_path / "source"
    _enable(store, artifact_root)

    missing_ref, missing_point = _publish_source_point(
        source_root,
        run_id="missing-day",
        day="2026-07-21",
    )
    missing = capture_recommendation_point(
        store,
        source_root,
        artifact_root,
        point_ref=missing_ref,
        trading_date="2026-07-21",
        captured_at_utc="2026-07-21T02:01:00Z",
        environ=AVAILABLE,
    )
    assert missing["reason_code"] == "corpus_day_expectation_missing"
    assert store.corpus_point(
        "HK", "lx", missing_point["recommendation_point_id"]
    ) is None

    _seal(
        store,
        artifact_root,
        day="2026-07-22",
        sealed_at="2026-07-22T02:00:00Z",
    )
    late_ref, late_point = _publish_source_point(
        source_root,
        run_id="late-day",
        day="2026-07-22",
    )
    late = capture_recommendation_point(
        store,
        source_root,
        artifact_root,
        point_ref=late_ref,
        trading_date="2026-07-22",
        captured_at_utc="2026-07-22T02:01:00Z",
        environ=AVAILABLE,
    )
    assert late["reason_code"] == "corpus_day_not_evaluable"
    assert store.corpus_point("HK", "lx", late_point["recommendation_point_id"]) is None

    assert _seal(store, artifact_root, day="2026-07-23")["status"] == "published"
    unexpected_ref, unexpected_point = _publish_source_point(
        source_root,
        run_id="unexpected-point",
        day="2026-07-23",
        minute=5,
    )
    unexpected = capture_recommendation_point(
        store,
        source_root,
        artifact_root,
        point_ref=unexpected_ref,
        trading_date="2026-07-23",
        captured_at_utc="2026-07-23T02:06:00Z",
        environ=AVAILABLE,
    )
    assert unexpected["reason_code"] == "unexpected_recommendation_point"
    assert store.corpus_point(
        "HK", "lx", unexpected_point["recommendation_point_id"]
    ) is None


def test_no_candidate_and_incomplete_points_are_durable_terminal_facts(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    artifact_root = tmp_path / "artifacts"
    source_root = tmp_path / "source"
    _enable(store, artifact_root)

    no_candidate_day = "2026-07-21"
    _seal(store, artifact_root, day=no_candidate_day)
    no_candidate_ref, no_candidate_point = _publish_source_point(
        source_root,
        run_id="no-candidate-corpus",
        day=no_candidate_day,
        accepted=False,
    )
    no_candidate = capture_recommendation_point(
        store,
        source_root,
        artifact_root,
        point_ref=no_candidate_ref,
        trading_date=no_candidate_day,
        captured_at_utc="2026-07-21T02:01:00Z",
        environ=AVAILABLE,
    )
    assert no_candidate["status"] == "published"
    assert json.loads(
        (artifact_root / str(no_candidate["artifact_ref"])).read_text(encoding="utf-8")
    )["candidates"] == []

    partial_day = "2026-07-22"
    _seal(store, artifact_root, day=partial_day)
    partial_ref, partial_point = _publish_source_point(
        source_root,
        run_id="partial-corpus",
        day=partial_day,
    )
    partial_point["terminal_sell_put_status"] = "partial_data"
    partial_point["content_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in partial_point.items()
            if key != "content_sha256"
        }
    )
    partial_path = source_root / partial_ref
    partial_path.write_text(
        json.dumps(
            partial_point,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    partial = capture_recommendation_point(
        store,
        source_root,
        artifact_root,
        point_ref=partial_ref,
        trading_date=partial_day,
        captured_at_utc="2026-07-22T02:01:00Z",
        environ=AVAILABLE,
    )
    assert (partial["status"], partial["reason_code"]) == (
        "not_evaluable",
        "official_decision_incomplete",
    )
    assert store.corpus_point(
        "HK", "lx", partial_point["recommendation_point_id"]
    )["capture_status"] == "not_evaluable"
    assert capture_recommendation_point(
        store,
        source_root,
        artifact_root,
        point_ref=partial_ref,
        trading_date=partial_day,
        captured_at_utc="2026-07-22T02:02:00Z",
        environ=AVAILABLE,
    )["status"] == "idempotent"
    assert no_candidate_point["producer_accepted_candidate_ids"] == []


def test_missing_or_invalid_opening_snapshot_is_recorded_not_evaluable(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    artifact_root = tmp_path / "artifacts"
    source_root = tmp_path / "source"
    _enable(store, artifact_root)

    missing_day = "2026-07-23"
    _seal(store, artifact_root, day=missing_day)
    missing_ref, missing_point = _publish_source_point(
        source_root,
        run_id="missing-opening",
        day=missing_day,
    )
    missing_snapshot = (
        source_root
        / "output_runs/missing-opening/accounts/lx/state"
        / OPENING_CANDIDATE_SNAPSHOT_FILE
    )
    missing_snapshot.unlink()
    missing = capture_recommendation_point(
        store,
        source_root,
        artifact_root,
        point_ref=missing_ref,
        trading_date=missing_day,
        captured_at_utc="2026-07-23T02:01:00Z",
        environ=AVAILABLE,
    )
    assert (missing["status"], missing["reason_code"]) == (
        "not_evaluable",
        "opening_snapshot_missing",
    )
    assert store.corpus_point(
        "HK", "lx", missing_point["recommendation_point_id"]
    )["reason_code"] == "opening_snapshot_missing"

    conflict_day = "2026-07-24"
    _seal(store, artifact_root, day=conflict_day)
    conflict_ref, conflict_point = _publish_source_point(
        source_root,
        run_id="invalid-opening",
        day=conflict_day,
    )
    conflict_snapshot = (
        source_root
        / "output_runs/invalid-opening/accounts/lx/state"
        / OPENING_CANDIDATE_SNAPSHOT_FILE
    )
    payload = json.loads(conflict_snapshot.read_text(encoding="utf-8"))
    payload["content_sha256"] = "d" * 64
    conflict_snapshot.write_text(json.dumps(payload), encoding="utf-8")
    conflicted = capture_recommendation_point(
        store,
        source_root,
        artifact_root,
        point_ref=conflict_ref,
        trading_date=conflict_day,
        captured_at_utc="2026-07-24T02:01:00Z",
        environ=AVAILABLE,
    )
    assert (conflicted["status"], conflicted["reason_code"]) == (
        "not_evaluable",
        "opening_snapshot_conflict",
    )
    assert store.corpus_point(
        "HK", "lx", conflict_point["recommendation_point_id"]
    )["reason_code"] == "opening_snapshot_conflict"
