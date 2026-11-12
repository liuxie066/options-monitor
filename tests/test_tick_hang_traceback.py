from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from src.application.tick_cron import _run_tick_process_group


_CHILD = """
from src.application import tick_cron
import time

def blocked_opend_query():
    time.sleep(float(__import__('sys').argv[1]))

blocked_opend_query()
"""


def _environment(path: Path, *, timeout: float = 600) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["OM_TICK_HANG_TRACEBACK_PATH"] = str(path)
    env["OM_TIMEOUT_SECONDS"] = str(timeout)
    return env


def test_outer_timeout_requests_real_child_traceback_before_termination(tmp_path: Path) -> None:
    path = tmp_path / "run" / "state" / "tick_hang_traceback.txt"
    # The scheduled dump is at 570s; only the parent's signal can write this stack.
    with pytest.raises(subprocess.TimeoutExpired):
        _run_tick_process_group(
            command=[sys.executable, "-c", _CHILD, "60"],
            cwd=Path(__file__).resolve().parents[1],
            env=_environment(path),
            timeout_seconds=3,
            terminate_grace_seconds=0.1,
        )
    trace = path.read_text()
    assert "blocked_opend_query" in trace
    assert 'File "<string>"' in trace
    assert "Current thread" in trace


def test_child_writes_scheduled_traceback_without_parent_signal(tmp_path: Path) -> None:
    path = tmp_path / "state" / "tick_hang_traceback.txt"
    subprocess.run(
        [sys.executable, "-c", _CHILD, "0.5"],
        cwd=Path(__file__).resolve().parents[1],
        env=_environment(path, timeout=30.1),
        timeout=10,
        check=True,
    )
    trace = path.read_text()
    assert "Timeout" in trace
    assert "blocked_opend_query" in trace


def test_fast_child_leaves_no_hang_report(tmp_path: Path) -> None:
    path = tmp_path / "state" / "tick_hang_traceback.txt"
    subprocess.run(
        [sys.executable, "-c", _CHILD, "0"],
        cwd=Path(__file__).resolve().parents[1],
        env=_environment(path),
        timeout=10,
        check=True,
    )
    assert path.read_text() == ""
