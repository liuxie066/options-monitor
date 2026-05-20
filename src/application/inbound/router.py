from __future__ import annotations

import json
from datetime import date
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response, mask_path
from src.application.inbound.audit import InboundAuditStore, build_command_id, utc_now_iso
from src.application.inbound.contracts import InboundIntent, InboundRequest, InboundToolCall
from src.application.inbound.manual_trade_operations import (
    handle_manual_trade_operation,
    is_manual_trade_operation_intent,
)
from src.application.inbound.operation_store import InboundOperationStore
from src.application.inbound.parser import parse_inbound_text
from src.application.inbound.policy import enforce_sender_allowed, enforce_tool_allowed
from src.application.inbound.renderer import HELP_TEXT, render_inbound_text, render_pending_operations
from src.application.inbound.symbol_operations import handle_symbol_operation, is_symbol_operation_intent
from src.application.tool_execution import execute_tool


ExecuteToolFn = Callable[[str, dict[str, Any]], dict[str, Any]]


def handle_inbound_request(
    request: InboundRequest,
    *,
    audit_store: InboundAuditStore | None = None,
    execute_tool_fn: ExecuteToolFn = execute_tool,
    allowed_senders: str | None = None,
    now_fn: Callable[[], date] | None = None,
) -> dict[str, Any]:
    normalized_request = _normalize_request(request)
    store = audit_store or InboundAuditStore(normalized_request.audit_db)
    command_id = build_command_id(
        channel=normalized_request.channel,
        sender_id=normalized_request.sender_id,
        message_id=normalized_request.message_id,
        text=normalized_request.text,
    )

    existing = store.find_by_message(
        channel=normalized_request.channel,
        message_id=normalized_request.message_id,
        command_id=command_id,
    )
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
    intent: InboundIntent | None = None
    call: InboundToolCall | None = None
    response: dict[str, Any]
    decision = "unknown"
    error_code: str | None = None

    try:
        sender_decision = enforce_sender_allowed(
            channel=normalized_request.channel,
            sender_id=normalized_request.sender_id,
            allowed_senders=allowed_senders,
        )
        intent = parse_inbound_text(normalized_request.text, now_fn=now_fn)
        if intent.name == "help":
            response = build_response(
                tool_name="inbound.handle",
                ok=True,
                data={
                    "command_id": command_id,
                    "request": normalized_request.public_payload(),
                    "intent": intent.public_payload(),
                    "decision": {
                        "allowed": True,
                        "reason": "help",
                        "sender": sender_decision.public_payload(),
                    },
                    "response_text": HELP_TEXT,
                },
                meta={"audit_db": mask_path(store.path)},
            )
            decision = "allowed"
            return _record_and_return(
                store=store,
                request=normalized_request,
                command_id=command_id,
                created_at=created_at,
                intent=intent,
                call=None,
                decision=decision,
                response=response,
            )

        if intent.name == "pending_operations":
            call = InboundToolCall(
                tool_name="inbound.pending",
                payload={
                    "scope": "current_conversation",
                    "channel": normalized_request.channel,
                    "sender_id": normalized_request.sender_id,
                    "conversation_id": normalized_request.conversation_id,
                },
            )
            pending_operations = InboundOperationStore(store.path).list_pending_operations(
                channel=normalized_request.channel,
                sender_id=normalized_request.sender_id,
                conversation_id=normalized_request.conversation_id,
            )
            response_text = render_pending_operations(pending_operations)
            response = build_response(
                tool_name="inbound.handle",
                ok=True,
                data={
                    "command_id": command_id,
                    "request": normalized_request.public_payload(),
                    "intent": intent.public_payload(),
                    "tool_call": call.public_payload(),
                    "decision": {
                        "allowed": True,
                        "reason": "pending_operations",
                        "sender": sender_decision.public_payload(),
                    },
                    "pending_count": len(pending_operations),
                    "pending_operations": pending_operations,
                    "response_text": response_text,
                },
                meta={"audit_db": mask_path(store.path)},
            )
            decision = "allowed"
            return _record_and_return(
                store=store,
                request=normalized_request,
                command_id=command_id,
                created_at=created_at,
                intent=intent,
                call=call,
                decision=decision,
                response=response,
            )

        if is_manual_trade_operation_intent(intent):
            call = InboundToolCall(tool_name="inbound.manual_trade", payload=dict(intent.arguments))
            operation_result = handle_manual_trade_operation(
                intent,
                normalized_request,
                command_id=command_id,
                store=InboundOperationStore(store.path),
            )
            response = _operation_response(
                operation_result,
                command_id=command_id,
                request=normalized_request,
                intent=intent,
                sender_decision=sender_decision.public_payload(),
                reason="manual_trade_operation",
                audit_db=store.path,
            )
            decision = "allowed"
            return _record_and_return(
                store=store,
                request=normalized_request,
                command_id=command_id,
                created_at=created_at,
                intent=intent,
                call=call,
                decision=decision,
                response=response,
            )

        if is_symbol_operation_intent(intent):
            call = InboundToolCall(tool_name="inbound.symbols", payload=dict(intent.arguments))
            operation_result = handle_symbol_operation(
                intent,
                normalized_request,
                command_id=command_id,
                store=InboundOperationStore(store.path),
            )
            response = _operation_response(
                operation_result,
                command_id=command_id,
                request=normalized_request,
                intent=intent,
                sender_decision=sender_decision.public_payload(),
                reason="symbol_operation",
                audit_db=store.path,
            )
            decision = "allowed"
            return _record_and_return(
                store=store,
                request=normalized_request,
                command_id=command_id,
                created_at=created_at,
                intent=intent,
                call=call,
                decision=decision,
                response=response,
            )

        call = _tool_call_from_intent(intent, request=normalized_request)
        tool_decision = enforce_tool_allowed(call)
        tool_result = execute_tool_fn(call.tool_name, call.payload)
        response_text = render_inbound_text(intent=intent, tool_result=tool_result)
        response = build_response(
            tool_name="inbound.handle",
            ok=bool(tool_result.get("ok", False)),
            data={
                "command_id": command_id,
                "request": normalized_request.public_payload(),
                "intent": intent.public_payload(),
                "tool_call": call.public_payload(),
                "decision": {
                    **tool_decision,
                    "sender": sender_decision.public_payload(),
                },
                "tool_result": tool_result,
                "response_text": response_text,
            },
            error=tool_result.get("error") if not bool(tool_result.get("ok", False)) else None,
            meta={"audit_db": mask_path(store.path)},
        )
        decision = "allowed"
    except AgentToolError as err:
        error_code = err.code
        response = _error_response(command_id=command_id, request=normalized_request, err=err, audit_db=store.path)
        decision = _decision_for_error(err)

    return _record_and_return(
        store=store,
        request=normalized_request,
        command_id=command_id,
        created_at=created_at,
        intent=intent,
        call=call,
        decision=decision,
        response=response,
        error_code=error_code,
    )


