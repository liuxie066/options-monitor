from __future__ import annotations

import json
import queue
from types import SimpleNamespace

import pytest

from src.application.trades import order_fee_sync, order_fee_timing
from src.application.trades.auto_intake import (
    _QueuedFeeTarget,
    _drain_fee_target_queue,
    _fee_attempt_audit_summary,
    _run_fee_sync_cycle,
)
from test_order_fee_sync import EVENT_MS, _Provider, _repo_with_bare_option_event


TARGET = ("富途", "lx", "123", "order-1")


def _cycle(repo, provider, **kwargs):
    return _run_fee_sync_cycle(
        repo=repo, provider=provider, targets=[TARGET], apply_changes=True,
        observed_at_ms=EVENT_MS, **kwargs,
    )


def test_fee_timing_distinguishes_wall_clock_from_elapsed(monkeypatch, tmp_path):
    samples = iter((10_000 - n * 100, n * 10_000_000) for n in range(8))
    monkeypatch.setattr(order_fee_timing, "fee_clock_sample", lambda: next(samples))
    repo = _repo_with_bare_option_event(tmp_path)
    provider = _Provider()
    status, rows = _cycle(repo, provider, enqueued_times={TARGET: (20_000, -5_000_000)})
    timing = rows[0]["timing"]
    assert status["actual_count"] == 1
    assert timing["queue_wait_ms"] == 5
    assert timing["processing"] == {
        "started_at_ms": 10_000, "returned_at_ms": 9_300, "raised_at_ms": None,
        "elapsed_ms": 70, "outcome": "returned",
    }
    for phase in ("terminal_query", "fee_query", "ledger_enrichment"):
        assert timing[phase]["elapsed_ms"] == 10
        assert timing[phase]["outcome"] == "returned"
    assert rows[0]["provider_call_count"] == 2
    with repo._connect() as conn:
        updated_at_ms = conn.execute("SELECT updated_at_ms FROM trade_events").fetchone()[0]
        applied_at_ms = conn.execute("SELECT applied_at_ms FROM broker_fee_enrichment_audit").fetchone()[0]
    assert updated_at_ms == applied_at_ms == EVENT_MS
    assert timing["ledger_enrichment"]["returned_at_ms"] != applied_at_ms


def test_fee_queue_dedup_uses_monotonic_and_keeps_later_arrivals():
    pending = queue.SimpleQueue()
    pending.put(_QueuedFeeTarget(TARGET, (100, 200)))
    pending.put(_QueuedFeeTarget(TARGET, (200, 100)))
    pending.put(_QueuedFeeTarget(TARGET, (50, 300)))
    assert _drain_fee_target_queue(pending) == {TARGET: (200, 100)}
    pending.put(_QueuedFeeTarget(TARGET, (1, 400)))
    assert _drain_fee_target_queue(pending) == {TARGET: (1, 400)}


@pytest.mark.parametrize("failure_phase", ["terminal_query", "fee_query", "ledger_enrichment"])
def test_fee_failures_keep_completed_timing_and_reason(monkeypatch, tmp_path, failure_phase):
    repo = _repo_with_bare_option_event(tmp_path)
    provider = _Provider()

    def fail(*args, **kwargs):
        raise TimeoutError("private-order-and-path")

    if failure_phase == "terminal_query":
        monkeypatch.setattr(provider, "fetch_terminal_orders", fail)
    elif failure_phase == "fee_query":
        monkeypatch.setattr(provider, "fetch_order_fees", fail)
    else:
        monkeypatch.setattr(order_fee_sync, "enrich_order_fees", fail)
    status, rows = _cycle(repo, provider)
    assert status["failed_count"] == 1
    assert rows[0]["timing"][failure_phase]["outcome"] == "raised"
    assert rows[0]["timing"][failure_phase]["returned_at_ms"] is None
    assert rows[0]["timing"][failure_phase]["raised_at_ms"] is not None
    assert rows[0]["timing"]["queue_source"] == "recovery"
    assert rows[0]["timing"]["queue_wait_ms"] is None
    assert "private-order-and-path" not in json.dumps(_fee_attempt_audit_summary(rows[0]))
    if failure_phase == "ledger_enrichment":
        assert rows[0]["timing"]["fee_query"]["outcome"] == "returned"
        assert rows[0]["timing"]["processing"]["outcome"] == "raised"


