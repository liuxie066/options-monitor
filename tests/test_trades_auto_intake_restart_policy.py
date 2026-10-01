from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

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


def _backoff_clock(monkeypatch, *, stop_when):
    """Frozen monotonic clock plus the recording stop event the restart loop waits on."""
    waits: list[float] = []
    clock = [0.0]
    monkeypatch.setattr(auto_intake.time, "monotonic", lambda: clock[0])

    class _Stop:
        stopped = False

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, seconds):
            waits.append(seconds)
            clock[0] += seconds
            if stop_when(seconds, clock[0]):
                self.stopped = True
            return self.stopped

    return waits, _Stop()


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


@pytest.mark.parametrize("reason_code,message", [
    ("OPEND_NEEDS_PHONE_VERIFY", "OpenD 需要手机验证码登录"),
    ("OPEND_LOGIN_INVALID", "OpenD 登录已失效，需人工重新登录"),
    ("OPEND_NEEDS_PIC_VERIFY", "OpenD 需要图形验证码登录"),
])
def test_auth_required_stops_without_retry_and_writes_blocked_status(
    tmp_path: Path, monkeypatch, reason_code: str, message: str,
) -> None:
    class _Listener:
        starts = 0

        def __init__(self, **_kwargs):
            self.close_count = 0

        def start(self, **_kwargs):
            type(self).starts += 1
            return None

        def check_health(self):
            raise TradeIntakeAuthRequired(
                error_code=reason_code,
                message=message,
                detail="需要手机验证码",
            )

        def close(self):
            self.close_count += 1

    rc = _run(tmp_path, monkeypatch, _Listener)

    assert rc == auto_intake.TRADE_INTAKE_AUTH_REQUIRED_EXIT_CODE == 78
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "blocked"
    assert status["stage"] == "auth_required"
    assert status["error_code"] == reason_code
    assert status["reason_code"] == reason_code
    assert _Listener.starts == 1


def test_retryable_disconnect_recovers_and_resets_to_floor(tmp_path: Path, monkeypatch) -> None:
    waits, stop_event = _backoff_clock(monkeypatch, stop_when=lambda seconds, _total: seconds == 0)

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

    rc = _run(tmp_path, monkeypatch, _Listener, reconnect_sec=5, stop_event=stop_event)

    assert rc == 0
    assert _Listener.starts == 2
    assert waits == [1] * 5 + [0]


def test_retry_backoff_is_capped_at_sixty_seconds(tmp_path: Path, monkeypatch) -> None:
    waits, stop_event = _backoff_clock(monkeypatch, stop_when=lambda _seconds, total: total >= 100)

    class _Listener:
        def __init__(self, **_kwargs):
            return None

        def start(self, **_kwargs):
            return None

        def check_health(self):
            raise ConnectionResetError("connection reset")

        def close(self):
            return None

    rc = _run(tmp_path, monkeypatch, _Listener, reconnect_sec=40, stop_event=stop_event)

    assert rc == 0
    assert waits == [1] * 100
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["restart_count"] == 2


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


def test_order_hints_coalesce_to_one_probe_and_deal_backfill(tmp_path: Path, monkeypatch) -> None:
    stop = threading.Event()
    clock = [0.0]
    calls = []
    monkeypatch.setattr(auto_intake.time, "monotonic", lambda: clock[0])

    class Listener:
        def __init__(self, *, on_order_hint, **_kwargs):
            self.on_order_hint = on_order_hint

        def start(self, **_kwargs):
            for order_id in ("o1", "o1", "o2"):
                self.on_order_hint({
                    "futu_account_id": "123", "environment": "REAL",
                    "market": "HK", "order_id": order_id,
                })
            self.on_order_hint({
                "futu_account_id": "456", "environment": "REAL",
                "market": "HK", "order_id": "wrong",
            })

        def check_health(self):
            pass

        def close(self):
            pass

    class Client:
        def __init__(self, **_kwargs):
            pass

        def probe_order_hint(self, **kwargs):
            calls.append(("order", kwargs))
            return {"found": True, "dealt_qty": "2"}

        def fetch(self, **_kwargs):
            raise AssertionError("fake backfill should intercept")

        def close(self):
            pass

    def backfill(**_kwargs):
        calls.append(("deals", None))
        if calls.count(("deals", None)) == 1:
            clock[0] += 31
        else:
            stop.set()
        return {"ok": True, "finished_at_utc": "now", "deal_count": 0,
                "applied_count": 0, "skipped_duplicate_count": 0,
                "failed_count": 0, "unresolved_count": 0}

    monkeypatch.setattr(auto_intake, "OpenDTradePushListener", Listener)
    monkeypatch.setattr(auto_intake, "OpenDHistoryDealClient", Client)
    monkeypatch.setattr(auto_intake, "run_history_backfill", backfill)
    source = _source(tmp_path)
    source.update(
        account_mapping={"123": "lx"}, futu_account_ids=["123"],
        backfill={"enabled": True, "startup_check": False, "interval_sec": 300},
        settlement_observation={"enabled": False},
    )
    rc = auto_intake._run_listener_source_loop(
        source=source, repo=object(), cfg={}, cfg_path=tmp_path / "config.json",
        runtime_root=tmp_path, runtime_root_source="test",
        intake_cfg={"mode": "dry-run", "enabled": True, "account_mapping": {"123": "lx"},
                    "backfill": source["backfill"]},
        apply_changes=False, receipt_callback=lambda _context: {},
        process_lock=threading.RLock(), stop_event=stop,
    )
    assert rc == 0
    assert calls == [
        ("order", {"futu_account_id": "123", "order_id": "o1"}),
        ("deals", None),
        ("order", {"futu_account_id": "123", "order_id": "o2"}),
        ("deals", None),
    ]