def _tool_call_from_intent(intent: InboundIntent, *, request: InboundRequest) -> InboundToolCall:
    base = _base_payload(request)
    if intent.name == "runtime_status":
        return InboundToolCall(tool_name="runtime_status", payload=base)
    if intent.name == "healthcheck":
        return InboundToolCall(tool_name="healthcheck", payload=base)
    if intent.name == "config_validate":
        return InboundToolCall(tool_name="config_validate", payload=base)
    if intent.name == "option_positions_open":
        return InboundToolCall(
            tool_name="option_positions_read",
            payload={
                **base,
                "action": "list",
                "account": intent.arguments["account"],
                "status": intent.arguments.get("status") or "open",
            },
        )
    if intent.name == "monthly_income_report":
        payload = {**base, "account": intent.arguments["account"]}
        if intent.arguments.get("month"):
            payload["month"] = intent.arguments["month"]
        return InboundToolCall(tool_name="monthly_income_report", payload=payload)
    if intent.name == "runtime_runs":
        return InboundToolCall(tool_name="runtime_runs", payload={"limit": int(intent.arguments.get("limit") or 10)})
    if intent.name == "runtime_logs":
        return InboundToolCall(
            tool_name="runtime_logs",
            payload={
                "run_id": intent.arguments["run_id"],
                "kind": intent.arguments.get("kind") or "all",
                "lines": int(intent.arguments.get("lines") or 50),
            },
        )
    raise AgentToolError(
        code="INPUT_ERROR",
        message=f"unsupported inbound intent: {intent.name}",
    )


def _base_payload(request: InboundRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if request.config_path:
        payload["config_path"] = request.config_path
    elif request.config_key:
        payload["config_key"] = request.config_key
    return payload


def _operation_response(
    operation_result: dict[str, Any],
    *,
    command_id: str,
    request: InboundRequest,
    intent: InboundIntent,
    sender_decision: dict[str, Any],
    reason: str,
    audit_db: Any,
) -> dict[str, Any]:
    data = dict(operation_result.get("data") or {})
    data.update(
        {
            "command_id": command_id,
            "request": request.public_payload(),
            "intent": intent.public_payload(),
            "decision": {
                "allowed": True,
                "reason": reason,
                "sender": sender_decision,
            },
            "response_text": str(data.get("response_text") or ""),
        }
    )
    meta = dict(operation_result.get("meta") or {})
    meta["audit_db"] = mask_path(audit_db)
    return build_response(
        tool_name=str(operation_result.get("tool_name") or "inbound.handle"),
        ok=bool(operation_result.get("ok", False)),
        data=data,
        error=operation_result.get("error") if not bool(operation_result.get("ok", False)) else None,
        warnings=operation_result.get("warnings"),
        meta=meta,
    )


def _record_and_return(
    *,
    store: InboundAuditStore,
    request: InboundRequest,
    command_id: str,
    created_at: str,
    intent: InboundIntent | None,
    call: InboundToolCall | None,
    decision: str,
    response: dict[str, Any],
    error_code: str | None = None,
) -> dict[str, Any]:
    store.record_result(
        {
            "command_id": command_id,
            "channel": request.channel,
            "sender_id": request.sender_id,
            "conversation_id": request.conversation_id,
            "message_id": request.message_id,
            "raw_text": request.text,
            "parser": intent.parser if intent else None,
            "intent_name": intent.name if intent else None,
            "tool_name": call.tool_name if call else None,
            "tool_payload": call.payload if call else None,
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
    request: InboundRequest,
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


def _decision_for_error(err: AgentToolError) -> str:
    if err.code == "PERMISSION_DENIED":
        return "denied"
    if err.code == "NEEDS_CLARIFICATION":
        return "needs_clarification"
    return "failed"


def _normalize_request(request: InboundRequest) -> InboundRequest:
    channel = str(request.channel or "local").strip().lower() or "local"
    sender_id = str(request.sender_id or "").strip()
    conversation_id = str(request.conversation_id or "").strip() or f"{channel}:{sender_id}"
    return InboundRequest(
        text=str(request.text or "").strip(),
        sender_id=sender_id,
        channel=channel,
        message_id=str(request.message_id).strip() if request.message_id is not None and str(request.message_id).strip() else None,
        conversation_id=conversation_id,
        config_key=str(request.config_key or "").strip().lower() or None,
        config_path=str(request.config_path).strip() if request.config_path is not None and str(request.config_path).strip() else None,
        audit_db=str(request.audit_db).strip() if request.audit_db is not None and str(request.audit_db).strip() else None,
    )
