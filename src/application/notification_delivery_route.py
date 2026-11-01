from __future__ import annotations

from typing import Any, Callable
from pathlib import Path

from domain.domain.multi_tick import FEISHU_APP_NOTIFICATION_PROVIDER, resolve_notification_route_from_config, notification_route_identity_hash
from src.application.notification_delivery_adapter import resolve_feishu_bot_send_target
from src.application.channels.plugins import channel_activity_enabled
from src.application.runtime_config_paths import read_json_object_or_empty


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
    if not channel_activity_enabled(config, str(provider), activity="notify"):
        return {**route, "enabled": False, "disabled_reason": "channel_notify_disabled", "target": None}
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


def resolve_notification_delivery_routes(
    *, config: dict[str, Any] | None, account: str, base: Path | None = None,
) -> list[dict[str, Any]]:
    """Resolve only this account's enabled destinations, binding actual identity.

    Explicit empty routes intentionally suppress delivery and never fall back.
    Missing identity is a configuration error, not an empty successful delivery.
    """
    from src.application.agent_tool_contracts import AgentToolError

    cfg = config or {}
    if not notifications_enabled(cfg):
        return []
    notifications = cfg.get("notifications") or {}
    explicit = "routes" in notifications
    if explicit:
        raw_routes = notifications["routes"]
        if not isinstance(raw_routes, list):
            raise AgentToolError(code="CONFIG_ERROR", message="notifications.routes must be an array")
        routes = []
        for item in raw_routes:
            if account not in item.get("accounts", []):
                continue
            provider = item["provider"]
            if not channel_activity_enabled(cfg, provider, activity="notify"):
                continue
            options = {k: v for k, v in notifications.items() if k != "routes"}
            options.update(provider=provider, channel=provider, target=item["target"],
                           _explicit_route_target=True)
            routes.append({"route_id": item["id"], "provider": provider, "channel": provider,
                           "target": item["target"], "notifications": options, "enabled": True})
    else:
        route = resolve_notification_delivery_route(config=cfg)
        routes = [{**route, "route_id": "legacy"}] if route.get("enabled") else []
    identities: set[str] = set()
    for route in routes:
        identity = notification_reminder_route_key(config={**cfg, "notifications": route["notifications"]},
                                                   base=base or Path.cwd())
        if not identity:
            raise AgentToolError(code="CHANNEL_ROUTE_UNPROVEN", message="message sender/recipient identity is unavailable")
        if identity in identities:
            raise AgentToolError(code="CONFIG_ERROR", message="duplicate actual message destination")
        identities.add(identity)
        route.update(identity_key=identity, route_key=identity, account=account)
    return routes


def notification_delivery_route_is_current(
    *, config: dict[str, Any], account: str, recipient: dict[str, Any], base: Path,
) -> bool:
    """A frozen obligation cannot authorize a newly configured sender/recipient."""
    source = str(config.get("config_source_path") or "").strip()
    if source:
        path = Path(source).expanduser()
        current = read_json_object_or_empty(path if path.is_absolute() else base / path)
        # Only delivery authority reloads; the producer keeps its business snapshot.
        config = {key: current[key] for key in ("channels", "notifications") if key in current}
    if not notifications_enabled(config):
        return False
    provider = recipient.get("provider")
    if not channel_activity_enabled(config, str(provider), activity="notify"):
        return False
    routes = resolve_notification_delivery_routes(config=config, account=account, base=base)
    return any(route["route_id"] == recipient.get("route_id")
               and route["identity_key"] == recipient.get("identity_key") for route in routes)


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
            recipient = (route.get("target") if route["notifications"].get("_explicit_route_target")
                         else bot.user_open_id)
            if not bot.app_id or not recipient:
                return None
            identity.update(sender=bot.app_id, recipient=recipient)
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
