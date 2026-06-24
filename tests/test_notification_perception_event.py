from __future__ import annotations

import json

from src.application.multi_tick.assistant_perception_event import build_notification_perception_event


def test_notification_perception_event_is_compressed_and_safe() -> None:
    event = build_notification_perception_event(
        event_kind="notification_delivery_decided",
        run_id="run_1",
        results_count=2,
        notify_candidates=[{"symbol": "FUTU"}, {"symbol": "NVDA"}],
        account_messages={"lx": "secret notification body"},
        threshold_met=True,
        used_heartbeat=False,
        provider="wechat_clawbot",
        channel="wechat",
        target="https://example.invalid/webhook/token",
        no_send=True,
        delivery_decision={"action": "skip_no_send", "reason": "no_send", "should_send": False},
        conversation_scope={"channel": "wechat", "conversation_id": "wechat:group_1"},
    )

    serialized = json.dumps(event, ensure_ascii=False, sort_keys=True)
    assert event["schema_version"] == "om-notification-perception-event-v1"
    assert event["event_type"] == "assistant_perception"
    assert event["conversation_scope"] == {"channel": "wechat", "conversation_id": "wechat:group_1"}
    assert event["safe_slots"]["run_id"] == ["run_1"]
    assert event["safe_slots"]["symbol"] == ["FUTU", "NVDA"]
    assert event["message_len_by_account"] == {"lx": len("secret notification body")}
    assert event["message_sha256_by_account"]["lx"]
    assert "secret notification body" not in serialized
    assert "webhook/token" not in serialized
    assert event["target_masked"] == "[redacted_target]"


def test_notification_perception_event_summarizes_no_account_branch() -> None:
    event = build_notification_perception_event(
        event_kind="no_account_notification",
        run_id="run_2",
        results_count=0,
        notify_candidates=[],
        account_messages={},
        threshold_met=False,
        no_send=False,
    )

    assert event["event_kind"] == "no_account_notification"
    assert event["threshold_met"] is False
    assert event["message_count"] == 0
    assert event["notify_candidate_count"] == 0
    assert "threshold_met=False" in event["summary"]
