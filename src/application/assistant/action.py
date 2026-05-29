from __future__ import annotations

from typing import Any, Callable

from src.application.agent_tool_contracts import build_error_payload
from src.application.assistant.contracts import ActionResult, AssistantRequest, PerceptionResult, ReasoningResolution
from src.application.assistant.manual_trade_operations import handle_manual_trade_operation
from src.application.assistant.model_operations import handle_model_operation
from src.application.assistant.operation_store import InboundOperationStore
from src.application.assistant.policy import enforce_tool_allowed
from src.application.assistant.renderer import HELP_TEXT, SMALL_TALK_TEXT, render_pending_operations
from src.application.assistant.symbol_operations import handle_symbol_operation
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
            store=operation_store,
        )
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
    tool_decision = enforce_tool_allowed(call)
    tool_result = execute_tool_fn(call.tool_name, call.payload)
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
    return {
        "tool_name": tool_name,
        "ok": False,
        "error": build_error_payload_from_message("INPUT_ERROR", f"unsupported operation tool: {tool_name}"),
        "data": {"response_text": "不支持的操作类型。"},
    }


__all__ = ["ExecuteToolFn", "perform_action"]
