from __future__ import annotations

import re
from typing import Any, Callable

from src.application.agent_tool_contracts import build_error_payload
from src.application.assistant.contracts import ActionResult, AssistantRequest, PerceptionResult, ReasoningResolution
from src.application.copilot.channel_facade import run_channel_request
from src.application.copilot.contracts import AppResult, to_payload
from src.application.assistant.manual_trade_operations import handle_manual_trade_operation
from src.application.assistant.model_operations import handle_model_operation
from src.application.assistant.monitor_run_operations import handle_monitor_run_operation
from src.application.assistant.operation_store import InboundOperationStore
from src.application.assistant.policy import enforce_tool_allowed
from src.application.assistant.renderer import HELP_TEXT, SMALL_TALK_TEXT, render_pending_operations
from src.application.assistant.symbol_operations import handle_symbol_operation
from src.application.assistant.upgrade_operations import handle_upgrade_operation
from src.application.tool_execution import execute_tool


ExecuteToolFn = Callable[[str, dict[str, Any]], dict[str, Any]]
SAFE_COPILOT_EVENT_REASON_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


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
    if resolution.action_kind == "copilot":
        return _copilot_action(
            request=request,
            command_id=command_id,
            channel_scenes=_copilot_channel_scenes(resolution.arguments),
            human_review=_copilot_human_review(resolution.arguments),
        )
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
    tool_result = execute_tool_fn(call.tool_name, payload)
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


def _copilot_action(
    *,
    request: AssistantRequest,
    command_id: str,
    channel_scenes: tuple[str, ...],
    human_review: bool,
) -> ActionResult:
    result = run_channel_request(
        user_message=request.text,
        config_key=request.config_key,
        request_id=command_id,
        assistant_config_path=request.assistant_config_path,
        channel_scenes=channel_scenes,
        channel=request.channel,
        sender_id=request.sender_id,
        conversation_id=request.conversation_id,
    )
    response_text = _copilot_response_text(result, human_review=human_review)
    held = _copilot_human_review_held(result, response_text=response_text, human_review=human_review)
    result_data = {
        "response_text": response_text,
        "copilot": _copilot_channel_payload(result, response_text=response_text, held=held),
    }
    if human_review:
        result_data["copilot_human_review"] = {
            "enabled": True,
            "held": held,
        }
    if result.events:
        result_data["copilot_events"] = _copilot_event_summary(result)
    return ActionResult(
        executed=True,
        ok=bool(result.ok),
        action_kind="copilot",
        tool_name="copilot.channel",
        result={
            "tool_name": "copilot.channel",
            "ok": bool(result.ok),
            "data": result_data,
        },
        response_text=response_text,
    )


def _copilot_response_text(result: AppResult, *, human_review: bool) -> str:
    if not human_review or not str(result.run_id or "").strip():
        return result.user_response
    return "Copilot 分析已生成，等待人工复核后再发送；本次只记录审计摘要。"


def _copilot_human_review_held(result: AppResult, *, response_text: str, human_review: bool) -> bool:
    return bool(human_review and str(result.run_id or "").strip() and response_text != result.user_response)


def _copilot_channel_payload(result: AppResult, *, response_text: str, held: bool) -> dict[str, Any]:
    payload = to_payload(result, include_events=False)
    if held:
        payload["user_response"] = response_text
        payload["answer_report"] = None
        payload["human_review_held"] = True
    return payload


def _copilot_event_summary(result: AppResult) -> dict[str, Any]:
    events = list(result.events or [])
    return {
        "event_count": len(events),
        "event_types": [_event_type(event) for event in events if _event_type(event)],
        "final_status": result.status,
        "observation_refs": _dedupe_text(getattr(event, "visible_ref", None) for event in events),
        "tool_names": _dedupe_text(_event_payload_text(event, "tool_name") for event in events),
        "failure_reasons": _dedupe_text(_event_failure_reason(event) for event in events),
        "timeline": [_event_timeline_item(event) for event in events],
    }


def _event_timeline_item(event: Any) -> dict[str, Any]:
    payload = getattr(event, "payload", None)
    payload_dict = payload if isinstance(payload, dict) else {}
    item: dict[str, Any] = {"type": _event_type(event)}
    ref = str(getattr(event, "visible_ref", "") or "").strip()
    if ref:
        item["ref"] = ref
    for key in (
        "scene",
        "execution_environment",
        "read_only",
        "has_fixture",
        "requires_answer_synthesis",
        "requires_recommendations",
        "limit",
        "reason",
        "status",
        "ok",
        "tool_name",
        "tool_call_id",
        "turn",
        "error_code",
        "payload_keys",
        "evidence_refs",
        "missing_data",
    ):
        if key in payload_dict:
            if key in {"reason", "error_code"}:
                item[key] = _safe_event_reason(_event_type(event), payload_dict[key])
            else:
                item[key] = _summary_value(payload_dict[key])
    return item


def _event_type(event: Any) -> str:
    return str(getattr(event, "type", "") or "").strip()


def _event_payload_text(event: Any, key: str) -> str | None:
    payload = getattr(event, "payload", None)
    if not isinstance(payload, dict):
        return None
    text = str(payload.get(key) or "").strip()
    return text or None


def _event_failure_reason(event: Any) -> str | None:
    event_type = _event_type(event)
    payload = getattr(event, "payload", None)
    payload_dict = payload if isinstance(payload, dict) else {}
    if event_type == "tool_failed":
        return _safe_event_reason(event_type, payload_dict.get("error_code") or "tool_failed")
    if event_type in {
        "agent_action_rejected",
        "budget_exhausted",
        "contract_rejected",
        "engine_failed",
        "model_error",
        "result_admission_failed",
        "result_admission_rejected",
        "run_cancelled",
        "scene_preparation_failed",
        "tool_skipped",
    }:
        return _safe_event_reason(
            event_type,
            payload_dict.get("reason") or payload_dict.get("limit") or payload_dict.get("code") or event_type,
        )
    if event_type == "final_result" and bool(payload_dict.get("ok")) is False:
        return _safe_event_reason(event_type, payload_dict.get("status") or "failed")
    return None


def _safe_event_reason(event_type: str, value: Any) -> str | None:
    text = str(value or "").strip()
    if text and SAFE_COPILOT_EVENT_REASON_RE.fullmatch(text):
        return text
    fallback = str(event_type or "").strip()
    return fallback or None


def _summary_value(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.split())[:120]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_summary_value(item) for item in list(value)[:12]]
    if isinstance(value, dict):
        return "dict value"
    return str(type(value).__name__)[:120]


def _dedupe_text(values: Any) -> list[str]:
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _copilot_channel_scenes(arguments: dict[str, Any]) -> tuple[str, ...]:
    raw = arguments.get("channel_scenes")
    if not isinstance(raw, (list, tuple)):
        return ()
    scenes: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in scenes:
            scenes.append(text)
    return tuple(scenes)


def _copilot_human_review(arguments: dict[str, Any]) -> bool:
    return arguments.get("human_review") is True


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
