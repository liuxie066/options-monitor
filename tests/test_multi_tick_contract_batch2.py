from __future__ import annotations

import importlib
from types import SimpleNamespace
from pathlib import Path


class _FakeRunLogger:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def safe_event(self, step: str, status: str, **kwargs) -> None:
        rec = {"step": step, "status": status}
        rec.update(kwargs)
        self.events.append(rec)


def test_multi_tick_scheduled_renderer_authority_is_daily_brief_only() -> None:
    base = Path(__file__).resolve().parents[1]
    notification_flow_src = (base / "src" / "application" / "tick_notification_flow.py").read_text(encoding="utf-8")
    helper_src = (base / "src" / "application" / "scheduled_notification.py").read_text(encoding="utf-8")
    assert "_prepare_daily_brief_notification(request)" in notification_flow_src
    assert "prepare_multi_account_notification(" not in notification_flow_src
    assert "prepare_per_account_messages(" not in helper_src
    assert "snapshot_account_messages(" not in helper_src
    assert "build_account_message_compact" not in notification_flow_src
    assert "build_account_message" not in notification_flow_src


def test_multi_tick_scheduler_and_account_decision_use_objectized_contract_path() -> None:
    base = Path(__file__).resolve().parents[1]
    src = (base / "src" / "application" / "multi_tick_scheduler.py").read_text(encoding="utf-8")
    helper_src = (base / "src" / "application" / "scheduled_notification.py").read_text(encoding="utf-8")
    assert "build_multi_tick_scheduler_decision" in src
    assert "build_multi_tick_account_scheduler_view" in src
    assert "def _snapshot_payload_dict(" in helper_src
    assert '"scheduler_raw"' in helper_src
    assert "engine_entrypoint: Callable[..., dict[str, Any]] = resolve_multi_tick_engine_entrypoint" in helper_src
    assert "account scheduler decision view must be valid" in helper_src
    assert 'stage="account_scheduler_decision"' in src


def test_multi_tick_trading_day_guard_decision_delegates_to_engine() -> None:
    base = Path(__file__).resolve().parents[1]
    src = (base / "src" / "application" / "multi_tick_scheduler.py").read_text(encoding="utf-8")
    watchdog_src = (base / "src" / "application" / "multi_tick_watchdog.py").read_text(encoding="utf-8")
    notification_flow_src = (base / "src" / "application" / "tick_notification_flow.py").read_text(encoding="utf-8")
    helper_src = (base / "src" / "application" / "scheduled_notification.py").read_text(encoding="utf-8")
    assert "decide_trading_day_guard(" in src
    assert "opend_unhealthy={" in watchdog_src
    assert "build_per_account_delivery_batch(" in notification_flow_src
    assert "decision_builder: Callable[..., dict[str, Any]] = decide_notification_delivery" in helper_src


def test_multi_tick_io_and_decision_failure_audit_fields_are_distinguishable() -> None:
    base = Path(__file__).resolve().parents[1]
    scheduler_src = (base / "src" / "application" / "multi_tick_scheduler.py").read_text(encoding="utf-8")
    audit_src = (base / "src" / "application" / "multi_tick_audit.py").read_text(encoding="utf-8")
    notification_flow_src = (base / "src" / "application" / "tick_notification_flow.py").read_text(encoding="utf-8")
    delivery_adapter_src = (base / "src" / "application" / "notification_delivery_adapter.py").read_text(encoding="utf-8")
    helper_src = (base / "src" / "application" / "scheduled_notification.py").read_text(encoding="utf-8")
    account_run_src = (base / "src" / "application" / "account_run.py").read_text(encoding="utf-8")
    assert "normalize_subprocess_adapter_payload(" in scheduler_src
    assert "normalize_pipeline_subprocess_output(" in account_run_src
    assert "normalize_wechat_clawbot_send_output" in delivery_adapter_src
    assert "select_notification_delivery_adapter" in notification_flow_src
    assert 'failure_kind="io_error"' in helper_src
    assert 'failure_kind="decision_error"' in audit_src


