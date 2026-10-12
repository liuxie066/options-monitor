from __future__ import annotations

from typing import Any, Callable
from pathlib import Path

from domain.domain.multi_tick import FEISHU_APP_NOTIFICATION_PROVIDER, resolve_notification_route_from_config, notification_route_identity_hash
from src.application.notification_delivery_adapter import resolve_feishu_bot_send_target


NotificationRouteResolver = Callable[..., dict[str, Any]]


def notifications_enabled(config: dict[str, Any] | None) -> bool:
    """Outbound delivery requires explicit opt-in; legacy intent is preserved by migration."""
    notifications = (config or {}).get("notifications")
    return isinstance(notifications, dict) and notifications.get("enabled") is True


def resolve_notification_delivery_route(
    *,
    config: dict[str, Any] | None,
    route_resolver: NotificationRouteResolver = resolve_notification_route_from_config,
) -> dict[str, Any]:
    """Resolve the canonical delivery route used by notifications and receipts."""
    if not notifications_enabled(config):
        notifications = dict((config or {}).get("notifications") or {})
        return {"notifications": notifications, "enabled": False,
                "disabled_reason": "notifications_disabled", "provider": notifications.get("provider"),
                "channel": notifications.get("channel"), "target": None}
    route = route_resolver(config=config or {})
    route = route if isinstance(route, dict) else {}
    notifications = route.get("notifications") if isinstance(route.get("notifications"), dict) else {}
    provider = route.get("provider")
    target = route.get("target")
    if provider == FEISHU_APP_NOTIFICATION_PROVIDER:
        target = resolve_feishu_bot_send_target(notifications=notifications)
    return {
        **route,
        "enabled": True,
        "disabled_reason": None,
        "notifications": notifications,
        "provider": provider,
        "channel": route.get("channel"),
        "target": target,
    }


def notification_reminder_route_key(*, config: dict[str, Any], base: Path) -> str | None:
    """Bind seen details to the effective recipient and non-secret sender scope."""
    try:
        if not notifications_enabled(config):
            return None
        route = resolve_notification_route_from_config(config=config)
        identity = {key: route.get(key) for key in ("provider", "channel")}
        if route["provider"] == FEISHU_APP_NOTIFICATION_PROVIDER:
            from src.application.secret_resolver import resolve_feishu_bot_config
            bot = resolve_feishu_bot_config(metadata_only=True)
            if not bot.app_id or not bot.user_open_id:
                return None
            identity.update(sender=bot.app_id, recipient=bot.user_open_id)
        elif route["provider"] == "wechat_clawbot":
            from src.application.channels.wechat_clawbot.state import (
                resolve_wechat_clawbot_target, resolve_wechat_clawbot_state_dir,
            )
            from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore
            target = resolve_wechat_clawbot_target(route["target"], notifications=route["notifications"])
            state_dir = resolve_wechat_clawbot_state_dir(
                base=base, label=target.label, notifications=route["notifications"])
            with WechatClawbotStateStore(state_dir).read_directory() as store:
                binding = store.safe_bindings().get(target.binding_name) or {}
                sender_scope = store.sender_directory_scope()
            if sender_scope is None:
                return None
            if (not isinstance(binding.get("to_user_id"), str) or not binding["to_user_id"].strip()
                    or any(binding.get(key) is not None and not isinstance(binding[key], str)
                           for key in ("group_id", "chat_key"))):
                return None
            identity.update(sender=[target.label, *sender_scope],
                recipient=[binding.get(key) for key in ("to_user_id", "group_id", "chat_key")])
        else:
            return None
        return notification_route_identity_hash(**identity)
    except Exception:
        # Unknown routing evidence cannot establish who already saw a reminder.
        return None
