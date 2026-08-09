from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from src.application.conversation_scope import conversation_reference, conversation_scope_from_notification_route


NOTIFICATION_PERCEPTION_EVENT_SCHEMA_VERSION = "om-notification-perception-event-v1"
NOTIFICATION_PERCEPTION_EVENT_TYPE = "assistant_perception"


def build_notification_perception_event(
    *,
    event_kind: str,
    run_id: str,
    results_count: int | None = None,
    notify_candidates: list[Any] | None = None,
    account_messages: dict[str, str] | None = None,
    threshold_met: bool | None = None,
    used_heartbeat: bool | None = None,
    heartbeat_accounts: list[str] | tuple[str, ...] | None = None,
    provider: Any = None,
    channel: Any = None,
    target: Any = None,
    no_send: bool | None = None,
    quiet_hours: Any = None,
    delivery_decision: dict[str, Any] | None = None,
    conversation_scope: dict[str, Any] | None = None,
    sent_accounts: list[str] | None = None,
    notify_failures: list[dict[str, Any]] | None = None,
    send_attempted_count: int | None = None,
    send_confirmed_count: int | None = None,
) -> dict[str, Any]:
    messages = account_messages if isinstance(account_messages, dict) else {}
    candidates = notify_candidates if isinstance(notify_candidates, list) else []
    delivery = delivery_decision if isinstance(delivery_decision, dict) else {}
    route_scope = (
        {str(key): value for key, value in conversation_scope.items()}
        if isinstance(conversation_scope, dict)
        else conversation_scope_from_notification_route(provider=provider, channel=channel, target=target)
    )
    accounts = _string_list(messages.keys())
    symbol_summary = _symbol_summary(candidates)
    safe_slots = _safe_slots(
        run_id=run_id,
        accounts=accounts,
        symbols=symbol_summary.get("symbols", []),
        action=str(delivery.get("action") or event_kind),
    )
    return _strip_empty(
        {
            "schema_version": NOTIFICATION_PERCEPTION_EVENT_SCHEMA_VERSION,
            "event_type": NOTIFICATION_PERCEPTION_EVENT_TYPE,
            "event_kind": str(event_kind or "notification_event"),
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "run_id": str(run_id or ""),
            "accounts": accounts,
            "results_count": _optional_int(results_count),
            "notify_candidate_count": len(candidates),
            "symbols_summary": symbol_summary,
            "threshold_met": None if threshold_met is None else bool(threshold_met),
            "used_heartbeat": None if used_heartbeat is None else bool(used_heartbeat),
            "heartbeat_accounts": _string_list(heartbeat_accounts),
            "message_count": len(messages),
            "message_len_by_account": {str(account): len(str(message)) for account, message in messages.items()},
            "message_sha256_by_account": {
                str(account): _sha256_text(message)
                for account, message in messages.items()
            },
            "provider": str(provider) if provider else None,
            "channel": str(channel) if channel else route_scope.get("channel"),
            "target_masked": mask_notification_target(target),
            "conversation_scope": {
                key: value
                for key, value in {
                    "channel": route_scope.get("channel"),
                    "conversation_ref": conversation_reference(route_scope.get("conversation_id")),
                }.items()
                if value
            },
            "no_send": None if no_send is None else bool(no_send),
            "quiet_hours": str(quiet_hours) if quiet_hours else None,
            "delivery": _strip_empty(
                {
                    "action": delivery.get("action"),
                    "reason": delivery.get("reason"),
                    "should_send": delivery.get("should_send"),
                    "quiet_window": delivery.get("quiet_window"),
                    "config_error": delivery.get("config_error"),
                }
            ),
            "send_summary": _strip_empty(
                {
                    "sent_accounts": _string_list(sent_accounts),
                    "failure_count": len(notify_failures or []),
                    "send_attempted_count": _optional_int(send_attempted_count),
                    "send_confirmed_count": _optional_int(send_confirmed_count),
                }
            ),
            "safe_slots": safe_slots,
            "summary": _summary(
                event_kind=str(event_kind or "notification_event"),
                accounts=accounts,
                threshold_met=threshold_met,
                delivery=delivery,
                no_send=no_send,
            ),
        }
    )


def mask_notification_target(target: Any) -> str | None:
    text = str(target or "").strip()
    if not text:
        return None
    return "target:sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _symbol_summary(candidates: list[Any]) -> dict[str, Any]:
    all_symbols: list[str] = []
    for item in candidates:
        symbol = _candidate_symbol(item)
        if symbol and symbol not in all_symbols:
            all_symbols.append(symbol)
    symbols = all_symbols[:20]
    return {"symbols": symbols, "unique_symbol_count": len(all_symbols), "truncated": len(all_symbols) > len(symbols)}


def _candidate_symbol(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("symbol", "canonical_symbol", "stock", "code"):
            value = item.get(key)
            text = str(value or "").strip()
            if text:
                return text
    for key in ("symbol", "canonical_symbol", "stock", "code"):
        value = getattr(item, key, None)
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _safe_slots(*, run_id: str, accounts: list[str], symbols: list[str], action: str) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {"run_id": [str(run_id)]} if str(run_id or "").strip() else {}
    if accounts:
        out["account"] = accounts[:20]
    if symbols:
        out["symbol"] = symbols[:20]
    if action:
        out["action"] = [action]
    return out


def _summary(
    *,
    event_kind: str,
    accounts: list[str],
    threshold_met: bool | None,
    delivery: dict[str, Any],
    no_send: bool | None,
) -> str:
    parts = [f"notification {event_kind}"]
    if accounts:
        parts.append(f"accounts={','.join(accounts)}")
    if threshold_met is not None:
        parts.append(f"threshold_met={bool(threshold_met)}")
    if delivery.get("action"):
        parts.append(f"action={delivery.get('action')}")
    if delivery.get("reason"):
        parts.append(f"reason={delivery.get('reason')}")
    if no_send is not None:
        parts.append(f"no_send={bool(no_send)}")
    return " ".join(parts)


def _sha256_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _string_list(values: Any) -> list[str]:
    out: list[str] = []
    for item in values or []:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _strip_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


__all__ = [
    "NOTIFICATION_PERCEPTION_EVENT_SCHEMA_VERSION",
    "NOTIFICATION_PERCEPTION_EVENT_TYPE",
    "build_notification_perception_event",
    "mask_notification_target",
]