def test_order_hint_query_failure_retries_without_fabricated_deal(tmp_path: Path, monkeypatch) -> None:
    stop = threading.Event()
    clock = [0.0]
    calls = []
    monkeypatch.setattr(auto_intake.time, "monotonic", lambda: clock[0])

    class Listener:
        def __init__(self, *, on_order_hint, **_kwargs):
            self.on_order_hint = on_order_hint

        def start(self, **_kwargs):
            self.on_order_hint({
                "futu_account_id": "123", "environment": "REAL",
                "market": "HK", "order_id": "o1",
            })

        def check_health(self):
            pass

        def close(self):
            pass

    class Client:
        def __init__(self, **_kwargs):
            pass

        def probe_order_hint(self, **_kwargs):
            calls.append("order")
            if calls.count("order") == 1:
                raise RuntimeError("order query failed")
            return {"found": True}

        def fetch(self, **_kwargs):
            raise AssertionError("fake backfill should intercept")

        def close(self):
            pass

    def backfill(**_kwargs):
        calls.append("deals")
        if calls.count("deals") == 1:
            clock[0] += 31
            return {"ok": False, "finished_at_utc": "now", "deal_count": 0,
                    "applied_count": 0, "skipped_duplicate_count": 0,
                    "failed_count": 1, "unresolved_count": 0}
        stop.set()
        return {"ok": True, "finished_at_utc": "now", "deal_count": 0,
                "applied_count": 0, "skipped_duplicate_count": 0,
                "failed_count": 0, "unresolved_count": 0}

    monkeypatch.setattr(auto_intake, "OpenDTradePushListener", Listener)
    monkeypatch.setattr(auto_intake, "OpenDHistoryDealClient", Client)
    monkeypatch.setattr(auto_intake, "run_history_backfill", backfill)
    source = _source(tmp_path)
    source.update(
        account_mapping={"123": "lx"}, futu_account_ids=["123"],
        backfill={"enabled": True, "startup_check": False, "interval_sec": 300},
        settlement_observation={"enabled": False},
    )
    assert auto_intake._run_listener_source_loop(
        source=source, repo=object(), cfg={}, cfg_path=tmp_path / "config.json",
        runtime_root=tmp_path, runtime_root_source="test",
        intake_cfg={"mode": "dry-run", "enabled": True, "account_mapping": {"123": "lx"},
                    "backfill": source["backfill"]},
        apply_changes=False, receipt_callback=lambda _context: {},
        process_lock=threading.RLock(), stop_event=stop,
    ) == 0
    assert calls == ["order", "deals", "order", "deals"]


def test_order_hint_cancellation_skips_deal_query(tmp_path: Path, monkeypatch) -> None:
    stop = threading.Event()
    calls = []

    class Listener:
        def __init__(self, *, on_order_hint, **_kwargs):
            self.on_order_hint = on_order_hint

        def start(self, **_kwargs):
            self.on_order_hint({
                "futu_account_id": "123", "environment": "REAL",
                "market": "HK", "order_id": "o1",
            })

        def check_health(self):
            pass

        def close(self):
            pass

    class Client:
        def __init__(self, **_kwargs):
            pass

        def probe_order_hint(self, **_kwargs):
            calls.append("order")
            stop.set()
            return {"found": True}

        def fetch(self, **_kwargs):
            raise AssertionError("cancelled backfill must not query deals")

        def close(self):
            pass

    monkeypatch.setattr(auto_intake, "OpenDTradePushListener", Listener)
    monkeypatch.setattr(auto_intake, "OpenDHistoryDealClient", Client)
    monkeypatch.setattr(auto_intake, "run_history_backfill",
                        lambda **_kwargs: pytest.fail("cancelled backfill must not run"))
    source = _source(tmp_path)
    source.update(
        account_mapping={"123": "lx"}, futu_account_ids=["123"],
        backfill={"enabled": True, "startup_check": False, "interval_sec": 300},
        settlement_observation={"enabled": False},
    )
    assert auto_intake._run_listener_source_loop(
        source=source, repo=object(), cfg={}, cfg_path=tmp_path / "config.json",
        runtime_root=tmp_path, runtime_root_source="test",
        intake_cfg={"mode": "dry-run", "enabled": True, "account_mapping": {"123": "lx"},
                    "backfill": source["backfill"]},
        apply_changes=False, receipt_callback=lambda _context: {},
        process_lock=threading.RLock(), stop_event=stop,
    ) == 0
    assert calls == ["order"]
