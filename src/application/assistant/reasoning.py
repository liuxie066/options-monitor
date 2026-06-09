from __future__ import annotations

from pathlib import Path
from typing import Any

from domain.domain.symbol_identity import symbol_market
from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.commands import spec_by_intent
from src.application.assistant.contracts import (
    AssistantRequest,
    AssistantSafetyClass,
    PerceptionResult,
    ReasoningResolution,
    ToolCall,
)
from src.application.assistant.position_query import PositionQuery
from src.application.assistant.tool_policy import INTERNAL_TOOL_PLAN_NAME


CONFIG_SCOPED_INTENTS = frozenset(
    {
        "runtime_status",
        "healthcheck",
        "config_validate",
        "symbol_config_query",
        "position_query",
        "position_exit_analysis",
        "monthly_income_report",
        "symbol_list",
    }
)
_COMMAND_SPECS_BY_INTENT = spec_by_intent()


def resolve_reasoning(perception: PerceptionResult, *, request: AssistantRequest) -> ReasoningResolution:
    if perception.intent_name == "small_talk":
        return ReasoningResolution(
            status="supported",
            intent_name=perception.intent_name,
            arguments=dict(perception.arguments),
            safety_class="local",
            action_kind="local_response",
            reason="local_small_talk",
        )
    if perception.intent_name == "help":
        return ReasoningResolution(
            status="supported",
            intent_name=perception.intent_name,
            arguments=dict(perception.arguments),
            safety_class="local",
            action_kind="local_response",
            reason="local_help",
        )
    if perception.intent_name == "tool_plan":
        return _internal_tool_plan_resolution(perception, request=request)

    spec = _COMMAND_SPECS_BY_INTENT.get(perception.intent_name)
    if spec is None:
        return _unsupported(perception, reason="unknown_capability")
    if not spec.supported:
        return _unsupported(perception, reason="capability_not_implemented", display_name=spec.display_name)
    if spec.tool_name is None:
        return _unsupported(perception, reason="capability_has_no_action", display_name=spec.display_name)

    safety_class = _safety_class(perception.intent_name)
    _require_config_scope(perception=perception, request=request)
    payload = _tool_payload_from_perception(perception, request=request, tool_name=str(spec.tool_name))
    tool_call = ToolCall(tool_name=str(spec.tool_name), payload=payload)
    risk_level = spec.risk_level or ("read_only" if spec.read_only else "write")
    requires_confirmation = _requires_confirmation(risk_level)
    action_kind = _action_kind(str(spec.tool_name))
    status = "preview_required" if requires_confirmation else "supported"
    return ReasoningResolution(
        status=status,
        intent_name=perception.intent_name,
        arguments=payload if action_kind == "operation" else dict(perception.arguments),
        safety_class=safety_class,
        action_kind=action_kind,
        tool_call=tool_call,
        read_only=bool(spec.read_only),
        requires_confirmation=requires_confirmation,
        reason=_resolution_reason(risk_level, read_only=spec.read_only),
    )


def _internal_tool_plan_resolution(perception: PerceptionResult, *, request: AssistantRequest) -> ReasoningResolution:
    if perception.source != "agent_loop_plan":
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message="tool_plan is only allowed from agent_loop planning",
            details={"source": perception.source},
        )
    plan = perception.arguments.get("plan")
    if not isinstance(plan, dict):
        raise AgentToolError(code="INPUT_ERROR", message="tool_plan intent requires a structured plan")
    payload = {
        **_base_payload(request),
        "question": request.text,
        "plan": dict(plan),
    }
    return ReasoningResolution(
        status="supported",
        intent_name=perception.intent_name,
        arguments=dict(perception.arguments),
        safety_class="read",
        action_kind="tool",
        tool_call=ToolCall(tool_name=INTERNAL_TOOL_PLAN_NAME, payload=payload),
        read_only=True,
        requires_confirmation=False,
        reason="internal_agent_loop_plan",
    )


def _unsupported(perception: PerceptionResult, *, reason: str, display_name: str | None = None) -> ReasoningResolution:
    name = display_name or perception.intent_name
    message = f"已识别意图：{name}。当前聊天入口还不支持这个能力，不能降级为其他查询。"
    return ReasoningResolution(
        status="unsupported",
        intent_name=perception.intent_name,
        arguments=dict(perception.arguments),
        safety_class="local",
        action_kind="none",
        read_only=True,
        requires_confirmation=False,
        reason=reason,
        message=message,
    )


def _safety_class(intent_name: str) -> AssistantSafetyClass:
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


