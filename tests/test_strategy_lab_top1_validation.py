from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

import src.application.strategy_lab.top1.research_runner as runner_module
import tests.test_strategy_lab_top1_research as research_fixture
from src.application.recommendation_point import (
    RECOMMENDATION_POINT_FILE,
    capture_scheduled_recommendation_point,
)
from src.application.strategy_lab.top1.contracts import VALIDATION_REQUIRED_DAYS
from src.application.strategy_lab.top1.corpus import capture_recommendation_point
from src.application.strategy_lab.top1.fill_observation import observe_active_contracts
from src.application.strategy_lab.top1.lifecycle import (
    authorize_validation,
    lock_challenger,
    read_public_receipt,
    read_public_status,
    start_validation,
)
from src.application.strategy_lab.top1.outcome import (
    conclude_validation,
    settle_due_outcomes,
)
from src.application.strategy_lab.top1.validation import (
    Top1ValidationError,
    consume_validation_point,
    record_validation_day_gap,
)
from src.infrastructure.strategy_lab.experiment_store import ExperimentStore
from tests.candidate_evidence_helpers import (
    seal_market_calendar_fixture,
    seal_opening_candidate_fixture,
    top1_hk_schedule_fixture,
)
from tests.test_strategy_lab_top1_corpus import (
    _publish_source_point,
    _seal,
    _scheduler,
)
from tests.test_strategy_lab_top1_research_runner import (
    AVAILABLE,
    FakeGateway,
    _direct_limiter,
    _prepared,
    _run,
)


EXPERIMENT_ID = "experiment-w5-evaluator"


class NoSnapshotGateway:
    def get_snapshot(self, _codes: list[str]) -> object:  # pragma: no cover
        raise AssertionError("an empty candidate set must not call the provider")


class OutcomeGateway:
    def __init__(self, candidate: dict[str, Any]) -> None:
        self.candidate = candidate
        self.terms_error = False
        self.snapshot_calls: list[list[str]] = []
        self.terms_calls = 0
        self.calendar_calls = 0
        self.close_calls = 0

    def get_snapshot(self, codes: list[str]) -> list[dict[str, object]]:
        self.snapshot_calls.append(codes)
        return [
            {
                "code": codes[0],
                "bid_price": self.candidate["sell_limit"],
                "ask_price": float(self.candidate["sell_limit"]) + 0.01,
            }
        ]

    def get_exact_expiration_option_terms(self, **_kwargs: object) -> dict[str, object]:
        self.terms_calls += 1
        if self.terms_error:
            raise RuntimeError("temporary terms failure")
        return {
            "contract_symbol": str(self.candidate["contract_symbol"]).upper(),
            "stock_owner": str(self.candidate["stock_owner"]).upper(),
            "expiration": self.candidate["expiration"],
            "option_type": "PUT",
            "option_standard_type": "STANDARD",
            "strike": self.candidate["strike"],
            "multiplier": self.candidate["multiplier"],
            "currency": self.candidate["currency"],
        }

    def get_trading_days(self, **_kwargs: object) -> list[dict[str, str]]:
        self.calendar_calls += 1
        return [{"trading_date": str(self.candidate["expiration"])}]

    def get_exact_expiration_close(self, **_kwargs: object) -> dict[str, object]:
        self.close_calls += 1
        return {"close": 390.0}


