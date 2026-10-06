from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

import src.application.daily_decision_brief_repository as repo
import src.application.multi_tick_audit as audit
from test_daily_decision_brief_repository_v2 import _persist, _prepare_fixed


class RunLog:
    def __init__(self):
        self.events = []

    def safe_event(self, step, status, **kwargs):
        self.events.append({"step": step, "status": status, **kwargs})


def test_exact_duration_and_nested_account_reset(monkeypatch):
    times = iter(range(20))
    monkeypatch.setattr(audit, "monotonic", lambda: next(times))
    log = RunLog()
    with audit.daily_brief_timing_scope(runlog=log, account="lx", market="HK"):
        with audit.daily_brief_phase("render", operation="fixture"):
            pass
        with audit.daily_brief_timing_scope(runlog=log, account="sy", market="US"):
            with audit.daily_brief_phase("render", operation="fixture"):
                pass
        with audit.daily_brief_phase("render", operation="fixture"):
            pass
    before = len(log.events)
    with audit.daily_brief_phase("render", operation="unscoped"):
        pass
    assert len(log.events) == before
    ends = [e for e in log.events if e["status"] == "ok"]
    renders = [e for e in ends if e["data"]["phase"] == "render"]
    assert [(e["data"]["account"], e["data"]["market"]) for e in renders] == [
        ("lx", "HK"), ("sy", "US"), ("lx", "HK"),
    ]
    assert [e["duration_ms"] for e in renders] == [1000, 1000, 1000]
    assert all(e["data"]["outcome"] == "ok" for e in ends)


@pytest.mark.parametrize("error", [ValueError("fail"), KeyboardInterrupt(), asyncio.CancelledError()])
def test_exception_identity_terminal_and_scope_cleanup(error):
    log = RunLog()
    with pytest.raises(type(error)) as caught:
        with audit.daily_brief_timing_scope(runlog=log, account="lx", market="HK"):
            with audit.daily_brief_phase("assemble", operation="fixture"):
                raise error
    assert caught.value is error
    ends = [e for e in log.events if e["status"] == "error"]
    assert [e["data"]["phase"] for e in ends] == ["assemble", "account_prepare"]
    assert all(e["data"]["error_type"] == type(error).__name__ for e in ends)
    count = len(log.events)
    with audit.daily_brief_phase("render", operation="after_failure"):
        pass
    assert len(log.events) == count


@pytest.mark.parametrize("clock_failure", ["start", "end", "both"])
def test_unavailable_clock_does_not_hide_business_failure(monkeypatch, clock_failure):
    calls = 0

    def clock():
        nonlocal calls
        calls += 1
        if clock_failure == "both" or (clock_failure == "start" and calls == 1) or (
            clock_failure == "end" and calls == 2
        ):
            raise RuntimeError("clock unavailable")
        return 1.0

    monkeypatch.setattr(audit, "monotonic", clock)
    log = RunLog()
    error = ValueError("original")
    with pytest.raises(ValueError) as caught:
        with audit.daily_brief_timing_scope(runlog=log, account="lx", market="HK"):
            raise error
    assert caught.value is error
    end = log.events[-1]
    assert end["status"] == "error" and end["duration_ms"] is None


def test_throwing_logger_preserves_return_and_business_exception():
    class BrokenLog:
        def safe_event(self, *args, **kwargs):
            raise OSError("diagnostic unavailable")

    with audit.daily_brief_timing_scope(runlog=BrokenLog(), account="lx", market="HK"):
        with audit.daily_brief_phase("render", operation="fixture"):
            result = "original result"
    assert result == "original result"
    error = RuntimeError("original failure")
    with pytest.raises(RuntimeError) as caught:
        with audit.daily_brief_timing_scope(runlog=BrokenLog(), account="lx", market="HK"):
            raise error
    assert caught.value is error


