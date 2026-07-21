from __future__ import annotations

from types import SimpleNamespace

from tests.notification_format_assertions import assert_mobile_flat_markdown


def test_build_per_account_delivery_batch_supports_one_account_message() -> None:
    from src.application.scheduled_notification import build_per_account_delivery_batch

    seen: dict[str, object] = {}

    class FakeDeliveryPlan:
        @classmethod
        def from_payload(cls, payload):
            seen["delivery_payload"] = payload
            return payload

    def _build_decision(**kwargs):
        seen["decision_kwargs"] = kwargs
        return {
            "should_send": True,
            "meaningful": True,
            "config_error": None,
            "effective_target": "user:test",
            "reason": "send",
        }

    decision, batch, target = build_per_account_delivery_batch(
        channel="wechat_clawbot",
        target="user:test",
        account_messages={"sy": "[sy]\nhello"},
        should_notify_window=True,
        decision_builder=_build_decision,
        delivery_plan_cls=FakeDeliveryPlan,
    )

    assert decision["should_send"] is True
    assert batch is not None
    assert batch.messages_by_account == {"sy": "[sy]\nhello"}
    assert batch.mode == "per_account"
    assert target == "user:test"
    assert seen["decision_kwargs"]["notification_text"] == "[sy]\nhello"


def test_build_per_account_delivery_batch_supports_skip_paths() -> None:
    from src.application.scheduled_notification import build_per_account_delivery_batch

    def _decision_builder(**kwargs):
        assert kwargs["notification_text"] == "hello\nworld"
        return {
            "should_send": False,
            "meaningful": True,
            "config_error": None,
            "effective_target": None,
            "reason": "no_send",
            "action": "skip",
        }

    decision, batch, target = build_per_account_delivery_batch(
        channel="wechat_clawbot",
        target="user:test",
        account_messages={"lx": "hello", "sy": "world"},
        no_send=True,
        decision_builder=_decision_builder,
    )

    assert decision["should_send"] is False
    assert batch is None
    assert target is None


def test_build_per_account_delivery_batch_builds_delivery_batch() -> None:
    from src.application.scheduled_notification import build_per_account_delivery_batch

    class FakeDeliveryPlan:
        @classmethod
        def from_payload(cls, payload):
            return payload

    decision, batch, target = build_per_account_delivery_batch(
        channel="wechat_clawbot",
        target="user:test",
        account_messages={"lx": "hello"},
        delivery_plan_cls=FakeDeliveryPlan,
        decision_builder=lambda **_kwargs: {
            "should_send": True,
            "meaningful": True,
            "config_error": None,
            "effective_target": "user:test",
            "reason": "send",
            "action": "send",
        },
    )

    assert decision["should_send"] is True
    assert target == "user:test"
    assert batch is not None
    assert batch.target == "user:test"
    assert batch.messages_by_account == {"lx": "hello"}


def test_build_notify_summary_records_run_level_delivery_counts() -> None:
    from src.application.cron_runtime import apply_notify_results_to_tick_metrics, build_notify_summary

    summary = build_notify_summary(
        sent_accounts=["lx"],
        notify_failures=[{"account": "sy", "error_code": "SEND_TIMEOUT", "ambiguous_send": True, "duplicate_risk": True}],
        total_accounts=2,
        send_attempted_count=2,
        send_confirmed_count=1,
        retry_attempt_count=1,
        ambiguous_send_count=1,
        duplicate_risk_count=1,
    )
    tick_metrics: dict[str, object] = {}

    apply_notify_results_to_tick_metrics(
        tick_metrics=tick_metrics,
        no_send=False,
        sent_accounts=["lx"],
        notify_failures=[{"account": "sy", "error_code": "SEND_TIMEOUT", "ambiguous_send": True, "duplicate_risk": True}],
        notify_summary=summary,
    )

    assert summary["account_messages_count"] == 2
    assert summary["send_attempted_count"] == 2
    assert summary["send_confirmed_count"] == 1
    assert summary["retry_attempt_count"] == 1
    assert summary["ambiguous_send_count"] == 1
    assert summary["duplicate_risk_count"] == 1
    assert tick_metrics["account_messages_count"] == 2
    assert tick_metrics["send_attempted_count"] == 2
    assert tick_metrics["send_confirmed_count"] == 1
    assert tick_metrics["retry_attempt_count"] == 1
    assert tick_metrics["ambiguous_send_count"] == 1
    assert tick_metrics["duplicate_risk_count"] == 1
    assert tick_metrics["reason"] == "sent_partial_notify_failure"


