from __future__ import annotations

import json
from typing import Any

from src.application.assistant.user_profile import user_profile_trace
from src.application.tool_allowlist import PURE_READ_TOOLS


CONVERSATION_EVENT_SCHEMA_VERSION = "om-conversation-event-v1"
CONTEXT_PROJECTION_SCHEMA_VERSION = "om-context-projection-v1"

DEFAULT_MAX_RECENT_TURNS = 6
DEFAULT_MAX_SUCCESSFUL_TOOLS = 5
DEFAULT_MAX_OPEN_GAPS = 5
DEFAULT_MAX_CHARS = 12000
DEFAULT_MAX_TEXT_EXCERPT_CHARS = 360
DEFAULT_MAX_PAYLOAD_CHARS = 1000

SAFE_SLOT_KEYS = {
    "account",
    "symbol",
    "month",
    "market",
    "status",
    "action",
    "function",
    "strategy",
    "option_type",
    "side",
    "expiration",
    "strike",
    "setting_path",
    "setting_field",
    "setting_new_value",
    "operation_id",
    "run_id",
}

_SAFE_SLOT_ALIASES = {
    "expiration_ymd": "expiration",
    "exp": "expiration",
}
_SAFE_SLOT_NESTED_KEYS = {"filters", "query", "safe_slots"}
_SENSITIVE_TEXT_TOKENS = (
    "access_token",
    "api key",
    "api_key",
    "authorization",
    "cookie",
    "password",
    "private_key",
    "secret",
    "token",
    "webhook",
)