def _trading_days(start: str, count: int) -> list[str]:
    current = date.fromisoformat(start)
    days: list[str] = []
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def _start_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ExperimentStore, Path, Path, list[str]]:
    monkeypatch.setattr(research_fixture, "EXPIRATION", "2026-12-18")
    store, artifact_root, case, research_hash = _prepared(tmp_path)
    monkeypatch.setattr(runner_module, "rate_limited_opend_call", _direct_limiter)
    result = _run(store, artifact_root, case, research_hash, FakeGateway())
    assert result["selection"] == "research_leader"

    dates = _trading_days("2026-10-05", VALIDATION_REQUIRED_DAYS)
    validation_spec = research_fixture._spec(
        case["sealed_dataset"],
        variants=(
            ("without", "without_concentration"),
            ("concentration", "concentration_first"),
        ),
        validation=True,
    )
    seal_market_calendar_fixture(
        artifact_root,
        dates,
        version=str(validation_spec["economics_contracts"]["market_calendar_version"]),
    )
    locked = lock_challenger(
        store,
        validation_spec,
        challenger_variant_id=str(result["leader_variant_id"]),
        validation_start_trading_date=dates[0],
        schedule=top1_hk_schedule_fixture(),
        actor="human",
        occurred_at_utc="2026-10-02T01:00:00Z",
        idempotency_key="lock-validation",
        artifact_root=artifact_root,
        environ=AVAILABLE,
    )
    commitment = json.loads(
        str(store.experiment(EXPERIMENT_ID)["proposed_commitment_json"])
    )
    assert commitment["schema_version"] == "sell_put_top1_hidden_window_commitment.v2"
    assert commitment["trading_dates"] == dates
    assert len(commitment["days"]) == VALIDATION_REQUIRED_DAYS
    assert all(day["expected_recommendation_point_ids"] for day in commitment["days"])
    seal_market_calendar_fixture(
        artifact_root,
        _trading_days("2026-11-02", VALIDATION_REQUIRED_DAYS),
        version=str(validation_spec["economics_contracts"]["market_calendar_version"]),
    )
    validation_hash = str(locked["validation_spec_sha256"])
    authorize_validation(
        store,
        experiment_id=EXPERIMENT_ID,
        validation_spec_sha256=validation_hash,
        actor="human",
        occurred_at_utc="2026-10-02T01:01:00Z",
        idempotency_key="authorize-validation",
        artifact_root=artifact_root,
        environ=AVAILABLE,
    )
    start_validation(
        store,
        experiment_id=EXPERIMENT_ID,
        validation_spec_sha256=validation_hash,
        actor="human",
        occurred_at_utc="2026-10-02T01:02:00Z",
        idempotency_key="start-validation",
        artifact_root=artifact_root,
        environ=AVAILABLE,
    )
    return store, artifact_root, tmp_path / "source", dates