def test_returned_provider_without_fee_remains_pending(tmp_path):
    class Pending(_Provider):
        def fetch_order_fees(self, **kwargs):
            return {}, {}

    status, rows = _cycle(_repo_with_bare_option_event(tmp_path), Pending())
    assert status["pending_count"] == 1
    assert rows[0]["reason_counts"]["fee_pending"] == 1
    assert rows[0]["timing"]["fee_query"]["outcome"] == "returned"
    assert rows[0]["actual_observation_count"] == 0


def test_already_actual_never_fabricates_provider_response(tmp_path):
    repo = _repo_with_bare_option_event(tmp_path)
    _cycle(repo, _Provider())
    status, rows = _cycle(repo, _Provider())
    assert status["already_actual_count"] == 1
    assert rows[0]["provider_call_count"] == 0
    assert "fee_query" not in rows[0]["timing"]
    assert "terminal_query" not in rows[0]["timing"]


def test_clock_failure_preserves_business_effect_and_queue_target(monkeypatch, tmp_path):
    def fail():
        raise OSError("clock unavailable")

    monkeypatch.setattr(order_fee_timing, "time", SimpleNamespace(time=fail, monotonic_ns=fail))
    sample = order_fee_timing.fee_clock_sample()
    pending = queue.SimpleQueue()
    pending.put(_QueuedFeeTarget(TARGET, sample))
    queued = _drain_fee_target_queue(pending)
    status, rows = _cycle(_repo_with_bare_option_event(tmp_path), _Provider(), enqueued_times=queued)
    assert status["actual_count"] == 1
    assert rows[0]["provider_call_count"] == 2
    timing = rows[0]["timing"]
    assert timing["queue_source"] == "enqueue"
    assert timing["enqueued_at_ms"] is None
    assert timing["queue_wait_ms"] is None
    assert timing["ledger_enrichment"] == {
        "started_at_ms": None, "returned_at_ms": None, "raised_at_ms": None,
        "elapsed_ms": None, "outcome": "returned",
    }


def test_timing_audit_has_nested_allowlist():
    summary = _fee_attempt_audit_summary({"timing": {
        "order_id": "private-order", "queue_source": "private-order",
        "enqueued_at_ms": "private-order", "queue_wait_ms": True,
        "fee_query": {"outcome": "returned", "elapsed_ms": 4,
                      "started_at_ms": {"private-path": "/secret"}, "query": "private-order"},
        "extra_phase": {"secret": "private-order"},
    }})
    assert summary["timing"] == {"fee_query": {
        "returned_at_ms": None, "raised_at_ms": None, "elapsed_ms": 4, "outcome": "returned",
    }}
    assert "private" not in json.dumps(summary)


def test_timing_cancellation_propagates_and_records_phase():
    timing = {}
    with pytest.raises(KeyboardInterrupt):
        with order_fee_timing.fee_timing_phase(timing, "fee_query"):
            raise KeyboardInterrupt()
    assert timing["fee_query"]["outcome"] == "raised"


def test_range_call_keeps_timing_disabled(tmp_path):
    timing = {}
    receipt = order_fee_sync.sync_order_fees(
        _repo_with_bare_option_event(tmp_path), account="lx", start_ms=EVENT_MS - 1,
        end_exclusive_ms=EVENT_MS + 1, provider=_Provider(), apply=False,
        observed_at_ms=EVENT_MS, timing=timing,
    )
    assert receipt["provider_call_count"] == 2
    assert timing == {}
