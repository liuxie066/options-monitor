from __future__ import annotations

from typing import Any, Callable

from domain.domain.multi_tick import FEISHU_APP_NOTIFICATION_PROVIDER, resolve_notification_route_from_config
from src.application.notification_delivery_adapter import resolve_feishu_bot_send_target


NotificationRouteResolver = Callable[..., dict[str, Any]]


def resolve_notification_delivery_route(
    *,
    config: dict[str, Any] | None,
    route_resolver: NotificationRouteResolver = resolve_notification_route_from_config,
) -> dict[str, Any]:
    """Resolve the canonical delivery route used by notifications and receipts."""
    route = route_resolver(config=config or {})
    route = route if isinstance(route, dict) else {}
    notifications = route.get("notifications") if isinstance(route.get("notifications"), dict) else {}
    provider = route.get("provider")
    target = route.get("target")
    if provider == FEISHU_APP_NOTIFICATION_PROVIDER:
        target = resolve_feishu_bot_send_target(notifications=notifications)
    return {
        **route,
        "notifications": notifications,
        "provider": provider,
        "channel": route.get("channel"),
        "target": target,
    }
