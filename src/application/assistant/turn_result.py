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
    result_data = _action_result_data(data)
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
        tool_calls=(),
        evidence=result_data.get("evidence_bundle") if isinstance(result_data.get("evidence_bundle"), dict) else {},
        trace={"route": route},
        data=dict(data),
        meta=dict(meta),
    )


def _action_result_data(data: dict[str, Any]) -> dict[str, Any]:
    action = data.get("action") if isinstance(data.get("action"), dict) else {}
    result = action.get("result") if isinstance(action.get("result"), dict) else {}
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
    return route or "router"


def _turn_identifier(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return None
