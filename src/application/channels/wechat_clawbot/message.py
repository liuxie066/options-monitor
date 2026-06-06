from __future__ import annotations

from typing import Any


def extract_first_string(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for item in _walk_dicts(payload):
        for key in keys:
            value = item.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
    return ""


def extract_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for item in _walk_dicts(payload):
        for key in ("message_list", "messages", "updates", "msgs", "item_list", "items"):
            raw = item.get(key)
            if isinstance(raw, list):
                for entry in raw:
                    if isinstance(entry, dict):
                        if key == "item_list" and not (message_context_token(entry) and message_user_id(entry)):
                            continue
                        messages.append(entry)
    if not messages:
        for item in _walk_dicts(payload):
            if message_context_token(item) and message_user_id(item):
                messages.append(item)
    return messages


def message_context_token(message: dict[str, Any]) -> str:
    return extract_first_string(message, ("context_token", "contextToken", "reply_context_token"))


def message_user_id(message: dict[str, Any]) -> str:
    for key in ("from_user_id", "fromUserId", "user_id", "userId", "sender_id", "senderId"):
        value = message.get(key)
        text = str(value or "").strip()
        if text:
            return text
    return ""


def message_group_id(message: dict[str, Any]) -> str:
    return extract_first_string(message, ("group_id", "groupId", "room_id", "roomId"))


def message_chat_key(message: dict[str, Any]) -> str:
    return (
        extract_first_string(message, ("chat_key", "chatKey", "conversation_id", "conversationId"))
        or message_group_id(message)
        or message_user_id(message)
    )


def message_id(message: dict[str, Any]) -> str:
    return extract_first_string(message, ("message_id", "messageId", "msg_id", "msgId", "id"))


def message_text(message: dict[str, Any]) -> str:
    for item in _walk_dicts(message):
        for key in ("text", "content"):
            value = item.get(key)
            text = str(value or "").strip()
            if text:
                return text
    return ""


def _walk_dicts(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(value, dict):
        out.append(value)
        for child in value.values():
            out.extend(_walk_dicts(child))
    elif isinstance(value, list):
        for child in value:
            out.extend(_walk_dicts(child))
    return out