def test_multi_tick_pipeline_calls_share_context_dir() -> None:
    base = Path(__file__).resolve().parents[1]
    helper_src = (base / "src" / "application" / "account_run.py").read_text(encoding="utf-8")
    assert "shared_context_dir=run_repo.get_run_state_dir(request.base, request.run_id)" in helper_src


def test_multi_tick_notify_failure_is_account_isolated() -> None:
    base = Path(__file__).resolve().parents[1]
    notification_flow_src = (base / "src" / "application" / "tick_notification_flow.py").read_text(encoding="utf-8")
    finalization_src = (base / "src" / "application" / "multi_tick_finalization.py").read_text(encoding="utf-8")
    helper_src = (base / "src" / "application" / "scheduled_notification.py").read_text(encoding="utf-8")
    cron_runtime_src = (base / "src" / "application" / "cron_runtime.py").read_text(encoding="utf-8")
    assert "notify_failures: list[dict[str, object]] = []" in notification_flow_src
    assert "NOTIFY_SEND_MAX_ATTEMPTS = 2" in helper_src
    assert "NOTIFY_SEND_RETRY_DELAYS_SEC: tuple[float, ...] = (1.0,)" in helper_src
    assert "notify_failures.append(" in helper_src
    assert '"final_returncode": int(send_result.get("final_returncode") or 0)' in helper_src
    assert "sent_accounts.append(acct)" in helper_src
    assert "mark_accounts_notified(" not in notification_flow_src
    assert "_confirm_daily_brief_execution(" in notification_flow_src
    assert "confirm_daily_decision_brief_delivery_v2(" in notification_flow_src
    assert "NOTIFY_PARTIAL_FAILED" in finalization_src
    assert "build_run_end_payload(" in finalization_src
    assert '"notify_summary": notify_summary' in cron_runtime_src
    assert '"send_confirmed_count"' in cron_runtime_src
    assert '"delivery_decision"' in notification_flow_src and '"account_messages_count"' in notification_flow_src


def test_multi_tick_notify_unconfirmed_is_not_retried() -> None:
    helper = importlib.import_module("src.application.scheduled_notification")

    send_calls: list[dict] = []
    audit_events: list[dict] = []
    sleeps: list[float] = []
    runlog = _FakeRunLogger()

    def _send(**kwargs):
        send_calls.append(dict(kwargs))
        return SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")

    def _audit(event_type, action, **kwargs):
        audit_events.append({"event_type": event_type, "action": action, **kwargs})

    result = helper.send_account_message_with_retry(
        base=Path("/tmp/options-monitor-test"),
        channel="wechat_clawbot",
        target="user:test",
        account="lx",
        message="hello",
        run_id="run-1",
        runlog=runlog,
        audit_fn=_audit,
        send_fn=_send,
        normalize_fn=lambda **kwargs: importlib.import_module("domain.domain").normalize_notify_subprocess_output(**kwargs),
        safe_data_fn=lambda payload: payload,
        failure_fields_builder=lambda **kwargs: kwargs,
        sleep_fn=lambda seconds: sleeps.append(seconds),
    )

    assert result["ok"] is False
    assert result["error_code"] == "SEND_UNCONFIRMED"
    assert result["attempts"] == 1
    assert len(send_calls) == 1
    assert sleeps == []
    assert [e["action"] for e in audit_events] == ["send_start", "send_fail"]
    assert [e["status"] for e in audit_events] == ["start", "unconfirmed"]
    assert audit_events[-1]["extra"]["delivery_confirmed"] is False
    assert [e["status"] for e in runlog.events] == ["error"]


