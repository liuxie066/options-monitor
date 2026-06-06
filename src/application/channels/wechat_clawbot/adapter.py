from __future__ import annotations

import importlib
from typing import Any

from domain.domain.multi_tick import WECHAT_CLAWBOT_NOTIFICATION_PROVIDER
from src.application.channels.contracts import ChannelCapabilities
from src.application.channels.service import ChannelAdapter, ChannelRegistry, ChannelService, NormalizeFn, SendFn


def _handle_wechat_clawbot_message(payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    handler = importlib.import_module("src.application.channels.wechat_clawbot.inbound").handle_wechat_clawbot_message
    return handler(payload, **kwargs)


def build_wechat_clawbot_channel_adapter(
    *,
    send_fn: SendFn | None = None,
    normalize_fn: NormalizeFn | None = None,
    failure_stage: str = "send_wechat_clawbot_message",
) -> ChannelAdapter:
    return ChannelAdapter(
        channel=WECHAT_CLAWBOT_NOTIFICATION_PROVIDER,
        capabilities=ChannelCapabilities(
            outbound=send_fn is not None,
            inbound=True,
            reply=True,
            bind=True,
            health=True,
        ),
        send_fn=send_fn,
        normalize_fn=normalize_fn,
        inbound_fn=_handle_wechat_clawbot_message,
        failure_stage=failure_stage,
    )


def build_wechat_clawbot_inbound_channel_service() -> ChannelService:
    return ChannelService(ChannelRegistry((build_wechat_clawbot_channel_adapter(),)))


__all__ = [
    "build_wechat_clawbot_channel_adapter",
    "build_wechat_clawbot_inbound_channel_service",
]
