from __future__ import annotations

import pytest

from src.application.conversation_scope import (
    conversation_scope_from_notification_route,
    derive_session_id,
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


def test_host_session_identity_preserves_persisted_keys_and_scope_isolation():
    parts = ("feishu", "ou_1", "chat_1", "key:us")
    expected = "om_dfad1e762df4152e8a3aa39131a58d9648a6572acbae7c823004b82322bfe332"
    assert derive_session_id(*parts) == expected
    for index, replacement in enumerate(("wechat", "ou_2", "chat_2", "key:hk")):
        changed = list(parts)
        changed[index] = replacement
        assert derive_session_id(*changed) != expected


@pytest.mark.parametrize("invalid", ["", " ", "bad\0identity", None])
@pytest.mark.parametrize("index", range(4))
def test_host_session_identity_rejects_missing_or_ambiguous_parts(index, invalid):
    parts = ["feishu", "ou_1", "chat_1", "key:us"]
    parts[index] = invalid
    with pytest.raises(ValueError, match="session identity"):
        derive_session_id(*parts)
