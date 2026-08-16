from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import date, datetime, timedelta, timezone
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
    CORPUS_STATUS_SCHEMA,
    DATASET_FREEZE_RESULT_SCHEMA,
    RESEARCH_WINDOW_FACTS_SCHEMA,
    SEALED_HISTORICAL_DATASET_SCHEMA,
    CorpusError,
    capture_recommendation_point,
    discover_recommendation_points,
    freeze_research_dataset,
    read_market_calendar_binding,
    read_corpus_status,
    seal_committed_day_expectation,
    seal_day_expectation,
)
from src.application.strategy_lab.top1.lifecycle import (
    build_hidden_window_commitment,
    set_account_opt_in,
)
from src.application.strategy_lab.top1.ranking import Top1RankingError
from src.infrastructure.strategy_lab.experiment_store import ExperimentStore
from tests.candidate_evidence_helpers import (
    seal_market_calendar_fixture,
    seal_opening_candidate_fixture,
)


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


def _multi_point_schedule() -> dict[str, Any]:
    return {
        "enabled": True,
        "timezone": "Asia/Hong_Kong",
        "run_window": {"start": "09:30", "end": "10:20"},
        "run_points": {
            "start_plus_min": 10,
            "hourly_minute": 0,
            "end_minus_min": 10,
        },
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


def _target_for(day: str, *, hour: int = 10, minute: int = 0) -> str:
    return f"{day}T{hour:02d}:{minute:02d}:00+08:00"


def _scheduler(
    day: str,
    *,
    hour: int = 10,
    minute: int = 0,
) -> dict[str, Any]:
    target = datetime.fromisoformat(_target_for(day, hour=hour, minute=minute))
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
    hour: int = 10,
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
        _scheduler(day, hour=hour, minute=minute),
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


def _trading_days(start: str, count: int) -> list[str]:
    current = date.fromisoformat(start)
    days: list[str] = []
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def _window_facts(
    days: list[str],
    *,
    latest_mature_trading_date: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": RESEARCH_WINDOW_FACTS_SCHEMA,
        "market": "HK",
        "account": "lx",
        "cutoff_at_utc": f"{days[-1]}T08:00:00Z",
        "cutoff_trading_date": days[-1],
        "market_calendar_version": "hk-calendar.fixture.v1",
        "market_calendar_ref": "evidence/hk-calendar.fixture.json",
        "market_calendar_sha256": CALENDAR_HASH,
        "trading_calendar_dates": days,
        "trading_calendar_dates_sha256": canonical_sha256(days),
        "latest_mature_trading_date": (
            days[-1]
            if latest_mature_trading_date is None
            else latest_mature_trading_date
        ),
        "maturity_evidence_ref": "evidence/hk-maturity.fixture.json",
        "maturity_evidence_sha256": "b" * 64,
        "recommendation_point_selector": "official_scheduled_sell_put.v1",
    }
    payload["content_sha256"] = canonical_sha256(payload)
    return payload


def _rehash(payload: dict[str, Any]) -> dict[str, Any]:
    payload["content_sha256"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "content_sha256"}
    )
    return payload


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


def test_calendar_binding_is_content_addressed_and_fails_closed(
    tmp_path: Path,
) -> None:
    with pytest.raises(CorpusError) as missing:
        read_market_calendar_binding(tmp_path / "missing", market="HK")
    assert missing.value.reason_code == "market_calendar_binding_unavailable"

    days = _trading_days("2026-07-21", 20)
    binding = seal_market_calendar_fixture(
        tmp_path, days, version="hk-calendar.fixture.v1"
    )
    assert binding["trading_dates"] == days
    snapshot_path = tmp_path.joinpath(*str(binding["snapshot_ref"]).split("/"))
    snapshot_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(CorpusError) as tampered:
        read_market_calendar_binding(tmp_path, market="HK")
    assert tampered.value.reason_code == "market_calendar_binding_unavailable"


def test_committed_denominator_rejects_later_schedule_drift(tmp_path: Path) -> None:
    store = _store(tmp_path)
    artifact_root = tmp_path / "artifacts"
    _enable(store, artifact_root)
    days = _trading_days("2026-07-21", 20)
    binding = seal_market_calendar_fixture(
        artifact_root, days, version="hk-calendar.fixture.v1"
    )
    commitment = build_hidden_window_commitment(
        experiment_id="experiment-denominator",
        account="lx",
        validation_start_trading_date=days[0],
        market_calendar_binding=binding,
        schedule=_multi_point_schedule(),
        challenger_variant_id="challenger",
        research_spec_sha256="a" * 64,
        research_terminal_file_sha256="b" * 64,
        behavior_binding_sha256="c" * 64,
    )
    committed_day = commitment["days"][0]
    sealed = seal_committed_day_expectation(
        store,
        artifact_root,
        market="HK",
        account="lx",
        committed_day=committed_day,
        market_calendar_version=str(commitment["market_calendar_version"]),
        market_calendar_sha256=str(
            commitment["market_calendar_snapshot_content_sha256"]
        ),
        schedule_config_sha256=str(commitment["schedule_config_sha256"]),
        sealed_at_utc=f"{days[0]}T00:00:00Z",
        environ=AVAILABLE,
    )
    assert sealed["status"] == "published"
    drifted = _seal(
        store,
        artifact_root,
        day=days[0],
        sealed_at=f"{days[0]}T00:01:00Z",
        schedule=_schedule(start_plus_min=5),
    )
    assert drifted["status"] == "conflict"


