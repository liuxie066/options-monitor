from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

from src.application.trades import process_supervisor


def test_hard_exit_does_not_wait_for_non_daemon_threads() -> None:
    script = """
import threading
import time
from src.application.trades.process_supervisor import hard_exit
threading.Thread(target=lambda: time.sleep(60), daemon=False).start()
hard_exit(78)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        timeout=5,
    )
    assert completed.returncode == 78


def test_parent_exit_code_preserves_application_status() -> None:
    assert process_supervisor._parent_exit_code(0) == 0
    assert process_supervisor._parent_exit_code(1) == 1
    assert process_supervisor._parent_exit_code(78) == 78


def test_parent_exit_code_maps_signal_and_missing_status_to_failure() -> None:
    assert process_supervisor._parent_exit_code(-15) == 143
    assert process_supervisor._parent_exit_code(None) == 1


def test_child_entry_hard_exits_with_application_status(monkeypatch) -> None:
    import src.application.trades.auto_intake as auto_intake

    observed: list[int] = []
    monkeypatch.setattr(auto_intake, "main", lambda argv: 78)
    monkeypatch.setattr(process_supervisor, "hard_exit", lambda code: observed.append(code))

    process_supervisor._trade_intake_child(["--config", "config.us.json"])

    assert observed == [78]


def test_supervisor_terminates_child_on_keyboard_interrupt(monkeypatch) -> None:
    events: list[object] = []

    class _FakeChild:
        exitcode = None

        def start(self) -> None:
            events.append("start")

        def join(self, timeout=None) -> None:
            events.append(("join", timeout))
            if timeout is None and events.count(("join", None)) == 1:
                raise KeyboardInterrupt

        def is_alive(self) -> bool:
            return "terminate" not in events

        def terminate(self) -> None:
            events.append("terminate")

        def kill(self) -> None:
            events.append("kill")

    fake_child = _FakeChild()
    fake_context = SimpleNamespace(
        Process=lambda **kwargs: events.append(("process", kwargs)) or fake_child
    )
    monkeypatch.setattr(process_supervisor.multiprocessing, "get_context", lambda method: fake_context)

    assert process_supervisor.run_trade_intake_process(["--config", "config.us.json"]) == 0
    assert events[0][0] == "process"
    assert events[0][1]["target"] is process_supervisor._trade_intake_child
    assert events[0][1]["args"] == (["--config", "config.us.json"],)
    assert "terminate" in events
    assert "kill" not in events
