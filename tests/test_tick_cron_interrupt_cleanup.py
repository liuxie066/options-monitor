"""A real wrapper interrupt must stop its own children before releasing flock."""
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from src.application import tick_cron


@pytest.fixture
def python_interrupt_handler():
    # SDK imports in other tests install a process-wide quit handler. This
    # wrapper contract exercises Python's KeyboardInterrupt, as in its own CLI.
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def test_sigint_stops_real_descendant_before_tick_lock_is_released(tmp_path, monkeypatch, python_interrupt_handler):
    heartbeat = tmp_path / "heartbeat"
    ready = tmp_path / "ready"
    child_code = (
        "import pathlib,signal,sys,time; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "p=pathlib.Path(sys.argv[1]); "
        "exec('while True:\\n p.write_text(str(time.monotonic()))\\n time.sleep(.01)')"
    )
    parent_code = (
        "import os,pathlib,subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-I','-c',sys.argv[1],sys.argv[2]]); "
        "pathlib.Path(sys.argv[3]).write_text(str(os.getpid())); time.sleep(60)"
    )
    plan = replace(
        tick_cron.build_tick_cron_plan(
            market="hk", accounts=["lx"], no_send=True,
            lock_path=str(tmp_path / "tick.lock"), timeout_seconds=10,
        ),
        tick_argv=[sys.executable, "-I", "-c", parent_code, child_code, str(heartbeat), str(ready)],
    )
    monkeypatch.setattr(tick_cron, "build_tick_cron_plan", lambda **kwargs: plan)
    started = []
    original_popen = subprocess.Popen

    def observe_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(tick_cron.subprocess, "Popen", observe_popen)
    stop = threading.Event()
    interrupted = threading.Event()

    def interrupt_when_children_are_running():
        deadline = time.monotonic() + 5
        while not stop.wait(.01):
            if ready.exists() and heartbeat.exists():
                interrupted.set()
                os.kill(os.getpid(), signal.SIGINT)
                return
            if time.monotonic() >= deadline:
                return

    interrupter = threading.Thread(target=interrupt_when_children_are_running)
    interrupter.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            tick_cron.run_tick_cron(
                market="hk", accounts=["lx"], no_send=True,
                runtime_root=tmp_path, preflight_config_fn=None,
                environ=dict(os.environ),
            )
        assert interrupted.is_set(), "the real SIGINT path was not exercised"
        timing_files = list(tmp_path.glob("output_runs/*/state/tick_process_timings.json"))
        assert len(timing_files) == 1
        phases = json.loads(timing_files[0].read_text())
        assert set(phases) == {"business_wait_seconds", "diagnostic_seconds", "cleanup_seconds"}
        assert phases["business_wait_seconds"] > 0
        assert phases["diagnostic_seconds"] >= 0 and phases["cleanup_seconds"] >= 0
        with Path(plan.lock_path).open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            previous = heartbeat.read_text()
            stop.wait(.15)
            assert heartbeat.read_text() == previous, "owned descendant still writes after flock was released"
        assert started[0].returncode is not None, "wrapper child was not reaped"
        assert not (tmp_path / "output_shared/state/tick_cron_last_result.hk.current.json").exists()
    finally:
        stop.set()
        interrupter.join(timeout=1)
        for process in started:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
