from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable

from domain.domain.multi_tick import (
    SUPPORTED_NOTIFICATION_PROVIDERS,
    normalize_notification_provider,
)
from src.application.channels.feishu import build_feishu_channel_adapter
from src.application.channels.feishu_notification import (
    normalize_feishu_app_send_output,
    resolve_feishu_bot_send_target,
    send_feishu_app_message,
    send_feishu_app_message_process,
)
from src.application.channels.service import ChannelRegistry
from src.application.channels.wechat_clawbot.adapter import build_wechat_clawbot_channel_adapter
from src.application.channels.wechat_clawbot.notification import (
    normalize_wechat_clawbot_send_output,
    send_wechat_clawbot_message_process,
)


@dataclass(frozen=True)
class NotificationDeliveryAdapter:
    send_fn: Callable[..., Any]
    normalize_fn: Callable[..., dict[str, Any]]
    failure_stage: str


def build_notification_idempotency_key(
    *,
    run_id: str,
    account: str,
    renderer: str,
) -> str:
    raw = "\n".join(
        [
            str(run_id or "").strip(),
            str(account or "").strip().lower(),
            str(renderer or "").strip(),
        ]
    )
    return "om-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def build_notification_transport_key(logical_key: str) -> str:
    """Map a stable logical delivery key to a provider-safe compact key."""

    value = str(logical_key or "").strip()
    if not value:
        raise ValueError("logical notification idempotency key is required")
    return "om-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def notification_target_reference(target: Any) -> str | None:
    """Return a stable audit-safe reference without retaining the recipient identifier."""

    value = str(target or "").strip()
    if not value:
        return None
    return "target:sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def normalize_notification_delivery_result(
    send_result: Any,
    *,
    normalize_fn: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    if isinstance(send_result, dict) and ("delivery_confirmed" in send_result or "command_ok" in send_result):
        return dict(send_result)
    try:
        return normalize_fn(send_result=getattr(send_result, "raw", send_result))
    except TypeError:
        return normalize_fn(
            returncode=int(getattr(send_result, "returncode", 0) or 0),
            stdout=str(getattr(send_result, "stdout", "") or ""),
            stderr=str(getattr(send_result, "stderr", "") or ""),
        )


def build_notification_channel_registry() -> ChannelRegistry:
    return ChannelRegistry(
        (
            build_feishu_channel_adapter(),
            build_wechat_clawbot_channel_adapter(),
        )
    )


def select_notification_delivery_adapter(provider: Any) -> NotificationDeliveryAdapter:
    resolved_provider = normalize_notification_provider(provider)
    try:
        channel_adapter = build_notification_channel_registry().require(resolved_provider)
    except ValueError as exc:
        allowed = ", ".join(SUPPORTED_NOTIFICATION_PROVIDERS)
        raise ValueError(f"unsupported notification provider: {provider}; expected one of: {allowed}") from exc
    if channel_adapter.send_fn is None or channel_adapter.normalize_fn is None:
        raise ValueError(f"notification channel is missing outbound send support: {resolved_provider}")
    return NotificationDeliveryAdapter(
        send_fn=channel_adapter.send_fn,
        normalize_fn=channel_adapter.normalize_fn,
        failure_stage=channel_adapter.failure_stage,
    )
