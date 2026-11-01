"""Feishu SDK connectivity; loaded only for an explicitly requested connection."""
from __future__ import annotations

import logging
from typing import Any, Callable

FeishuWsEventCallback = Callable[[dict[str, Any]], None]
LOG = logging.getLogger(__name__)


def start_feishu_ws_client(
    *,
    app_id: str,
    app_secret: str,
    on_event: FeishuWsEventCallback,
    event_to_payload_fn: Callable[[Any], dict[str, Any]],
    log_level: str = "info",
) -> None:
    try:
        import lark_oapi as lark
        from lark_oapi.core.enum import LogLevel
    except Exception as exc:
        raise RuntimeError("lark-oapi is required for Feishu long-connection inbound") from exc

    def _on_message(event: Any) -> None:
        try:
            on_event(event_to_payload_fn(event))
        except Exception:
            LOG.exception("failed to enqueue Feishu WebSocket event")

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(_on_message)
        .build()
    )
    client = lark.ws.Client(
        app_id=app_id,
        app_secret=app_secret,
        event_handler=handler,
        log_level=getattr(LogLevel, str(log_level or "").strip().upper(), LogLevel.INFO),
    )
    client.start()
