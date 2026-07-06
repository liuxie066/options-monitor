from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.application.assistant.audit import InboundAuditStore
from src.application.assistant.operation_store import InboundOperationStore
from src.application.assistant.contracts import AssistantRequest
from src.application.assistant.context_projection import build_context_projection, context_projection_trace
from src.application.assistant.memory import assistant_memory_trace, load_assistant_memory_context
from src.application.assistant.session_store import AgentSessionStore
from src.application.assistant.user_profile import load_user_profile_context, user_profile_trace
from src.application.conversation_scope import normalize_conversation_scope
from src.application.notification_perception_read import read_notification_perception_events
from src.application.tool_allowlist import PURE_READ_TOOLS


def build_conversation_context(
    request: AssistantRequest,
    *,
    audit_store: InboundAuditStore,
    max_messages: int,
    max_pending: int = 5,
    user_profile_path: str | Path | None = None,
    assistant_memory_path: str | Path | None = None,
) -> dict[str, Any]:
    window = max(0, min(int(max_messages or 0), 20))
    pending_limit = max(0, min(int(max_pending or 0), 10))
    normalized = _normalized_scope(request)

    recent_messages = []
    recent_sessions = []
    recent_system_events = []
    if window > 0:
        history_sender_id = _history_sender_id(normalized)
        recent_messages = [
            _audit_context_item(row)
            for row in reversed(
                audit_store.list_recent(
                    channel=normalized["channel"],
                    sender_id=history_sender_id,
                    conversation_id=normalized["conversation_id"],
                    limit=window,
                )
            )
        ]
        recent_sessions = AgentSessionStore(audit_store.path).list_recent(
            channel=normalized["channel"],
            sender_id=history_sender_id,
            conversation_id=normalized["conversation_id"],
            limit=window,
        )
        recent_system_events = _recent_system_events(
            request=request,
            conversation_id=normalized["conversation_id"],
            limit=window,
        )

    pending_operations = []
    if pending_limit > 0:
        pending_operations = InboundOperationStore(audit_store.path).list_pending_operations(
            channel=normalized["channel"],
            sender_id=normalized["sender_id"],
            conversation_id=normalized["conversation_id"],
            include_expired=False,
            limit=pending_limit,
        )

    context = {
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
        "recent_system_events": recent_system_events,
        "last_successful_read": _last_successful_read(recent_messages),
        "pending_operations": pending_operations,
        "user_profile": load_user_profile_context(user_profile_path),
        "assistant_memory": load_assistant_memory_context(
            path=assistant_memory_path,
            query=request.text,
        ),
    }
    context["context_projection"] = build_context_projection(
        current_user_message=request.text,
        conversation_context=context,
        recent_sessions=recent_sessions,
    )
    return context


