"""Fixed Feishu identity lookup and normalization of authenticated SDK events."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Callable

from .config import PreflightSettings

BOT_INFO_URL = "https://open.feishu.cn/open-apis/bot/v3/info"


class EventRejected(ValueError):
    """A stable reason code, never the original payload or message text."""


@dataclass(frozen=True)
class VerifiedGroupEvent:
    app_id: str
    tenant_key: str
    sender_open_id: str
    sender_tenant_key: str | None
    chat_id: str
    thread_id: str | None
    message_id: str
    event_id: str | None

    def scope_sha256(self, instance_id: str) -> str:
        fields = [instance_id, self.app_id, self.tenant_key, self.chat_id,
                  self.sender_open_id, self.thread_id]
        return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value and value == value.strip() else None


def normalize_sdk_event(payload: object, expected_app_id: str,
                        expected_bot_open_id: str,
                        allowed_chat_ids: tuple[str, ...]) -> VerifiedGroupEvent:
    """Call only behind the app-authenticated long-connection SDK callback."""
    body = _mapping(payload)
    header = _mapping(body.get("header"))
    event = _mapping(body.get("event"))
    sender = _mapping(event.get("sender"))
    sender_id = _mapping(sender.get("sender_id"))
    message = _mapping(event.get("message"))
    if body.get("schema") != "2.0" or header.get("event_type") != "im.message.receive_v1":
        raise EventRejected("unsupported_event")
    if not expected_app_id or header.get("app_id") != expected_app_id:
        raise EventRejected("wrong_app")
    if message.get("chat_type") != "group" or (
            allowed_chat_ids != ("*",) and message.get("chat_id") not in allowed_chat_ids):
        raise EventRejected("unauthorized_chat")
    tenant = _text(header.get("tenant_key"))
    sender_open_id = _text(sender_id.get("open_id"))
    message_id = _text(message.get("message_id"))
    if not tenant or not sender_open_id or not message_id or sender.get("sender_type") != "user":
        raise EventRejected("missing_identity")
    if message.get("message_type") != "text":
        raise EventRejected("unsupported_message")
    thread = message.get("thread_id")
    if thread not in (None, "") and not _text(thread):
        raise EventRejected("invalid_thread")
    mentions = message.get("mentions")
    if not expected_bot_open_id or not isinstance(mentions, list) or not any(
        _mapping(_mapping(mention).get("id")).get("open_id") == expected_bot_open_id
        for mention in mentions
    ):
        raise EventRejected("not_mentioned")
    return VerifiedGroupEvent(expected_app_id, tenant, sender_open_id,
                              _text(sender.get("tenant_key")), message["chat_id"],
                              thread or None, message_id, _text(header.get("event_id")))


def resolve_bot_identity(settings: PreflightSettings, secret: str,
                         transport: Callable | None = None) -> str:
    """The token for the configured app binds this fixed lookup to that app."""
    if transport is None:
        from src.infrastructure.feishu_bitable import http_json, with_tenant_token_retry

        def transport(method, url, *, app_id, app_secret):
            return with_tenant_token_retry(
                app_id, app_secret,
                lambda token: http_json(method, url, headers={"Authorization": f"Bearer {token}"},
                                        timeout=20, retry_max_attempts=1, log_fn=lambda record: None),
                token_timeout=20, token_retry_max_attempts=1,
            )
    response = transport("GET", BOT_INFO_URL, app_id=settings.app_id, app_secret=secret)
    response = _mapping(response)
    bot = _mapping(response.get("bot"))
    open_id = _text(bot.get("open_id"))
    if type(response.get("code")) is not int or response["code"] != 0 or not open_id:
        raise ValueError("bot_identity_unavailable")
    return open_id


def message_text(payload: object) -> str:
    """Extract the plain user text after identity checks, dropping mention keys."""
    body = _mapping(payload)
    event = _mapping(body.get("event"))
    message = _mapping(event.get("message"))
    if message.get("message_type") != "text":
        raise EventRejected("unsupported_message")
    raw = message.get("content")
    if not isinstance(raw, str):
        raise EventRejected("unsupported_message")
    try:
        content = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise EventRejected("unsupported_message") from exc
    text = content.get("text") if isinstance(content, dict) else None
    if not isinstance(text, str):
        raise EventRejected("unsupported_message")
    for mention in message.get("mentions") or []:
        key = _mapping(mention).get("key")
        if isinstance(key, str) and key:
            text = text.replace(key, " ")
    cleaned = " ".join(text.split())
    if not cleaned:
        raise EventRejected("empty_message")
    if len(cleaned) > 4000:
        raise EventRejected("message_too_long")
    return cleaned


def redacted_sdk_sample(payload: object) -> dict:
    """Allowlist structural identity evidence; discard content, names and extra keys."""
    def alias(value):
        value = _text(value)
        return "anon_" + hashlib.sha256(value.encode()).hexdigest()[:24] if value else None

    raw = _mapping(payload)
    header = _mapping(raw.get("header"))
    event = _mapping(raw.get("event"))
    sender = _mapping(event.get("sender"))
    message = _mapping(event.get("message"))
    ids = ("open_id", "user_id", "union_id")
    mentions = message.get("mentions")
    return {
        "schema": "2.0" if raw.get("schema") == "2.0" else None,
        "header": {key: alias(header.get(key)) for key in ("app_id", "tenant_key", "event_id")},
        "sender": {"sender_id": {key: alias(_mapping(sender.get("sender_id")).get(key)) for key in ids},
                   "tenant_key": alias(sender.get("tenant_key"))},
        "message": {**{key: alias(message.get(key)) for key in ("message_id", "chat_id", "thread_id")},
                    "mentions": [{"key": alias(_mapping(item).get("key")),
                                  "id": {key: alias(_mapping(_mapping(item).get("id")).get(key)) for key in ids},
                                  "tenant_key": alias(_mapping(item).get("tenant_key"))}
                                 for item in (mentions[:16] if isinstance(mentions, list) else [])]},
    }
