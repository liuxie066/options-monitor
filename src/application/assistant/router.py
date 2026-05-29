from __future__ import annotations

import json
from datetime import date
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response, mask_path
from src.application.assistant.action import ExecuteToolFn, perform_action
from src.application.assistant.audit import InboundAuditStore, build_command_id, utc_now_iso
from src.application.assistant.contracts import (
    ActionResult,
    AssistantRequest,
    ObservationResponse,
    PerceptionResult,
    ReasoningResolution,
)
from src.application.assistant.observation import build_observation
from src.application.assistant.operation_store import InboundOperationStore
from src.application.assistant.parser import parse_inbound_text
from src.application.assistant.policy import enforce_sender_allowed
from src.application.assistant.reasoning import resolve_reasoning
from src.application.assistant.renderer import render_inbound_text
from src.application.tool_execution import execute_tool


ParsePerceptionFn = Callable[[str, Callable[[], date] | None], PerceptionResult]


def handle_assistant_request(
    request: AssistantRequest,
    *,
    audit_store: InboundAuditStore | None = None,
    execute_tool_fn: ExecuteToolFn = execute_tool,
    allowed_senders: str | None = None,
    now_fn: Callable[[], date] | None = None,
    parse_perception_fn: ParsePerceptionFn | None = None,
) -> dict[str, Any]:
    normalized_request = _normalize_request(request)
    store = audit_store or InboundAuditStore(normalized_request.audit_db)
    command_id = build_command_id(
        channel=normalized_request.channel,
        sender_id=normalized_request.sender_id,
        message_id=normalized_request.message_id,
        text=normalized_request.text,
    )

    try:
        existing = store.find_by_message(
            channel=normalized_request.channel,
            message_id=normalized_request.message_id,
            command_id=command_id,
        )
    except AgentToolError as err:
        return _error_response(command_id=command_id, request=normalized_request, err=err, audit_db=store.path)
    if existing is not None:
        if str(existing.get("sender_id") or "") != normalized_request.sender_id:
            store.mark_duplicate(
                command_id=str(existing.get("command_id") or ""),
                sender_id=normalized_request.sender_id,
                decision="sender_conflict",
            )
            return _error_response(
                command_id=command_id,
                request=normalized_request,
                err=AgentToolError(
                    code="PERMISSION_DENIED",
                    message="message_id was already used by a different sender",
                ),
            )
        store.mark_duplicate(
            command_id=str(existing.get("command_id") or ""),
            sender_id=normalized_request.sender_id,
        )
        return _duplicate_response(existing)

    created_at = utc_now_iso()
    perception: PerceptionResult | None = None
    resolution: ReasoningResolution | None = None
    action: ActionResult | None = None
    observation: ObservationResponse | None = None
    response: dict[str, Any]
    decision = "unknown"
    error_code: str | None = None

    try:
        sender_decision = enforce_sender_allowed(
            channel=normalized_request.channel,
            sender_id=normalized_request.sender_id,
            allowed_senders=allowed_senders,
        )
        perception = _parse_perception(normalized_request.text, now_fn=now_fn, parse_perception_fn=parse_perception_fn)
        resolution = resolve_reasoning(perception, request=normalized_request)
        action = perform_action(
            perception=perception,
            resolution=resolution,
            request=normalized_request,
            command_id=command_id,
            operation_store=InboundOperationStore(store.path),
            execute_tool_fn=execute_tool_fn,
        )
        observation = build_observation(perception=perception, resolution=resolution, action=action)
        response = _success_response(
            command_id=command_id,
            request=normalized_request,
            perception=perception,
            resolution=resolution,
            action=action,
            observation=observation,
            sender_decision=sender_decision.public_payload(),
            audit_db=store.path,
        )
        decision = _decision_for_resolution(resolution, action)
    except AgentToolError as err:
        error_code = err.code
        response = _error_response(command_id=command_id, request=normalized_request, err=err, audit_db=store.path)
        decision = _decision_for_error(err)

    return _record_and_return(
        store=store,
        request=normalized_request,
        command_id=command_id,
        created_at=created_at,
        perception=perception,
        resolution=resolution,
        action=action,
        observation=observation,
        decision=decision,
        response=response,
        error_code=error_code,
    )


def _parse_perception(
    text: str,
    *,
    now_fn: Callable[[], date] | None,
    parse_perception_fn: ParsePerceptionFn | None,
) -> PerceptionResult:
    if parse_perception_fn is not None:
        return parse_perception_fn(text, now_fn)
    return parse_inbound_text(text, now_fn=now_fn)


