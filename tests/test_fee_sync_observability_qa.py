"""Independent adversarial checks for additive fee-attempt evidence."""
from __future__ import annotations

from copy import deepcopy
import json
import queue
import threading
from types import SimpleNamespace

import pytest

from src.application.bot.control.renderer import render_canonical_tool_result
from src.application.trades import auto_intake, order_fee_sync, order_fee_timing
from test_option_performance_agent_tool import _report
from test_order_fee_sync import EVENT_MS, _Provider, _repo_with_bare_option_event


TARGET = ("富途", "lx", "123", "order-1")


def _pending(**_kwargs):
    return {"reason_counts": {"fee_pending": 1}}


def test_qa_clock_failures_preserve_queued_target_and_pending_result(monkeypatch):
    def unavailable():
        raise OSError("synthetic clock unavailable")

    monkeypatch.setattr(order_fee_timing, "time", SimpleNamespace(time=unavailable, monotonic_ns=unavailable))
    targets = queue.SimpleQueue()
    targets.put(auto_intake._QueuedFeeTarget(TARGET, order_fee_timing.fee_clock_sample()))
    drained = auto_intake._drain_fee_target_queue(targets)
    called = []

    def sync(_repo, **kwargs):
        called.append(kwargs["target_identity"])
        return _pending()

    status, receipts = auto_intake._run_fee_sync_cycle(
        repo=object(), provider=object(), targets=list(drained), enqueued_times=drained,
        apply_changes=True, observed_at_ms=EVENT_MS, sync_fn=sync,
    )
    assert called == [TARGET]
    assert status["pending_count"] == 1 and status["failed_count"] == 0
    timing = receipts[0]["timing"]
    assert timing["queue_source"] == "enqueue"
    assert timing["enqueued_at_ms"] is None and timing["queue_wait_ms"] is None
    assert timing["processing"]["outcome"] == "returned"
    assert timing["processing"]["elapsed_ms"] is None


def test_qa_concurrent_late_put_keeps_earliest_monotonic_sample_and_next_attempt():
    targets = queue.SimpleQueue()
    later_put = threading.Event()

    def earlier_sample_put_late():
        assert later_put.wait(2)
        targets.put(auto_intake._QueuedFeeTarget(TARGET, (9000, 1_000_000)))

    producer = threading.Thread(target=earlier_sample_put_late)
    producer.start()
    targets.put(auto_intake._QueuedFeeTarget(TARGET, (1000, 2_000_000)))
    later_put.set()
    producer.join(2)
    assert not producer.is_alive()
    drained = auto_intake._drain_fee_target_queue(targets)
    assert drained == {TARGET: (9000, 1_000_000)}
    calls = []

    def sync(_repo, **kwargs):
        calls.append(kwargs["target_identity"])
        targets.put(auto_intake._QueuedFeeTarget(TARGET, (8000, 3_000_000)))
        return _pending()

    auto_intake._run_fee_sync_cycle(
        repo=object(), provider=object(), targets=list(drained) * 2, enqueued_times=drained,
        apply_changes=True, observed_at_ms=EVENT_MS, sync_fn=sync,
    )
    assert calls == [TARGET]
    assert auto_intake._drain_fee_target_queue(targets) == {TARGET: (8000, 3_000_000)}


def test_qa_serial_wait_includes_prior_target_but_processing_does_not(monkeypatch):
    clock = [1000, 100_000_000]
    monkeypatch.setattr(order_fee_timing, "fee_clock_sample", lambda: tuple(clock))
    second = (*TARGET[:3], "order-2")

    def sync(_repo, **_kwargs):
        clock[0] -= 300  # Wall clock steps backwards during each request.
        clock[1] += 250_000_000
        return _pending()

    status, rows = auto_intake._run_fee_sync_cycle(
        repo=object(), provider=object(), targets=[second, TARGET],
        enqueued_times={TARGET: (1100, 0), second: (1100, 0)},
        apply_changes=True, observed_at_ms=EVENT_MS, sync_fn=sync,
    )
    assert status["pending_count"] == 2
    assert [row["timing"]["queue_wait_ms"] for row in rows] == [100, 350]
    assert [row["timing"]["processing"]["elapsed_ms"] for row in rows] == [250, 250]
    assert rows[0]["timing"]["processing"]["returned_at_ms"] < rows[0]["timing"]["processing"]["started_at_ms"]


