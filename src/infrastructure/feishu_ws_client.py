from __future__ import annotations

import logging
from typing import Any, Callable

from src.application.channels.plugins import channel_plugin_installed, load_channel_plugin


FeishuWsEventCallback = Callable[[dict[str, Any]], None]
LOG = logging.getLogger("om_feishu_channel.ws_client")


def is_feishu_ws_sdk_available() -> bool:
    if not channel_plugin_installed("feishu"):
        return False
    try:
        import lark_oapi  # noqa: F401
    except Exception:
        return False
    return True


def start_feishu_ws_client(
    *,
    app_id: str,
    app_secret: str,
    on_event: FeishuWsEventCallback,
    log_level: str = "info",
) -> None:
    return load_channel_plugin("feishu", module="ws_client").start_feishu_ws_client(
        app_id=app_id, app_secret=app_secret, on_event=on_event, log_level=log_level,
        event_to_payload_fn=feishu_event_model_to_payload,
    )


def feishu_event_model_to_payload(event: Any) -> dict[str, Any]:
    header = _getattr(event, "header")
    event_body = _getattr(event, "event")
    sender = _getattr(event_body, "sender")
    sender_id = _getattr(sender, "sender_id")
    message = _getattr(event_body, "message")

    return {
        "schema": _text(_getattr(event, "schema")) or "2.0",
        "header": {
            "event_id": _text(_getattr(header, "event_id")),
            "event_type": _text(_getattr(header, "event_type")) or "im.message.receive_v1",
            "create_time": _text(_getattr(header, "create_time")),
            "tenant_key": _text(_getattr(header, "tenant_key")),
            "app_id": _text(_getattr(header, "app_id")),
        },
        "event": {
            "sender": {
                "sender_id": {
                    "open_id": _text(_getattr(sender_id, "open_id")),
                    "user_id": _text(_getattr(sender_id, "user_id")),
                    "union_id": _text(_getattr(sender_id, "union_id")),
                },
                "sender_type": _text(_getattr(sender, "sender_type")),
                "tenant_key": _text(_getattr(sender, "tenant_key")),
            },
            "message": {
                "message_id": _text(_getattr(message, "message_id")),
                "root_id": _text(_getattr(message, "root_id")),
                "parent_id": _text(_getattr(message, "parent_id")),
                "chat_id": _text(_getattr(message, "chat_id")),
                "thread_id": _text(_getattr(message, "thread_id")),
                "chat_type": _text(_getattr(message, "chat_type")),
                "message_type": _text(_getattr(message, "message_type")),
                "content": _text(_getattr(message, "content")),
            },
        },
    }


def _sdk_log_level(value: str, log_level_type: Any) -> Any:
    raw = str(value or "").strip().upper()
    return getattr(log_level_type, raw, getattr(log_level_type, "INFO"))


def _getattr(value: Any, name: str) -> Any:
    return getattr(value, name, None) if value is not None else None


def _text(value: Any) -> str:
    return str(value or "").strip()

