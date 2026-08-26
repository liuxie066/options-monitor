from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from src.application.trades import auto_intake
from src.infrastructure.futu_trade_push import TradeIntakeAuthRequired


def _source(tmp_path: Path, *, reconnect_sec: int = 5) -> dict:
    return {
        "id": "lx",
        "account": "lx",
        "host": "127.0.0.1",
        "port": 11111,
        "state_path": tmp_path / "state.json",
        "audit_path": tmp_path / "audit.jsonl",
        "status_path": tmp_path / "status.json",
        "reconnect_sec": reconnect_sec,
        "account_mapping": {},
        "futu_account_ids": [],
        "backfill": {"enabled": False},
    }


def _run(tmp_path: Path, monkeypatch, listener_type, *, reconnect_sec: int = 5, stop_event=None) -> int:
    monkeypatch.setattr(auto_intake, "OpenDTradePushListener", listener_type)
    return auto_intake._run_listener_source_loop(
        source=_source(tmp_path, reconnect_sec=reconnect_sec),
        repo=object(),
        cfg={},
        cfg_path=tmp_path / "config.json",
        runtime_root=tmp_path,
        runtime_root_source="test",
        intake_cfg={"mode": "dry-run", "enabled": True, "account_mapping": {}, "backfill": {"enabled": False}},
        apply_changes=False,
        receipt_callback=lambda _context: {},
        process_lock=threading.RLock(),
        stop_event=stop_event,
    )


def test_auth_required_stops_without_retry_and_writes_blocked_status(tmp_path: Path, monkeypatch) -> None:
    class _Listener:
        def __init__(self, **_kwargs):
            self.close_count = 0

        def start(self, **_kwargs):
            return None

        def check_health(self):
            raise TradeIntakeAuthRequired(
                error_code="OPEND_NEEDS_PHONE_VERIFY",
                message="OpenD 需要手机验证码登录",
                detail="需要手机验证码",
            )

        def close(self):
            self.close_count += 1

    rc = _run(tmp_path, monkeypatch, _Listener)

    assert rc == auto_intake.TRADE_INTAKE_AUTH_REQUIRED_EXIT_CODE == 78
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "blocked"
    assert status["stage"] == "auth_required"
    assert status["error_code"] == "OPEND_NEEDS_PHONE_VERIFY"


def test_retryable_disconnect_recovers_and_resets_to_floor(tmp_path: Path, monkeypatch) -> None:
    waits: list[float] = []

    class _Stop:
        stopped = False

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, seconds):
            waits.append(seconds)
            if len(waits) >= 2:
                self.stopped = True
            return self.stopped

    class _Listener:
        starts = 0

        def __init__(self, **_kwargs):
            return None

        def start(self, **_kwargs):
            type(self).starts += 1

        def check_health(self):
            if type(self).starts == 1:
                raise ConnectionResetError("connection reset")

        def close(self):
            return None

    rc = _run(tmp_path, monkeypatch, _Listener, reconnect_sec=5, stop_event=_Stop())

    assert rc == 0
    assert _Listener.starts == 2
    assert waits == [5, 5]


def test_retry_backoff_is_capped_at_sixty_seconds(tmp_path: Path, monkeypatch) -> None:
    waits: list[float] = []

    class _Stop:
        stopped = False

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, seconds):
            waits.append(seconds)
            if len(waits) >= 2:
                self.stopped = True
            return self.stopped

    class _Listener:
        def __init__(self, **_kwargs):
            return None

        def start(self, **_kwargs):
            return None

        def check_health(self):
            raise ConnectionResetError("connection reset")

        def close(self):
            return None

    rc = _run(tmp_path, monkeypatch, _Listener, reconnect_sec=40, stop_event=_Stop())

    assert rc == 0
    assert waits == [40, 60]


def test_multi_source_auth_stops_sibling_and_propagates_exit_code() -> None:
    sibling_stopped = threading.Event()

    def _runner(source: dict, stop_event: threading.Event) -> int:
        if source["id"] == "auth":
            return auto_intake.TRADE_INTAKE_AUTH_REQUIRED_EXIT_CODE
        assert stop_event.wait(2), "auth result did not stop sibling source"
        sibling_stopped.set()
        return 0

    rc = auto_intake._coordinate_listener_sources(
        [{"id": "auth"}, {"id": "sibling"}],
        run_source=_runner,
    )

    assert rc == auto_intake.TRADE_INTAKE_AUTH_REQUIRED_EXIT_CODE
    assert sibling_stopped.is_set()


def test_source_loop_treats_start_cancellation_as_clean_stop(tmp_path: Path, monkeypatch) -> None:
    from src.infrastructure.futu_trade_push import TradeIntakeStartCancelled

    class _Listener:
        def __init__(self, **_kwargs):
            return None

        def start(self, **_kwargs):
            raise TradeIntakeStartCancelled("cancelled")

        def close(self):
            return None

    rc = _run(tmp_path, monkeypatch, _Listener, stop_event=threading.Event())

    assert rc == 0
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "stopped"
    assert status["stage"] == "start_cancelled"


def test_multi_source_crash_stops_sibling_and_returns_failure() -> None:
    sibling_stopped = threading.Event()

    def _runner(source: dict, stop_event: threading.Event) -> int:
        if source["id"] == "crash":
            raise RuntimeError("boom")
        assert stop_event.wait(2), "crashed source did not stop sibling source"
        sibling_stopped.set()
        return 0

    rc = auto_intake._coordinate_listener_sources(
        [{"id": "crash"}, {"id": "sibling"}],
        run_source=_runner,
    )

    assert rc == 1
    assert sibling_stopped.is_set()


def test_multi_source_shutdown_is_bounded_when_sibling_ignores_stop() -> None:
    release_sibling = threading.Event()

    def _runner(source: dict, _stop_event: threading.Event) -> int:
        if source["id"] == "failed":
            return 1
        release_sibling.wait(2)
        return 0

    started_at = time.monotonic()
    rc = auto_intake._coordinate_listener_sources(
        [{"id": "failed"}, {"id": "stuck"}],
        run_source=_runner,
        shutdown_timeout_sec=0.05,
    )
    elapsed = time.monotonic() - started_at
    release_sibling.set()

    assert rc == 1
    assert elapsed < 0.5