def _success_response(
    *,
    command_id: str,
    request: AssistantRequest,
    perception: PerceptionResult,
    resolution: ReasoningResolution,
    action: ActionResult,
    observation: ObservationResponse,
    sender_decision: dict[str, Any],
    audit_db: Any,
) -> dict[str, Any]:
    result = dict(action.result or {})
    operation_data = dict(result.get("data") or {}) if action.action_kind == "operation" else {}
    meta = dict(result.get("meta") or {}) if action.action_kind == "operation" else {}
    meta["audit_db"] = mask_path(audit_db)
    data: dict[str, Any] = {
        "command_id": command_id,
        "request": request.public_payload(),
        "perception": perception.public_payload(),
        "reasoning": resolution.public_payload(),
        "action": action.public_payload(),
        "observation": observation.public_payload(),
        "decision": {
            "allowed": True,
            "reason": resolution.reason,
            "sender": sender_decision,
        },
        "response_text": observation.response_text,
    }
    if operation_data:
        data.update(operation_data)
        data["response_text"] = observation.response_text
    if action.action_kind == "pending" and isinstance(action.result, dict):
        data.update(action.result)
    if action.action_kind == "tool":
        data["tool_call"] = resolution.tool_call.public_payload() if resolution.tool_call else None
        data["tool_result"] = action.result or {}
        tool_decision = result.get("_tool_decision")
        if isinstance(tool_decision, dict):
            data["decision"] = {**tool_decision, "sender": sender_decision}
    return build_response(
        tool_name=str(result.get("tool_name") or action.tool_name or "inbound.handle"),
        ok=bool(observation.ok),
        data=data,
        error=action.error if not bool(observation.ok) else None,
        warnings=result.get("warnings") if action.action_kind == "operation" else None,
        meta=meta,
    )


def _record_and_return(
    *,
    store: InboundAuditStore,
    request: AssistantRequest,
    command_id: str,
    created_at: str,
    perception: PerceptionResult | None,
    resolution: ReasoningResolution | None,
    action: ActionResult | None,
    observation: ObservationResponse | None,
    decision: str,
    response: dict[str, Any],
    error_code: str | None = None,
) -> dict[str, Any]:
    call = resolution.tool_call if resolution else None
    store.record_result(
        {
            "command_id": command_id,
            "channel": request.channel,
            "sender_id": request.sender_id,
            "conversation_id": request.conversation_id,
            "message_id": request.message_id,
            "raw_text": request.text,
            "parser": perception.source if perception else None,
            "intent_name": perception.intent_name if perception else None,
            "tool_name": call.tool_name if call else None,
            "tool_payload": call.payload if call else None,
            "perception": perception.public_payload() if perception else None,
            "reasoning": resolution.public_payload() if resolution else None,
            "action": action.public_payload() if action else None,
            "observation": observation.public_payload() if observation else None,
            "decision": decision,
            "result_ok": bool(response.get("ok", False)),
            "error_code": error_code or _response_error_code(response),
            "response": response,
            "created_at": created_at,
            "finished_at": utc_now_iso(),
        }
    )
    return response


def _duplicate_response(existing: dict[str, Any]) -> dict[str, Any]:
    raw = str(existing.get("response_json") or "{}")
    try:
        response = json.loads(raw)
    except Exception:
        response = build_response(
            tool_name="inbound.handle",
            ok=False,
            error=build_error_payload(
                AgentToolError(
                    code="INTERNAL_ERROR",
                    message="failed to load prior inbound response for duplicate message",
                )
            ),
        )
    if isinstance(response, dict):
        meta = dict(response.get("meta") or {})
        meta["idempotent_replay"] = True
        meta["original_command_id"] = existing.get("command_id")
        response["meta"] = meta
    return response


def _error_response(
    *,
    command_id: str,
    request: AssistantRequest,
    err: AgentToolError,
    audit_db: Any | None = None,
) -> dict[str, Any]:
    error = build_error_payload(err)
    return build_response(
        tool_name="inbound.handle",
        ok=False,
        data={
            "command_id": command_id,
            "request": request.public_payload(),
            "response_text": render_inbound_text(intent=None, tool_result=None, error=error),
        },
        error=error,
        meta={"audit_db": mask_path(audit_db)} if audit_db is not None else {},
    )


def _response_error_code(response: dict[str, Any]) -> str | None:
    error = response.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or "") or None
    return None


def _decision_for_resolution(resolution: ReasoningResolution, action: ActionResult) -> str:
    if resolution.status == "unsupported":
        return "unsupported"
    if resolution.status == "preview_required":
        return "preview"
    if not action.ok:
        return "failed"
    return "allowed"


def _decision_for_error(err: AgentToolError) -> str:
    if err.code == "PERMISSION_DENIED":
        return "denied"
    if err.code == "NEEDS_CLARIFICATION":
        return "needs_clarification"
    return "failed"


def _normalize_request(request: AssistantRequest) -> AssistantRequest:
    channel = str(request.channel or "local").strip().lower() or "local"
    sender_id = str(request.sender_id or "").strip()
    conversation_id = str(request.conversation_id or "").strip() or f"{channel}:{sender_id}"
    return AssistantRequest(
        text=str(request.text or "").strip(),
        sender_id=sender_id,
        channel=channel,
        message_id=str(request.message_id).strip() if request.message_id is not None and str(request.message_id).strip() else None,
        conversation_id=conversation_id,
        config_key=str(request.config_key or "").strip().lower() or None,
        config_path=str(request.config_path).strip() if request.config_path is not None and str(request.config_path).strip() else None,
        audit_db=str(request.audit_db).strip() if request.audit_db is not None and str(request.audit_db).strip() else None,
        assistant_config_path=str(request.assistant_config_path).strip() if request.assistant_config_path is not None and str(request.assistant_config_path).strip() else None,
    )


__all__ = ["ExecuteToolFn", "ParsePerceptionFn", "handle_assistant_request"]