def context_trace(context: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {"provided": False}
    recent = context.get("recent_messages")
    system_events = context.get("recent_system_events")
    pending = context.get("pending_operations")
    trace = {
        "provided": True,
        "degraded": bool(context.get("degraded")),
        "window_messages": int(context.get("window_messages") or 0),
        "recent_count": len(recent) if isinstance(recent, list) else 0,
        "system_event_count": len(system_events) if isinstance(system_events, list) else 0,
        "pending_count": len(pending) if isinstance(pending, list) else 0,
        "user_profile": user_profile_trace(
            context.get("user_profile") if isinstance(context.get("user_profile"), dict) else None
        ),
        "assistant_memory": assistant_memory_trace(
            context.get("assistant_memory") if isinstance(context.get("assistant_memory"), dict) else None
        ),
    }
    projection_trace = context_projection_trace(
        context.get("context_projection") if isinstance(context.get("context_projection"), dict) else None
    )
    if projection_trace.get("provided"):
        trace["context_projection"] = projection_trace
    if isinstance(context.get("error"), dict):
        error = context["error"]
        trace["error"] = {
            "stage": str(error.get("stage") or ""),
            "error_type": str(error.get("error_type") or ""),
        }
    return trace


def _normalized_scope(request: AssistantRequest) -> dict[str, str]:
    return normalize_conversation_scope(
        channel=request.channel,
        sender_id=request.sender_id,
        conversation_id=request.conversation_id,
    )


def _history_sender_id(scope: dict[str, str]) -> str | None:
    channel = str(scope.get("channel") or "").strip().lower()
    conversation_id = str(scope.get("conversation_id") or "").strip()
    if channel == "wechat" and conversation_id.startswith("wechat:"):
        return None
    return str(scope.get("sender_id") or "").strip()


def _recent_system_events(*, request: AssistantRequest, conversation_id: str, limit: int) -> list[dict[str, Any]]:
    try:
        data = read_notification_perception_events(
            repo_root=_context_repo_root(request),
            conversation_id=conversation_id,
            limit=limit,
        )
    except Exception:
        return []
    events = data.get("events") if isinstance(data, dict) else []
    return [event for event in events if isinstance(event, dict)]


def _context_repo_root(request: AssistantRequest) -> Path:
    reply_context = request.reply_context if isinstance(request.reply_context, dict) else {}
    base = str(reply_context.get("base") or "").strip()
    if base:
        return Path(base).expanduser().resolve()
    return Path.cwd().resolve()


def _audit_context_item(row: dict[str, Any]) -> dict[str, Any]:
    raw_tool_name = row.get("tool_name")
    tool_name = raw_tool_name
    intent_name = row.get("intent_name")
    tool_payload = _safe_tool_payload(row.get("tool_payload_json"))
    response_text = ""
    if str(raw_tool_name or "").strip() == "assistant.tool_loop":
        derived = _agent_loop_read_context(row)
        if derived is not None:
            tool_name = derived.get("tool_name") or tool_name
            intent_name = derived.get("intent_name") or intent_name
            tool_payload = derived.get("tool_payload") if isinstance(derived.get("tool_payload"), dict) else tool_payload
            response_text = str(derived.get("response_text") or "")
    item = {
        "created_at": row.get("created_at"),
        "raw_text": _clip(row.get("raw_text"), 240),
        "parser": row.get("parser"),
        "intent_name": intent_name,
        "tool_name": tool_name,
        "tool_payload": tool_payload,
        "decision": row.get("decision"),
        "result_ok": bool(row.get("result_ok")),
        "error_code": row.get("error_code"),
    }
    if raw_tool_name and raw_tool_name != tool_name:
        item["raw_tool_name"] = raw_tool_name
    if response_text:
        item["response_text"] = _clip(response_text, 360)
    return item


def _agent_loop_read_context(row: dict[str, Any]) -> dict[str, Any] | None:
    if row.get("result_ok") in (False, 0):
        return None
    payload = _loads_object(row.get("tool_payload_json"))
    response = _loads_object(row.get("response_json"))
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else None
    if plan is None:
        action = _nested_dict(response, "data", "action")
        action_payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        plan = action_payload.get("plan") if isinstance(action_payload.get("plan"), dict) else None
    if plan is None:
        return _agent_loop_event_read_context(payload=payload, response=response)
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    for step in steps:
        if not isinstance(step, dict):
            continue
        tool_name = str(step.get("tool_name") or "").strip()
        if tool_name not in PURE_READ_TOOLS:
            continue
        arguments = step.get("arguments") if isinstance(step.get("arguments"), dict) else {}
        task_contract = plan.get("task_contract") if isinstance(plan.get("task_contract"), dict) else {}
        intent_families = task_contract.get("intent_families") if isinstance(task_contract.get("intent_families"), list) else []
        intent_name = str(intent_families[0]) if intent_families else tool_name
        return {
            "intent_name": intent_name,
            "tool_name": tool_name,
            "tool_payload": _safe_tool_payload_from_dict(arguments),
            "response_text": _extract_response_text(response),
        }
    return None


def _agent_loop_event_read_context(*, payload: dict[str, Any], response: dict[str, Any]) -> dict[str, Any] | None:
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    if not events:
        action_payload = _nested_dict(response, "data", "action", "payload")
        events = action_payload.get("events") if isinstance(action_payload.get("events"), list) else []
    if not events:
        result_data = _nested_dict(response, "data", "action", "result", "data")
        transcript = result_data.get("event_transcript") if isinstance(result_data.get("event_transcript"), list) else []
        events = transcript
    if not events:
        return None
    task_contract = payload.get("task_contract") if isinstance(payload.get("task_contract"), dict) else {}
    if not task_contract:
        action_payload = _nested_dict(response, "data", "action", "payload")
        task_contract = action_payload.get("task_contract") if isinstance(action_payload.get("task_contract"), dict) else {}
    if not task_contract:
        result_data = _nested_dict(response, "data", "action", "result", "data")
        task_contract = result_data.get("task_contract") if isinstance(result_data.get("task_contract"), dict) else {}
    for event in events:
        if not isinstance(event, dict) or event.get("event_type") != "model_tool_call":
            continue
        tool_name = str(event.get("tool_name") or "").strip()
        if tool_name not in PURE_READ_TOOLS:
            continue
        arguments = event.get("arguments") if isinstance(event.get("arguments"), dict) else {}
        intent_families = task_contract.get("intent_families") if isinstance(task_contract.get("intent_families"), list) else []
        intent_name = str(intent_families[0]) if intent_families else tool_name
        return {
            "intent_name": intent_name,
            "tool_name": tool_name,
            "tool_payload": _safe_tool_payload_from_dict(arguments),
            "response_text": _extract_response_text(response),
        }
    return None


def _nested_dict(value: dict[str, Any], *keys: str) -> dict[str, Any]:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _extract_response_text(response: dict[str, Any]) -> str:
    candidates: list[Any] = [
        response.get("response_text"),
        _nested_dict(response, "data").get("response_text"),
        _nested_dict(response, "data", "action").get("response_text"),
        _nested_dict(response, "data", "action", "payload").get("response_text"),
        _nested_dict(response, "data", "action", "result", "data").get("response_text"),
    ]
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def _loads_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_tool_payload_from_dict(parsed: dict[str, Any]) -> dict[str, Any]:
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
        "function",
    }
    return {key: parsed[key] for key in sorted(allowed) if key in parsed}


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
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return _safe_tool_payload_from_dict(parsed)


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."
