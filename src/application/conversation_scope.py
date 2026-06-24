from __future__ import annotations

from typing import Any


def normalize_conversation_scope(
    *,
    channel: str | None,
    sender_id: str | None,
    conversation_id: str | None = None,
) -> dict[str, str]:
    normalized_channel = str(channel or "local").strip().lower() or "local"
    normalized_sender = str(sender_id or "").strip()
    normalized_conversation = str(conversation_id or "").strip() or f"{normalized_channel}:{normalized_sender}"
    return {
        "channel": normalized_channel,
        "sender_id": normalized_sender,
        "conversation_id": normalized_conversation,
    }


def wechat_window_conversation_id(
    *,
    chat_key: Any = None,
    group_id: Any = None,
    bound_target: Any = None,
    sender_id: Any = None,
) -> str | None:
    key = _first_text(chat_key, group_id, bound_target, sender_id)
    if not key:
        return None
    return f"wechat:{key}"


def conversation_scope_from_notification_route(
    *,
    provider: Any = None,
    channel: Any = None,
    target: Any = None,
) -> dict[str, str | None]:
    provider_text = str(provider or "").strip()
    channel_text = str(channel or "").strip().lower()
    target_text = str(target or "").strip()
    if provider_text == "wechat_clawbot" or channel_text == "wechat":
        conversation_id = wechat_window_conversation_id(bound_target=_route_target_key(target_text))
        return {"channel": "wechat", "conversation_id": conversation_id}
    if channel_text:
        conversation_id = f"{channel_text}:{_route_target_key(target_text)}" if target_text else None
        return {"channel": channel_text, "conversation_id": conversation_id}
    return {"channel": None, "conversation_id": None}


def _route_target_key(target: str) -> str:
    text = str(target or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return "webhook"
    if ":" in text:
        parts = [part for part in text.split(":") if part]
        return parts[-1] if parts else text
    return text


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


__all__ = [
    "conversation_scope_from_notification_route",
    "normalize_conversation_scope",
    "wechat_window_conversation_id",
]
