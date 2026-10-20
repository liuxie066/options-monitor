from __future__ import annotations

import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.application.positions import auto_close_schedule as schedule

REAL_POPEN = subprocess.Popen


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


@pytest.fixture
def clock():
    return [instant("2026-10-18T04:07:00")]


@pytest.fixture
def scope(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"_generated": {"market": "US"}}))
    return schedule.schedule_scope("us", config, tmp_path / "runtime", ["demo"])


@pytest.fixture(autouse=True)
def forbid_live_io(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr(schedule.subprocess, "Popen", lambda *a, **k: pytest.fail("real CLI forbidden"))


def run(scope, clock, calls, code=0):
    def child(argv):
        calls.append(argv)
        return code
    return schedule.run_scheduled(scope=scope, repo=Path("/isolated/repo"), now_fn=lambda: clock[0], run_child=child)


def checkpoint(scope):
    return json.loads(schedule.schedule_paths(scope)[0].read_text())


@pytest.mark.parametrize("market,utc,expected", [
    ("us", "2026-07-04T04:06:59", "2026-07-03"),
    ("us", "2026-07-04T04:07:00", "2026-07-04"),
    ("us", "2026-01-04T05:07:00", "2026-01-04"),
    ("us", "2026-03-08T05:07:00", "2026-03-08"),
    ("us", "2026-03-09T04:07:00", "2026-03-09"),
    ("us", "2026-11-01T04:07:00", "2026-11-01"),
    ("us", "2026-11-02T05:07:00", "2026-11-02"),
    ("hk", "2026-10-17T16:04:59", "2026-10-17"),
    ("hk", "2026-10-17T16:05:00", "2026-10-18"),
])
def test_market_clock_and_weekend_dst(market, utc, expected):
    assert schedule.latest_occurrence(market, instant(utc)).isoformat() == expected


def test_scope_canonicalization_and_isolation(tmp_path):
    a = schedule.schedule_scope("us", tmp_path / "x/../config", tmp_path, ["B", "a", "a"])
    b = schedule.schedule_scope("us", tmp_path / "config", tmp_path, ["a", "b"])
    assert a == b
    for key, value in [("market", "hk"), ("accounts", ["a"]), ("config", str(tmp_path / "other")), ("runtime", str(tmp_path / "other"))]:
        assert schedule.schedule_paths(a) != schedule.schedule_paths({**a, key: value})
    with pytest.raises(ValueError):
        schedule.schedule_scope("us", tmp_path, tmp_path, ["../bad"])


def test_success_restart_and_backlog_coalesce(scope, clock):
    calls = []
    assert run(scope, clock, calls) == 0
    assert calls[0][-3:] == ["--apply", "--yes", "--quiet"]
    assert "--runtime-root" in calls[0]
    Path(scope["config"]).unlink()  # Completed gate does not even read config.
    assert run(scope, clock, calls) == 0
    assert len(calls) == 1
    Path(scope["config"]).write_text('{"_generated":{"market":"us"}}')
    clock[0] += timedelta(days=4)
    assert run(scope, clock, calls) == 0
    assert len(calls) == 2
    assert checkpoint(scope)["attempts"] == 1
    assert checkpoint(scope)["occurrence_date"] == "2026-10-22"
    assert not (Path(scope["runtime"]) / "output_runs").exists()


def test_first_boot_before_today_slot_catches_latest_due(scope, clock):
    clock[0] -= timedelta(seconds=1)
    calls = []
    run(scope, clock, calls)
    assert checkpoint(scope)["occurrence_date"] == "2026-10-17"


def test_failure_has_three_attempt_budget_and_five_minute_gap(scope, clock):
    calls = []
    for attempt in range(1, 4):
        assert run(scope, clock, calls, 1) == (75 if attempt < 3 else 78)
        assert checkpoint(scope)["attempts"] == attempt
        assert run(scope, clock, calls, 1) == (75 if attempt < 3 else 78)
        assert len(calls) == attempt
        clock[0] += timedelta(seconds=300)
    assert run(scope, clock, calls, 1) == 78
    assert len(calls) == 3


@pytest.mark.parametrize("status", ["running", "failed", "succeeded", "exhausted"])
def test_rollback_preserves_every_status_and_budget(scope, clock, status):
    calls = []
    if status == "exhausted":
        for _ in range(3):
            run(scope, clock, calls, 1)
            clock[0] += timedelta(seconds=300)
    else:
        run(scope, clock, calls, 0 if status == "succeeded" else 1)
    path = schedule.schedule_paths(scope)[0]
    state = checkpoint(scope)
    if status == "running":
        state.update(status="running", finished_at_utc=None, child_exit_code=None,
                     retry_not_before_utc=(instant("2026-10-18T04:07:00") + timedelta(seconds=900)).isoformat())
        schedule._save_checkpoint(path, state)
    saved = path.read_bytes()
    clock[0] = instant("2026-10-18T04:06:59")
    assert run(scope, clock, calls, 1) == 0
    assert path.read_bytes() == saved
    before = len(calls)
    clock[0] = instant("2026-10-18T04:07:00")
    assert run(scope, clock, calls, 1) == ({"running": 75, "failed": 75, "succeeded": 0, "exhausted": 78}[status])
    assert len(calls) == before
    assert path.read_bytes() == saved


def test_crash_claim_is_persisted_before_effect_and_survives_restart(scope, clock):
    def crash(_argv):
        assert checkpoint(scope)["status"] == "running"
        raise RuntimeError("synthetic process death")
    with pytest.raises(RuntimeError):
        schedule.run_scheduled(scope=scope, repo=Path("/isolated"), now_fn=lambda: clock[0], run_child=crash)
    calls = []
    clock[0] += timedelta(seconds=899)
    assert run(scope, clock, calls) == 75
    clock[0] += timedelta(seconds=1)
    assert run(scope, clock, calls) == 0
    assert checkpoint(scope)["attempts"] == 2


def test_boot_wake_resumes_interrupted_occurrence_without_reset(scope, clock):
    def crash(_argv):
        raise RuntimeError("power loss after claim")
    with pytest.raises(RuntimeError):
        schedule.run_scheduled(scope=scope, repo=Path("/isolated"), now_fn=lambda: clock[0], run_child=crash)
    path = schedule.schedule_paths(scope)[0]
    original = path.read_bytes()
    calls = []
    clock[0] += timedelta(minutes=3)  # Boot wake after calendar already fired.
    assert run(scope, clock, calls) == 75
    assert path.read_bytes() == original
    assert not calls
    clock[0] += timedelta(minutes=12)
    assert run(scope, clock, calls) == 0
    assert checkpoint(scope)["attempts"] == 2
    assert len(calls) == 1
    succeeded = path.read_bytes()
    clock[0] += timedelta(minutes=1)
    assert run(scope, clock, calls) == 0
    assert path.read_bytes() == succeeded
    assert len(calls) == 1


@pytest.mark.parametrize("mutation", [
    lambda state: state.update(attempts=True),
    lambda state: state.update(status="unknown"),
    lambda state: state.update(schema_version=2),
    lambda state: state.update(schema_version=True),
    lambda state: state.update(occurrence_date="2099-01-01"),
    lambda state: state.update(scope={}),
    lambda state: state.update(retry_not_before_utc="broken", status="failed", child_exit_code=1),
])
def test_corrupt_checkpoint_never_launches(scope, clock, mutation):
    calls = []
    run(scope, clock, calls)
    state = checkpoint(scope)
    mutation(state)
    schedule.schedule_paths(scope)[0].write_text(json.dumps(state))
    with pytest.raises((ValueError, KeyError, TypeError)):
        run(scope, clock, calls)
    assert len(calls) == 1


def test_truncated_and_symlink_checkpoint_fail_closed(scope, clock):
    path, _ = schedule.schedule_paths(scope)
    path.parent.mkdir(parents=True)
    path.write_text("{")
    with pytest.raises(ValueError):
        run(scope, clock, [])
    path.unlink()
    path.symlink_to(path.parent / "absent")
    with pytest.raises(OSError):
        run(scope, clock, [])


def test_scope_lock_contention_consumes_no_attempt(scope, clock):
    path, lock = schedule.schedule_paths(scope)
    lock.parent.mkdir(parents=True)
    with lock.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run(scope, clock, []) == 0
        assert not path.exists()


def test_runtime_parent_must_exist_before_any_mkdir(scope, clock, tmp_path):
    scope = {**scope, "runtime": str(tmp_path / "not-installed" / "runtime")}
    with pytest.raises(ValueError, match="runtime parent"):
        run(scope, clock, [])
    assert not (tmp_path / "not-installed").exists()


def test_checkpoint_write_failures_never_reset_budget(scope, clock, monkeypatch):
    calls = []
    real = schedule._save_checkpoint
    writes = []
    def save(path, state):
        writes.append(state["status"])
        if len(writes) == 2:
            raise OSError("disk error")
        real(path, state)
    monkeypatch.setattr(schedule, "_save_checkpoint", save)
    with pytest.raises(OSError):
        run(scope, clock, calls)
    assert len(calls) == 1
    assert checkpoint(scope)["status"] == "running"
    assert checkpoint(scope)["attempts"] == 1
    monkeypatch.setattr(schedule, "_save_checkpoint", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    clock[0] += timedelta(seconds=900)
    with pytest.raises(OSError):
        run(scope, clock, calls)
    assert len(calls) == 1


def test_checkpoint_directories_always_sync_through_runtime_parent(scope, monkeypatch):
    path, _ = schedule.schedule_paths(scope)
    original_open, original_sync = os.open, os.fsync
    opened, synced = {}, []
    def open_path(target, *args, **kwargs):
        fd = original_open(target, *args, **kwargs)
        opened[fd] = Path(target)
        return fd
    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            synced.append(opened[fd])
        original_sync(fd)
    monkeypatch.setattr(schedule.os, "open", open_path)
    monkeypatch.setattr(schedule.os, "fsync", sync)
    expected = [path.parent, path.parent.parent, path.parent.parent.parent,
                Path(scope["runtime"]), Path(scope["runtime"]).parent]
    schedule._save_checkpoint(path, {"scope": scope})
    assert synced == expected
    synced.clear()
    schedule._save_checkpoint(path, {"scope": scope})
    assert synced == expected


def test_parent_directory_sync_failure_retries_existing_chain_before_child(scope, clock, monkeypatch):
    path, _ = schedule.schedule_paths(scope)
    original_open, original_sync = os.open, os.fsync
    opened = {}
    def open_path(target, *args, **kwargs):
        fd = original_open(target, *args, **kwargs)
        opened[fd] = Path(target)
        return fd
    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) and opened[fd] == path.parent.parent:
            raise OSError("synthetic parent directory sync failure")
        original_sync(fd)
    monkeypatch.setattr(schedule.os, "open", open_path)
    monkeypatch.setattr(schedule.os, "fsync", sync)
    calls = []
    with pytest.raises(OSError, match="parent directory sync"):
        run(scope, clock, calls)
    assert calls == []
    assert checkpoint(scope)["status"] == "running"
    assert checkpoint(scope)["attempts"] == 1
    # The directories now exist, but the retry must still fsync their entries.
    clock[0] += timedelta(seconds=900)
    with pytest.raises(OSError, match="parent directory sync"):
        run(scope, clock, calls)
    assert calls == []
    assert checkpoint(scope)["attempts"] == 2
    monkeypatch.setattr(schedule.os, "fsync", original_sync)
    clock[0] += timedelta(seconds=900)
    assert run(scope, clock, calls) == 0
    assert len(calls) == 1
    assert checkpoint(scope)["attempts"] == 3


def test_market_mismatch_and_confirmation_stop_before_child(scope, clock):
    Path(scope["config"]).write_text('{"_generated":{"market":"hk"}}')
    with pytest.raises(ValueError):
        run(scope, clock, [])
    args = ["--market", "us", "--config", scope["config"], "--accounts", "demo", "--runtime-root", scope["runtime"]]
    with pytest.raises(SystemExit) as exc:
        schedule.main(args)
    assert exc.value.code == 2
    assert schedule.main([*args, "--apply", "--yes"]) == 78


@pytest.mark.parametrize("market,start_utc", [
    ("us", "2026-10-18T04:07:00"),
    ("hk", "2026-10-17T16:05:00"),
])
@pytest.mark.parametrize("outcome,expected_status,child_code", [
    ("review_required", "noop", 0),
    ("receipt_failed", "applied", 0),
    ("receipt_unconfirmed", "applied", 0),
    ("business_failed", "failed", 1),
    ("partial_failed", "partial_failed", 1),
])
def test_schedule_public_cli_preserves_business_and_receipt_outcomes(
    tmp_path, monkeypatch, capsys, market, start_utc, outcome, expected_status, child_code,
):
    import socket

    from src.application.positions import auto_close, maintenance, maintenance_receipt
    from src.interfaces.cli import option_positions

    runtime = tmp_path / "runtime"
    accounts = ["fixturea", "fixtureb"]
    config = {
        "_generated": {"market": market.upper()},
        "accounts": accounts,
        "portfolio": {"data_config": str(tmp_path / "unused-portfolio.json")},
    }
    config_path = tmp_path / "config.json"
    config_bytes = json.dumps(config).encode()
    config_path.write_bytes(config_bytes)
    scope = schedule.schedule_scope(market, config_path, runtime, accounts)
    clock = [instant(start_utc)]
    launches, maintenance_calls, forbidden = [], [], []

    def unexpected(*_args, **_kwargs):
        forbidden.append(True)
        pytest.fail("live ledger, receipt, network or subprocess boundary reached")

    monkeypatch.setattr(auto_close, "safe_send_auto_close_receipt", unexpected)
    monkeypatch.setattr(maintenance, "open_position_ledger", unexpected)
    monkeypatch.setattr(maintenance, "safe_send_auto_close_receipt", unexpected)
    monkeypatch.setattr(maintenance_receipt, "select_notification_delivery_adapter", unexpected)
    monkeypatch.setattr(socket.socket, "connect", unexpected)
    monkeypatch.setattr(socket.socket, "connect_ex", unexpected)
    monkeypatch.setattr(subprocess, "run", unexpected)

    def load_config(*, base, config_path: Path, is_scheduled, log):
        assert base == runtime.resolve()
        assert config_path == (tmp_path / "config.json").resolve()
        assert is_scheduled is False
        return config

    def maintain(**kwargs):
        assert kwargs["base"] == runtime.resolve()
        assert kwargs["report_dir"].is_relative_to(runtime.resolve() / "output_runs")
        assert kwargs["cfg"]["config_source_path"] == str(config_path.resolve())
        assert kwargs["cfg"]["_generated"]["market"] == market.upper()
        assert kwargs["dry_run"] is False and kwargs["send_receipt"] is True
        account = kwargs["account"]
        maintenance_calls.append(account)
        failed = outcome == "business_failed" or outcome == "partial_failed" and account == "fixturea"
        applied = int(not failed and outcome != "review_required")
        return {
            "mode": "error" if failed else "applied", "account": account,
            "positions_checked": 1, "candidates_should_close": applied,
            "applied_closed": applied, "skipped_already_closed": 0,
            "skipped_review_required": int(outcome == "review_required"),
            "errors": ["synthetic business failure"] if failed else [],
            "applied": [{"record_id": f"fixture-{account}"}] if applied else [],
            "receipt": {
                "status": outcome.removeprefix("receipt_") if outcome.startswith("receipt_") else "skipped",
                "delivery_confirmed": False,
            },
        }

    monkeypatch.setattr(auto_close, "load_config", load_config)
    monkeypatch.setattr(auto_close, "run_expired_position_maintenance_for_account", maintain)

    def public_cli(argv):
        assert argv == [
            str(tmp_path / "om"), "option-positions", "auto-close-expired",
            "--config", str(config_path.resolve()), "--accounts", *accounts,
            "--runtime-root", str(runtime.resolve()), "--apply", "--yes", "--quiet",
        ]
        launches.append(list(argv))
        return option_positions.main(argv[2:])

    def scheduled():
        return schedule.run_scheduled(
            scope=scope, repo=tmp_path, now_fn=lambda: clock[0], run_child=public_cli,
        )

    assert scheduled() == (schedule.RETRY if child_code else 0)
    assert maintenance_calls == accounts
    assert checkpoint(scope)["child_exit_code"] == child_code
    assert checkpoint(scope)["attempts"] == 1
    result = json.loads((runtime / "output_shared/state/auto_close_expired.json").read_text())
    assert result["status"] == expected_status
    assert result["accounts"] == accounts
    assert result["runtime_config_path"] == str(config_path.resolve())
    assert Path(result["run_dir"]).is_relative_to(runtime.resolve() / "output_runs")
    assert [item["account"] for item in result["account_results"]] == accounts
    for item in result["account_results"]:
        assert item["receipt"]["delivery_confirmed"] is False
        if outcome.startswith("receipt_"):
            assert item["receipt"]["status"] == outcome.removeprefix("receipt_")
    if outcome == "review_required":
        assert result["summary"]["skipped_review_required"] == len(accounts)

    saved = {path.relative_to(runtime): path.read_bytes() for path in runtime.rglob("*") if path.is_file()}
    clock[0] += timedelta(seconds=299)
    assert scheduled() == (schedule.RETRY if child_code else 0)
    assert len(launches) == 1
    assert {path.relative_to(runtime): path.read_bytes() for path in runtime.rglob("*") if path.is_file()} == saved
    clock[0] += timedelta(seconds=1)
    assert scheduled() == (schedule.RETRY if child_code else 0)
    assert len(launches) == (2 if child_code else 1)
    assert checkpoint(scope)["attempts"] == (2 if child_code else 1)
    assert len(list((runtime / "output_runs").iterdir())) == len(launches)
    assert config_path.read_bytes() == config_bytes
    assert forbidden == []
    assert capsys.readouterr().out == ""


def test_child_inherits_group_timeout_kills_and_reaps(monkeypatch):
    class Child:
        returncode = None
        def wait(self, timeout=None):
            if self.returncode is None:
                raise subprocess.TimeoutExpired("fixture", timeout)
            return self.returncode
        def poll(self):
            return self.returncode
        def terminate(self):
            pass  # Simulate a stubborn child.
        def kill(self):
            self.returncode = -signal.SIGKILL
    child = Child()
    def popen(argv, **kwargs):
        assert argv == ["fixture"]
        assert not kwargs  # No setsid/process_group/pass_fds/shell.
        return child
    monkeypatch.setattr(schedule.subprocess, "Popen", popen)
    assert schedule._run_child(["fixture"]) == 124
    assert child.returncode == -signal.SIGKILL


def test_spawn_error_and_cancellation(monkeypatch):
    def fail(*args):
        raise OSError("fixture")
    monkeypatch.setattr(schedule.subprocess, "Popen", fail)
    assert schedule._run_child(["fixture"]) == 127
    class Child:
        returncode = None
        def wait(self, timeout=None):
            if self.returncode is None:
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return self.returncode
        def poll(self):
            return self.returncode
        def terminate(self):
            self.returncode = -signal.SIGTERM
    child = Child()
    monkeypatch.setattr(schedule.subprocess, "Popen", lambda *a: child)
    previous = signal.getsignal(signal.SIGTERM)
    assert schedule._run_child(["fixture"]) == 130
    assert child.returncode == -signal.SIGTERM
    assert signal.getsignal(signal.SIGTERM) is previous


def test_child_exit_during_cleanup_still_reaps_and_restores_signals(monkeypatch):
    class Child:
        returncode = None
        waited = 0
        def poll(self):
            return None
        def wait(self, timeout=None):
            self.waited += 1
            if self.returncode is None:
                raise subprocess.TimeoutExpired("fixture", timeout)
            return self.returncode
        def terminate(self):
            self.returncode = 0
            raise ProcessLookupError("exited just now")
    child = Child()
    monkeypatch.setattr(schedule.subprocess, "Popen", lambda *a: child)
    previous = signal.getsignal(signal.SIGTERM)
    assert schedule._run_child(["fixture"]) == 124
    assert child.waited >= 2
    assert signal.getsignal(signal.SIGTERM) is previous


def test_real_fixture_global_lock_wait_counts_and_times_out(tmp_path, scope, clock, monkeypatch):
    marker, global_lock = tmp_path / "child.json", tmp_path / "global.lock"
    child_source = tmp_path / "waiter.py"
    child_source.write_text(
        "import fcntl,json,os,sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text(json.dumps([os.getpid(),os.getpgrp()]))\n"
        "with open(sys.argv[2],'a') as lock:\n"
        "    fcntl.flock(lock,fcntl.LOCK_EX)\n"
    )
    monkeypatch.setattr(schedule.subprocess, "Popen", REAL_POPEN)
    monkeypatch.setattr(schedule, "ATTEMPT_SECONDS", 1)
    with global_lock.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        result = schedule.run_scheduled(
            scope=scope, repo=tmp_path, now_fn=lambda: clock[0],
            run_child=lambda _argv: schedule._run_child([sys.executable, str(child_source), str(marker), str(global_lock)]),
        )
    assert result == 75
    assert checkpoint(scope)["attempts"] == 1
    assert checkpoint(scope)["child_exit_code"] == 124
    pid, pgid = json.loads(marker.read_text())
    assert pgid == os.getpgrp()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
