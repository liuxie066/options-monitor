from __future__ import annotations

from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.commands import spec_by_intent
from src.application.assistant.contracts import (
    AssistantFrame,
    AssistantRequest,
    AssistantSafetyClass,
    SemanticFrame,
    ToolPlan,
)
from src.application.assistant.semantic_frames import PositionQuery


CONFIG_SCOPED_INTENTS = frozenset(
    {
        "runtime_status",
        "healthcheck",
        "config_validate",
        "position_query",
        "monthly_income_report",
        "symbol_list",
    }
)
_COMMAND_SPECS_BY_INTENT = spec_by_intent()
PLANNED_TOOL_INTENTS = frozenset(
    spec.intent_name
    for spec in _COMMAND_SPECS_BY_INTENT.values()
    if spec.tool_name is not None
)
READ_TOOL_INTENTS = frozenset(
    spec.intent_name
    for spec in _COMMAND_SPECS_BY_INTENT.values()
    if spec.tool_name is not None and spec.read_only
)


def frame_from_semantic_frame(semantic_frame: SemanticFrame) -> AssistantFrame:
    return AssistantFrame(
        intent=semantic_frame.name,
        payload=_frame_payload(semantic_frame),
        safety_class=_safety_class(semantic_frame.name),
        parser=semantic_frame.parser,
        confidence=float(semantic_frame.confidence),
    )


frame_from_intent = frame_from_semantic_frame


def tool_plan_from_frame(frame: AssistantFrame, *, request: AssistantRequest) -> ToolPlan:
    if frame.intent not in PLANNED_TOOL_INTENTS:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"unsupported assistant frame: {frame.intent}",
        )
    spec = _COMMAND_SPECS_BY_INTENT[frame.intent]
    expected_safety = _safety_class(frame.intent)
    if frame.safety_class != expected_safety:
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message=f"assistant frame safety class does not match intent: {frame.intent}",
            details={"safety_class": frame.safety_class, "expected_safety_class": expected_safety},
        )
    _require_config_scope(frame=frame, request=request)
    payload = _tool_payload_from_frame(frame, request=request, tool_name=str(spec.tool_name))
    return ToolPlan(
        tool_name=str(spec.tool_name),
        payload=payload,
        safety_class=frame.safety_class,
        read_only=bool(spec.read_only),
        requires_confirmation=_requires_confirmation(spec.risk_level),
        reason=_plan_reason(spec.risk_level, read_only=spec.read_only),
        source_intent=frame.intent,
    )


def _frame_payload(semantic_frame: SemanticFrame) -> dict[str, Any]:
    if semantic_frame.name == "position_query":
        return {"query": PositionQuery.from_payload(semantic_frame.arguments).to_payload()}
    return dict(semantic_frame.arguments)


def _safety_class(intent_name: str) -> AssistantSafetyClass:
    if intent_name == "small_talk":
        return "local"
    spec = _COMMAND_SPECS_BY_INTENT.get(intent_name)
    if spec is None or spec.tool_name is None:
        return "local"
    risk_level = spec.risk_level or ("read_only" if spec.read_only else "write")
    if risk_level == "read_only":
        return "read"
    if risk_level == "preview_write":
        return "write_preview"
    if risk_level == "confirm_write":
        return "write_apply"
    if risk_level == "preview_admin":
        return "admin_preview"
    return "write_preview"


def _tool_payload_from_frame(frame: AssistantFrame, *, request: AssistantRequest, tool_name: str) -> dict[str, Any]:
    base = _base_payload(request)
    if frame.intent in {"runtime_status", "healthcheck", "config_validate"}:
        return base
    if frame.intent == "position_query":
        query = frame.payload.get("query")
        if not isinstance(query, dict):
            raise AgentToolError(code="INPUT_ERROR", message="position query frame is missing query payload")
        return {
            **base,
            "action": "list",
            "query": dict(query),
        }
    if frame.intent == "monthly_income_report":
        payload = {**base}
        if frame.payload.get("account"):
            payload["account"] = frame.payload["account"]
        if frame.payload.get("month"):
            payload["month"] = frame.payload["month"]
        return payload
    if frame.intent == "runtime_runs":
        return {"limit": int(frame.payload.get("limit") or 10)}
    if frame.intent == "runtime_logs":
        run_id = str(frame.payload.get("run_id") or "").strip()
        if not run_id:
            raise AgentToolError(code="NEEDS_CLARIFICATION", message="runtime logs query requires run_id")
        return {
            "run_id": run_id,
            "kind": frame.payload.get("kind") or "all",
            "lines": int(frame.payload.get("lines") or 50),
        }
    if frame.intent == "pending_operations":
        return {
            "scope": "current_conversation",
            "channel": request.channel,
            "sender_id": request.sender_id,
            "conversation_id": request.conversation_id,
        }
    if tool_name in {"inbound.manual_trade", "inbound.symbols", "inbound.upgrade", "inbound.model"}:
        return dict(frame.payload)
    raise AgentToolError(
        code="INPUT_ERROR",
        message=f"unsupported assistant frame: {frame.intent}",
    )


def _requires_confirmation(risk_level: str | None) -> bool:
    return str(risk_level or "").strip() in {"preview_write", "preview_admin"}


def _plan_reason(risk_level: str | None, *, read_only: bool) -> str:
    if read_only:
        return "read_only_intent"
    normalized = str(risk_level or "").strip()
    if normalized == "preview_write":
        return "write_preview_operation"
    if normalized == "preview_admin":
        return "admin_preview_operation"
    if normalized == "confirm_write":
        return "confirmed_write_operation"
    return "write_operation"


def _require_config_scope(*, frame: AssistantFrame, request: AssistantRequest) -> None:
    if frame.intent not in CONFIG_SCOPED_INTENTS:
        return
    if request.config_path or request.config_key:
        return
    raise AgentToolError(
        code="NEEDS_CLARIFICATION",
        message="需要先指定要查看的市场。",
        hint="请明确说美股或港股，或通过 --config-key us/hk、--config-path、assistant.default_market_scope 配置默认市场。",
        details={"intent_name": frame.intent, "required": "config_key_or_config_path"},
    )


def _base_payload(request: AssistantRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if request.config_path:
        payload["config_path"] = request.config_path
    elif request.config_key:
        payload["config_key"] = request.config_key
    return payload


__all__ = [
    "CONFIG_SCOPED_INTENTS",
    "PLANNED_TOOL_INTENTS",
    "READ_TOOL_INTENTS",
    "frame_from_intent",
    "frame_from_semantic_frame",
    "tool_plan_from_frame",
]