def test_shared_last_run_meta_marks_no_send_as_not_sent() -> None:
    from types import SimpleNamespace
    from src.application.cron_runtime import build_shared_last_run_meta

    meta = build_shared_last_run_meta(
        now_utc="2026-05-12T00:00:00Z",
        channel="wechat_clawbot",
        target="group://test",
        results=[SimpleNamespace(account="lx")],
        sent_accounts=["lx"],
        notify_failures=[],
        notify_summary={"success_count": 1},
        no_send=True,
    )

    assert meta["sent"] is False
    assert meta["no_send"] is True
    assert meta["sent_accounts"] == []
    assert meta["would_send_accounts"] == ["lx"]


def test_local_feishu_size_failure_is_audited_and_never_retried() -> None:
    from src.application.scheduled_notification import (
        build_notify_failure_summary_message,
        send_account_message_with_retry,
    )

    send_calls: list[dict[str, object]] = []
    sleep_calls: list[float] = []
    audit_events: list[dict[str, object]] = []
    runlog_events: list[dict[str, object]] = []
    diagnostics = {
        "local_error_code": "FEISHU_POST_TOO_LARGE",
        "error_code": "FEISHU_POST_TOO_LARGE",
        "request_body_bytes": 28673,
        "request_body_budget_bytes": 28672,
        "normalized_markdown_chars": 12000,
        "normalized_markdown_sha256": "a" * 64,
    }

    class FakeRunlog:
        def safe_event(self, step: str, status: str, **kwargs) -> None:  # type: ignore[no-untyped-def]
            runlog_events.append({"step": step, "status": status, **kwargs})

    def _send_fn(**kwargs):  # type: ignore[no-untyped-def]
        send_calls.append(dict(kwargs))
        return {
            "ok": False,
            "returncode": 1,
            "command_ok": False,
            "delivery_confirmed": False,
            "message_id": None,
            "http_attempts": [],
            "retry_attempt_count": 0,
            "ambiguous_send": False,
            "duplicate_risk": False,
            **diagnostics,
        }

    result = send_account_message_with_retry(
        base="/tmp/base",
        channel="feishu_app",
        target="",
        account="lx",
        message="# 简报",
        run_id="run-size-failure",
        runlog=FakeRunlog(),
        audit_fn=lambda kind, action, **kwargs: audit_events.append(
            {"kind": kind, "action": action, **kwargs}
        ),
        send_fn=_send_fn,
        normalize_fn=lambda **_kwargs: {},
        safe_data_fn=lambda payload: payload,
        failure_fields_builder=lambda **_kwargs: {},
        max_attempts=3,
        sleep_fn=lambda seconds: sleep_calls.append(seconds),
    )

    assert len(send_calls) == 1
    assert sleep_calls == []
    assert result["ok"] is False
    assert result["attempts"] == 1
    assert result["error_code"] == "FEISHU_POST_TOO_LARGE"
    assert result["local_error_code"] == "FEISHU_POST_TOO_LARGE"
    assert result["ambiguous_send"] is False
    assert result["duplicate_risk"] is False
    final = result["final"]
    assert isinstance(final, dict)
    assert final["will_retry"] is False
    assert final["http_attempts"] == []
    for key, value in diagnostics.items():
        assert final[key] == value
        if key != "error_code":
            assert result[key] == value

    send_fail = next(event for event in audit_events if event["action"] == "send_fail")
    assert send_fail["error_code"] == "FEISHU_POST_TOO_LARGE"
    assert isinstance(send_fail["extra"], dict)
    for key, value in diagnostics.items():
        assert send_fail["extra"][key] == value

    error_event = next(event for event in runlog_events if event["status"] == "error")
    assert error_event["error_code"] == "FEISHU_POST_TOO_LARGE"
    assert error_event["data"]["will_retry"] is False
    assert error_event["data"]["request_body_bytes"] == 28673

    summary = build_notify_failure_summary_message(
        run_id="run-size-failure",
        sent_accounts=[],
        notify_failures=[
            {
                "account": "lx",
                "error_code": result["error_code"],
                "attempts": result["attempts"],
                "delivery_confirmed": result["delivery_confirmed"],
            }
        ],
    )
    assert "lx｜FEISHU_POST_TOO_LARGE · 尝试 1 次 · 未确认" in summary
    assert_mobile_flat_markdown(summary)
