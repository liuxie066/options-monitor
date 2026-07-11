from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from domain.domain.symbol_identity import symbol_market
from src.application.agent_tool_contracts import AgentToolError, build_error_payload
from src.application.assistant.capability_catalog import spec_by_intent
from src.application.assistant.contracts import AssistantRequest, AssistantSafetyClass, ControlCommand
from src.application.assistant.manual_trade_operations import handle_manual_trade_operation
from src.application.assistant.model_operations import handle_model_operation
from src.application.assistant.monitor_run_operations import handle_monitor_run_operation
from src.application.assistant.operation_store import InboundOperationStore
from src.application.assistant.policy import enforce_tool_allowed
from src.application.assistant.position_query import PositionQuery
from src.application.assistant.renderer import HELP_TEXT, render_inbound_text, render_pending_operations
from src.application.assistant.symbol_operations import handle_symbol_operation
from src.application.assistant.tool_bindings import binding_for_intent, config_required_intent_names
from src.application.assistant.upgrade_operations import handle_upgrade_operation
from src.application.tool_execution import execute_tool


ExecuteToolFn = Callable[[str, dict[str, Any]], dict[str, Any]]
_CONFIG_SCOPED_INTENTS = config_required_intent_names()
_COMMAND_SPECS_BY_INTENT = spec_by_intent()
_OPERATION_TOOLS = frozenset(
    {
        "inbound.manual_trade",
        "inbound.symbols",
        "inbound.upgrade",
        "inbound.model",
        "inbound.monitor_run",
    }
)


@dataclass(frozen=True)
class ControlExecution:
    status: str
    intent_name: str
    safety_class: AssistantSafetyClass
    action_kind: str
    reason: str
    tool_name: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    response_text: str = ""
    executed: bool = False
    ok: bool = False
    requires_confirmation: bool = False

    def public_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "intent_name": self.intent_name,
            "safety_class": self.safety_class,
            "action_kind": self.action_kind,
            "reason": self.reason,
            "tool_name": self.tool_name,
            "payload": dict(self.payload),
            "result": dict(self.result or {}),
            "error": dict(self.error or {}),
            "response_text": self.response_text,
            "executed": bool(self.executed),
            "ok": bool(self.ok),
            "requires_confirmation": bool(self.requires_confirmation),
        }