@pytest.mark.parametrize("failure,expected_calls,phase", [
    ("terminal", (1, 0), "terminal_query"),
    ("fee", (1, 1), "fee_query"),
    ("ledger", (1, 1), "ledger_enrichment"),
])
def test_qa_failure_preserves_phase_evidence_and_redacts_message(tmp_path, monkeypatch, failure, expected_calls, phase):
    repo = _repo_with_bare_option_event(tmp_path)

    class Provider(_Provider):
        def fetch_terminal_orders(self, **kwargs):
            if failure == "terminal":
                self.terminal_calls += 1
                raise TimeoutError("sensitive-order-id=/private/synthetic")
            return super().fetch_terminal_orders(**kwargs)

        def fetch_order_fees(self, **kwargs):
            if failure == "fee":
                self.fee_calls += 1
                raise TimeoutError("sensitive-order-id=/private/synthetic")
            return super().fetch_order_fees(**kwargs)

    if failure == "ledger":
        def fail_enrichment(*_args, **_kwargs):
            raise RuntimeError("sensitive-order-id=/private/synthetic")
        monkeypatch.setattr(order_fee_sync, "enrich_order_fees", fail_enrichment)
    provider = Provider()
    _, rows = auto_intake._run_fee_sync_cycle(
        repo=repo, provider=provider, targets=[TARGET], apply_changes=False,
        observed_at_ms=EVENT_MS,
    )
    assert (provider.terminal_calls, provider.fee_calls) == expected_calls
    summary = auto_intake._fee_attempt_audit_summary(rows[0])
    assert summary["timing"][phase]["outcome"] == "raised"
    assert summary["timing"][phase]["returned_at_ms"] is None
    assert summary["timing"]["queue_source"] == "recovery"
    assert summary["timing"]["queue_wait_ms"] is None
    assert summary["timing"]["enqueued_at_ms"] is None
    assert "sensitive-order-id" not in json.dumps(summary)
    assert "/private" not in json.dumps(summary)
    assert "order-1" not in json.dumps(summary)


def test_qa_cancellation_is_not_changed_into_business_failure_receipt():
    timing = {}
    with pytest.raises(KeyboardInterrupt):
        with order_fee_timing.fee_timing_phase(timing, "fee_query"):
            raise KeyboardInterrupt()
    assert timing["fee_query"]["outcome"] == "raised"
    assert timing["fee_query"]["returned_at_ms"] is None


def test_qa_range_call_does_not_publish_last_batch_as_complete_timing(tmp_path):
    repo = _repo_with_bare_option_event(tmp_path)
    provider = _Provider()
    timing = {}
    receipt = order_fee_sync.sync_order_fees(
        repo, account="lx", start_ms=EVENT_MS - 1, end_exclusive_ms=EVENT_MS + 1,
        provider=provider, apply=False, observed_at_ms=EVENT_MS, timing=timing,
    )
    assert receipt["provider_call_count"] == 2
    assert (provider.terminal_calls, provider.fee_calls) == (1, 1)
    assert timing == {}


def test_qa_nested_audit_allowlist_does_not_copy_provider_payload():
    dirty = {
        "enqueued_at_ms": "account-secret", "queue_wait_ms": True,
        "queue_source": "enqueue", "order_id": "account-secret",
        "processing": {"started_at_ms": 5, "returned_at_ms": 4, "elapsed_ms": -1,
                       "outcome": "returned", "order_id": "account-secret"},
        "fee_query": {"started_at_ms": {"private": "account-secret"},
                      "outcome": "account-secret", "response": "account-secret"},
        "unknown_phase": {"response": "account-secret"},
    }
    summary = auto_intake._fee_attempt_audit_summary({"timing": dirty})["timing"]
    assert "account-secret" not in json.dumps(summary)
    assert "enqueued_at_ms" not in summary and "queue_wait_ms" not in summary
    assert summary["processing"]["started_at_ms"] == 5
    assert "elapsed_ms" not in summary["processing"]
    assert "unknown_phase" not in summary


def test_qa_fee_uncertainty_is_scoped_to_cohort_and_keeps_period_cash():
    data = _report()
    observed = {"amount": 10, "status": "observed", "missing": []}
    data["period_option_net_cashflow"]["by_currency"] = {
        "USD": {key: dict(observed) for key in ("gross", "fees", "total")},
    }
    data["option_net_cashflow"]["by_currency"] = {
        "USD": {key: {"amount": None, "status": "partial", "missing": ["fee_estimated"]}
                for key in ("open", "terminated", "total")},
    }
    original = deepcopy(data)
    text = render_canonical_tool_result(renderer_key="option_performance", tool_result={"ok": True}, data=data)
    period_line = next(line for line in text.splitlines() if line.startswith("- 期间期权净现金流"))
    cohort_line = next(line for line in text.splitlines() if line.startswith("- 开仓批次期权现金结果"))
    assert "合计 10.00" in period_line and "尚未确认" not in period_line
    assert "实际手续费尚未确认" in cohort_line
    assert "手续费待同步" not in text and "重建" not in text
    assert "统计截止：2026-09-02T23:59:59.999+08:00" in text
    assert data == original
