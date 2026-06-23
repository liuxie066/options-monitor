from __future__ import annotations

from typing import Any, Callable

from src.application.agent_tool_contracts import build_error_payload
from src.application.assistant.contracts import ActionResult, AssistantRequest, PerceptionResult, ReasoningResolution
from src.application.assistant.manual_trade_operations import handle_manual_trade_operation
from src.application.assistant.model_operations import handle_model_operation
from src.application.assistant.monitor_run_operations import handle_monitor_run_operation
from src.application.assistant.operation_store import InboundOperationStore
from src.application.assistant.policy import enforce_tool_allowed
from src.application.assistant.preview_request import preview_request_perception_from_payload
from src.application.assistant.reasoning import resolve_reasoning
from src.application.assistant.renderer import HELP_TEXT, SMALL_TALK_TEXT, render_pending_operations
from src.application.assistant.symbol_operations import handle_symbol_operation
from src.application.assistant.tool_policy import INTERNAL_TOOL_LOOP_NAME
from src.application.assistant.upgrade_operations import handle_upgrade_operation
from src.application.tool_execution import execute_tool


ExecuteToolFn = Callable[[str, dict[str, Any]], dict[str, Any]]


def perform_action(
    *,
    perception: PerceptionResult | None,
    resolution: ReasoningResolution,
    request: AssistantRequest,
    command_id: str,
    operation_store: InboundOperationStore,
    execute_tool_fn: ExecuteToolFn = execute_tool,
) -> ActionResult:
    if resolution.status == "unsupported":
        return ActionResult(
            executed=False,
            ok=True,
            action_kind="none",
            response_text=resolution.message or "当前聊天入口还不支持这个能力。",
        )
    if resolution.status in {"clarify", "denied", "failed"}:
        return ActionResult(
            executed=False,
            ok=False,
            action_kind="none",
            response_text=resolution.message or "请求没有执行。",
        )
    if resolution.action_kind == "local_response":
        return _local_response_action(perception, resolution=resolution)
    call = resolution.tool_call
    if call is None:
        return ActionResult(
            executed=False,
            ok=False,
            action_kind="none",
            error=build_error_payload_from_message("INPUT_ERROR", "reasoning did not produce an action"),
        )
    if resolution.action_kind == "pending":
        pending_operations = operation_store.list_pending_operations(
            channel=request.channel,
            sender_id=request.sender_id,
            conversation_id=request.conversation_id,
        )
        return ActionResult(
            executed=True,
            ok=True,
            action_kind="pending",
            tool_name=call.tool_name,
            payload=call.payload,
            result={
                "pending_count": len(pending_operations),
                "pending_operations": pending_operations,
            },
            response_text=render_pending_operations(pending_operations),
        )
    if resolution.action_kind == "operation":
        return _operation_action_result(
            perception=perception,
            resolution=resolution,
            request=request,
            command_id=command_id,
            store=operation_store,
        )
    tool_decision = enforce_tool_allowed(call)
    payload = dict(call.payload)
    if call.tool_name == INTERNAL_TOOL_LOOP_NAME:
        payload["_command_id"] = command_id
    tool_result = execute_tool_fn(call.tool_name, payload)
    if call.tool_name == INTERNAL_TOOL_LOOP_NAME:
        preview_action = _tool_loop_preview_action_result(
            perception=perception,
            tool_result=tool_result,
            request=request,
            command_id=command_id,
            store=operation_store,
        )
        if preview_action is not None:
            return preview_action
    data = dict(tool_result.get("data") or {}) if isinstance(tool_result.get("data"), dict) else {}
    return ActionResult(
        executed=True,
        ok=bool(tool_result.get("ok", False)),
        action_kind="tool",
        tool_name=call.tool_name,
        payload=call.payload,
        result={**tool_result, "_tool_decision": tool_decision},
        error=tool_result.get("error") if not bool(tool_result.get("ok", False)) else None,
        response_text=str(data.get("response_text") or ""),
    )