def execute_explicit_control(
    command: ControlCommand,
    *,
    request: AssistantRequest,
    command_id: str,
    operation_store: InboundOperationStore,
    execute_tool_fn: ExecuteToolFn = execute_tool,
) -> ControlExecution:
    if command.intent_name == "help":
        return ControlExecution(
            status="supported",
            intent_name="help",
            safety_class="local",
            action_kind="local_response",
            reason="local_help",
            response_text=HELP_TEXT,
            ok=True,
        )

    spec = _COMMAND_SPECS_BY_INTENT.get(command.intent_name)
    if spec is None:
        return _unsupported(command, reason="unknown_capability")
    if not spec.supported:
        return _unsupported(command, reason="capability_not_implemented", display_name=spec.display_name)
    if spec.tool_name is None:
        return _unsupported(command, reason="capability_has_no_action", display_name=spec.display_name)

    _require_config_scope(command=command, request=request)
    tool_name = str(spec.tool_name)
    payload = _tool_payload(command, request=request, tool_name=tool_name)
    risk_level = spec.risk_level or ("read_only" if spec.read_only else "write")
    safety_class = _safety_class(risk_level)
    requires_confirmation = risk_level in {"preview_write", "preview_admin"}
    action_kind = _action_kind(tool_name)
    status = "preview_required" if requires_confirmation else "supported"
    reason = _control_reason(risk_level, read_only=spec.read_only)

    if action_kind == "pending":
        pending = operation_store.list_pending_operations(
            channel=request.channel,
            sender_id=request.sender_id,
            conversation_id=request.conversation_id,
        )
        result = {"pending_count": len(pending), "pending_operations": pending}
        return ControlExecution(
            status=status,
            intent_name=command.intent_name,
            safety_class=safety_class,
            action_kind=action_kind,
            reason=reason,
            tool_name=tool_name,
            payload=payload,
            result=result,
            response_text=render_pending_operations(pending),
            executed=True,
            ok=True,
            requires_confirmation=requires_confirmation,
        )

    if action_kind == "operation":
        operation_command = ControlCommand(
            intent_name=command.intent_name,
            arguments=dict(payload),
            source=command.source,
            confidence=command.confidence,
        )
        result = _handle_operation(
            tool_name,
            operation_command,
            request,
            command_id=command_id,
            store=operation_store,
        )
        ok = bool(result.get("ok", False))
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        error = result.get("error") if not ok and isinstance(result.get("error"), dict) else None
        response_text = str(data.get("response_text") or "").strip()
        if not response_text:
            response_text = render_inbound_text(intent=operation_command, tool_result=result, error=error)
        return ControlExecution(
            status=status,
            intent_name=command.intent_name,
            safety_class=safety_class,
            action_kind=action_kind,
            reason=reason,
            tool_name=tool_name,
            payload=payload,
            result=result,
            error=error,
            response_text=response_text,
            executed=True,
            ok=ok,
            requires_confirmation=requires_confirmation,
        )

    tool_decision = enforce_tool_allowed(tool_name)
    result = execute_tool_fn(tool_name, dict(payload))
    ok = bool(result.get("ok", False))
    error = result.get("error") if not ok and isinstance(result.get("error"), dict) else None
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    response_text = str(data.get("response_text") or "").strip()
    if not response_text:
        response_text = render_inbound_text(intent=command, tool_result=result, error=error)
    return ControlExecution(
        status=status,
        intent_name=command.intent_name,
        safety_class=safety_class,
        action_kind="tool",
        reason=reason,
        tool_name=tool_name,
        payload=payload,
        result={**result, "_tool_decision": tool_decision},
        error=error,
        response_text=response_text,
        executed=True,
        ok=ok,
        requires_confirmation=requires_confirmation,
    )


def _unsupported(
    command: ControlCommand,
    *,
    reason: str,
    display_name: str | None = None,
) -> ControlExecution:
    name = display_name or command.intent_name
    return ControlExecution(
        status="unsupported",
        intent_name=command.intent_name,
        safety_class="local",
        action_kind="none",
        reason=reason,
        response_text=f"已识别意图：{name}。当前聊天入口还不支持这个能力，不能降级为其他查询。",
        ok=True,
    )


def _safety_class(risk_level: str) -> AssistantSafetyClass:
    if risk_level == "read_only":
        return "read"
    if risk_level == "preview_write":
        return "write_preview"
    if risk_level == "confirm_write":
        return "write_apply"
    if risk_level == "preview_admin":
        return "admin_preview"
    return "write_preview"


def _tool_payload(
    command: ControlCommand,
    *,
    request: AssistantRequest,
    tool_name: str,
) -> dict[str, Any]:
    base = _base_payload(request)
    intent_name = command.intent_name
    arguments = dict(command.arguments)
    if intent_name in {"runtime_status", "healthcheck", "config_validate"}:
        return base
    if intent_name in {"symbol_config_query", "symbol_resolve", "candidate_filter_explain"}:
        return _bound_symbol_payload(intent_name=intent_name, arguments=arguments, request=request)
    if intent_name == "position_query":
        return {**base, "action": "list", "query": PositionQuery.from_payload(arguments).to_payload()}
    if intent_name == "assigned_stock_position_query":
        status = str(arguments.get("assigned_stock_status") or arguments.get("status") or "open").strip().lower()
        payload = {**base, "action": "assigned-stock", "refresh_quotes": arguments.get("refresh_quotes") is not False}
        for key in ("account", "symbol", "stock_lot_id"):
            if arguments.get(key):
                payload[key] = arguments[key]
        if status and status != "all":
            payload["status"] = status
        return payload
    if intent_name == "position_exit_analysis":
        query = PositionQuery.from_payload(arguments).to_payload()
        payload = {**base, "query": query}
        if not query.get("symbol"):
            payload["market_scope"] = "all"
        return payload
    if intent_name == "monthly_income_report":
        payload = dict(base)
        for key in ("account", "month"):
            if arguments.get(key):
                payload[key] = arguments[key]
        return payload
    if intent_name == "runtime_runs":
        return {"limit": int(arguments.get("limit") or 10)}
    if intent_name == "runtime_logs":
        run_id = str(arguments.get("run_id") or "").strip()
        if not run_id:
            raise AgentToolError(code="NEEDS_CLARIFICATION", message="runtime logs query requires run_id")
        return {"run_id": run_id, "kind": arguments.get("kind") or "all", "lines": int(arguments.get("lines") or 50)}
    if intent_name == "pending_operations":
        return {
            "scope": "current_conversation",
            "channel": request.channel,
            "sender_id": request.sender_id,
            "conversation_id": request.conversation_id,
        }
    if tool_name in _OPERATION_TOOLS:
        return arguments
    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported assistant command: {intent_name}")


