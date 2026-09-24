"""Minimal tests for OpenD watchdog error mapping + alert rate limit."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from src.application import system_alerts
from tests.notification_format_assertions import assert_mobile_flat_markdown


def _adapter(send_fn):
    """A clawbot delivery-adapter selector; asserts the guard picks that provider."""

    def fake_select(provider):  # type: ignore[no-untyped-def]
        assert provider == "wechat_clawbot"
        return SimpleNamespace(send_fn=send_fn, normalize_fn=lambda **_: {}, failure_stage="send_wechat_clawbot_message")

    return fake_select


def _cfg(**overrides) -> dict:
    """Notification config with the clawbot provider/target these tests share."""
    return {"notifications": {"provider": "wechat_clawbot", "target": "test_target", **overrides}}


def test_watchdog_error_code_mapping() -> None:
    import src.infrastructure.opend_watchdog as w

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


def test_opend_alert_rate_limit(tmp_path: Path) -> None:
    from src.application.multi_tick.opend_guard import should_send_opend_alert

    base = Path(tmp_path)

    # First send for a code should pass.
    assert should_send_opend_alert(base, 'OPEND_RATE_LIMIT', cooldown_sec=600) is True
    # Immediate second send for same code should be blocked.
    assert should_send_opend_alert(base, 'OPEND_RATE_LIMIT', cooldown_sec=600) is False
    # Different code should still pass.
    assert should_send_opend_alert(base, 'OPEND_NOT_READY', cooldown_sec=600) is True


def test_opend_alert_is_latched_until_recovery(tmp_path: Path) -> None:
    from src.application.multi_tick import opend_guard

    base = Path(tmp_path)

    assert opend_guard.should_send_opend_alert(base, 'OPEND_RATE_LIMIT', cooldown_sec=60) is True
    # A stale cooldown must not reopen the same incident.
    state_path = opend_guard.opend_alert_rl_path(base)
    state = opend_guard.read_json(state_path, {})
    state['last_sent_utc_by_error']['project::OPEND_RATE_LIMIT'] = '2020-01-01T00:00:00+00:00'
    opend_guard.write_json(state_path, state)
    assert opend_guard.should_send_opend_alert(base, 'OPEND_RATE_LIMIT', cooldown_sec=60) is False

    opend_guard.record_opend_recovery(base)
    assert opend_guard.should_send_opend_alert(base, 'OPEND_RATE_LIMIT', cooldown_sec=60) is True


def test_opend_alert_family_dedupe_and_burst_limit(tmp_path: Path) -> None:
    from src.application.multi_tick.opend_guard import should_send_opend_alert

    base = Path(tmp_path)

    # Same unhealthy family should dedupe even if concrete error code differs.
    assert should_send_opend_alert(base, 'OPEND_NOT_READY', cooldown_sec=600) is True
    assert should_send_opend_alert(base, 'OPEND_API_ERROR', cooldown_sec=600) is False

    # Burst limit should cap project-level alert storms.
    assert should_send_opend_alert(base, 'OPEND_RATE_LIMIT', cooldown_sec=1, burst_window_sec=600, burst_max=2) is True
    assert should_send_opend_alert(base, 'OPEND_NEEDS_PHONE_VERIFY', cooldown_sec=1, burst_window_sec=600, burst_max=2) is False


def test_opend_alert_routes_wechat_clawbot_through_delivery_adapter(monkeypatch, tmp_path: Path) -> None:
    from src.application.multi_tick import opend_guard

    captured: dict[str, object] = {}

    def fake_send(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return {"ok": True, "command_ok": True, "delivery_confirmed": True, "message_id": "msg_1"}

    def fake_select(provider):  # type: ignore[no-untyped-def]
        captured["provider"] = provider
        return SimpleNamespace(send_fn=fake_send, normalize_fn=lambda **_: {}, failure_stage="send_wechat_clawbot_message")

    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", fake_select)
    monkeypatch.setattr(opend_guard, "utc_now", lambda: "2026-07-21T08:30:00+00:00")

    base = Path(tmp_path)
    cfg = {"notifications": {"channel": "wechat_clawbot", "target": "clawbot:test", "opend_alert_after_consecutive_failures": 1}}
    ok = opend_guard.send_opend_alert(
        base,
        cfg,
        error_code="OPEND_RATE_LIMIT",
        message_text="rate limited\n  - retry exhausted",
        detail="first line\n    - nested detail",
    )

    assert ok is True
    assert captured["provider"] == "wechat_clawbot"
    assert captured["channel"] == "wechat_clawbot"
    assert captured["target"] == "clawbot:test"
    message = str(captured["message"])
    assert message.startswith("# OM · 系统通知 · OpenD")
    assert "状态｜❌ 不可用" in message
    assert "时间｜2026-07-21 16:30:00 北京时间" in message
    assert "影响｜本轮行情与交易数据可能不完整" in message
    assert "原因｜rate limited · - retry exhausted" in message
    assert "诊断｜`OPEND_RATE_LIMIT`" in message
    assert "详情｜first line · - nested detail" in message
    assert_mobile_flat_markdown(message)


def test_send_opend_alert_no_send_does_not_consume_rate_limit(monkeypatch, tmp_path: Path) -> None:
    from src.application.multi_tick import opend_guard

    calls: list[dict[str, object]] = []

    def fake_send(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(dict(kwargs))
        return {"ok": True, "command_ok": True, "delivery_confirmed": True, "message_id": "msg_1"}

    fake_select = _adapter(fake_send)

    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", fake_select)

    base = Path(tmp_path)
    cfg = _cfg(opend_alert_cooldown_sec=600)

    dry_run = opend_guard.send_opend_alert(
        base,
        cfg,
        error_code="OPEND_NEEDS_PHONE_VERIFY",
        message_text="needs phone",
        no_send=True,
        skip_consecutive_gate=True,
    )
    assert dry_run is False
    assert calls == []
    assert not opend_guard.opend_alert_rl_path(base).exists()

    sent = opend_guard.send_opend_alert(
        base,
        cfg,
        error_code="OPEND_NEEDS_PHONE_VERIFY",
        message_text="needs phone",
        skip_consecutive_gate=True,
    )
    assert sent is True
    assert len(calls) == 1


def test_send_opend_alert_failed_send_reserves_incident_attempt(monkeypatch, tmp_path: Path) -> None:
    from src.application.multi_tick import opend_guard

    calls: list[dict[str, object]] = []
    send_results: list[dict[str, object]] = [
        {"ok": False, "command_ok": False, "delivery_confirmed": False},
        {"ok": True, "command_ok": True, "delivery_confirmed": True, "message_id": "msg_2"},
    ]

    def fake_send(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(dict(kwargs))
        return send_results.pop(0)

    fake_select = _adapter(fake_send)

    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", fake_select)

    base = Path(tmp_path)
    cfg = _cfg(opend_alert_cooldown_sec=600)

    failed = opend_guard.send_opend_alert(
        base,
        cfg,
        error_code="OPEND_NEEDS_PHONE_VERIFY",
        message_text="needs phone",
        skip_consecutive_gate=True,
    )
    assert failed is False
    assert len(calls) == 1
    assert opend_guard.opend_alert_rl_path(base).exists()
    alert_state = json.loads(opend_guard.opend_alert_rl_path(base).read_text())
    assert alert_state["last_delivery_status_by_error"]["project::OPEND_LOGIN_ACTION_REQUIRED"] == "delivery_unknown"
    assert opend_guard.send_opend_alert(
        base, cfg, error_code="OPEND_LOGIN_INVALID", message_text="invalid login",
        skip_consecutive_gate=True,
    ) is False
    assert len(calls) == 1

    opend_guard.send_opend_recovery_notice(base, cfg, no_send=True)
    sent = opend_guard.send_opend_alert(
        base,
        cfg,
        error_code="OPEND_NEEDS_PHONE_VERIFY",
        message_text="needs phone",
        skip_consecutive_gate=True,
    )
    assert sent is True
    assert len(calls) == 2
    alert_state = json.loads(opend_guard.opend_alert_rl_path(base).read_text())
    assert alert_state["last_delivery_status_by_error"]["project::OPEND_LOGIN_ACTION_REQUIRED"] == "delivery_confirmed"

    blocked = opend_guard.send_opend_alert(
        base,
        cfg,
        error_code="OPEND_NEEDS_PHONE_VERIFY",
        message_text="needs phone",
        skip_consecutive_gate=True,
    )
    assert blocked is False
    assert len(calls) == 2


def test_opend_watchdog_uses_system_incident_for_repeat_and_recovery(monkeypatch, tmp_path: Path) -> None:
    from src.application.multi_tick import opend_guard

    sends: list[dict[str, object]] = []
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", _adapter(
        lambda **kwargs: sends.append(kwargs) or {"delivery_confirmed": True},
    ))
    cfg = _cfg(opend_alert_after_consecutive_failures=1)
    fields = dict(base=tmp_path, cfg=cfg, error_code="OPEND_LOGIN_INVALID",
                  message_text="login invalid")
    assert opend_guard.send_opend_alert(**fields)
    assert not opend_guard.send_opend_alert(**fields)
    assert len(sends) == 1
    state_path = tmp_path / "output_shared/state/system_alerts.json"
    first = next(iter(json.loads(state_path.read_text()).values()))
    assert first["failure_code"] == "OPEND_LOGIN_ACTION_REQUIRED"
    assert first["stage"] == "watchdog"
    assert first["status"] == "failed"
    assert sends[0]["idempotency_key"].startswith("om-")

    assert opend_guard.send_opend_recovery_notice(tmp_path, cfg)
    assert not opend_guard.send_opend_recovery_notice(tmp_path, cfg)
    assert next(iter(json.loads(state_path.read_text()).values()))["status"] == "recovered"
    assert len(sends) == 2
    assert opend_guard.send_opend_alert(**fields)
    assert len(sends) == 3
    assert sends[0]["idempotency_key"] != sends[2]["idempotency_key"]


def test_opend_alert_state_failure_emits_error_without_provider_send(monkeypatch, tmp_path: Path, capsys) -> None:
    from src.application.multi_tick import opend_guard

    path = tmp_path / "output_shared/state/system_alerts.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken")
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", lambda _provider: (_ for _ in ()).throw(AssertionError("provider must not send")))
    assert not opend_guard.send_opend_alert(
        tmp_path, _cfg(), error_code="OPEND_LOGIN_INVALID", message_text="invalid",
        skip_consecutive_gate=True,
    )
    assert "<3>OPEND_ALERT_INFRA_FAILED" in capsys.readouterr().err


def test_opend_recovery_notice_after_unconfigured_failure_uses_system_state(monkeypatch, tmp_path: Path) -> None:
    from src.application.multi_tick import opend_guard

    sends: list[dict[str, object]] = []
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", _adapter(
        lambda **kwargs: sends.append(kwargs) or {"delivery_confirmed": True},
    ))
    unconfigured = {"notifications": {"opend_alert_after_consecutive_failures": 1}}
    assert not opend_guard.send_opend_alert(
        tmp_path, unconfigured, error_code="OPEND_RATE_LIMIT", message_text="rate limited",
    )
    assert not (tmp_path / "output_shared/state/system_alerts.json").exists()
    configured = _cfg(opend_alert_after_consecutive_failures=1)
    assert opend_guard.send_opend_recovery_notice(tmp_path, configured)
    assert not opend_guard.send_opend_recovery_notice(tmp_path, configured)
    assert len(sends) == 1
    state = json.loads((tmp_path / "output_shared/state/system_alerts.json").read_text())
    assert next(iter(state.values()))["status"] == "recovered"


def test_late_delivery_confirmation_does_not_mark_new_incident(monkeypatch, tmp_path: Path) -> None:
    import src.application.multi_tick.opend_guard as opend_guard

    def confirm_after_new_incident(base: Path, cfg: dict, message: str, key: str) -> bool:
        del cfg, message, key
        opend_guard.record_opend_recovery(base)
        assert opend_guard.should_send_opend_alert(base, "OPEND_LOGIN_INVALID") is True
        path = base / "output_shared/state/system_alerts.json"
        state = json.loads(path.read_text())
        incident = next(iter(state.values()))
        incident["reserved_at"] = "2026-01-01T00:00:00+00:00"
        path.write_text(json.dumps(state))
        return True

    monkeypatch.setattr(system_alerts, "_send", confirm_after_new_incident)
    base = Path(tmp_path)
    assert opend_guard.send_opend_alert(
        base, _cfg(), error_code="OPEND_NEEDS_PHONE_VERIFY", message_text="needs phone",
        skip_consecutive_gate=True,
    ) is True
    state = json.loads(opend_guard.opend_alert_rl_path(base).read_text())
    assert state["last_delivery_status_by_error"]["project::OPEND_LOGIN_ACTION_REQUIRED"] == "delivery_unknown"


def test_port_retry_loop_recovers_within_window(monkeypatch) -> None:
    """Port recovers after 2 closed checks → retry loop returns True."""
    import src.infrastructure.opend_watchdog as w

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
    import src.infrastructure.opend_watchdog as w

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
    import src.infrastructure.opend_watchdog as w

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


def test_record_opend_failure_increments_count(tmp_path: Path) -> None:
    from src.application.multi_tick.opend_guard import record_opend_failure, record_opend_recovery

    base = Path(tmp_path)

    assert record_opend_failure(base) == 1
    assert record_opend_failure(base) == 2
    assert record_opend_failure(base) == 3

    # Recovery should return the previous count and reset to 0.
    prev = record_opend_recovery(base)
    assert prev == 3

    # After recovery the count is 0; recovery again returns 0.
    assert record_opend_recovery(base) == 0

    # Failures restart from 1.
    assert record_opend_failure(base) == 1


def test_record_opend_recovery_on_clean_state(tmp_path: Path) -> None:
    """record_opend_recovery on a fresh base returns 0 without error."""
    from src.application.multi_tick.opend_guard import record_opend_recovery

    assert record_opend_recovery(Path(tmp_path)) == 0


def test_consecutive_threshold_gates_alert(tmp_path: Path) -> None:
    """send_opend_alert is suppressed until consecutive_threshold is reached."""
    from src.application.multi_tick import opend_guard
    import unittest.mock as mock

    calls: list[str] = []

    def fake_send(**kwargs):  # type: ignore[no-untyped-def]
        calls.append("send")
        return {"ok": True, "command_ok": True, "delivery_confirmed": True, "message_id": "msg_1"}

    fake_select = _adapter(fake_send)

    base = Path(tmp_path)
    cfg = _cfg(opend_alert_after_consecutive_failures=3, opend_alert_cooldown_sec=1)

    with (
            mock.patch.object(system_alerts, "select_notification_delivery_adapter", fake_select),
        mock.patch.object(opend_guard, "utc_now", lambda: "2026-07-21T08:30:00+00:00"),
    ):
        # First two calls should be gated (below threshold).
        r1 = opend_guard.send_opend_alert(base, cfg, error_code="OPEND_PORT_CLOSED", message_text="test")
        assert r1 is False, "should be gated at count=1"
        r2 = opend_guard.send_opend_alert(base, cfg, error_code="OPEND_PORT_CLOSED", message_text="test")
        assert r2 is False, "should be gated at count=2"
        # Third call reaches threshold.
        r3 = opend_guard.send_opend_alert(base, cfg, error_code="OPEND_PORT_CLOSED", message_text="test")
        assert r3 is True, "should pass threshold at count=3"
        assert calls == ["send"]


def test_consecutive_threshold_skip_gate_sends_immediately(tmp_path: Path) -> None:
    """skip_consecutive_gate=True bypasses the consecutive failure check."""
    from src.application.multi_tick import opend_guard
    import unittest.mock as mock

    calls: list[str] = []

    def fake_send(**kwargs):  # type: ignore[no-untyped-def]
        calls.append("send")
        return {"ok": True, "command_ok": True, "delivery_confirmed": True, "message_id": "msg_1"}

    fake_select = _adapter(fake_send)

    base = Path(tmp_path)
    cfg = _cfg(opend_alert_after_consecutive_failures=3, opend_alert_cooldown_sec=1)
    with (
            mock.patch.object(system_alerts, "select_notification_delivery_adapter", fake_select),
        mock.patch.object(opend_guard, "utc_now", lambda: "2026-07-21T08:30:00+00:00"),
    ):
        r = opend_guard.send_opend_alert(
            base, cfg,
            error_code="OPEND_NEEDS_PHONE_VERIFY",
            message_text="needs phone",
            skip_consecutive_gate=True,
        )
    assert r is True
    assert calls == ["send"]


def test_send_opend_recovery_notice_after_threshold_failures(tmp_path: Path) -> None:
    """Recovery notice is sent only when prev_count >= threshold."""
    from src.application.multi_tick import opend_guard
    import unittest.mock as mock

    calls: list[dict[str, object]] = []

    def fake_send(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(dict(kwargs))
        return {"ok": True, "command_ok": True, "delivery_confirmed": True, "message_id": "msg_1"}

    fake_select = _adapter(fake_send)

    base = Path(tmp_path)
    cfg = _cfg(opend_alert_after_consecutive_failures=3, opend_alert_send_recovery_notice=True)

    with (
            mock.patch.object(system_alerts, "select_notification_delivery_adapter", fake_select),
        mock.patch.object(opend_guard, "utc_now", lambda: "2026-07-21T08:30:00+00:00"),
    ):
        # No failures recorded yet; recovery notice should NOT be sent.
        r = opend_guard.send_opend_recovery_notice(base, cfg)
        assert r is False
        assert calls == []

        # Record 3 failures (reach threshold).
        for _ in range(3):
            opend_guard.record_opend_failure(base)

        # Now recovery notice should be sent.
        r = opend_guard.send_opend_recovery_notice(base, cfg)
        assert r is True
        assert len(calls) == 1
        # Message should indicate recovery.
        sent_msg = str(calls[0]["message"])
        assert sent_msg.startswith("# OM · 系统通知 · OpenD")
        assert "状态｜✅ 已恢复" in sent_msg
        assert "时间｜2026-07-21 16:30:00 北京时间" in sent_msg
        assert "结果｜数据连接已恢复，后续批次将自动重新评估" in sent_msg
        assert_mobile_flat_markdown(sent_msg)

        # Counter reset: second recovery sends nothing.
        r = opend_guard.send_opend_recovery_notice(base, cfg)
        assert r is False
        assert len(calls) == 1


def test_send_opend_recovery_notice_disabled_by_config(tmp_path: Path) -> None:
    """Recovery notice is suppressed when opend_alert_send_recovery_notice is false."""
    from src.application.multi_tick import opend_guard
    import unittest.mock as mock

    base = Path(tmp_path)
    cfg = _cfg(opend_alert_send_recovery_notice=False)
    for _ in range(5):
        opend_guard.record_opend_failure(base)

    calls: list[object] = []
    with mock.patch.object(system_alerts, "select_notification_delivery_adapter", lambda *_: calls.append("select")):
        r = opend_guard.send_opend_recovery_notice(base, cfg)
    assert r is False
    assert calls == []
