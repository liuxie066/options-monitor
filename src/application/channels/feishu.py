from __future__ import annotations

import importlib
from typing import Any

from domain.domain.multi_tick import FEISHU_APP_NOTIFICATION_PROVIDER
from src.application.channels import feishu_notification
from src.application.channels.contracts import ChannelCapabilities
from src.application.channels.service import ChannelAdapter, ChannelRegistry, ChannelService, NormalizeFn, SendFn


def _handle_feishu_payload(payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    handler = importlib.import_module("src.application.inbound.feishu").handle_feishu_payload
    return handler(payload, **kwargs)


def build_feishu_channel_adapter(
    *,
    send_fn: SendFn | None = None,
    normalize_fn: NormalizeFn | None = None,
    failure_stage: str = "send_feishu_app_message",
) -> ChannelAdapter:
    send_fn = feishu_notification.send_feishu_app_message_process if send_fn is None else send_fn
    normalize_fn = feishu_notification.normalize_feishu_app_send_output if normalize_fn is None else normalize_fn
    return ChannelAdapter(
        channel=FEISHU_APP_NOTIFICATION_PROVIDER,
        capabilities=ChannelCapabilities(
            outbound=send_fn is not None,
            inbound=True,
            reply=True,
            health=True,
        ),
        send_fn=send_fn,
        normalize_fn=normalize_fn,
        inbound_fn=_handle_feishu_payload,
        failure_stage=failure_stage,
        resolve_target_fn=feishu_notification.resolve_feishu_notification_target,
        resolve_identity_fn=feishu_notification.resolve_feishu_notification_identity,
    )


def build_feishu_inbound_channel_service() -> ChannelService:
    return ChannelService(ChannelRegistry((build_feishu_channel_adapter(),)))


__all__ = [
    "build_feishu_channel_adapter",
    "build_feishu_inbound_channel_service",
]
