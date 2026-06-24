from __future__ import annotations

from src.application.conversation_scope import (
    conversation_scope_from_notification_route,
    normalize_conversation_scope,
    wechat_window_conversation_id,
)


def test_wechat_window_conversation_id_uses_chat_or_group_not_sender_suffix() -> None:
    assert (
        wechat_window_conversation_id(chat_key="group_1", group_id="ignored", sender_id="user_1")
        == "wechat:group_1"
    )
    assert wechat_window_conversation_id(group_id="group_2", sender_id="user_1") == "wechat:group_2"
    assert wechat_window_conversation_id(sender_id="user_1") == "wechat:user_1"


def test_normalize_conversation_scope_keeps_generic_fallback() -> None:
    assert normalize_conversation_scope(channel="Feishu", sender_id="ou_1", conversation_id=None) == {
        "channel": "feishu",
        "sender_id": "ou_1",
        "conversation_id": "feishu:ou_1",
    }


def test_notification_route_scope_uses_wechat_window_target_without_secret_urls() -> None:
    assert conversation_scope_from_notification_route(
        provider="wechat_clawbot",
        channel="wechat",
        target="wechat:ops:main",
    ) == {"channel": "wechat", "conversation_id": "wechat:main"}
    assert conversation_scope_from_notification_route(
        provider="feishu_app",
        channel="feishu_app",
        target="https://example.invalid/webhook/token",
    ) == {"channel": "feishu_app", "conversation_id": "feishu_app:webhook"}
