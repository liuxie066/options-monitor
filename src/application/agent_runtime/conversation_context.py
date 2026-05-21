from __future__ import annotations

from typing import Any

from src.application.inbound.audit import InboundAuditStore
from src.application.inbound.contracts import InboundRequest
from src.application.inbound.operation_store import InboundOperationStore


def build_conversation_context(
    request: InboundRequest,
    *,
    audit_store: InboundAuditStore,
    max_messages: int,
    max_pending: int = 5,
) -> dict[str, Any]:
    window = max(0, min(int(max_messages or 0), 20))
    pending_limit = max(0, min(int(max_pending or 0), 10))
    normalized = _normalized_scope(request)

    recent_messages = []
    if window > 0:
        recent_messages = [
            _audit_context_item(row)
            for row in reversed(
                audit_store.list_recent(
                    channel=normalized["channel"],
                    sender_id=normalized["sender_id"],
                    conversation_id=normalized["conversation_id"],
                    limit=window,
                )
            )
        ]

    pending_operations = []
    if pending_limit > 0:
        pending_operations = InboundOperationStore(audit_store.path).list_pending_operations(
            channel=normalized["channel"],
            sender_id=normalized["sender_id"],
            conversation_id=normalized["conversation_id"],
            include_expired=False,
            limit=pending_limit,
        )

    return {
        "scope": normalized,
        "window_messages": window,
        "recent_messages": recent_messages,
        "pending_operations": pending_operations,
    }


def context_trace(context: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {"provided": False}
    recent = context.get("recent_messages")
    pending = context.get("pending_operations")
    return {
        "provided": True,
        "window_messages": int(context.get("window_messages") or 0),
        "recent_count": len(recent) if isinstance(recent, list) else 0,
        "pending_count": len(pending) if isinstance(pending, list) else 0,
    }


def _normalized_scope(request: InboundRequest) -> dict[str, str]:
    channel = str(request.channel or "local").strip().lower() or "local"
    sender_id = str(request.sender_id or "").strip()
    conversation_id = str(request.conversation_id or "").strip() or f"{channel}:{sender_id}"
    return {"channel": channel, "sender_id": sender_id, "conversation_id": conversation_id}


def _audit_context_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "created_at": row.get("created_at"),
        "raw_text": _clip(row.get("raw_text"), 240),
        "parser": row.get("parser"),
        "intent_name": row.get("intent_name"),
        "tool_name": row.get("tool_name"),
        "decision": row.get("decision"),
        "result_ok": bool(row.get("result_ok")),
        "error_code": row.get("error_code"),
    }


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."
