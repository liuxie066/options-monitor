"""Application delivery bridge to the shared frozen notification fanout owner."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.infrastructure.fetch_control import check_fetch_control
from src.application.notification_delivery_adapter import (
    normalize_notification_delivery_result, select_notification_delivery_adapter,
)
from src.application.notification_delivery_route import resolve_notification_delivery_routes


def uses_proactive_fanout(config: dict[str, Any] | None) -> bool:
    cfg = config or {}
    notifications = cfg.get("notifications")
    return "channels" in cfg or (isinstance(notifications, dict) and "routes" in notifications)


def send_notification_fanout(
    *, base: Path, config: dict[str, Any] | None, account: str, logical_key: str,
    message: str, send_fn: Callable[..., Any] | None = None,
    normalize_fn: Callable[..., dict[str, Any]] | None = None,
    adapter_selector: Callable[[Any], Any] = select_notification_delivery_adapter,
    routes: list[dict[str, Any]] | None = None, now_ms: int | None = None,
    max_attempts: int = 20, retry_delays_ms: tuple[int, ...] = (60_000,),
    deadline: float | None = None, cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Adapt a producer's frozen message to the shared durable delivery owner."""
    from src.application.notification_delivery_route import notification_delivery_route_is_current
    from src.application.notification_fanout import (
        FanoutRetryPolicy, dispatch_notification_fanout, notification_fanout_id,
        prepare_notification_fanout, read_notification_fanout,
    )

    fanout_id = notification_fanout_id(account=account, logical_key=logical_key)
    snapshot = read_notification_fanout(base=base, fanout_id=fanout_id)
    if snapshot is None:
        routes = routes if routes is not None else resolve_notification_delivery_routes(
            config=config, account=account, base=base)
        if not routes:
            return {"fanout_id": fanout_id, "enabled": False, "status": "pending", "reason": "routes_disabled_or_unavailable",
                    "delivery_confirmed": False, "has_retryable_children": True, "attempted": False,
                    "message_id": None, "provider": "fanout", "fallback_used": False}
        snapshot = prepare_notification_fanout(base=base, account=account, logical_key=logical_key,
            deliveries=[{"recipient": {"route_id": route["route_id"], "provider": route["provider"],
                "channel": route["channel"], "target": route["target"], "identity_key": route["identity_key"],
                "transport_options": route.get("notifications") or {}}, "message": message,
                "transport_envelope": None, "metadata": {"account": route.get("account", account)}} for route in routes],
            retry_policy=FanoutRetryPolicy(max_attempts=max_attempts, retry_delays_ms=retry_delays_ms),
            now_ms=now_ms)

    def can_send(spec: dict[str, Any]) -> bool:
        check_fetch_control(deadline, cancelled)
        return notification_delivery_route_is_current(config=config, account=spec["metadata"].get("account", account),
            recipient=spec["recipient"], base=base)

    def send(*, spec: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        check_fetch_control(deadline, cancelled)
        recipient = spec["recipient"]
        try:
            adapter = adapter_selector(recipient["provider"]) if send_fn is None or normalize_fn is None else None
            selected_send = send_fn or adapter.send_fn
            selected_normalize = normalize_fn or adapter.normalize_fn
            controls = {"deadline": deadline, "cancelled": cancelled} if deadline is not None or cancelled is not None else {}
            raw = selected_send(base=base, channel=recipient["channel"], target=recipient["target"],
                message=spec["message"], notifications=recipient["transport_options"], idempotency_key=idempotency_key,
                **controls)
            return normalize_notification_delivery_result(raw, normalize_fn=selected_normalize)
        except AgentToolError as exc:
            if exc.code == "CHANNEL_PLUGIN_NOT_INSTALLED":
                return {"explicit_pre_acceptance_failure": True, "local_error_code": exc.code}
            raise

    before = sum(child["attempt_count"] for child in snapshot["children"])
    snapshot = dispatch_notification_fanout(base=base, fanout_id=fanout_id, send_fn=send,
        can_send_fn=can_send, now_ms=now_ms)
    return {**snapshot, "enabled": True,
        "attempted": sum(child["attempt_count"] for child in snapshot["children"]) > before,
        "message_id": None, "provider": "fanout", "fallback_used": False}