def _operation_action_result(
    *,
    perception: PerceptionResult | None,
    resolution: ReasoningResolution,
    request: AssistantRequest,
    command_id: str,
    store: InboundOperationStore,
) -> ActionResult:
    call = resolution.tool_call
    if call is None:
        return ActionResult(
            executed=False,
            ok=False,
            action_kind="none",
            error=build_error_payload_from_message("INPUT_ERROR", "operation resolution did not produce an action"),
        )
    operation_perception = PerceptionResult(
        intent_name=resolution.intent_name or "",
        arguments=dict(call.payload),
        source=perception.source if perception else "unknown",
        confidence=perception.confidence if perception else 0.0,
        evidence=perception.evidence if perception else None,
    )
    operation_result = _handle_operation(
        call.tool_name,
        operation_perception,
        request,
        command_id=command_id,
        store=store,
    )
    operation_result = _with_operation_context(operation_result, operation_perception, resolution)
    data = dict(operation_result.get("data") or {})
    return ActionResult(
        executed=True,
        ok=bool(operation_result.get("ok", False)),
        action_kind="operation",
        tool_name=call.tool_name,
        payload=call.payload,
        result=operation_result,
        error=operation_result.get("error") if not bool(operation_result.get("ok", False)) else None,
        response_text=str(data.get("response_text") or ""),
    )


def _with_operation_context(
    operation_result: dict[str, Any],
    perception: PerceptionResult,
    resolution: ReasoningResolution,
) -> dict[str, Any]:
    data = operation_result.get("data") if isinstance(operation_result, dict) else None
    if not isinstance(data, dict):
        return operation_result
    return {
        **operation_result,
        "data": {
            **data,
            "perception": perception.public_payload(),
            "reasoning": resolution.public_payload(),
        },
    }


def _tool_loop_preview_action_result(
    *,
    perception: PerceptionResult | None,
    tool_result: dict[str, Any],
    request: AssistantRequest,
    command_id: str,
    store: InboundOperationStore,
) -> ActionResult | None:
    data = tool_result.get("data") if isinstance(tool_result, dict) else {}
    if not isinstance(data, dict):
        return None
    task_contract = data.get("task_contract") if isinstance(data.get("task_contract"), dict) else {}
    if str(task_contract.get("requested_effect") or "").strip() != "preview_write":
        return None
    event_loop = data.get("event_loop") if isinstance(data.get("event_loop"), dict) else {}
    if str(event_loop.get("status") or "").strip() != "preview_requested":
        return None
    preview_request = event_loop.get("preview_request") if isinstance(event_loop.get("preview_request"), dict) else {}
    if not preview_request:
        return None
    preview_perception = preview_request_perception_from_payload(
        preview_request,
        question=request.text,
        source=perception.source if perception else "agent_loop_events",
    )
    preview_resolution = resolve_reasoning(preview_perception, request=request)
    if preview_resolution.action_kind != "operation":
        return None
    return _operation_action_result(
        perception=preview_perception,
        resolution=preview_resolution,
        request=request,
        command_id=command_id,
        store=store,
    )


def build_error_payload_from_message(code: str, message: str) -> dict[str, Any]:
    from src.application.agent_tool_contracts import AgentToolError

    return build_error_payload(AgentToolError(code=code, message=message))


def _local_response_action(
    perception: PerceptionResult | None,
    *,
    resolution: ReasoningResolution,
) -> ActionResult:
    if perception and perception.intent_name == "help":
        text = HELP_TEXT
    elif perception and perception.intent_name == "small_talk":
        text = str(perception.arguments.get("response_text") or SMALL_TALK_TEXT).strip() or SMALL_TALK_TEXT
    else:
        text = resolution.message or ""
    return ActionResult(
        executed=False,
        ok=True,
        action_kind="local_response",
        response_text=text,
    )


def _handle_operation(
    tool_name: str,
    perception: PerceptionResult,
    request: AssistantRequest,
    *,
    command_id: str,
    store: InboundOperationStore,
) -> dict[str, Any]:
    if tool_name == "inbound.manual_trade":
        return handle_manual_trade_operation(perception, request, command_id=command_id, store=store)
    if tool_name == "inbound.symbols":
        return handle_symbol_operation(perception, request, command_id=command_id, store=store)
    if tool_name == "inbound.upgrade":
        return handle_upgrade_operation(perception, request, command_id=command_id, store=store)
    if tool_name == "inbound.model":
        return handle_model_operation(perception, request, command_id=command_id, store=store)
    if tool_name == "inbound.monitor_run":
        return handle_monitor_run_operation(perception, request, command_id=command_id, store=store)
    return {
        "tool_name": tool_name,
        "ok": False,
        "error": build_error_payload_from_message("INPUT_ERROR", f"unsupported operation tool: {tool_name}"),
        "data": {"response_text": "不支持的操作类型。"},
    }


__all__ = ["ExecuteToolFn", "perform_action"]