def test_multi_tick_notify_does_not_confirm_when_message_id_exists_without_delivery_confirmation() -> None:
    helper = importlib.import_module("src.application.scheduled_notification")

    send_calls: list[dict] = []
    audit_events: list[dict] = []
    sleeps: list[float] = []
    runlog = _FakeRunLogger()

    def _send(**kwargs):
        send_calls.append(dict(kwargs))
        return SimpleNamespace(returncode=0, stdout='{"messageId":"lx-1"}', stderr="")

    def _normalize(**_kwargs):
        return {
            "ok": False,
            "command_ok": True,
            "delivery_confirmed": False,
            "message_id": "lx-1",
            "stdout_tail": '{"messageId":"lx-1"}',
            "stderr_tail": "",
            "adapter": "notify",
        }

    def _audit(event_type, action, **kwargs):
        audit_events.append({"event_type": event_type, "action": action, **kwargs})

    result = helper.send_account_message_with_retry(
        base=Path("/tmp/options-monitor-test"),
        channel="wechat_clawbot",
        target="user:test",
        account="lx",
        message="hello",
        run_id="run-1",
        runlog=runlog,
        audit_fn=_audit,
        send_fn=_send,
        normalize_fn=_normalize,
        safe_data_fn=lambda payload: payload,
        failure_fields_builder=lambda **kwargs: kwargs,
        sleep_fn=lambda seconds: sleeps.append(seconds),
    )

    assert result["ok"] is False
    assert result["error_code"] == "SEND_UNCONFIRMED"
    assert result["attempts"] == 1
    assert len(send_calls) == 1
    assert sleeps == []
    assert [e["action"] for e in audit_events] == ["send_start", "send_fail"]
    assert audit_events[-1]["status"] == "unconfirmed"
    assert audit_events[-1]["extra"]["delivery_confirmed"] is False
    assert audit_events[-1]["extra"]["message_id"] == "lx-1"
    assert send_calls[0]["idempotency_key"].startswith("om-")


def test_multi_tick_notify_unconfirmed_is_not_retried_even_when_explicitly_requested() -> None:
    helper = importlib.import_module("src.application.scheduled_notification")

    audit_events: list[dict] = []
    sleeps: list[float] = []
    runlog = _FakeRunLogger()

    def _send(**_kwargs):
        return SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")

    def _audit(event_type, action, **kwargs):
        audit_events.append({"event_type": event_type, "action": action, **kwargs})

    result = helper.send_account_message_with_retry(
        base=Path("/tmp/options-monitor-test"),
        channel="wechat_clawbot",
        target="user:test",
        account="lx",
        message="hello",
        run_id="run-1",
        runlog=runlog,
        audit_fn=_audit,
        send_fn=_send,
        normalize_fn=lambda **kwargs: importlib.import_module("domain.domain").normalize_notify_subprocess_output(**kwargs),
        safe_data_fn=lambda payload: payload,
        failure_fields_builder=lambda **kwargs: kwargs,
        sleep_fn=lambda seconds: sleeps.append(seconds),
        max_attempts=3,
        retry_delays_sec=(1.0, 3.0),
    )

    assert result["ok"] is False
    assert result["error_code"] == "SEND_UNCONFIRMED"
    assert result["attempts"] == 1
    assert result["final_returncode"] == 0
    assert result["command_ok"] is True
    assert result["delivery_confirmed"] is False
    assert sleeps == []
    assert [e["action"] for e in audit_events] == ["send_start", "send_fail"]
    assert [e["status"] for e in audit_events] == ["start", "unconfirmed"]
    assert audit_events[-1]["extra"]["attempt"] == 1


