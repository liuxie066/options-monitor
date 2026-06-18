from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.application.assistant.audit import InboundAuditStore
from src.application.assistant.operation_store import InboundOperationStore
from src.application.assistant.contracts import AssistantRequest
from src.application.assistant.context_projection import build_context_projection, context_projection_trace
from src.application.assistant.session_store import AgentSessionStore
from src.application.assistant.user_profile import load_user_profile_context, user_profile_trace
from src.application.tool_allowlist import PURE_READ_TOOLS


CONVERSATION_FRAME_SCHEMA_VERSION = "om-conversation-frame-v1"
_FRAME_STACK_LIMIT = 3
_BUSINESS_READ_TOOL_PRIORITY: dict[str, int] = {
    "candidate_filter_explain": 10,
    "candidate_rank_explain": 15,
    "analysis_query": 20,
    "monthly_income_report": 25,
    "option_positions_read": 40,
    "symbol_config_read": 50,
    "close_advice_read": 55,
    "runtime_status": 70,
    "scheduler_status": 75,
    "healthcheck": 80,
}


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
    recent_sessions = []
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
        recent_sessions = AgentSessionStore(audit_store.path).list_recent(
            channel=normalized["channel"],
            sender_id=normalized["sender_id"],
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

    frame_stack = _conversation_frame_stack(
        session_rows=recent_sessions,
        recent_messages=recent_messages,
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
        "last_successful_read": _last_successful_read(recent_messages),
        "active_frame": frame_stack[0] if frame_stack else None,
        "frame_stack": frame_stack,
        "pending_operations": pending_operations,
        "user_profile": load_user_profile_context(user_profile_path),
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
    pending = context.get("pending_operations")
    frame = context.get("active_frame") if isinstance(context.get("active_frame"), dict) else None
    frames = context.get("frame_stack") if isinstance(context.get("frame_stack"), list) else []
    trace = {
        "provided": True,
        "window_messages": int(context.get("window_messages") or 0),
        "recent_count": len(recent) if isinstance(recent, list) else 0,
        "pending_count": len(pending) if isinstance(pending, list) else 0,
        "frame_count": len(frames),
        "active_frame": _frame_trace(frame),
        "user_profile": user_profile_trace(
            context.get("user_profile") if isinstance(context.get("user_profile"), dict) else None
        ),
    }
    projection_trace = context_projection_trace(
        context.get("context_projection") if isinstance(context.get("context_projection"), dict) else None
    )
    if projection_trace.get("provided"):
        trace["context_projection"] = projection_trace
    return trace


def _normalized_scope(request: AssistantRequest) -> dict[str, str]:
    channel = str(request.channel or "local").strip().lower() or "local"
    sender_id = str(request.sender_id or "").strip()
    conversation_id = str(request.conversation_id or "").strip() or f"{channel}:{sender_id}"
    return {"channel": channel, "sender_id": sender_id, "conversation_id": conversation_id}


def _conversation_frame_stack(
    *,
    session_rows: list[dict[str, Any]],
    recent_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for rank, row in enumerate(session_rows, start=1):
        frame = _session_row_frame(row, rank=rank)
        if frame:
            frames.append(frame)
    for rank, item in enumerate(reversed(recent_messages), start=len(frames) + 1):
        frame = _audit_item_frame(item, rank=rank)
        if frame:
            frames.append(frame)
    frames.sort(key=_frame_recency_key, reverse=True)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for frame in frames:
        signature = _frame_signature(frame)
        if signature in seen:
            continue
        seen.add(signature)
        frame = dict(frame)
        frame["rank"] = len(out) + 1
        out.append(frame)
        if len(out) >= _FRAME_STACK_LIMIT:
            break
    return out


def _frame_recency_key(frame: dict[str, Any]) -> tuple[str, int]:
    timestamp = _first_text(frame.get("updated_at"), frame.get("created_at"))
    source_priority = 1 if str(frame.get("source") or "") == "agent_session" else 0
    return timestamp, source_priority


def _session_row_frame(row: dict[str, Any], *, rank: int) -> dict[str, Any] | None:
    snapshot = row.get("snapshot") if isinstance(row.get("snapshot"), dict) else {}
    if not snapshot:
        return None
    transcript = snapshot.get("tool_transcript") if isinstance(snapshot.get("tool_transcript"), list) else []
    read = _best_business_read_from_transcript(transcript)
    if read is None:
        return None
    task_contract = snapshot.get("task_contract") if isinstance(snapshot.get("task_contract"), dict) else {}
    answer_trace = snapshot.get("answer_trace") if isinstance(snapshot.get("answer_trace"), dict) else {}
    final_response = answer_trace.get("final_response") if isinstance(answer_trace.get("final_response"), dict) else {}
    synthesis = answer_trace.get("synthesis") if isinstance(answer_trace.get("synthesis"), dict) else {}
    response_text = _first_text(
        final_response.get("response_text"),
        synthesis.get("response_text"),
        snapshot.get("goal"),
    )
    return _frame_from_read(
        tool_name=str(read.get("tool_name") or ""),
        payload=read.get("payload") if isinstance(read.get("payload"), dict) else {},
        source="agent_session",
        rank=rank,
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        intent_name=_first_text(
            _first_list_item(task_contract.get("intent_families")),
            str(read.get("tool_name") or ""),
        ),
        task_contract=task_contract,
        response_text=response_text,
        confidence=0.9,
    )


def _audit_item_frame(item: dict[str, Any], *, rank: int) -> dict[str, Any] | None:
    if item.get("result_ok") is not True:
        return None
    tool_name = str(item.get("tool_name") or "").strip()
    if tool_name not in PURE_READ_TOOLS:
        return None
    return _frame_from_read(
        tool_name=tool_name,
        payload=item.get("tool_payload") if isinstance(item.get("tool_payload"), dict) else {},
        source="inbound_audit",
        rank=rank,
        created_at=item.get("created_at"),
        updated_at=None,
        intent_name=str(item.get("intent_name") or ""),
        task_contract={},
        response_text=_first_text(item.get("response_text"), item.get("raw_text")),
        confidence=0.7,
    )


def _best_business_read_from_transcript(transcript: list[Any]) -> dict[str, Any] | None:
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for index, item in enumerate(transcript):
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        tool_name = str(item.get("tool_name") or "").strip()
        priority = _BUSINESS_READ_TOOL_PRIORITY.get(tool_name)
        if priority is None:
            continue
        candidates.append((priority, -index, item))
    if not candidates:
        return None
    candidates.sort(key=lambda value: (value[0], value[1]))
    return candidates[0][2]


def _frame_from_read(
    *,
    tool_name: str,
    payload: dict[str, Any],
    source: str,
    rank: int,
    created_at: Any,
    updated_at: Any,
    intent_name: str,
    task_contract: dict[str, Any],
    response_text: str,
    confidence: float,
) -> dict[str, Any] | None:
    domain, task_mode, metric_namespace, evidence_source = _read_semantics(
        tool_name=tool_name,
        payload=payload,
        task_contract=task_contract,
        response_text=response_text,
    )
    if not domain:
        return None
    frame: dict[str, Any] = {
        "schema_version": CONVERSATION_FRAME_SCHEMA_VERSION,
        "source": source,
        "rank": int(rank),
        "domain": domain,
        "task_mode": task_mode,
        "tool_name": tool_name,
        "intent_name": intent_name or tool_name,
        "metric_namespace": metric_namespace,
        "key_terms": _key_terms(
            domain=domain,
            metric_namespace=metric_namespace,
            response_text=response_text,
        ),
        "tool_payload": _safe_tool_payload_from_dict(payload),
        "evidence_source": evidence_source,
        "confidence": float(confidence),
    }
    if created_at:
        frame["created_at"] = created_at
    if updated_at:
        frame["updated_at"] = updated_at
    excerpt = _clip(response_text, 240)
    if excerpt:
        frame["response_excerpt"] = excerpt
    return frame


def _read_semantics(
    *,
    tool_name: str,
    payload: dict[str, Any],
    task_contract: dict[str, Any],
    response_text: str,
) -> tuple[str, str, str, str]:
    contract_domain = str(task_contract.get("domain") or "").strip()
    contract_mode = str(task_contract.get("task_mode") or "").strip()
    if tool_name in {"candidate_filter_explain", "candidate_rank_explain"}:
        return "candidate", contract_mode or "diagnose", "candidate_option_metrics", "OM candidate filter trace"
    if tool_name == "monthly_income_report":
        return "income", contract_mode or "analyze", "account_income_metrics", "OM local ledger income report"
    if tool_name == "analysis_query":
        inferred = _infer_analysis_namespace(payload=payload, response_text=response_text)
        if inferred == "candidate":
            return "candidate", contract_mode or "analyze", "candidate_option_metrics", "OM analysis workspace candidate diagnostics"
        if inferred == "income":
            return "income", contract_mode or "analyze", "account_income_metrics", "OM analysis workspace account income"
        if contract_domain in {"candidate", "income"}:
            namespace = "candidate_option_metrics" if contract_domain == "candidate" else "account_income_metrics"
            source = "OM analysis workspace candidate diagnostics" if contract_domain == "candidate" else "OM analysis workspace account income"
            return contract_domain, contract_mode or "analyze", namespace, source
        return "analysis", contract_mode or "analyze", "", "OM analysis workspace"
    if tool_name == "option_positions_read":
        return "position", contract_mode or "summarize", "", "OM local option position ledger"
    if tool_name == "symbol_config_read":
        return "config", contract_mode or "explain", "", "selected runtime config"
    if tool_name == "close_advice_read":
        return "strategy", contract_mode or "diagnose", "", "OM close-advice snapshot"
    if tool_name in {"runtime_status", "scheduler_status", "healthcheck"}:
        return "runtime", contract_mode or "diagnose", "", "OM runtime diagnostics"
    return "", "", "", ""


def _infer_analysis_namespace(*, payload: dict[str, Any], response_text: str) -> str:
    haystack = _compact_text(
        _compact_json(payload),
        response_text,
    )
    if any(
        token in haystack
        for token in (
            "account_monthly_performance",
            "account_monthly_income_components",
            "monthly_income_",
            "net_income_cny",
            "income_cashflow_ex_assignment_stock",
        )
    ):
        return "income"
    if any(
        token in haystack
        for token in (
            "candidate_filter_diagnostics",
            "candidate_filter_explain",
            "metrics_net_income_non_positive",
            "净收入非正",
            "候选",
            "过滤",
        )
    ):
        return "candidate"
    return ""


def _key_terms(*, domain: str, metric_namespace: str, response_text: str) -> list[str]:
    terms: list[str] = []
    if domain == "candidate":
        terms.extend(["候选", "过滤"])
    if domain == "income":
        terms.extend(["收益", "现金流"])
    if metric_namespace == "candidate_option_metrics":
        terms.extend(["净收入", "net_income"])
        if "净收入非正" in response_text or "net_income_non_positive" in response_text:
            terms.append("净收入非正")
        if "metrics_net_income_non_positive" in response_text:
            terms.append("metrics_net_income_non_positive")
    elif metric_namespace == "account_income_metrics":
        terms.extend(["净收入", "net_income_cny"])
    return _unique_strings(terms)


def _frame_signature(frame: dict[str, Any]) -> str:
    payload = frame.get("tool_payload") if isinstance(frame.get("tool_payload"), dict) else {}
    return _compact_json(
        {
            "domain": frame.get("domain"),
            "tool_name": frame.get("tool_name"),
            "metric_namespace": frame.get("metric_namespace"),
            "payload": payload,
        }
    )


def _frame_trace(frame: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(frame, dict):
        return {}
    return {
        "domain": frame.get("domain"),
        "tool_name": frame.get("tool_name"),
        "metric_namespace": frame.get("metric_namespace"),
        "confidence": frame.get("confidence"),
    }


def _audit_context_item(row: dict[str, Any]) -> dict[str, Any]:
    raw_tool_name = row.get("tool_name")
    tool_name = raw_tool_name
    intent_name = row.get("intent_name")
    tool_payload = _safe_tool_payload(row.get("tool_payload_json"))
    response_text = ""
    if str(raw_tool_name or "").strip() == "assistant.tool_plan":
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
        return None
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


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _first_list_item(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    for item in value:
        text = str(item or "").strip()
        if text:
            return text
    return ""


def _compact_text(*values: Any) -> str:
    text = " ".join(str(value or "") for value in values if str(value or "").strip()).lower()
    return f"{text} {_strip_whitespace(text)}"


def _strip_whitespace(value: str) -> str:
    return "".join(str(value or "").split())


def _compact_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        return str(value)


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
