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