def test_multi_tick_notify_failed_send_retries_once_by_default() -> None:
    helper = importlib.import_module("src.application.scheduled_notification")

    audit_events: list[dict] = []
    sleeps: list[float] = []
    send_calls: list[dict] = []
    runlog = _FakeRunLogger()

    def _send(**kwargs):
        send_calls.append(dict(kwargs))
        return SimpleNamespace(returncode=2, stdout="", stderr="boom")

    def _audit(event_type, action, **kwargs):
        audit_events.append({"event_type": event_type, "action": action, **kwargs})

    result = helper.send_account_message_with_retry(
        base=Path("/tmp/options-monitor-test"),
        channel="wechat_clawbot",
        target="user:test",
        account="sy",
        message="hello",
        run_id="run-1",
        runlog=runlog,
        audit_fn=_audit,
        send_fn=_send,
        normalize_fn=lambda **kwargs: importlib.import_module("domain.domain").normalize_notify_subprocess_output(**kwargs),
        safe_data_fn=lambda payload: payload,
        failure_fields_builder=lambda **kwargs: kwargs,
        sleep_fn=lambda seconds: sleeps.append(seconds),
    )

    assert result["ok"] is False
    assert result["error_code"] == "SEND_FAILED"
    assert result["attempts"] == 2
    assert result["final_returncode"] == 2
    assert result["command_ok"] is False
    assert result["delivery_confirmed"] is False
    assert len(send_calls) == 2
    assert sleeps == [1.0]
    assert [e["action"] for e in audit_events] == ["send_start", "send_fail", "send_start", "send_fail"]
    assert [e["status"] for e in audit_events] == ["start", "error", "start", "error"]


def test_multi_tick_notify_records_feishu_inner_retry_without_outer_retry() -> None:
    helper = importlib.import_module("src.application.scheduled_notification")

    audit_events: list[dict] = []
    send_calls: list[dict] = []
    runlog = _FakeRunLogger()

    def _send(**kwargs):
        send_calls.append(dict(kwargs))
        return SimpleNamespace(returncode=0, stdout='{"message_id":"lx-1"}', stderr="")

    def _normalize(**_kwargs):
        return {
            "ok": True,
            "command_ok": True,
            "delivery_confirmed": True,
            "message_id": "lx-1",
            "idempotency_key": send_calls[-1]["idempotency_key"],
            "http_attempts": [
                {"level": "warn", "category": "transient", "http_status": 500, "feishu_code": 2200, "attempt": 1},
                {"level": "info", "category": "success", "http_status": 200, "feishu_code": 0, "attempt": 2, "message_id": "lx-1"},
            ],
            "retry_attempt_count": 1,
            "ambiguous_send": True,
            "duplicate_risk": False,
            "stdout_tail": '{"message_id":"lx-1"}',
            "stderr_tail": "",
            "adapter": "notify",
        }

    def _audit(event_type, action, **kwargs):
        audit_events.append({"event_type": event_type, "action": action, **kwargs})

    result = helper.send_account_message_with_retry(
        base=Path("/tmp/options-monitor-test"),
        channel="feishu_app",
        target="ou_1",
        account="lx",
        message="hello",
        run_id="run-1",
        runlog=runlog,
        audit_fn=_audit,
        send_fn=_send,
        normalize_fn=_normalize,
        safe_data_fn=lambda payload: payload,
        failure_fields_builder=lambda **kwargs: kwargs,
        sleep_fn=lambda seconds: None,
    )

    assert result["ok"] is True
    assert result["attempts"] == 1
    assert result["retry_attempt_count"] == 1
    assert result["ambiguous_send"] is True
    assert result["duplicate_risk"] is False
    assert len(send_calls) == 1
    assert send_calls[0]["idempotency_key"].startswith("om-")
    assert audit_events[0]["extra"]["idempotency_key"] == send_calls[0]["idempotency_key"]
    assert audit_events[-1]["extra"]["retry_attempt_count"] == 1
    assert audit_events[-1]["extra"]["ambiguous_send"] is True


