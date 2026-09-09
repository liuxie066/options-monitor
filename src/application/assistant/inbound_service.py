from __future__ import annotations

import time
from dataclasses import replace

import json
from datetime import date
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response, mask_path
from src.application.account_config import accounts_from_config
from src.application.agent_tool_config import load_runtime_config
from src.application.assistant.audit import InboundAuditStore, build_command_id, utc_now_iso
from src.application.assistant.contracts import AssistantRequest, ControlCommand
from src.application.assistant.command_parser import parse_assistant_command
from src.application.assistant.capability_catalog import commands_by_intent, preview_operation_capabilities
from src.application.assistant.inbound_control import ControlExecution, ExecuteToolFn, execute_explicit_control
from src.application.assistant.monitor_run_operations import _load_market_runtime_config, _resolve_market
from src.application.assistant.operation_store import InboundOperationStore
from src.application.assistant.permission_response import parse_permission_response
from src.application.assistant.policy import enforce_sender_allowed
from src.application.assistant.renderer import render_inbound_text
from src.application.bot.channel_facade import run_channel_request
from src.application.bot.contracts import AppResult
from src.application.tool_execution import execute_tool


ParseCommandFn = Callable[[str, Callable[[], date] | None], ControlCommand | None]
_ACCOUNT_SCOPED_COMMANDS = frozenset(
    command
    for intent_name in (
        "position_query",
        "assigned_stock_position_query",
        "option_performance_report",
        "monitor_run_now",
    )
    for command in commands_by_intent()[intent_name]
)


def handle_assistant_request(
    request: AssistantRequest,
    *,
    audit_store: InboundAuditStore | None = None,
    execute_tool_fn: ExecuteToolFn = execute_tool,
    allowed_senders: str | None = None,
    now_fn: Callable[[], date] | None = None,
    parse_command_fn: ParseCommandFn | None = None,
) -> dict[str, Any]:
    if request.received_monotonic is None:
        request = replace(request, received_monotonic=time.monotonic())
    normalized_request = _normalize_request(request)
    store = audit_store or InboundAuditStore(normalized_request.audit_db, deadline_monotonic=normalized_request.received_monotonic + 180)
    command_id = build_command_id(
        channel=normalized_request.channel,
        sender_id=normalized_request.sender_id,
        message_id=normalized_request.message_id,
        text=normalized_request.text,
    )

    # Expired deliveries still authenticate before replaying a prior response.
    if time.monotonic() >= normalized_request.received_monotonic + 180:
        try:
            enforce_sender_allowed(channel=normalized_request.channel,
                                   sender_id=normalized_request.sender_id, allowed_senders=allowed_senders)
        except AgentToolError as err:
            return _error_response(command_id=command_id, request=normalized_request, err=err, audit_db=store.path)

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
    command: ControlCommand | None = None
    control: ControlExecution | None = None
    response: dict[str, Any]
    decision = "unknown"
    error_code: str | None = None

    try:
        sender_decision = enforce_sender_allowed(
            channel=normalized_request.channel,
            sender_id=normalized_request.sender_id,
            allowed_senders=allowed_senders,
        )
        _check_request_deadline(normalized_request)
        command = _parse_command(
            normalized_request,
            store=store,
            now_fn=now_fn,
            parse_command_fn=parse_command_fn,
        )
        _check_request_deadline(normalized_request)
        if command is None:
            bot_result = _run_bot(normalized_request, command_id=command_id, audit_db=store.path)
            if bot_result.control_request:
                _check_request_deadline(normalized_request)
                command = _control_command_from_bot(bot_result.control_request)
                control = execute_explicit_control(
                    command,
                    request=normalized_request,
                    command_id=command_id,
                    operation_store=InboundOperationStore(store.path),
                    execute_tool_fn=execute_tool_fn,
                )
                response = _success_response(
                    command_id=command_id,
                    request=normalized_request,
                    control=control,
                    sender_decision=sender_decision.public_payload(),
                    audit_db=store.path,
                )
                data = response.get("data") if isinstance(response.get("data"), dict) else {}
                data["bot"] = _bot_result_payload(bot_result)
                response["data"] = data
                decision = _decision_for_control(control)
            else:
                response = _bot_response(
                    normalized_request,
                    command_id=command_id,
                    audit_db=store.path,
                    result=bot_result,
                )
                decision = "bot"
            return _record_and_return(
                store=store,
                request=normalized_request,
                command_id=command_id,
                created_at=created_at,
                command=command,
                control=control,
                decision=decision,
                response=response,
            )
        control = execute_explicit_control(
            command,
            request=normalized_request,
            command_id=command_id,
            operation_store=InboundOperationStore(store.path),
            execute_tool_fn=execute_tool_fn,
        )
        response = _success_response(
            command_id=command_id,
            request=normalized_request,
            control=control,
            sender_decision=sender_decision.public_payload(),
            audit_db=store.path,
        )
        decision = _decision_for_control(control)
    except AgentToolError as err:
        error_code = err.code
        response = _error_response(command_id=command_id, request=normalized_request, err=err, audit_db=store.path)
        decision = _decision_for_error(err)

    return _record_and_return(
        store=store,
        request=normalized_request,
        command_id=command_id,
        created_at=created_at,
        command=command,
        control=control,
        decision=decision,
        response=response,
        error_code=error_code,
    )