def _publish_candidate_point(
    source_root: Path,
    *,
    trading_date: str,
    run_id: str,
    candidate: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    seal_opening_candidate_fixture(
        source_root,
        run_id=run_id,
        market="HK",
        accepted_rows=[candidate],
    )
    publication, point = capture_scheduled_recommendation_point(
        source_root,
        run_id,
        "lx",
        _scheduler(trading_date),
        source_commit_sha="c" * 40,
    )
    assert publication == "published"
    return (
        f"output_runs/{run_id}/accounts/lx/state/{RECOMMENDATION_POINT_FILE}",
        point,
    )


def test_ten_day_empty_candidate_run_concludes_without_provider_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, artifact_root, source_root, dates = _start_validation(
        tmp_path, monkeypatch
    )
    gateway = NoSnapshotGateway()

    for index, trading_date in enumerate(dates):
        assert _seal(store, artifact_root, day=trading_date)["status"] == "published"
        point_ref, point = _publish_source_point(
            source_root,
            run_id=f"validation-{index}",
            day=trading_date,
            accepted=False,
        )
        captured = capture_recommendation_point(
            store,
            source_root,
            artifact_root,
            point_ref=point_ref,
            trading_date=trading_date,
            captured_at_utc=f"{trading_date}T02:00:30Z",
            environ=AVAILABLE,
        )
        point_id = str(captured["recommendation_point_id"])
        consume_validation_point(
            store,
            artifact_root,
            experiment_id=EXPERIMENT_ID,
            recommendation_point_id=point_id,
            source_status="available",
            actor="runner",
            occurred_at_utc=f"{trading_date}T02:00:45Z",
            idempotency_key=f"consume-{index}",
            environ=AVAILABLE,
        )
        observe_active_contracts(
            store,
            artifact_root,
            experiment_id=EXPERIMENT_ID,
            observed_recommendation_point_id=point_id,
            gateway=gateway,
            actor="runner",
            occurred_at_utc=f"{trading_date}T02:01:00Z",
            idempotency_key=f"observe-{index}",
            environ=AVAILABLE,
        )

    experiment = store.experiment(EXPERIMENT_ID)
    assert experiment["completed_validation_partitions"] == VALIDATION_REQUIRED_DAYS
    assert experiment["validation_progress"] == "ready_to_conclude"
    before = read_public_status(
        store, experiment_id=EXPERIMENT_ID, environ=AVAILABLE
    )
    assert before["experiment"]["final_outcome_status"] is None
    assert before["validation"] == {
        "consumed_point_count": VALIDATION_REQUIRED_DAYS,
        "outcome_job_count": 0,
        "pending_outcome_count": 0,
    }

    concluded = conclude_validation(
        store,
        artifact_root,
        experiment_id=EXPERIMENT_ID,
        actor="runner",
        occurred_at_utc="2026-11-02T03:00:00Z",
        idempotency_key="conclude",
        environ=AVAILABLE,
    )

    assert concluded["status"] == "insufficient_evidence"
    assert concluded["result"]["effective_days"] == 0
    assert store.experiment(EXPERIMENT_ID)["phase"] == "concluded"
    assert read_public_receipt(store, experiment_id=EXPERIMENT_ID) is not None


def test_fill_terms_and_shared_close_are_observed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, artifact_root, source_root, dates = _start_validation(
        tmp_path, monkeypatch
    )
    trading_date = dates[0]
    candidate = research_fixture._candidates()[0]
    gateway = OutcomeGateway(candidate)
    _seal(store, artifact_root, day=trading_date)
    point_ref, _point = _publish_candidate_point(
        source_root,
        trading_date=trading_date,
        run_id="validation-fill",
        candidate=candidate,
    )
    captured = capture_recommendation_point(
        store,
        source_root,
        artifact_root,
        point_ref=point_ref,
        trading_date=trading_date,
        captured_at_utc=f"{trading_date}T02:00:30Z",
        environ=AVAILABLE,
    )
    point_id = str(captured["recommendation_point_id"])
    consume_validation_point(
        store,
        artifact_root,
        experiment_id=EXPERIMENT_ID,
        recommendation_point_id=point_id,
        source_status="available",
        actor="runner",
        occurred_at_utc=f"{trading_date}T02:00:45Z",
        idempotency_key="consume-fill",
        environ=AVAILABLE,
    )
    observe_active_contracts(
        store,
        artifact_root,
        experiment_id=EXPERIMENT_ID,
        observed_recommendation_point_id=point_id,
        gateway=gateway,
        actor="runner",
        occurred_at_utc=f"{trading_date}T02:01:00Z",
        idempotency_key="observe-fill",
        environ=AVAILABLE,
    )

    assert gateway.snapshot_calls == [[str(candidate["contract_symbol"]).upper()]]
    observe_active_contracts(
        store,
        artifact_root,
        experiment_id=EXPERIMENT_ID,
        observed_recommendation_point_id=point_id,
        gateway=gateway,
        actor="runner",
        occurred_at_utc=f"{trading_date}T02:01:00Z",
        idempotency_key="observe-fill",
        environ=AVAILABLE,
    )
    assert gateway.snapshot_calls == [[str(candidate["contract_symbol"]).upper()]]
    assert {job["status"] for job in store.outcome_jobs(EXPERIMENT_ID)} == {
        "pending_terms"
    }

    expiration = str(candidate["expiration"])
    _seal(store, artifact_root, day=expiration)
    terms_ref, _terms_point = _publish_source_point(
        source_root,
        run_id="expiration-terms",
        day=expiration,
        accepted=False,
    )
    capture_recommendation_point(
        store,
        source_root,
        artifact_root,
        point_ref=terms_ref,
        trading_date=expiration,
        captured_at_utc=f"{expiration}T02:00:30Z",
        environ=AVAILABLE,
    )
    gateway.terms_error = True
    retryable = settle_due_outcomes(
        store,
        artifact_root,
        experiment_id=EXPERIMENT_ID,
        gateway=gateway,
        actor="runner",
        occurred_at_utc=f"{expiration}T02:30:00Z",
        idempotency_key="settle-retryable",
        environ=AVAILABLE,
    )
    assert retryable == {"status": "processed", "processed": 2, "pending": 2}
    assert gateway.terms_calls == 1
    assert settle_due_outcomes(
        store,
        artifact_root,
        experiment_id=EXPERIMENT_ID,
        gateway=gateway,
        actor="runner",
        occurred_at_utc=f"{expiration}T02:30:00Z",
        idempotency_key="settle-retryable",
        environ=AVAILABLE,
    ) == retryable
    assert gateway.terms_calls == 1

    gateway.terms_error = False
    before_due = settle_due_outcomes(
        store,
        artifact_root,
        experiment_id=EXPERIMENT_ID,
        gateway=gateway,
        actor="runner",
        occurred_at_utc=f"{expiration}T03:00:00Z",
        idempotency_key="settle-before-due",
        environ=AVAILABLE,
    )
    assert before_due == {"status": "processed", "processed": 2, "pending": 2}
    assert gateway.terms_calls == 2
    assert gateway.calendar_calls == gateway.close_calls == 0

    at_due = settle_due_outcomes(
        store,
        artifact_root,
        experiment_id=EXPERIMENT_ID,
        gateway=gateway,
        actor="runner",
        occurred_at_utc=f"{expiration}T16:00:00Z",
        idempotency_key="settle-at-due",
        environ=AVAILABLE,
    )
    assert at_due == {"status": "processed", "processed": 2, "pending": 0}
    assert gateway.calendar_calls == gateway.close_calls == 1
    assert {job["status"] for job in store.outcome_jobs(EXPERIMENT_ID)} == {
        "evaluable"
    }

    settled_calls = (
        gateway.terms_calls,
        gateway.calendar_calls,
        gateway.close_calls,
    )
    assert settle_due_outcomes(
        store,
        artifact_root,
        experiment_id=EXPERIMENT_ID,
        gateway=gateway,
        actor="runner",
        occurred_at_utc=f"{expiration}T17:00:00Z",
        idempotency_key="settle-replay",
        environ=AVAILABLE,
    ) == {"status": "pending", "processed": 0, "pending": 0}
    assert (
        gateway.terms_calls,
        gateway.calendar_calls,
        gateway.close_calls,
    ) == settled_calls


def test_missing_day_and_point_seal_only_after_derived_deadlines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, artifact_root, source_root, dates = _start_validation(
        tmp_path, monkeypatch
    )
    first = dates[0]
    with pytest.raises(Top1ValidationError) as too_early:
        record_validation_day_gap(
            store,
            artifact_root,
            experiment_id=EXPERIMENT_ID,
            trading_date=first,
            actor="runner",
            occurred_at_utc=f"{first}T16:02:29Z",
            idempotency_key="day-gap-early",
            environ=AVAILABLE,
        )
    assert too_early.value.reason_code == "validation_deadline_not_reached"
    committed = record_validation_day_gap(
        store,
        artifact_root,
        experiment_id=EXPERIMENT_ID,
        trading_date=first,
        actor="runner",
        occurred_at_utc=f"{first}T16:02:30Z",
        idempotency_key="day-gap",
        environ=AVAILABLE,
    )
    assert committed["status"] == "committed"
    assert record_validation_day_gap(
        store,
        artifact_root,
        experiment_id=EXPERIMENT_ID,
        trading_date=first,
        actor="runner",
        occurred_at_utc=f"{first}T16:02:30Z",
        idempotency_key="day-gap",
        environ=AVAILABLE,
    )["status"] == "idempotent"

    second = dates[1]
    _seal(store, artifact_root, day=second)
    _point_ref, point = _publish_source_point(
        source_root,
        run_id="missing-validation-point",
        day=second,
        accepted=False,
    )
    point_id = str(point["recommendation_point_id"])
    with pytest.raises(Top1ValidationError) as point_early:
        consume_validation_point(
            store,
            artifact_root,
            experiment_id=EXPERIMENT_ID,
            recommendation_point_id=point_id,
            source_status="missing_after_deadline",
            actor="runner",
            occurred_at_utc=f"{second}T02:02:29Z",
            idempotency_key="point-gap-early",
            environ=AVAILABLE,
        )
    assert point_early.value.reason_code == "validation_deadline_not_reached"
    consume_validation_point(
        store,
        artifact_root,
        experiment_id=EXPERIMENT_ID,
        recommendation_point_id=point_id,
        source_status="missing_after_deadline",
        actor="runner",
        occurred_at_utc=f"{second}T02:02:30Z",
        idempotency_key="point-gap",
        environ=AVAILABLE,
    )

    days = store.validation_days(EXPERIMENT_ID)
    assert [row["expected_point_count"] for row in days] == [None, 1]
    assert all(row["hard_risk_status"] == "missing" for row in days)
