from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application import multi_tick_audit as audit


def test_nested_phases_are_inclusive_and_shared_work_has_no_account(monkeypatch):
    clock = Mock(side_effect=[0, 1, 3, 5])
    monkeypatch.setattr(audit, "monotonic", clock)
    log = SimpleNamespace(safe_event=Mock())
    with audit.prepared_context_timing_scope(runlog=log):
        with audit.prepared_context_phase("ledger_read_a"):
            pass
    calls = log.safe_event.call_args_list
    assert [(c.args[0], c.args[1], c.kwargs["data"]["phase"]) for c in calls] == [
        ("prepared_context_phase", "start", "post_portfolio_prepare"),
        ("prepared_context_phase", "start", "ledger_read_a"),
        ("prepared_context_phase", "ok", "ledger_read_a"),
        ("prepared_context_phase", "ok", "post_portfolio_prepare"),
    ]
    assert calls[2].kwargs["duration_ms"] == 2000
    assert calls[3].kwargs["duration_ms"] == 5000
    assert all("account" not in c.kwargs["data"] for c in calls)
    assert calls[1].kwargs["data"] == {"phase": "ledger_read_a"}


@pytest.mark.parametrize("error", [ValueError("private value"), KeyboardInterrupt("private value")])
def test_exception_identity_redaction_and_scope_reset(error):
    log = SimpleNamespace(safe_event=Mock())
    with pytest.raises(type(error)) as caught:
        with audit.prepared_context_timing_scope(runlog=log):
            with audit.prepared_context_phase("account_context", account="lx"):
                raise error
    assert caught.value is error
    terminal = log.safe_event.call_args_list[-2]
    assert terminal.args == ("prepared_context_phase", "error")
    assert terminal.kwargs["data"] == {
        "phase": "account_context", "account": "lx", "outcome": "error",
        "error_type": type(error).__name__,
    }
    assert "private value" not in str(log.safe_event.call_args_list)
    count = log.safe_event.call_count
    with audit.prepared_context_phase("after_failure"):
        pass
    assert log.safe_event.call_count == count


@pytest.mark.parametrize("clock_values", [[OSError("clock"), 2], [0, OSError("clock")]])
def test_failed_clock_keeps_duration_unknown(monkeypatch, clock_values):
    monkeypatch.setattr(audit, "monotonic", Mock(side_effect=clock_values))
    log = SimpleNamespace(safe_event=Mock())
    with audit.prepared_context_timing_scope(runlog=log):
        result = 7
    assert result == 7
    assert log.safe_event.call_args.args[1] == "ok"
    assert log.safe_event.call_args.kwargs["duration_ms"] is None


def test_logger_failure_preserves_result_exception_and_nested_scope(monkeypatch):
    clock = Mock(return_value=1)
    monkeypatch.setattr(audit, "monotonic", clock)
    failed = SimpleNamespace(safe_event=Mock(side_effect=OSError("disk")))
    outer = SimpleNamespace(safe_event=Mock())
    with audit.prepared_context_timing_scope(runlog=outer):
        with audit.prepared_context_timing_scope(runlog=failed):
            result = 7
        assert result == 7
        count = clock.call_count
        with audit.prepared_context_timing_scope(runlog=None):
            with audit.prepared_context_phase("skipped"):
                pass
        assert clock.call_count == count
        with audit.prepared_context_phase("restored", account="sy"):
            pass
    assert outer.safe_event.call_args_list[-2].kwargs["data"]["account"] == "sy"
    assert all(c.kwargs["data"]["phase"] != "skipped" for c in outer.safe_event.call_args_list)
    error = RuntimeError("original")
    with pytest.raises(RuntimeError) as caught:
        with audit.prepared_context_timing_scope(runlog=failed):
            raise error
    assert caught.value is error
    count = clock.call_count
    with audit.prepared_context_phase("unbound"):
        pass
    assert clock.call_count == count


def test_real_run_logger_binds_run_and_omits_unknown_duration(monkeypatch, tmp_path):
    import json
    from src.infrastructure.run_log import RunLogger

    monkeypatch.setattr(audit, "monotonic", Mock(side_effect=OSError("clock")))
    log = RunLogger(tmp_path, run_id="isolated-timing")
    with audit.prepared_context_timing_scope(runlog=log):
        with audit.prepared_context_phase("current_projection", account="lx"):
            pass
    rows = [json.loads(line) for line in log.log_path.read_text().splitlines()]
    assert len(rows) == 4
    assert {row["run_id"] for row in rows} == {"isolated-timing"}
    assert all(row["ts"] and "duration_ms" not in row for row in rows)
    assert rows[2]["data"] == {"phase": "current_projection", "account": "lx", "outcome": "ok"}