def _check_request_deadline(request: AssistantRequest) -> None:
    if request.received_monotonic is not None and time.monotonic() >= request.received_monotonic + 180:
        raise AgentToolError(code="BUDGET_EXHAUSTED",
                             message="请求等待或准备已超过总时间预算，本次未完成。请重新发起请求。")


def _parse_command(
    request: AssistantRequest,
    *,
    store: InboundAuditStore,
    now_fn: Callable[[], date] | None,
    parse_command_fn: ParseCommandFn | None,
) -> ControlCommand | None:
    if parse_command_fn is not None:
        return parse_command_fn(request.text, now_fn)
    command = parse_assistant_command(
        request.text,
        now_fn=now_fn,
        accounts=_configured_accounts_for_command(request),
    )
    if command is not None:
        return command
    permission_response = parse_permission_response(
        request.text,
        request=request,
        store=InboundOperationStore(store.path),
    )
    if permission_response is not None:
        return permission_response
    return None


def _configured_accounts_for_command(request: AssistantRequest) -> list[str] | None:
    command = str(request.text or "").strip().split(maxsplit=1)[0].lower()
    if command not in _ACCOUNT_SCOPED_COMMANDS:
        return None
    try:
        if command in commands_by_intent()["monitor_run_now"]:
            market = _resolve_market({}, request=request, symbols=[])
            _path, cfg = _load_market_runtime_config(market, request=request)
        else:
            _path, cfg = load_runtime_config(
                config_key=request.config_key,
                config_path=request.config_path,
            )
        return accounts_from_config(cfg, fallback=())
    except (AgentToolError, OSError, ValueError):
        return None


def _bot_reply_builder(request: AssistantRequest, command_id: str):
    context = request.reply_context or {}
    if context.get('bot_reply_enabled') is not True:
        return None
    if request.channel == 'feishu' and request.message_id:
        from src.application.channels.feishu_reply_renderer import render_feishu_conversation_reply
        def build(result: AppResult) -> dict[str, Any]:
            return {'delivery_key': f'feishu:{command_id}', 'channel': 'feishu', 'payload': render_feishu_conversation_reply(
                message_id=request.message_id, text=result.user_response, reply_in_thread=bool(context.get('reply_in_thread')),
                max_chars=int(context.get('max_reply_chars') or 0), render_route='bot')}
        return build
    if request.channel == 'wechat' and context.get('to_user_id') and context.get('context_token'):
        from src.application.channels.reply_decision import trim_reply
        def build(result: AppResult) -> dict[str, Any]:
            return {'delivery_key': f'wechat:{command_id}', 'channel': 'wechat', 'payload': {
                'to_user_id': context['to_user_id'], 'context_token': context['context_token'],
                'group_id': context.get('group_id'), 'text': trim_reply(result.user_response, max_chars=int(context.get('max_reply_chars') or 0))}}
        return build
    return None


def _run_bot(request: AssistantRequest, *, command_id: str, audit_db: Any) -> AppResult:
    deadline = (request.received_monotonic if request.received_monotonic is not None else time.monotonic()) + 180
    pending = InboundOperationStore(audit_db, deadline_monotonic=deadline).list_pending_operations(
        channel=request.channel,
        sender_id=request.sender_id,
        conversation_id=request.conversation_id,
    )
    return run_channel_request(
        user_message=request.text,
        received_monotonic=request.received_monotonic,
        authenticated_sender_id=request.sender_id,
        reply_builder=_bot_reply_builder(request, command_id),
        config_key=request.config_key,
        config_path=request.config_path,
        request_id=command_id,
        assistant_config_path=request.assistant_config_path,
        channel=request.channel,
        sender_id=request.sender_id,
        conversation_id=(
            None
            if request.conversation_id == f"{request.channel}:{request.sender_id}"
            else request.conversation_id
        ),
        host_db_path=str(audit_db),
        control_preview_specs=preview_operation_capabilities(),
        control_context=tuple(dict(item) for item in pending),
    )