@pytest.mark.parametrize("error", [None, ValueError("body failed"), KeyboardInterrupt()])
def test_lock_acquisition_excludes_body_and_unlocks_on_failure(monkeypatch, tmp_path, error):
    clock = [0.0]
    monkeypatch.setattr(audit, "monotonic", lambda: clock[0])
    order = []

    @contextmanager
    def lock(path):
        order.append("acquire")
        clock[0] += 2
        try:
            yield
        finally:
            order.append("release")
            clock[0] += 3

    monkeypatch.setattr(repo, "_shared_exclusive_lock", lock)
    log = RunLog()

    def run():
        with audit.daily_brief_timing_scope(runlog=log, account="lx", market="HK"):
            with repo._exclusive_lock(tmp_path / "fixture.lock"):
                # The successful lock-acquisition terminal already exists.
                assert log.events[-1]["data"]["phase"] == "lock_wait"
                assert log.events[-1]["status"] == "ok"
                order.append("body")
                clock[0] += 10
                if error is not None:
                    raise error

    if error is None:
        run()
    else:
        with pytest.raises(type(error)) as caught:
            run()
        assert caught.value is error
    assert order == ["acquire", "body", "release"]
    end = next(e for e in log.events if e["data"]["phase"] == "lock_wait" and e["status"] == "ok")
    assert end["duration_ms"] == 2000
    assert log.events[-1]["duration_ms"] == 15000
    assert log.events[-1]["status"] == ("error" if error is not None else "ok")


def test_acquisition_error_preserved_without_entering_body(monkeypatch, tmp_path):
    error = OSError("lock unavailable")

    @contextmanager
    def lock(path):
        raise error
        yield

    monkeypatch.setattr(repo, "_shared_exclusive_lock", lock)
    log = RunLog()
    with pytest.raises(OSError) as caught:
        with audit.daily_brief_timing_scope(runlog=log, account="lx", market="HK"):
            with repo._exclusive_lock(tmp_path / "fixture.lock"):
                pytest.fail("lock body entered")
    assert caught.value is error
    assert [e["data"]["phase"] for e in log.events if e["status"] == "error"] == [
        "lock_wait", "account_prepare",
    ]


def test_real_repository_history_and_persistence_keep_outputs(tmp_path):
    log = RunLog()
    with audit.daily_brief_timing_scope(runlog=log, account="lx", market="US"):
        persisted = _persist(tmp_path)
        _prepare_fixed(tmp_path, persisted)
    expected = repo.read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")
    with audit.daily_brief_timing_scope(runlog=log, account="lx", market="US"):
        actual = repo.read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")
    assert actual == expected
    phases = {e["data"]["phase"] for e in log.events if e["status"] == "ok"}
    assert {"persist", "lock_wait", "history_validate"} <= phases
    assert all(e["data"]["account"] == "lx" for e in log.events)
    # Corruption stays unavailable; phase outcome reports failed validation.
    expected["path"].write_text('{"schema_version": "broken"}')
    log.events.clear()
    with audit.daily_brief_timing_scope(runlog=log, account="lx", market="US"):
        damaged = repo.read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")
    assert damaged["reason"] == "state_invalid"
    assert any(e["status"] == "error" and e["data"]["phase"] == "history_validate" for e in log.events)


def test_atomic_write_failure_is_logged_and_original_error_preserved(monkeypatch, tmp_path):
    error = OSError("disk fixture full")
    monkeypatch.setattr(repo, "atomic_write_json", lambda *_: (_ for _ in ()).throw(error))
    log = RunLog()
    with pytest.raises(OSError) as caught:
        with audit.daily_brief_timing_scope(runlog=log, account="lx", market="US"):
            _persist(tmp_path)
    assert caught.value is error
    assert any(e["status"] == "error" and e["data"]["phase"] == "persist" for e in log.events)
    # Original shared lock really released, permitting the next normal attempt.
    monkeypatch.undo()
    assert _persist(tmp_path)["current_revision"] == 0