def test_point_discovery_uses_the_validated_point_clock(tmp_path: Path) -> None:
    point_ref, point = _publish_source_point(
        tmp_path, run_id="run-clock", day="2026-07-21"
    )
    discovered = discover_recommendation_points(tmp_path, market="HK", account="lx")
    assert discovered == [
        {
            "status": "available",
            "point_ref": point_ref,
            "recommendation_point_id": point["recommendation_point_id"],
            "scheduled_scan_target_market": point[
                "scheduled_scan_target_market"
            ],
            "trading_date": "2026-07-21",
        }
    ]


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


def test_capture_rejects_a_timestamp_before_the_official_decision(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    artifact_root = tmp_path / "artifacts"
    source_root = tmp_path / "source"
    _enable(store, artifact_root)
    day = "2026-07-21"
    _seal(store, artifact_root, day=day)
    point_ref, point = _publish_source_point(
        source_root,
        run_id="capture-before-decision",
        day=day,
    )

    with pytest.raises(CorpusError) as raised:
        capture_recommendation_point(
            store,
            source_root,
            artifact_root,
            point_ref=point_ref,
            trading_date=day,
            captured_at_utc="2026-07-21T02:00:00Z",
            environ=AVAILABLE,
        )
    assert raised.value.reason_code == "corpus_input_invalid"
    assert store.corpus_point("HK", "lx", point["recommendation_point_id"]) is None


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


def test_status_counts_clean_not_evaluable_conflicting_and_missing(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    artifact_root = tmp_path / "artifacts"
    source_root = tmp_path / "source"
    _enable(store, artifact_root)

    _seal(
        store,
        artifact_root,
        day="2026-07-21",
        sealed_at="2026-07-21T02:00:00Z",
    )
    _seal(store, artifact_root, day="2026-07-22")
    partial_ref, partial_point = _publish_source_point(
        source_root,
        run_id="status-partial",
        day="2026-07-22",
    )
    partial_point["terminal_sell_put_status"] = "partial_data"
    partial_point["content_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in partial_point.items()
            if key != "content_sha256"
        }
    )
    (source_root / partial_ref).write_text(
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
    capture_recommendation_point(
        store,
        source_root,
        artifact_root,
        point_ref=partial_ref,
        trading_date="2026-07-22",
        captured_at_utc="2026-07-22T02:01:00Z",
        environ=AVAILABLE,
    )
    _seal(store, artifact_root, day="2026-07-23")
    _seal(
        store,
        artifact_root,
        day="2026-07-23",
        schedule=_schedule(start_plus_min=5),
    )

    assert read_corpus_status(store, market="HK", account="lx") == {
        "schema_version": CORPUS_STATUS_SCHEMA,
        "market": "HK",
        "account": "lx",
        "days_total": 3,
        "days_on_time": 1,
        "days_not_evaluable": 1,
        "days_conflicting": 1,
        "expected_points_total": 3,
        "points_captured": 0,
        "points_not_evaluable": 1,
        "points_conflicting": 0,
        "points_missing": 2,
        "earliest_trading_date": "2026-07-21",
        "latest_trading_date": "2026-07-23",
        "ranking_projection_schema_version": "sell_put_ranking_projection.v1",
    }


def test_freeze_exact_40_days_survives_source_deletion_and_never_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    artifact_root = tmp_path / "artifacts"
    source_root = tmp_path / "source"
    _enable(store, artifact_root)
    all_days = _trading_days("2026-01-05", 41)
    first_window = all_days[:40]
    for index, trading_date in enumerate(first_window):
        targets = [(9, 40), (10, 0), (10, 10)] if index == 0 else [(10, 0)]
        assert _seal(
            store,
            artifact_root,
            day=trading_date,
            schedule=_multi_point_schedule() if index == 0 else _schedule(),
        )["status"] == "published"
        for hour, minute in targets:
            point_ref, _point = _publish_source_point(
                source_root,
                run_id=f"freeze-{index:02d}-{hour:02d}{minute:02d}",
                day=trading_date,
                hour=hour,
                minute=minute,
                accepted=False,
            )
            target = datetime.fromisoformat(
                _target_for(trading_date, hour=hour, minute=minute)
            )
            captured_at = (target.astimezone(timezone.utc) + timedelta(minutes=1))
            captured = capture_recommendation_point(
                store,
                source_root,
                artifact_root,
                point_ref=point_ref,
                trading_date=trading_date,
                captured_at_utc=captured_at.isoformat().replace("+00:00", "Z"),
                environ=AVAILABLE,
            )
            assert captured["status"] == "published"

    status = read_corpus_status(store, market="HK", account="lx")
    assert status == {
        "schema_version": CORPUS_STATUS_SCHEMA,
        "market": "HK",
        "account": "lx",
        "days_total": 40,
        "days_on_time": 40,
        "days_not_evaluable": 0,
        "days_conflicting": 0,
        "expected_points_total": 42,
        "points_captured": 42,
        "points_not_evaluable": 0,
        "points_conflicting": 0,
        "points_missing": 0,
        "earliest_trading_date": first_window[0],
        "latest_trading_date": first_window[-1],
        "ranking_projection_schema_version": "sell_put_ranking_projection.v1",
    }

    facts = _window_facts(first_window)
    frozen = freeze_research_dataset(
        store,
        artifact_root,
        window_facts=facts,
        environ=AVAILABLE,
    )
    assert frozen["schema_version"] == DATASET_FREEZE_RESULT_SCHEMA
    assert set(frozen) == {
        "schema_version",
        "status",
        "reason_code",
        "market",
        "account",
        "window_facts_content_sha256",
        "selected_trading_dates",
        "dataset_ref",
        "dataset_sha256",
        "dataset_content_sha256",
    }
    assert (frozen["status"], frozen["reason_code"]) == ("ready", None)
    assert frozen["selected_trading_dates"] == first_window
    dataset_path = artifact_root / str(frozen["dataset_ref"])
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    assert dataset["schema_version"] == SEALED_HISTORICAL_DATASET_SCHEMA
    assert dataset["selected_trading_dates"] == first_window
    assert [item["trading_date"] for item in dataset["days"]] == first_window
    assert [len(item["points"]) for item in dataset["days"]] == [3] + [1] * 39
    assert "candidates" not in json.dumps(dataset)
    assert dataset["content_sha256"] == canonical_sha256(
        {key: value for key, value in dataset.items() if key != "content_sha256"}
    )

    shutil.rmtree(source_root / "output_runs")
    repeated = freeze_research_dataset(
        store,
        artifact_root,
        window_facts=facts,
        environ=AVAILABLE,
    )
    assert repeated == frozen

    too_early_cutoff = json.loads(json.dumps(facts))
    too_early_cutoff["cutoff_at_utc"] = f"{first_window[-1]}T00:30:00Z"
    _rehash(too_early_cutoff)
    cutoff_blocked = freeze_research_dataset(
        store,
        artifact_root,
        window_facts=too_early_cutoff,
        environ=AVAILABLE,
    )
    assert (cutoff_blocked["reason_code"], cutoff_blocked["dataset_ref"]) == (
        "research_dataset_coverage_missing",
        None,
    )

    first_point = store.corpus_points(
        "HK", "lx", trading_date=first_window[0]
    )[0]
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE strategy_lab_corpus_points "
            "SET ranking_projection_schema_version = ? "
            "WHERE market = ? AND account = ? AND recommendation_point_id = ?",
            (
                "sell_put_ranking_projection.v0",
                "HK",
                "lx",
                first_point["recommendation_point_id"],
            ),
        )
    assert freeze_research_dataset(
        store,
        artifact_root,
        window_facts=facts,
        environ=AVAILABLE,
    )["reason_code"] == "research_dataset_coverage_missing"
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE strategy_lab_corpus_points "
            "SET ranking_projection_schema_version = ? "
            "WHERE market = ? AND account = ? AND recommendation_point_id = ?",
            (
                "sell_put_ranking_projection.v1",
                "HK",
                "lx",
                first_point["recommendation_point_id"],
            ),
        )

    calendar_drift = json.loads(json.dumps(facts))
    calendar_drift["market_calendar_version"] = "hk-calendar.fixture.v2"
    calendar_drift["market_calendar_sha256"] = "f" * 64
    _rehash(calendar_drift)
    assert freeze_research_dataset(
        store,
        artifact_root,
        window_facts=calendar_drift,
        environ=AVAILABLE,
    )["reason_code"] == "research_dataset_coverage_missing"

    with monkeypatch.context() as patcher:
        def _parity_failure(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise Top1RankingError(
                "baseline_rank_parity_mismatch", "fixture parity mismatch"
            )

        patcher.setattr(
            "src.application.strategy_lab.top1.corpus.rerank_recommendation_point",
            _parity_failure,
        )
        assert freeze_research_dataset(
            store,
            artifact_root,
            window_facts=facts,
            environ=AVAILABLE,
        )["reason_code"] == "research_dataset_coverage_missing"

    latest_day = all_days[-1]
    assert _seal(store, artifact_root, day=latest_day)["status"] == "published"
    latest_facts = _window_facts(all_days)
    latest_gap = freeze_research_dataset(
        store,
        artifact_root,
        window_facts=latest_facts,
        environ=AVAILABLE,
    )
    assert latest_gap["selected_trading_dates"] == all_days[1:]
    assert (latest_gap["status"], latest_gap["reason_code"]) == (
        "blocked",
        "research_dataset_coverage_missing",
    )
    assert latest_gap["dataset_ref"] is None

    tampered_point = store.corpus_points(
        "HK", "lx", trading_date=all_days[1]
    )[0]
    (artifact_root / str(tampered_point["projection_ref"])).write_text(
        "{}\n", encoding="utf-8"
    )
    conflict = freeze_research_dataset(
        store,
        artifact_root,
        window_facts=latest_facts,
        environ=AVAILABLE,
    )
    assert (conflict["status"], conflict["reason_code"]) == (
        "blocked",
        "research_corpus_conflict",
    )


def test_freeze_validates_window_facts_feature_gate_and_warming(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    artifact_root = tmp_path / "artifacts"
    days = _trading_days("2026-01-05", 39)
    facts = _window_facts(days)

    invalid_required_days_values: list[Any] = [True, 1, 39, 40.0, 41, "40"]
    for invalid_required_days in invalid_required_days_values:
        with pytest.raises(CorpusError) as raised:
            freeze_research_dataset(
                store,
                artifact_root,
                window_facts=facts,
                required_days=invalid_required_days,
                environ=AVAILABLE,
            )
        assert raised.value.reason_code == "corpus_input_invalid"

    feature_off = freeze_research_dataset(
        store,
        artifact_root,
        window_facts=facts,
        environ=AVAILABLE,
    )
    assert (feature_off["status"], feature_off["reason_code"]) == (
        "blocked",
        "feature_disabled",
    )
    assert not (artifact_root / "strategy_lab/top1/corpus").exists()

    _enable(store, artifact_root)
    warming = freeze_research_dataset(
        store,
        artifact_root,
        window_facts=facts,
        environ=AVAILABLE,
    )
    assert (warming["status"], warming["reason_code"]) == (
        "blocked",
        "research_corpus_warming",
    )

    invalid_payloads: list[dict[str, Any]] = []
    extra_key = json.loads(json.dumps(facts))
    extra_key["unexpected"] = True
    invalid_payloads.append(extra_key)
    unsafe_ref = json.loads(json.dumps(facts))
    unsafe_ref["market_calendar_ref"] = "../calendar.json"
    invalid_payloads.append(_rehash(unsafe_ref))
    unordered = json.loads(json.dumps(facts))
    unordered["trading_calendar_dates"][0:2] = reversed(
        unordered["trading_calendar_dates"][0:2]
    )
    unordered["trading_calendar_dates_sha256"] = canonical_sha256(
        unordered["trading_calendar_dates"]
    )
    invalid_payloads.append(_rehash(unordered))
    bad_cutoff = json.loads(json.dumps(facts))
    bad_cutoff["cutoff_trading_date"] = "2025-12-31"
    invalid_payloads.append(_rehash(bad_cutoff))
    bad_mature = json.loads(json.dumps(facts))
    bad_mature["latest_mature_trading_date"] = "2025-12-31"
    invalid_payloads.append(_rehash(bad_mature))
    bad_selector = json.loads(json.dumps(facts))
    bad_selector["recommendation_point_selector"] = "other.v1"
    invalid_payloads.append(_rehash(bad_selector))
    bad_dates_hash = json.loads(json.dumps(facts))
    bad_dates_hash["trading_calendar_dates_sha256"] = "d" * 64
    invalid_payloads.append(_rehash(bad_dates_hash))
    bad_content_hash = json.loads(json.dumps(facts))
    bad_content_hash["content_sha256"] = "e" * 64
    invalid_payloads.append(bad_content_hash)

    for invalid in invalid_payloads:
        with pytest.raises(CorpusError) as raised:
            freeze_research_dataset(
                store,
                artifact_root,
                window_facts=invalid,
                environ=AVAILABLE,
            )
        assert raised.value.reason_code == "corpus_input_invalid"

    no_mature = json.loads(json.dumps(_window_facts(_trading_days("2026-01-05", 40))))
    no_mature["latest_mature_trading_date"] = None
    _rehash(no_mature)
    assert freeze_research_dataset(
        store,
        artifact_root,
        window_facts=no_mature,
        environ=AVAILABLE,
    )["reason_code"] == "research_corpus_warming"
