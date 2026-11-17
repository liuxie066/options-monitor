from __future__ import annotations

import asyncio

import pytest

import src.application.daily_decision_brief_repository as repo
import src.application.multi_tick_audit as audit
from test_daily_brief_phase_timing import RunLog
from test_daily_decision_brief_repository_v2 import _persist, _prepare_fixed

NEW_PHASES = {"state_read", "delivery_observation", "source_read"}


def test_warm_validation_is_measured_and_reads_sources_without_per_row_events(tmp_path, monkeypatch):
    persisted = _persist(tmp_path)
    _prepare_fixed(tmp_path, persisted)
    expected = repo.read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")
    source_reads = []
    normalize_calls = []
    original_get = repo.DecisionHistoryStore.get
    original_normalize = repo._normalize_delivery_state

    def get(store, **kwargs):
        source_reads.append((kwargs.get("account"), kwargs.get("market")))
        return original_get(store, **kwargs)

    def normalize(*args, **kwargs):
        normalize_calls.append(1)
        return original_normalize(*args, **kwargs)

    monkeypatch.setattr(repo.DecisionHistoryStore, "get", get)
    monkeypatch.setattr(repo, "_normalize_delivery_state", normalize)
    log = RunLog()
    with repo.daily_brief_delivery_validation_scope():
        with audit.daily_brief_timing_scope(runlog=log, account="lx", market="US"):
            first = repo.read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")
            first_reads = len(source_reads)
            second = repo.read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")
    assert first == second == expected
    assert first_reads > 0 and len(source_reads) > first_reads
    assert len(normalize_calls) == 1
    phases = [e for e in log.events if e["data"]["phase"] in NEW_PHASES]
    assert [(e["data"]["phase"], e["status"]) for e in phases] == [
        ("state_read", "start"), ("state_read", "ok"),
        ("delivery_observation", "start"), ("delivery_observation", "ok"),
    ] * 2
    assert all(isinstance(e["data"]["process_cpu_ms"], int) for e in phases if e["status"] == "ok")
    assert all(set(e["data"]) <= {
        "account", "market", "phase", "operation", "outcome", "process_cpu_ms",
    } for e in phases)
    # No per-history-row source events; eight added events for these two observations.
    assert len(phases) == 8


def test_independent_source_read_is_measured_but_batch_sources_are_not(tmp_path):
    persisted = _persist(tmp_path)
    lookup = dict(base=tmp_path, account="lx", market="US",
                  market_trading_date=persisted["brief"]["market_trading_date"],
                  revision=persisted["current_revision"], source_digest=persisted["current_brief_digest"])
    expected = repo._read_validated_successful_revision_source(**lookup)
    log = RunLog()
    with audit.daily_brief_timing_scope(runlog=log, account="lx", market="US"):
        actual = repo._read_validated_successful_revision_source(**lookup)
        with repo._brief_history_observation(base=tmp_path, account="lx", market="US"):
            batch = repo._read_validated_successful_revision_source(**lookup)
    assert actual == batch == expected
    events = [e for e in log.events if e["data"]["phase"] == "source_read"]
    assert [e["status"] for e in events] == ["start", "ok"]
    assert isinstance(events[-1]["data"]["process_cpu_ms"], int)


@pytest.mark.parametrize("phase", ["state_read", "delivery_observation", "source_read"])
@pytest.mark.parametrize("error", [KeyboardInterrupt(), asyncio.CancelledError()])
def test_boundary_cancellation_identity_and_context_reset(tmp_path, monkeypatch, phase, error):
    def fail(*_args, **_kwargs):
        raise error

    if phase == "state_read":
        path = tmp_path / "state.json"
        path.write_text("{}")
        monkeypatch.setattr(repo.json, "loads", fail)
        operation = lambda: repo._read_json_strict(path)
    elif phase == "delivery_observation":
        monkeypatch.setattr(repo, "_normalize_delivery_state", fail)
        operation = lambda: repo._observe_delivery_state({}, base=tmp_path, path=tmp_path / "delivery.json",
                                                        account="lx", market="US")
    else:
        monkeypatch.setattr(repo, "_history_raw", fail)
        operation = lambda: repo._read_validated_successful_revision_source(
            base=tmp_path, account="lx", market="US", market_trading_date="2026-07-21",
            revision=0, source_digest="a" * 64,
        )
    log = RunLog()
    with pytest.raises(type(error)) as caught:
        with audit.daily_brief_timing_scope(runlog=log, account="lx", market="US"):
            operation()
    assert caught.value is error
    events = [e for e in log.events if e["data"]["phase"] == phase]
    assert [e["status"] for e in events] == ["start", "error"]
    assert events[-1]["data"]["error_type"] == type(error).__name__
    assert repo._BRIEF_HISTORY_OBSERVATION.get() is None
    assert audit._daily_brief_timing.get() is None
    before = len(log.events)
    with pytest.raises(type(error)):
        operation()
    assert len(log.events) == before


@pytest.mark.parametrize("different", ["base", "account", "market"])
def test_other_history_observation_does_not_suppress_independent_source_timing(tmp_path, different):
    persisted = _persist(tmp_path)
    identity = dict(base=tmp_path, account="lx", market="US")
    identity[different] = {"base": tmp_path / "other", "account": "sy", "market": "HK"}[different]
    log = RunLog()
    with audit.daily_brief_timing_scope(runlog=log, account="lx", market="US"):
        with repo._brief_history_observation(**identity):
            actual, _ = repo._read_validated_successful_revision_source(
                base=tmp_path, account="lx", market="US",
                market_trading_date=persisted["brief"]["market_trading_date"],
                revision=persisted["current_revision"], source_digest=persisted["current_brief_digest"],
            )
    assert actual == persisted["brief"]
    events = [e for e in log.events if e["data"]["phase"] == "source_read"]
    assert [e["status"] for e in events] == ["start", "ok"]