def _tool_payload_from_perception(
    perception: PerceptionResult,
    *,
    request: AssistantRequest,
    tool_name: str,
) -> dict[str, Any]:
    base = _base_payload(request)
    intent_name = perception.intent_name
    arguments = dict(perception.arguments)
    if intent_name in {"runtime_status", "healthcheck", "config_validate"}:
        return base
    if intent_name == "symbol_config_query":
        symbol = str(arguments.get("symbol") or "").strip()
        if not symbol:
            raise AgentToolError(code="NEEDS_CLARIFICATION", message="需要指定要查询的标的。")
        payload = {
            **_base_payload_for_symbol_config(request, symbol=symbol),
            "symbol": symbol,
        }
        for key in ("strategy", "field"):
            value = str(arguments.get(key) or "").strip()
            if value:
                payload[key] = value
        return payload
    if intent_name == "position_query":
        query = PositionQuery.from_payload(arguments).to_payload()
        return {
            **base,
            "action": "list",
            "query": query,
        }
    if intent_name == "position_exit_analysis":
        query = PositionQuery.from_payload(arguments).to_payload()
        payload = {
            **base,
            "query": query,
        }
        if not query.get("symbol"):
            payload["market_scope"] = "all"
        return payload
    if intent_name == "monthly_income_report":
        payload = {**base}
        if arguments.get("account"):
            payload["account"] = arguments["account"]
        if arguments.get("month"):
            payload["month"] = arguments["month"]
        return payload
    if intent_name == "tool_plan":
        plan = arguments.get("plan")
        if not isinstance(plan, dict):
            raise AgentToolError(code="INPUT_ERROR", message="tool_plan intent requires a structured plan")
        return {
            **base,
            "question": request.text,
            "plan": dict(plan),
        }
    if intent_name == "runtime_runs":
        return {"limit": int(arguments.get("limit") or 10)}
    if intent_name == "runtime_logs":
        run_id = str(arguments.get("run_id") or "").strip()
        if not run_id:
            raise AgentToolError(code="NEEDS_CLARIFICATION", message="runtime logs query requires run_id")
        return {
            "run_id": run_id,
            "kind": arguments.get("kind") or "all",
            "lines": int(arguments.get("lines") or 50),
        }
    if intent_name == "pending_operations":
        return {
            "scope": "current_conversation",
            "channel": request.channel,
            "sender_id": request.sender_id,
            "conversation_id": request.conversation_id,
        }
    if tool_name in {"inbound.manual_trade", "inbound.symbols", "inbound.upgrade", "inbound.model"}:
        return dict(arguments)
    raise AgentToolError(
        code="INPUT_ERROR",
        message=f"unsupported assistant perception: {intent_name}",
    )


def _requires_confirmation(risk_level: str | None) -> bool:
    return str(risk_level or "").strip() in {"preview_write", "preview_admin"}


def _resolution_reason(risk_level: str | None, *, read_only: bool) -> str:
    if read_only:
        return "read_only_capability"
    normalized = str(risk_level or "").strip()
    if normalized == "preview_write":
        return "write_preview_operation"
    if normalized == "preview_admin":
        return "admin_preview_operation"
    if normalized == "confirm_write":
        return "confirmed_write_operation"
    return "write_operation"


def _action_kind(tool_name: str) -> str:
    if tool_name == "inbound.pending":
        return "pending"
    if tool_name in {"inbound.manual_trade", "inbound.symbols", "inbound.upgrade", "inbound.model"}:
        return "operation"
    return "tool"


def _require_config_scope(*, perception: PerceptionResult, request: AssistantRequest) -> None:
    if perception.intent_name not in CONFIG_SCOPED_INTENTS:
        return
    if request.config_path or request.config_key:
        return
    raise AgentToolError(
        code="NEEDS_CLARIFICATION",
        message="需要先指定要查看的市场。",
        hint="请明确说美股或港股，或通过 --config-key us/hk、--config-path、assistant.default_market_scope 配置默认市场。",
        details={"intent_name": perception.intent_name, "required": "config_key_or_config_path"},
    )


def _base_payload(request: AssistantRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if request.config_path:
        payload["config_path"] = request.config_path
    elif request.config_key:
        payload["config_key"] = request.config_key
    return payload


def _base_payload_for_symbol_config(request: AssistantRequest, *, symbol: str) -> dict[str, Any]:
    market_key = _market_config_key(symbol)
    if request.config_path:
        path = Path(str(request.config_path))
        if market_key is not None and path.name in {"config.us.json", "config.hk.json"}:
            return {"config_path": str(path.with_name(f"config.{market_key}.json"))}
        return {"config_path": request.config_path}
    if market_key is not None:
        return {"config_key": market_key}
    return _base_payload(request)


def _market_config_key(symbol: Any) -> str | None:
    market = str(symbol_market(symbol) or "").strip().upper()
    if market == "HK":
        return "hk"
    if market == "US":
        return "us"
    return None


__all__ = ["CONFIG_SCOPED_INTENTS", "resolve_reasoning"]
