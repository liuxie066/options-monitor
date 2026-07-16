from __future__ import annotations

from typing import Any

from src.application.assistant.contracts import AssistantTurnResult


def with_assistant_turn_result(response: dict[str, Any], *, route: str) -> dict[str, Any]:
    meta = dict(response.get("meta") or {}) if isinstance(response.get("meta"), dict) else {}
    assistant = dict(meta.get("assistant") or {}) if isinstance(meta.get("assistant"), dict) else {}
    turn_result = assistant_turn_result_from_response(response, route=route)
    assistant["turn_result"] = turn_result.public_payload()
    meta["assistant"] = assistant
    return {**response, "meta": meta}


def assistant_turn_result_from_response_payload(response: dict[str, Any]) -> AssistantTurnResult:
    meta = response.get("meta") if isinstance(response.get("meta"), dict) else {}
    return assistant_turn_result_from_response(
        response,
        route=str((meta.get("assistant") if isinstance(meta.get("assistant"), dict) else {}).get("route") or ""),
    )


def assistant_turn_result_from_response(
    response: dict[str, Any],
    *,
    route: str,
) -> AssistantTurnResult:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    meta = response.get("meta") if isinstance(response.get("meta"), dict) else {}
    trace = {"route": route}
    copilot_trace = copilot_trace_from_response_data(data)
    if copilot_trace:
        trace["copilot"] = copilot_trace
    copilot_events = copilot_events_from_response_data(data)
    if copilot_events:
        trace["copilot_events"] = copilot_events
    return AssistantTurnResult(
        response_text=str(data.get("response_text") or ""),
        render_route=_turn_render_route(response=response, route=route, data=data),
        ok=bool(response.get("ok", False)),
        status=str(data.get("status") or ("ok" if bool(response.get("ok", False)) else "error")),
        tool_name=str(response.get("tool_name") or "assistant.handle"),
        error=response.get("error") if isinstance(response.get("error"), dict) else None,
        permission_request=data.get("permission_request") if isinstance(data.get("permission_request"), dict) else None,
        operation_id=_turn_identifier(data, "operation_id", "resolved_operation_id"),
        command_id=_turn_identifier(data, "command_id"),
        trace=trace,
        data=dict(data),
        meta=dict(meta),
    )


def copilot_trace_from_response_data(data: dict[str, Any]) -> dict[str, Any]:
    result_data = _control_result_data(data)
    copilot = result_data.get("copilot") if isinstance(result_data.get("copilot"), dict) else {}
    if not copilot:
        return {}
    decision_trace = copilot.get("decision_trace") if isinstance(copilot.get("decision_trace"), dict) else {}
    trace = {
        "status": _text(copilot.get("status")),
        "scene": _text(decision_trace.get("selected_scene")),
        "run_id": _text(copilot.get("run_id")),
        "contract_id": _text(copilot.get("contract_id")),
        "channel_gate": _text(decision_trace.get("channel_gate")),
    }
    return {key: value for key, value in trace.items() if value}


def copilot_events_from_response_data(data: dict[str, Any]) -> dict[str, Any]:
    result_data = _control_result_data(data)
    summary = result_data.get("copilot_events") if isinstance(result_data.get("copilot_events"), dict) else {}
    if not summary:
        return {}
    event_count = _int(summary.get("event_count"))
    timeline = summary.get("timeline") if isinstance(summary.get("timeline"), list) else []
    out = {
        "event_count": event_count,
        "event_types": _text_list(summary.get("event_types")),
        "final_status": _text(summary.get("final_status")),
        "observation_refs": _text_list(summary.get("observation_refs")),
        "tool_names": _text_list(summary.get("tool_names")),
        "failure_reasons": _text_list(summary.get("failure_reasons")),
        "timeline_count": len(timeline),
    }
    return {key: value for key, value in out.items() if value not in (None, "", [], 0)}


def _control_result_data(data: dict[str, Any]) -> dict[str, Any]:
    control = data.get("control") if isinstance(data.get("control"), dict) else {}
    result = control.get("result") if isinstance(control.get("result"), dict) else {}
    result_data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return dict(result_data)


def _turn_render_route(*, response: dict[str, Any], route: str, data: dict[str, Any]) -> str:
    if not bool(response.get("ok", False)):
        return "error"
    if isinstance(data.get("permission_request"), dict):
        return "preview_request"
    status = str(data.get("status") or "").strip()
    if status in {"needs_user_input", "clarify", "clarification"}:
        return "clarification"
    return route or "unknown"


def _turn_identifier(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return None


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = _text(item)
        if text and text not in out:
            out.append(text)
    return out


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
