"""Minimal tests for OpenD watchdog error mapping + alert rate limit (no pytest)."""

from __future__ import annotations

from pathlib import Path
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
    import scripts.send_if_needed_multi as s

    with TemporaryDirectory() as td:
        base = Path(td)

        # First send for a code should pass.
        assert s._should_send_opend_alert(base, 'OPEND_RATE_LIMIT', cooldown_sec=600) is True
        # Immediate second send for same code should be blocked.
        assert s._should_send_opend_alert(base, 'OPEND_RATE_LIMIT', cooldown_sec=600) is False
        # Different code should still pass.
        assert s._should_send_opend_alert(base, 'OPEND_NOT_READY', cooldown_sec=600) is True


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