def _bound_symbol_payload(
    *,
    intent_name: str,
    arguments: dict[str, Any],
    request: AssistantRequest,
) -> dict[str, Any]:
    binding = binding_for_intent(intent_name)
    if binding is None:
        raise AgentToolError(code="INPUT_ERROR", message=f"unsupported bound assistant command: {intent_name}")
    symbol = str(arguments.get("symbol") or "").strip()
    if not symbol:
        raise AgentToolError(code="NEEDS_CLARIFICATION", message="需要指定标的。")
    payload = {**_base_payload_for_symbol_config(request, symbol=symbol), "symbol": symbol}
    for key in binding.arguments:
        if key == "symbol":
            continue
        value = str(arguments.get(key) or "").strip()
        if value:
            payload[key] = value
    return payload


def _control_reason(risk_level: str, *, read_only: bool) -> str:
    if read_only:
        return "read_only_capability"
    return {
        "preview_write": "write_preview_operation",
        "preview_admin": "admin_preview_operation",
        "confirm_write": "confirmed_write_operation",
    }.get(risk_level, "write_operation")


def _action_kind(tool_name: str) -> str:
    if tool_name == "inbound.pending":
        return "pending"
    return "operation" if tool_name in _OPERATION_TOOLS else "tool"


def _require_config_scope(*, command: ControlCommand, request: AssistantRequest) -> None:
    if command.intent_name not in _CONFIG_SCOPED_INTENTS or request.config_path or request.config_key:
        return
    raise AgentToolError(
        code="NEEDS_CLARIFICATION",
        message="需要先指定要查看的市场。",
        hint="请明确说美股或港股，或通过 --config-key us/hk、--config-path、assistant.default_market_scope 配置默认市场。",
        details={"intent_name": command.intent_name, "required": "config_key_or_config_path"},
    )


def _base_payload(request: AssistantRequest) -> dict[str, Any]:
    if request.config_path:
        return {"config_path": request.config_path}
    if request.config_key:
        return {"config_key": request.config_key}
    return {}


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
    return {"HK": "hk", "US": "us"}.get(market)


def _handle_operation(
    tool_name: str,
    command: ControlCommand,
    request: AssistantRequest,
    *,
    command_id: str,
    store: InboundOperationStore,
) -> dict[str, Any]:
    if tool_name == "inbound.manual_trade":
        return handle_manual_trade_operation(command, request, command_id=command_id, store=store)
    if tool_name == "inbound.symbols":
        return handle_symbol_operation(command, request, command_id=command_id, store=store)
    if tool_name == "inbound.upgrade":
        return handle_upgrade_operation(command, request, command_id=command_id, store=store)
    if tool_name == "inbound.model":
        return handle_model_operation(command, request, command_id=command_id, store=store)
    if tool_name == "inbound.monitor_run":
        return handle_monitor_run_operation(command, request, command_id=command_id, store=store)
    return {
        "tool_name": tool_name,
        "ok": False,
        "error": build_error_payload(AgentToolError(code="INPUT_ERROR", message=f"unsupported operation tool: {tool_name}")),
        "data": {"response_text": "不支持的操作类型。"},
    }


__all__ = ["ControlExecution", "ExecuteToolFn", "execute_explicit_control"]