def build_context_projection(
    *,
    current_user_message: str,
    conversation_context: dict[str, Any] | None,
    recent_sessions: list[dict[str, Any]] | None = None,
    max_recent_turns: int = DEFAULT_MAX_RECENT_TURNS,
    max_successful_tools: int = DEFAULT_MAX_SUCCESSFUL_TOOLS,
    max_open_gaps: int = DEFAULT_MAX_OPEN_GAPS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> dict[str, Any]:
    context = conversation_context if isinstance(conversation_context, dict) else {}
    ref_allocator = _RefAllocator()
    turn_items: list[dict[str, Any]] = []
    tool_items: list[dict[str, Any]] = []
    evidence_refs: list[dict[str, Any]] = []
    open_gaps: list[dict[str, Any]] = []

    for rank, row in enumerate(recent_sessions or (), start=1):
        turn, tools, refs, gaps = _turn_from_session_row(row, rank=rank, ref_allocator=ref_allocator)
        if turn:
            turn_items.append(turn)
        tool_items.extend(tools)
        evidence_refs.extend(refs)
        open_gaps.extend(gaps)

    recent_system_events = context.get("recent_system_events") if isinstance(context.get("recent_system_events"), list) else []
    system_event_items: list[dict[str, Any]] = []
    for rank, item in enumerate(recent_system_events, start=1):
        turn, ref, system_event = _turn_from_system_event(
            item if isinstance(item, dict) else {},
            rank=rank,
            ref_allocator=ref_allocator,
        )
        if turn:
            turn_items.append(turn)
        if ref:
            evidence_refs.append(ref)
        if system_event:
            system_event_items.append(system_event)

    recent_messages = context.get("recent_messages") if isinstance(context.get("recent_messages"), list) else []
    for rank, item in enumerate(recent_messages, start=1):
        turn, tool, ref = _turn_from_audit_item(item if isinstance(item, dict) else {}, rank=rank, ref_allocator=ref_allocator)
        if turn:
            turn_items.append(turn)
        if tool:
            tool_items.append(tool)
        if ref:
            evidence_refs.append(ref)

    last_read = context.get("last_successful_read") if isinstance(context.get("last_successful_read"), dict) else {}
    if last_read and not recent_messages:
        turn, tool, ref = _turn_from_audit_item(
            {
                "created_at": "last_successful_read",
                "raw_text": last_read.get("raw_text") or last_read.get("intent_name") or last_read.get("tool_name"),
                "intent_name": last_read.get("intent_name"),
                "tool_name": last_read.get("tool_name"),
                "tool_payload": last_read.get("tool_payload") if isinstance(last_read.get("tool_payload"), dict) else {},
                "result_ok": True,
            },
            rank=1,
            ref_allocator=ref_allocator,
        )
        if turn:
            turn_items.append(turn)
        if tool:
            tool_items.append(tool)
        if ref:
            evidence_refs.append(ref)

    open_gaps.extend(_gaps_from_context(context, start_index=len(open_gaps) + 1))

    turn_items = _dedupe_by_id(_sort_by_recency(turn_items), "turn_id")
    tool_items = _dedupe_tools(_sort_by_recency(tool_items))
    evidence_refs = _dedupe_by_id(evidence_refs, "ref_id")
    open_gaps = _dedupe_by_id(_sort_by_recency(open_gaps), "gap_id")

    max_turns = max(0, min(int(max_recent_turns or 0), 20))
    max_tools = max(0, min(int(max_successful_tools or 0), 20))
    max_gaps = max(0, min(int(max_open_gaps or 0), 20))
    projection = {
        "schema_version": CONTEXT_PROJECTION_SCHEMA_VERSION,
        "current_user_message": {
            "text": _text_excerpt(current_user_message, DEFAULT_MAX_TEXT_EXCERPT_CHARS),
        },
        "recent_turns": turn_items[:max_turns],
        "recent_successful_tools": tool_items[:max_tools],
        "available_evidence_refs": [],
        "open_evidence_gaps": open_gaps[:max_gaps],
        "pending_operations": _pending_operations(context.get("pending_operations")),
        "user_profile": _projection_user_profile(context.get("user_profile")),
        "policy": {
            "current_message_wins": True,
            "context_is_hint": True,
            "ask_when_ambiguous": True,
            "declare_context_use": True,
        },
        "budget": {
            "max_recent_turns": max_turns,
            "max_successful_tools": max_tools,
            "max_open_gaps": max_gaps,
            "max_chars": max(1, int(max_chars or DEFAULT_MAX_CHARS)),
            "max_text_excerpt_chars": DEFAULT_MAX_TEXT_EXCERPT_CHARS,
            "max_payload_chars": DEFAULT_MAX_PAYLOAD_CHARS,
            "truncated": False,
            "truncation_reason": None,
        },
        "system_events": system_event_items[:max_turns],
    }
    _filter_evidence_refs(projection, evidence_refs)
    truncated_reasons: list[str] = []
    if len(turn_items) > max_turns:
        truncated_reasons.append("recent_turn_limit")
    if len(tool_items) > max_tools:
        truncated_reasons.append("successful_tool_limit")
    if len(open_gaps) > max_gaps:
        truncated_reasons.append("open_gap_limit")
    _enforce_char_budget(projection, truncated_reasons)
    return projection


def context_projection_trace(projection: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(projection, dict) or not projection:
        return {"provided": False}
    budget = projection.get("budget") if isinstance(projection.get("budget"), dict) else {}
    return {
        "provided": True,
        "schema_version": projection.get("schema_version"),
        "recent_turn_count": len(projection.get("recent_turns") or []),
        "recent_successful_tool_count": len(projection.get("recent_successful_tools") or []),
        "evidence_ref_count": len(projection.get("available_evidence_refs") or []),
        "open_gap_count": len(projection.get("open_evidence_gaps") or []),
        "pending_operation_count": len(projection.get("pending_operations") or []),
        "system_event_count": len(projection.get("system_events") or []),
        "truncated": bool(budget.get("truncated")),
        "truncation_reason": budget.get("truncation_reason"),
    }


class _RefAllocator:
    def __init__(self) -> None:
        self._index = 0

    def next(self) -> str:
        self._index += 1
        return f"ev_{self._index:03d}"


def _turn_from_session_row(
    row: dict[str, Any],
    *,
    rank: int,
    ref_allocator: _RefAllocator,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    snapshot = row.get("snapshot") if isinstance(row.get("snapshot"), dict) else {}
    if not snapshot:
        return None, [], [], []
    session_id = str(row.get("session_id") or snapshot.get("session_id") or rank).strip()
    turn_id = _turn_id("session", session_id)
    transcript = snapshot.get("tool_transcript") if isinstance(snapshot.get("tool_transcript"), list) else []
    tools: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = []
    turn_refs: list[str] = []
    safe_slots: dict[str, list[Any]] = {}
    for item in transcript:
        if not isinstance(item, dict):
            continue
        tool_name = str(item.get("tool_name") or "").strip()
        if not tool_name:
            continue
        slots = _tool_safe_slots(item)
        safe_slots = _merge_slots(safe_slots, slots)
        if item.get("ok") is not True or tool_name not in PURE_READ_TOOLS:
            continue
        ref = _evidence_ref(
            ref_id=ref_allocator.next(),
            turn_id=turn_id,
            source_tool=tool_name,
            label=f"{tool_name} result",
            safe_slots=slots,
            data_shape=_data_shape(item),
        )
        refs.append(ref)
        turn_refs.append(ref["ref_id"])
        tools.append(_successful_tool_summary(item=item, turn_id=turn_id, evidence_refs=[ref["ref_id"]]))
    request = snapshot.get("request") if isinstance(snapshot.get("request"), dict) else {}
    answer_trace = snapshot.get("answer_trace") if isinstance(snapshot.get("answer_trace"), dict) else {}
    final_response = answer_trace.get("final_response") if isinstance(answer_trace.get("final_response"), dict) else {}
    synthesis = answer_trace.get("synthesis") if isinstance(answer_trace.get("synthesis"), dict) else {}
    turn = {
        "turn_id": turn_id,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "user_summary": _text_excerpt(_first_text(row.get("raw_text"), request.get("text"), snapshot.get("goal")), 180),
        "assistant_summary": _text_excerpt(
            _first_text(row.get("response_text"), final_response.get("response_text"), synthesis.get("response_text")),
            240,
        ),
        "tools": _unique_strings([str(item.get("tool_name") or "") for item in transcript if isinstance(item, dict)]),
        "safe_slots": safe_slots,
        "evidence_refs": turn_refs,
        "result_status": _session_result_status(row, final_response),
    }
    gaps = _gaps_from_session(row=row, snapshot=snapshot, start_index=1)
    return _strip_empty(turn), tools, refs, gaps


def _turn_from_audit_item(
    item: dict[str, Any],
    *,
    rank: int,
    ref_allocator: _RefAllocator,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    created = _first_text(item.get("created_at"), str(rank))
    turn_id = _turn_id("audit", f"{created}:{rank}")
    tool_name = str(item.get("tool_name") or "").strip()
    payload = item.get("tool_payload") if isinstance(item.get("tool_payload"), dict) else {}
    slots = _safe_slots(payload)
    ref: dict[str, Any] | None = None
    tool: dict[str, Any] | None = None
    turn_refs: list[str] = []
    if item.get("result_ok") is True and tool_name in PURE_READ_TOOLS:
        ref = _evidence_ref(
            ref_id=ref_allocator.next(),
            turn_id=turn_id,
            source_tool=tool_name,
            label=f"{tool_name} result",
            safe_slots=slots,
            data_shape={},
        )
        tool = {
            "turn_id": turn_id,
            "created_at": item.get("created_at"),
            "tool_name": tool_name,
            "purpose": _clip(_first_text(item.get("intent_name"), tool_name), 120),
            "safe_payload": _safe_payload(payload),
            "safe_slots": slots,
            "evidence_refs": [ref["ref_id"]],
            "data_shape": {},
            "result_status": "ok",
        }
        turn_refs.append(ref["ref_id"])
    turn = {
        "turn_id": turn_id,
        "created_at": item.get("created_at"),
        "user_summary": _text_excerpt(item.get("raw_text"), 180),
        "assistant_summary": _text_excerpt(item.get("response_text"), 240),
        "tools": [tool_name] if tool_name else [],
        "safe_slots": slots,
        "evidence_refs": turn_refs,
        "result_status": "ok" if item.get("result_ok") is True else "error",
    }
    return _strip_empty(turn), _strip_empty(tool), ref


def _turn_from_system_event(
    item: dict[str, Any],
    *,
    rank: int,
    ref_allocator: _RefAllocator,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    created = _first_text(item.get("created_at_utc"), item.get("event_at_utc"), str(rank))
    event_kind = str(item.get("event_kind") or "notification_event").strip()
    run_id = str(item.get("run_id") or rank).strip()
    turn_id = _turn_id("system", f"{created}:{event_kind}:{run_id}:{rank}")
    slots = _safe_slots(item.get("safe_slots") if isinstance(item.get("safe_slots"), dict) else item)
    ref = _evidence_ref(
        ref_id=ref_allocator.next(),
        turn_id=turn_id,
        source_tool="notification_perception",
        label="notification perception event",
        safe_slots=slots,
        data_shape=_system_event_shape(item),
        source_type="system_event",
    )
    summary = _text_excerpt(_first_text(item.get("summary"), event_kind), 240)
    turn = {
        "turn_id": turn_id,
        "created_at": created,
        "user_summary": "",
        "assistant_summary": summary,
        "tools": [],
        "safe_slots": slots,
        "evidence_refs": [ref["ref_id"]],
        "result_status": "ok",
        "event_type": "system_event",
    }
    event = {
        "schema_version": CONVERSATION_EVENT_SCHEMA_VERSION,
        "event_id": turn_id,
        "event_type": "notification_perception",
        "event_kind": event_kind,
        "summary": summary,
        "safe_slots": slots,
        "evidence_refs": [ref["ref_id"]],
    }
    return _strip_empty(turn), ref, _strip_empty(event)


def _successful_tool_summary(*, item: dict[str, Any], turn_id: str, evidence_refs: list[str]) -> dict[str, Any]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    return {
        "turn_id": turn_id,
        "tool_name": str(item.get("tool_name") or ""),
        "purpose": _clip(_first_text(item.get("purpose"), item.get("tool_name")), 120),
        "safe_payload": _safe_payload(payload),
        "safe_slots": _tool_safe_slots(item),
        "evidence_refs": list(evidence_refs),
        "data_shape": _data_shape(item),
        "result_status": "ok",
    }


def _evidence_ref(
    *,
    ref_id: str,
    turn_id: str,
    source_tool: str,
    label: str,
    safe_slots: dict[str, list[Any]],
    data_shape: dict[str, Any],
    source_type: str = "tool_result",
) -> dict[str, Any]:
    return {
        "ref_id": ref_id,
        "turn_id": turn_id,
        "source_type": source_type,
        "source_tool": source_tool,
        "label": label,
        "safe_slots": safe_slots,
        "data_shape": data_shape,
    }


def _system_event_shape(item: dict[str, Any]) -> dict[str, Any]:
    delivery = item.get("delivery") if isinstance(item.get("delivery"), dict) else {}
    send_summary = item.get("send_summary") if isinstance(item.get("send_summary"), dict) else {}
    out: dict[str, Any] = {}
    for key in ("message_count", "notify_candidate_count", "threshold_met", "used_heartbeat"):
        if item.get(key) is not None:
            out[key] = item.get(key)
    if delivery.get("action") is not None:
        out["delivery_action"] = delivery.get("action")
    if delivery.get("reason") is not None:
        out["delivery_reason"] = delivery.get("reason")
    if send_summary.get("send_confirmed_count") is not None:
        out["send_confirmed_count"] = send_summary.get("send_confirmed_count")
    if send_summary.get("failure_count") is not None:
        out["failure_count"] = send_summary.get("failure_count")
    return out


def _gaps_from_session(*, row: dict[str, Any], snapshot: dict[str, Any], start_index: int) -> list[dict[str, Any]]:
    coverage = snapshot.get("coverage") if isinstance(snapshot.get("coverage"), dict) else {}
    gaps = coverage.get("gaps") if isinstance(coverage.get("gaps"), list) else []
    out: list[dict[str, Any]] = []
    for offset, gap in enumerate(gaps, start=start_index):
        if not isinstance(gap, dict):
            continue
        out.append(_gap_payload(gap, turn_id=_turn_id("session", row.get("session_id") or offset), gap_id=f"gap_{offset:03d}"))
    return out


def _gaps_from_context(context: dict[str, Any], *, start_index: int) -> list[dict[str, Any]]:
    followup = context.get("agent_loop_followup") if isinstance(context.get("agent_loop_followup"), dict) else {}
    gaps = followup.get("evidence_gaps") if isinstance(followup.get("evidence_gaps"), list) else []
    out: list[dict[str, Any]] = []
    for offset, gap in enumerate(gaps, start=start_index):
        if isinstance(gap, dict):
            out.append(_gap_payload(gap, turn_id=str(gap.get("turn_id") or "context"), gap_id=f"gap_{offset:03d}"))
    return out


def _gap_payload(gap: dict[str, Any], *, turn_id: str, gap_id: str) -> dict[str, Any]:
    suggested_tool = str(gap.get("suggested_tool") or gap.get("recoverable_by") or "").strip()
    suggested_tools = [suggested_tool] if suggested_tool else _string_list(gap.get("suggested_tools"))
    return _strip_empty(
        {
            "gap_id": gap_id,
            "turn_id": turn_id,
            "kind": str(gap.get("kind") or "evidence_gap"),
            "summary": _text_excerpt(_first_text(gap.get("summary"), gap.get("reason"), gap.get("missing")), 180),
            "suggested_tools": suggested_tools,
            "suggested_views": _string_list(gap.get("suggested_views")),
            "safe_slots": _safe_slots(gap.get("safe_slots") if isinstance(gap.get("safe_slots"), dict) else gap),
            "created_at": gap.get("created_at"),
        }
    )


def _pending_operations(value: Any) -> list[dict[str, Any]]:
    operations = value if isinstance(value, list) else []
    out: list[dict[str, Any]] = []
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        out.append(
            _strip_empty(
                {
                    "operation_id": str(operation.get("operation_id") or ""),
                    "operation_type": str(operation.get("operation_type") or ""),
                    "status": str(operation.get("status") or "previewed"),
                    "summary": _text_excerpt(operation.get("summary"), 180),
                    "created_at": operation.get("created_at"),
                    "expires_at": operation.get("expires_at"),
                    "safe_slots": _safe_slots(operation),
                }
            )
        )
    return out


def _projection_user_profile(value: Any) -> dict[str, Any]:
    profile = value if isinstance(value, dict) else {}
    trace = user_profile_trace(profile)
    if not profile.get("provided"):
        return trace
    content = _clip(profile.get("content"), 600)
    if content:
        trace["content"] = _redact_sensitive_text(content)
    return trace


def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in payload.items():
        safe_key = _canonical_slot_key(key)
        if safe_key not in SAFE_SLOT_KEYS:
            continue
        clipped = _safe_payload_value(value)
        if clipped not in (None, "", [], {}):
            out[safe_key] = clipped
    query = payload.get("query")
    if isinstance(query, dict):
        query_payload = _safe_payload(query)
        if query_payload:
            out["query"] = query_payload
    return _clip_payload(out)


def _safe_slots(payload: Any) -> dict[str, list[Any]]:
    if not isinstance(payload, dict):
        return {}
    out: dict[str, list[Any]] = {}
    for key, value in payload.items():
        safe_key = _canonical_slot_key(key)
        if safe_key in SAFE_SLOT_KEYS:
            _add_slot_value(out, safe_key, value)
        elif safe_key in _SAFE_SLOT_NESTED_KEYS and isinstance(value, dict):
            out = _merge_slots(out, _safe_slots(value))
    return out


def _tool_safe_slots(item: dict[str, Any]) -> dict[str, list[Any]]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    slots = _safe_slots(payload)
    tool_name = str(item.get("tool_name") or "").strip()
    if tool_name != "symbol_config_read":
        return slots
    summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
    evidence = item.get("evidence_summary") if isinstance(item.get("evidence_summary"), dict) else {}
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    source = _merged_read_setting_summary(payload=payload, summary=summary, evidence=evidence, data=data)
    for key in ("symbol", "market", "strategy", "setting_path", "setting_field"):
        if source.get(key) is not None:
            _add_slot_value(slots, key, source[key])
    return slots


def _merged_read_setting_summary(
    *,
    payload: dict[str, Any],
    summary: dict[str, Any],
    evidence: dict[str, Any],
    data: dict[str, Any],
) -> dict[str, Any]:
    strategy = _first_value(summary.get("strategy"), evidence.get("strategy"), data.get("strategy"), payload.get("strategy"))
    field = _first_value(summary.get("field"), evidence.get("field"), data.get("field"), payload.get("field"))
    path = _first_value(summary.get("path"), evidence.get("path"), data.get("path"))
    if path is None and strategy is not None and field is not None:
        field_text = str(field).strip()
        path = field_text if "." in field_text else f"{strategy}.{field_text}"
    setting_field = str(field).strip().split(".")[-1] if field is not None else None
    return {
        "symbol": _first_value(summary.get("canonical_symbol"), data.get("canonical_symbol"), payload.get("symbol")),
        "market": _first_value(summary.get("market"), evidence.get("market"), data.get("market"), payload.get("market")),
        "strategy": strategy,
        "setting_path": path,
        "setting_field": setting_field,
    }


def _canonical_slot_key(key: Any) -> str:
    text = str(key or "").strip()
    return _SAFE_SLOT_ALIASES.get(text, text)


def _add_slot_value(out: dict[str, list[Any]], key: str, value: Any) -> None:
    values = value if isinstance(value, list) else [value]
    bucket = out.setdefault(key, [])
    for item in values:
        if isinstance(item, dict):
            continue
        text = str(item).strip()
        if text in {"", "None"}:
            continue
        normalized: Any = item if isinstance(item, (int, float, bool)) else _clip(text, 240)
        if normalized not in bucket:
            bucket.append(normalized)


def _safe_payload_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _clip(value, 240) if isinstance(value, str) else value
    if isinstance(value, list):
        return [_safe_payload_value(item) for item in value[:20] if not isinstance(item, dict)]
    return None


def _clip_payload(payload: dict[str, Any]) -> dict[str, Any]:
    text = _compact_json(payload)
    if len(text) <= DEFAULT_MAX_PAYLOAD_CHARS:
        return payload
    out = dict(payload)
    out["_truncated"] = True
    while len(_compact_json(out)) > DEFAULT_MAX_PAYLOAD_CHARS and out:
        for key in sorted(out.keys(), reverse=True):
            if key != "_truncated":
                out.pop(key, None)
                break
        else:
            break
    return out


def _data_shape(item: dict[str, Any]) -> dict[str, Any]:
    tool_name = str(item.get("tool_name") or "").strip()
    summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
    evidence = item.get("evidence_summary") if isinstance(item.get("evidence_summary"), dict) else {}
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    out: dict[str, Any] = {}
    row_count = _first_value(summary.get("row_count"), evidence.get("row_count"), summary.get("count"), evidence.get("count"))
    if row_count is not None:
        out["row_count"] = row_count
    columns = _string_list(_first_value(summary.get("columns"), evidence.get("columns")))
    if columns:
        out["columns"] = columns[:30]
    views = _string_list(_first_value(summary.get("views_used"), evidence.get("views_used"), evidence.get("views")))
    if views:
        out["views_used"] = views[:20]
    if summary.get("truncated") is not None or evidence.get("truncated") is not None:
        out["truncated"] = bool(_first_value(summary.get("truncated"), evidence.get("truncated")))
    if tool_name == "symbol_config_read":
        setting = _merged_read_setting_summary(
            payload=item.get("payload") if isinstance(item.get("payload"), dict) else {},
            summary=summary,
            evidence=evidence,
            data=data,
        )
        if setting.get("setting_path"):
            out["kind"] = "single_symbol_setting"
            out["setting_path"] = setting["setting_path"]
        value = _first_value(summary.get("value"), data.get("value"))
        if value is not None:
            out["value_type"] = type(value).__name__
    return out


def _filter_evidence_refs(projection: dict[str, Any], evidence_refs: list[dict[str, Any]]) -> None:
    visible_ids: set[str] = set()
    for container_key in ("recent_turns", "recent_successful_tools"):
        for item in projection.get(container_key) or []:
            if isinstance(item, dict):
                visible_ids.update(str(ref) for ref in item.get("evidence_refs") or [] if str(ref).strip())
    projection["available_evidence_refs"] = [ref for ref in evidence_refs if str(ref.get("ref_id") or "") in visible_ids]


def _enforce_char_budget(projection: dict[str, Any], truncated_reasons: list[str]) -> None:
    max_chars = int(projection.get("budget", {}).get("max_chars") or DEFAULT_MAX_CHARS)
    all_refs = list(projection.get("available_evidence_refs") or [])
    while len(_compact_json(projection)) > max_chars:
        if projection.get("recent_turns"):
            projection["recent_turns"].pop()
        elif projection.get("recent_successful_tools"):
            projection["recent_successful_tools"].pop()
        elif projection.get("open_evidence_gaps"):
            projection["open_evidence_gaps"].pop()
        else:
            break
        if "char_budget" not in truncated_reasons:
            truncated_reasons.append("char_budget")
        _filter_evidence_refs(projection, all_refs)
    if truncated_reasons:
        projection["budget"]["truncated"] = True
        projection["budget"]["truncation_reason"] = ",".join(_unique_strings(truncated_reasons))
        projection["system_events"] = [
            {
                "schema_version": CONVERSATION_EVENT_SCHEMA_VERSION,
                "event_id": "system:projection-truncated",
                "event_type": "system_boundary",
                "summary": "Context projection was truncated by budget.",
                "reason": projection["budget"]["truncation_reason"],
            }
        ]


def _turn_id(prefix: str, value: Any) -> str:
    text = str(value or "").strip().replace(" ", "_")
    return f"{prefix}:{text or 'unknown'}"


def _sort_by_recency(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: _first_text(item.get("updated_at"), item.get("created_at")), reverse=True)


def _dedupe_by_id(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        item_id = str(item.get(key) or "").strip()
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        out.append(item)
    return out


def _dedupe_tools(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        signature = _compact_json(
            {
                "turn_id": item.get("turn_id"),
                "tool_name": item.get("tool_name"),
                "safe_payload": item.get("safe_payload"),
                "refs": item.get("evidence_refs"),
            }
        )
        if signature in seen:
            continue
        seen.add(signature)
        out.append(item)
    return out


def _merge_slots(left: dict[str, list[Any]], right: dict[str, list[Any]]) -> dict[str, list[Any]]:
    out = {key: list(value) for key, value in left.items()}
    for key, values in right.items():
        for value in values:
            _add_slot_value(out, key, value)
    return out


def _session_result_status(row: dict[str, Any], final_response: dict[str, Any]) -> str:
    status = str(final_response.get("status") or row.get("task_state") or "").strip()
    if status in {"done", "rendered", "synthesized", "complete", "completed"}:
        return "ok"
    if status in {"failed", "error"}:
        return "error"
    return status or "unknown"


def _strip_empty(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        values = value
    elif value is None:
        values = []
    else:
        values = [value]
    return _unique_strings([str(item) for item in values if str(item or "").strip()])


def _unique_strings(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _first_value(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _clip(value: Any, limit: int = DEFAULT_MAX_TEXT_EXCERPT_CHARS) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _text_excerpt(value: Any, limit: int = DEFAULT_MAX_TEXT_EXCERPT_CHARS) -> str:
    return _redact_sensitive_text(_clip(value, limit))


def _compact_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        return str(value)


def _redact_sensitive_text(value: str) -> str:
    lines: list[str] = []
    for line in str(value or "").splitlines():
        lowered = line.lower()
        if any(token in lowered for token in _SENSITIVE_TEXT_TOKENS):
            lines.append("[redacted sensitive line]")
        else:
            lines.append(line)
    return "\n".join(lines)


__all__ = [
    "CONTEXT_PROJECTION_SCHEMA_VERSION",
    "CONVERSATION_EVENT_SCHEMA_VERSION",
    "SAFE_SLOT_KEYS",
    "build_context_projection",
    "context_projection_trace",
]
