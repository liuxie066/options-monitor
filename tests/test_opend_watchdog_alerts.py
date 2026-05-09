"""Minimal tests for OpenD watchdog error mapping + alert rate limit (no pytest)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory


def _ensure_repo_path() -> None:
    import sys

    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))


def test_watchdog_error_code_mapping() -> None:
    _ensure_repo_path()
    import scripts.opend_watchdog as w

    c, _ = w.classify_watchdog_result(None, 'OpenD port not open: 127.0.0.1:11111')
    assert c == 'OPEND_PORT_CLOSED'

    c, _ = w.classify_watchdog_result({'program_status_type': 'INITING'}, None)
    assert c == 'OPEND_NOT_READY'

    c, _ = w.classify_watchdog_result({'program_status_type': 'READY', 'qot_logined': False}, None)
    assert c == 'OPEND_QOT_NOT_LOGINED'

    c, _ = w.classify_watchdog_result(None, 'ret=-1 err=请求频率太高，请稍后再试')
    assert c == 'OPEND_RATE_LIMIT'

    c, _ = w.classify_watchdog_result(None, 'OpenD waiting phone verification code')
    assert c == 'OPEND_NEEDS_PHONE_VERIFY'

    c, _ = w.classify_watchdog_result(None, 'something weird')
    assert c == 'OPEND_API_ERROR'


def test_opend_alert_rate_limit() -> None:
    _ensure_repo_path()
    from scripts.multi_tick.opend_guard import should_send_opend_alert

    with TemporaryDirectory() as td:
        base = Path(td)

        # First send for a code should pass.
        assert should_send_opend_alert(base, 'OPEND_RATE_LIMIT', cooldown_sec=600) is True
        # Immediate second send for same code should be blocked.
        assert should_send_opend_alert(base, 'OPEND_RATE_LIMIT', cooldown_sec=600) is False
        # Different code should still pass.
        assert should_send_opend_alert(base, 'OPEND_NOT_READY', cooldown_sec=600) is True


def test_opend_alert_family_dedupe_and_burst_limit() -> None:
    _ensure_repo_path()
    from scripts.multi_tick.opend_guard import should_send_opend_alert

    with TemporaryDirectory() as td:
        base = Path(td)

        # Same unhealthy family should dedupe even if concrete error code differs.
        assert should_send_opend_alert(base, 'OPEND_NOT_READY', cooldown_sec=600) is True
        assert should_send_opend_alert(base, 'OPEND_API_ERROR', cooldown_sec=600) is False

        # Burst limit should cap project-level alert storms.
        assert should_send_opend_alert(base, 'OPEND_RATE_LIMIT', cooldown_sec=1, burst_window_sec=600, burst_max=2) is True
        assert should_send_opend_alert(base, 'OPEND_NEEDS_PHONE_VERIFY', cooldown_sec=1, burst_window_sec=600, burst_max=2) is False


def test_opend_alert_translates_wechat_clawbot_to_openclaw_weixin(monkeypatch) -> None:
    _ensure_repo_path()
    from scripts.multi_tick import opend_guard

    captured: dict[str, object] = {}

    def fake_run(cmd, *, cwd, capture_output=False, text=False):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return SimpleNamespace(returncode=0, stdout='{"message_id":"msg_1"}', stderr="")

    monkeypatch.setattr(opend_guard.subprocess, "run", fake_run)

    with TemporaryDirectory() as td:
        base = Path(td)
        ok = opend_guard.send_opend_alert(
            base,
            {"notifications": {"channel": "wechat_clawbot", "target": "clawbot:test"}},
            error_code="OPEND_RATE_LIMIT",
            message_text="rate limited",
        )

    assert ok is True
    cmd = captured["cmd"]
    assert cmd[cmd.index("--channel") + 1] == "openclaw-weixin"
    assert cmd[cmd.index("--target") + 1] == "clawbot:test"


def test_port_retry_loop_recovers_within_window(monkeypatch) -> None:
    """Port recovers after 2 closed checks → retry loop returns True."""
    _ensure_repo_path()
    import scripts.opend_watchdog as w

    call_count = {"n": 0}

    def fake_port_open(host, port, timeout=0.8):
        call_count["n"] += 1
        # First two calls return False; from 3rd onwards True.
        return call_count["n"] >= 3

    monkeypatch.setattr(w, "port_open", fake_port_open)
    monkeypatch.setattr(w, "try_start_opend", lambda: (True, "started"))
    monkeypatch.setattr(w.time, "sleep", lambda _s: None)

    h = w.Health(ok=False, ports_open=False)
    recovered = w._port_retry_loop(
        h,
        "127.0.0.1",
        11111,
        ensure=True,
        retry_interval_sec=0.01,
        retry_timeout_sec=10.0,
        success_threshold=2,
    )

    assert recovered is True
    assert h.recoveredts is not None
    assert h.startedbywatchdog is True
    assert h.retrycount is not None and h.retrycount >= 2
    assert h.firstfailts is not None
    assert h.retryelapsedms is not None


def test_port_retry_loop_exhausts_window(monkeypatch) -> None:
    """Port never opens → retry loop returns False after timeout."""
    _ensure_repo_path()
    import scripts.opend_watchdog as w

    monkeypatch.setattr(w, "port_open", lambda *_a, **_k: False)
    monkeypatch.setattr(w, "try_start_opend", lambda: (False, "failed"))

    # Use a very short timeout so the test completes instantly.
    sleep_calls = {"n": 0}

    def fake_sleep(s):
        sleep_calls["n"] += 1

    monkeypatch.setattr(w.time, "sleep", fake_sleep)

    # Patch time.time to simulate fast-forward: after initial call, advance
    # past the deadline immediately.
    _t = [0.0]

    def fake_time():
        v = _t[0]
        _t[0] += 5.0  # advance 5 seconds per call
        return v

    monkeypatch.setattr(w.time, "time", fake_time)

    h = w.Health(ok=False, ports_open=False)
    recovered = w._port_retry_loop(
        h,
        "127.0.0.1",
        11111,
        ensure=False,
        retry_interval_sec=3.0,
        retry_timeout_sec=10.0,
        success_threshold=2,
    )

    assert recovered is False
    assert h.recoveredts is None
    assert h.retryelapsedms is not None
    assert h.firstfailts is not None


def test_port_retry_loop_no_start_when_ensure_false(monkeypatch) -> None:
    """With ensure=False, try_start_opend must not be called."""
    _ensure_repo_path()
    import scripts.opend_watchdog as w

    start_called = {"n": 0}

    def fake_start():
        start_called["n"] += 1
        return (True, "started")

    monkeypatch.setattr(w, "try_start_opend", fake_start)
    monkeypatch.setattr(w, "port_open", lambda *_a, **_k: True)
    monkeypatch.setattr(w.time, "sleep", lambda _s: None)

    h = w.Health(ok=False, ports_open=False)
    w._port_retry_loop(
        h,
        "127.0.0.1",
        11111,
        ensure=False,
        retry_interval_sec=0.01,
        retry_timeout_sec=5.0,
        success_threshold=1,
    )

    assert start_called["n"] == 0
    assert h.startedbywatchdog is None
