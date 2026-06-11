from __future__ import annotations

from pathlib import Path
from typing import Any

from src.application.assistant.audit import InboundAuditStore
from src.application.assistant.operation_store import InboundOperationStore
from src.application.assistant.contracts import AssistantRequest
from src.application.assistant.user_profile import load_user_profile_context, user_profile_trace
from src.application.tool_allowlist import PURE_READ_TOOLS


def build_conversation_context(
    request: AssistantRequest,
    *,
    audit_store: InboundAuditStore,
    max_messages: int,
    max_pending: int = 5,
    user_profile_path: str | Path | None = None,
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
        "limits": {
            "max_recent_messages": window,
            "max_pending_operations": pending_limit,
        },
        "semantics": {
            "explicit_message_wins": True,
            "context_is_hint_only": True,
            "confirmation_must_be_deterministic": True,
        },
        "recent_messages": recent_messages,
        "last_successful_read": _last_successful_read(recent_messages),
        "pending_operations": pending_operations,
        "user_profile": load_user_profile_context(user_profile_path),
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
        "user_profile": user_profile_trace(
            context.get("user_profile") if isinstance(context.get("user_profile"), dict) else None
        ),
    }


def _normalized_scope(request: AssistantRequest) -> dict[str, str]:
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
        "tool_payload": _safe_tool_payload(row.get("tool_payload_json")),
        "decision": row.get("decision"),
        "result_ok": bool(row.get("result_ok")),
        "error_code": row.get("error_code"),
    }


def _last_successful_read(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in reversed(messages):
        if item.get("result_ok") is not True:
            continue
        tool_name = str(item.get("tool_name") or "").strip()
        if not tool_name or tool_name not in PURE_READ_TOOLS:
            continue
        return {
            "created_at": item.get("created_at"),
            "intent_name": item.get("intent_name"),
            "tool_name": tool_name,
            "tool_payload": item.get("tool_payload") if isinstance(item.get("tool_payload"), dict) else {},
        }
    return None


def _safe_tool_payload(value: Any) -> dict[str, Any]:
    import json

    try:
        parsed = json.loads(str(value or "{}"))
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    allowed = {
        "config_key",
        "account",
        "status",
        "month",
        "run_id",
        "kind",
        "limit",
        "lines",
        "action",
        "query",
        "symbol",
        "option_type",
        "side",
        "strike",
        "expiration",
    }
    return {key: parsed[key] for key in sorted(allowed) if key in parsed}


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."
