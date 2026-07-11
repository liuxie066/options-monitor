from __future__ import annotations

from datetime import date
from typing import Any, Callable

from src.application.assistant.settings import AssistantSettings
from src.application.assistant.contracts import AssistantRequest, AssistantTurnResult
from src.application.assistant.audit import InboundAuditStore
from src.application.assistant.router import ExecuteToolFn, handle_assistant_request
from src.application.assistant.turn_result import (
    assistant_turn_result_from_response_payload,
    with_assistant_turn_result,
)
from src.application.tool_execution import execute_tool


def handle_assistant_turn(
    request: AssistantRequest,
    *,
    audit_store: InboundAuditStore | None = None,
    execute_tool_fn: ExecuteToolFn = execute_tool,
    allowed_senders: str | None = None,
    now_fn: Callable[[], date] | None = None,
    settings: AssistantSettings | None = None,
) -> AssistantTurnResult:
    runtime_settings = settings or AssistantSettings()
    if not runtime_settings.enabled:
        return AssistantTurnResult(
            response_text="Inbound Assistant 已禁用。",
            render_route="disabled",
            ok=False,
            status="disabled",
            tool_name="assistant.handle",
            error={"code": "ASSISTANT_DISABLED", "message": "Inbound Assistant is disabled"},
            trace={"route": "disabled"},
            meta={"assistant": {"enabled": False, "route": "disabled"}},
        )
    response = _run_assistant_turn_response(
        request,
        audit_store=audit_store,
        execute_tool_fn=execute_tool_fn,
        allowed_senders=allowed_senders,
        now_fn=now_fn,
        settings=runtime_settings,
    )
    return assistant_turn_result_from_response_payload(response)


def _run_assistant_turn_response(
    request: AssistantRequest,
    *,
    audit_store: InboundAuditStore | None = None,
    execute_tool_fn: ExecuteToolFn = execute_tool,
    allowed_senders: str | None = None,
    now_fn: Callable[[], date] | None = None,
    settings: AssistantSettings | None = None,
) -> dict[str, Any]:
    runtime_settings = settings or AssistantSettings()
    request = _request_with_default_market_scope(request, runtime_settings)
    store = audit_store or InboundAuditStore(request.audit_db)
    response = handle_assistant_request(
        request,
        audit_store=store,
        execute_tool_fn=execute_tool_fn,
        allowed_senders=allowed_senders,
        now_fn=now_fn,
    )
    route = _response_route(response)
    response = _with_assistant_meta(
        response,
        route=route,
        settings=runtime_settings,
    )
    response = with_assistant_turn_result(response, route=route)
    _update_audit_response(store=store, response=response)
    return response


def _request_with_default_market_scope(request: AssistantRequest, settings: AssistantSettings) -> AssistantRequest:
    if request.config_path or request.config_key:
        return request
    scope = str(settings.default_market_scope or "").strip().lower()
    if scope not in {"us", "hk"}:
        return request
    return AssistantRequest(
        text=request.text,
        sender_id=request.sender_id,
        channel=request.channel,
        message_id=request.message_id,
        conversation_id=request.conversation_id,
        config_key=scope,
        config_path=request.config_path,
        audit_db=request.audit_db,
        assistant_config_path=request.assistant_config_path,
        reply_context=dict(request.reply_context) if isinstance(request.reply_context, dict) else None,
    )


def _update_audit_response(*, store: InboundAuditStore, response: dict[str, Any]) -> None:
    meta = response.get("meta")
    if isinstance(meta, dict) and bool(meta.get("idempotent_replay")):
        return
    data = response.get("data")
    command_id = data.get("command_id") if isinstance(data, dict) else None
    if not command_id:
        return
    store.update_response(command_id=str(command_id), response=response)


def _with_assistant_meta(
    response: dict[str, Any],
    *,
    route: str,
    settings: AssistantSettings,
) -> dict[str, Any]:
    meta_raw = response.get("meta")
    meta = dict(meta_raw) if isinstance(meta_raw, dict) else {}
    assistant_meta = {
        "enabled": bool(settings.enabled),
        "copilot": settings.copilot.public_payload(),
        "route": route,
    }
    assistant_meta["decision"] = {
        "route": route,
        "source": "copilot" if route == "copilot" else "deterministic_control",
    }
    meta["assistant"] = assistant_meta
    return {**response, "meta": meta}


def _response_route(response: dict[str, Any]) -> str:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    decision = data.get("decision") if isinstance(data.get("decision"), dict) else {}
    if str(decision.get("reason") or "") == "copilot_freeform":
        return "copilot"
    return "deterministic_control"