def test_multi_tick_notify_aggregates_provider_and_outer_retries() -> None:
    helper = importlib.import_module(
        "src.application.scheduled_notification"
    )
    send_calls: list[dict] = []
    normalize_calls = 0

    def _send(**kwargs):
        send_calls.append(dict(kwargs))
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    def _normalize(**_kwargs):
        nonlocal normalize_calls
        normalize_calls += 1
        if normalize_calls == 1:
            return {
                "ok": False,
                "command_ok": False,
                "delivery_confirmed": False,
                "error_code": "SEND_FAILED",
                "retry_attempt_count": 2,
                "fallback_used": False,
            }
        return {
            "ok": True,
            "command_ok": True,
            "delivery_confirmed": True,
            "message_id": "lx-confirmed",
            "retry_attempt_count": 0,
            "fallback_used": False,
        }

    result = helper.send_account_message_with_retry(
        base=Path("/tmp/options-monitor-test"),
        channel="feishu_app",
        target="ou_1",
        account="lx",
        message="hello",
        run_id="run-retry-aggregate",
        runlog=_FakeRunLogger(),
        audit_fn=lambda *_args, **_kwargs: None,
        send_fn=_send,
        normalize_fn=_normalize,
        safe_data_fn=lambda payload: payload,
        failure_fields_builder=lambda **kwargs: kwargs,
        sleep_fn=lambda _seconds: None,
    )

    assert result["ok"] is True
    assert result["attempts"] == 2
    assert result["provider_retry_attempt_count"] == 2
    assert result["outer_retry_attempt_count"] == 1
    assert result["fallback_attempt_count"] == 0
    assert result["retry_attempt_count"] == 3


def test_multi_tick_notify_without_override_preserves_legacy_transport_key() -> None:
    helper = importlib.import_module("src.application.scheduled_notification")
    adapter = importlib.import_module("src.application.notification_delivery_adapter")
    seen: list[str] = []

    def _send(**kwargs):
        seen.append(str(kwargs["idempotency_key"]))
        return SimpleNamespace(returncode=0, stdout='{"message_id":"m-1"}', stderr="")

    result = helper.send_account_message_with_retry(
        base=Path("/tmp/options-monitor-test"),
        channel="wechat_clawbot",
        target="user:test",
        account="lx",
        message="hello",
        run_id="run-legacy",
        runlog=_FakeRunLogger(),
        audit_fn=lambda *_args, **_kwargs: None,
        send_fn=_send,
        normalize_fn=lambda **kwargs: importlib.import_module("domain.domain").normalize_notify_subprocess_output(**kwargs),
        safe_data_fn=lambda payload: payload,
        failure_fields_builder=lambda **kwargs: kwargs,
    )

    expected = adapter.build_notification_idempotency_key(
        run_id="run-legacy",
        account="lx",
        target="user:test",
        message="hello",
    )
    assert result["ok"] is True
    assert seen == [expected]
    assert result["idempotency_key"] == expected


def test_multi_tick_notify_compacts_logical_override_and_reuses_it_for_retries() -> None:
    helper = importlib.import_module("src.application.scheduled_notification")
    adapter = importlib.import_module("src.application.notification_delivery_adapter")
    logical_key = "daily-brief:US:2026-07-19:lx:full:" + "a" * 64
    seen: list[str] = []

    def _send(**kwargs):
        seen.append(str(kwargs["idempotency_key"]))
        if len(seen) == 1:
            return SimpleNamespace(returncode=2, stdout="", stderr="retry")
        return SimpleNamespace(returncode=0, stdout='{"message_id":"m-2"}', stderr="")

    result = helper.send_account_message_with_retry(
        base=Path("/tmp/options-monitor-test"),
        channel="wechat_clawbot",
        target="user:test",
        account="lx",
        message="daily brief",
        run_id="run-brief",
        runlog=_FakeRunLogger(),
        audit_fn=lambda *_args, **_kwargs: None,
        send_fn=_send,
        normalize_fn=lambda **kwargs: importlib.import_module("domain.domain").normalize_notify_subprocess_output(**kwargs),
        safe_data_fn=lambda payload: payload,
        failure_fields_builder=lambda **kwargs: kwargs,
        idempotency_key_override=logical_key,
        sleep_fn=lambda _seconds: None,
    )

    expected = adapter.build_notification_transport_key(logical_key)
    assert result["ok"] is True
    assert seen == [expected, expected]
    assert result["idempotency_key"] == expected
    assert expected.startswith("om-")
    assert len(expected) == 35
