from __future__ import annotations

from typing import Any

from src.application.assistant.contracts import AssistantTurnResult


def with_assistant_turn_result(response: dict[str, Any], *, route: str) -> dict[str, Any]:
    meta = dict(response.get("meta") or {}) if isinstance(response.get("meta"), dict) else {}
    assistant = dict(meta.get("assistant") or {}) if isinstance(meta.get("assistant"), dict) else {}
    turn_result = assistant_turn_result_from_response(response, route=route, assistant_meta=assistant)
    assistant["turn_result"] = turn_result.public_payload()
    meta["assistant"] = assistant
    return {**response, "meta": meta}


def assistant_turn_result_from_response_payload(response: dict[str, Any]) -> AssistantTurnResult:
    meta = response.get("meta") if isinstance(response.get("meta"), dict) else {}
    assistant = meta.get("assistant") if isinstance(meta.get("assistant"), dict) else {}
    return assistant_turn_result_from_response(
        response,
        route=str(assistant.get("route") or ""),
        assistant_meta=dict(assistant),
    )


def assistant_turn_result_from_response(
    response: dict[str, Any],
    *,
    route: str,
    assistant_meta: dict[str, Any],
) -> AssistantTurnResult:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    meta = response.get("meta") if isinstance(response.get("meta"), dict) else {}
    llm = assistant_meta.get("llm") if isinstance(assistant_meta.get("llm"), dict) else {}
    agent_loop = llm.get("agent_loop") if isinstance(llm.get("agent_loop"), dict) else {}
    result_data = _action_result_data(data)
    return AssistantTurnResult(
        response_text=str(data.get("response_text") or ""),
        render_route=_turn_render_route(response=response, route=route, data=data, agent_loop=agent_loop),
        ok=bool(response.get("ok", False)),
        status=str(data.get("status") or ("ok" if bool(response.get("ok", False)) else "error")),
        tool_name=str(response.get("tool_name") or "assistant.handle"),
        error=response.get("error") if isinstance(response.get("error"), dict) else None,
        permission_request=data.get("permission_request") if isinstance(data.get("permission_request"), dict) else None,
        operation_id=_turn_identifier(data, "operation_id", "resolved_operation_id"),
        command_id=_turn_identifier(data, "command_id"),
        tool_calls=tuple(_turn_tool_calls(agent_loop)),
        evidence=result_data.get("evidence_bundle") if isinstance(result_data.get("evidence_bundle"), dict) else {},
        trace=_turn_trace(route=route, agent_loop=agent_loop),
        data=dict(data),
        meta=dict(meta),
    )


def _action_result_data(data: dict[str, Any]) -> dict[str, Any]:
    action = data.get("action") if isinstance(data.get("action"), dict) else {}
    result = action.get("result") if isinstance(action.get("result"), dict) else {}
    result_data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return dict(result_data)


def _turn_render_route(*, response: dict[str, Any], route: str, data: dict[str, Any], agent_loop: dict[str, Any]) -> str:
    if not bool(response.get("ok", False)):
        return "error"
    if isinstance(data.get("permission_request"), dict):
        return "preview_request"
    status = str(data.get("status") or "").strip()
    if status in {"needs_user_input", "clarify", "clarification"}:
        return "clarification"
    answer_route = str(agent_loop.get("answer_route") or "").strip()
    if answer_route in {"canonical_renderer", "user_fallback"}:
        return "canonical_renderer"
    if answer_route in {"llm_from_evidence", "model_final_answer"}:
        return "llm_verified"
    if answer_route == "copilot_answer":
        return "copilot_answer"
    if answer_route == "preview_lifecycle":
        return "preview_request"
    if answer_route == "clarification_request":
        return "clarification"
    final_response = agent_loop.get("final_response") if isinstance(agent_loop.get("final_response"), dict) else {}
    if bool(final_response.get("canonical_renderer_required")):
        return "canonical_renderer"
    if bool(final_response.get("llm_may_summarize")):
        return "llm_verified"
    return route or "router"


def _turn_identifier(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return None


def _turn_tool_calls(agent_loop: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in agent_loop.get("tool_events") or []:
        if not isinstance(item, dict):
            continue
        tool_name = str(item.get("tool_name") or "").strip()
        if not tool_name or item.get("phase") not in {"tool_result", "observe_tool_result", "model_tool_call"}:
            continue
        out.append({"tool_name": tool_name, "phase": str(item.get("phase") or "")})
    return out


def _turn_trace(*, route: str, agent_loop: dict[str, Any]) -> dict[str, Any]:
    trace = {"route": route}
    final_response = agent_loop.get("final_response") if isinstance(agent_loop.get("final_response"), dict) else {}
    if final_response:
        trace["final_response"] = dict(final_response)
    if agent_loop.get("answer_route"):
        trace["answer_route"] = str(agent_loop.get("answer_route") or "")
    if agent_loop.get("tool_calls_used") is not None:
        trace["tool_calls_used"] = agent_loop.get("tool_calls_used")
    return trace