def _bot_response(
    request: AssistantRequest,
    *,
    command_id: str,
    audit_db: Any,
    result: AppResult | None = None,
) -> dict[str, Any]:
    result = result or _run_bot(request, command_id=command_id, audit_db=audit_db)
    return build_response(
        tool_name="bot.chat",
        ok=bool(result.ok),
        data={
            "command_id": command_id,
            "request": request.public_payload(),
            "decision": {"allowed": True, "reason": "bot_freeform"},
            "response_text": result.user_response,
            "bot": _bot_result_payload(result),
        },
        meta={"audit_db": mask_path(audit_db)},
    )


def _control_command_from_bot(value: dict[str, Any]) -> ControlCommand:
    intent_name = str(value.get("intent_name") or "").strip()
    arguments = value.get("arguments")
    if not intent_name or not isinstance(arguments, dict):
        raise AgentToolError(code="INVALID_ACTION", message="Bot control preview request is invalid")
    allowed = {str(item["intent_name"]): item for item in preview_operation_capabilities()}
    spec = allowed.get(intent_name)
    if spec is None:
        raise AgentToolError(code="INVALID_ACTION", message="Bot requested a non-preview control capability")
    unknown = sorted(str(key) for key in arguments if str(key) not in set(spec.get("arguments") or ()))
    if unknown:
        raise AgentToolError(code="INVALID_ACTION", message="Bot control preview contains unsupported arguments")
    return ControlCommand(
        intent_name=intent_name,
        arguments=dict(arguments),
        source="bot_control_preview",
        confidence=1.0,
    )


def _bot_result_payload(result: AppResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "request_id": result.request_id,
        "contract_id": result.contract_id,
        "run_id": result.run_id,
        "event_count": len(result.events),
        "decision_trace": dict(result.decision_trace),
    }


def _success_response(
    *,
    command_id: str,
    request: AssistantRequest,
    control: ControlExecution,
    sender_decision: dict[str, Any],
    audit_db: Any,
) -> dict[str, Any]:
    result = dict(control.result or {})
    operation_data = dict(result.get("data") or {}) if control.action_kind == "operation" else {}
    meta = dict(result.get("meta") or {}) if control.action_kind == "operation" else {}
    meta["audit_db"] = mask_path(audit_db)
    data: dict[str, Any] = {
        "command_id": command_id,
        "request": request.public_payload(),
        "control": control.public_payload(),
        "decision": {
            "allowed": True,
            "reason": control.reason,
            "sender": sender_decision,
        },
        "response_text": control.response_text,
    }
    if operation_data:
        data.update(operation_data)
        data["response_text"] = control.response_text
    if control.action_kind == "pending" and isinstance(control.result, dict):
        data.update(control.result)
    if control.action_kind == "tool":
        data["tool_call"] = {"tool_name": control.tool_name, "payload": dict(control.payload)}
        data["tool_result"] = control.result or {}
        tool_decision = result.get("_tool_decision")
        if isinstance(tool_decision, dict):
            data["decision"] = {**tool_decision, "sender": sender_decision}
    return build_response(
        tool_name=str(result.get("tool_name") or control.tool_name or "inbound.handle"),
        ok=bool(control.ok),
        data=data,
        error=control.error if not bool(control.ok) else None,
        warnings=result.get("warnings") if control.action_kind == "operation" else None,
        meta=meta,
    )


def _record_and_return(
    *,
    store: InboundAuditStore,
    request: AssistantRequest,
    command_id: str,
    created_at: str,
    command: ControlCommand | None,
    control: ControlExecution | None,
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
            "parser": command.source if command else None,
            "intent_name": command.intent_name if command else None,
            "tool_name": control.tool_name if control else None,
            "tool_payload": control.payload if control else None,
            "control": control.public_payload() if control else None,
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


def _decision_for_control(control: ControlExecution) -> str:
    if control.status == "unsupported":
        return "unsupported"
    if control.status == "preview_required":
        return "preview"
    if not control.ok:
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
        reply_context=dict(request.reply_context) if isinstance(request.reply_context, dict) else None,
        received_monotonic=request.received_monotonic,
    )


__all__ = ["ExecuteToolFn", "ParseCommandFn", "handle_assistant_request"]