def test_actual_assembly_and_render_have_same_results(tmp_path):
    from src.application.daily_decision_brief_renderer import render_fixed_report
    from test_daily_decision_brief_service import _assemble

    expected = _assemble(tmp_path)
    log = RunLog()
    with audit.daily_brief_timing_scope(runlog=log, account="lx", market="US"):
        actual = _assemble(tmp_path)
        rendered = render_fixed_report(actual)
    assert actual == expected
    assert rendered == render_fixed_report(actual)
    ends = [e for e in log.events if e["status"] == "ok"]
    assert {"assemble", "render"} <= {e["data"]["phase"] for e in ends}
    assert all(e["data"]["market"] == "US" for e in ends)


@pytest.mark.parametrize("error", [RuntimeError("assembly"), KeyboardInterrupt(), asyncio.CancelledError()])
def test_actual_assembly_failure_records_original_exception(monkeypatch, tmp_path, error):
    import src.application.daily_decision_brief_service as service

    monkeypatch.setattr(service, "load_candidate_snapshot_bundle", lambda **_: (_ for _ in ()).throw(error))
    log = RunLog()
    with pytest.raises(type(error)) as caught:
        with audit.daily_brief_timing_scope(runlog=log, account="lx", market="HK"):
            service.assemble_daily_decision_brief(
                base=tmp_path, run_id="fixture", account="lx", market="HK",
                scheduler_decision={}, account_result={}, pipeline_succeeded=False, config={},
            )
    assert caught.value is error
    phases = [e for e in log.events if e["data"]["phase"] == "assemble"]
    assert [e["status"] for e in phases] == ["start", "error"]
    assert phases[-1]["data"]["error_type"] == type(error).__name__


def test_real_runlog_keeps_identity_and_omits_unavailable_duration(monkeypatch, tmp_path):
    import json
    from src.infrastructure.run_log import RunLogger

    monkeypatch.setattr(audit, "monotonic", lambda: (_ for _ in ()).throw(OSError("clock")))
    log = RunLogger(tmp_path, run_id="timing-fixture")
    with audit.daily_brief_timing_scope(runlog=log, account="lx", market="HK"):
        with audit.daily_brief_phase("render", operation="fixed_report"):
            pass
    rows = [json.loads(line) for line in log.log_path.read_text().splitlines()]
    assert [r["status"] for r in rows] == ["start", "start", "ok", "ok"]
    assert all(r["run_id"] == "timing-fixture" for r in rows)
    assert all(r["data"]["account"] == "lx" and r["data"]["market"] == "HK" for r in rows)
    assert all("duration_ms" not in r for r in rows)
    assert rows[2]["data"] == {
        "account": "lx", "market": "HK", "phase": "render",
        "operation": "fixed_report", "outcome": "ok",
    }


def test_hard_kill_leaves_real_runlog_start_without_success(tmp_path):
    import json
    import select
    import subprocess
    import sys

    script = '''
import signal, sys
from pathlib import Path
from src.application.multi_tick_audit import daily_brief_timing_scope, daily_brief_phase
from src.infrastructure.run_log import RunLogger
log = RunLogger(Path(sys.argv[1]), run_id="kill-fixture")
with daily_brief_timing_scope(runlog=log, account="lx", market="HK"):
    with daily_brief_phase("assemble", operation="kill-fixture"):
        print("ready", flush=True)
        signal.pause()
'''
    child = subprocess.Popen([sys.executable, "-u", "-c", script, str(tmp_path)], stdout=subprocess.PIPE)
    try:
        assert select.select([child.stdout], [], [], 10)[0], "fixture did not reach phase"
        assert child.stdout.readline().strip() == b"ready"
        child.kill()
        assert child.wait(timeout=5) != 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()
    log_path = next((tmp_path / "audit/run_logs").glob("*.jsonl"))
    rows = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert [r["data"]["phase"] for r in rows] == ["account_prepare", "assemble"]
    assert all(r["status"] == "start" and "duration_ms" not in r for r in rows)
