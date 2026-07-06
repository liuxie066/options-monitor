from __future__ import annotations

from datetime import date
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.agent_loop import AgentLoopPlanningOutcome, run_read_only_agent_loop, skipped_llm_trace
from src.application.assistant.audit import InboundAuditStore
from src.application.assistant.capability_catalog import is_llm_planner_preview_spec, spec_by_intent
from src.application.assistant.command_parser import parse_assistant_command
from src.application.assistant.contracts import AssistantRequest, PerceptionResult
from src.application.assistant.conversation_context import build_conversation_context, context_trace
from src.application.assistant.llm_reply import LlmReplyResult, generate_general_reply
from src.application.assistant.operation_store import InboundOperationStore
from src.application.assistant.permission_response import parse_permission_response
from src.application.assistant.perception_trace import (
    PerceptionTrace,
    accepted_candidate,
    build_perception_trace,
    error_candidate,
    skipped_candidate,
)
from src.application.assistant.settings import AssistantSettings
from src.application.assistant.time_filters import extract_month_filter

GenerateReplyFn = Callable[[str, AssistantSettings, dict[str, Any] | None], LlmReplyResult]
ExecuteToolFn = Callable[[str, dict[str, Any]], dict[str, Any]]

_COMMAND_SPECS_BY_INTENT = spec_by_intent()
_BUSINESS_OR_WRITE_TOKENS = (
    "确认",
    "取消",
    "记录",
    "开仓",
    "平仓",
    "交易",
    "买入",
    "卖出",
    "下单",
    "成交",
    "权利金",
    "行权价",
    "持仓",
    "收益",
    "状态",
    "健康",
    "配置",
    "日志",
    "账本",
    "监控",
    "标的",
    "升级",
    "重启",
    "启动",
    "停止",
    "写入",
    "删除",
    "修改",
    "增加",
    "confirm",
    "cancel",
    "trade",
    "position",
    "income",
    "status",
    "health",
    "config",
    "logs",
    "symbol",
    "upgrade",
    "restart",
    "start",
    "stop",
    "apply",
    "delete",
    "edit",
    "add",
    "premium",
    "strike",
    "止盈",
    "止损",
    "分析",
)


class PerceptionEngine:
    def __init__(
        self,
        *,
        request: AssistantRequest,
        audit_store: InboundAuditStore,
        settings: AssistantSettings,
        generate_reply_fn: GenerateReplyFn | None = None,
        execute_tool_fn: ExecuteToolFn | None = None,
    ) -> None:
        self._request = request
        self._audit_store = audit_store
        self._settings = settings
        self._generate_reply_fn = generate_reply_fn
        self._execute_tool_fn = execute_tool_fn
        self.route = self._initial_route(request.text)
        skipped_reason = "command" if self.route == "command" else "not_needed"
        self.llm_trace = skipped_llm_trace(settings.llm, reason=skipped_reason)
        self.trace: PerceptionTrace | None = None
        self.last_conversation_context: dict[str, Any] | None = None
        self.last_tool_loop_result: dict[str, Any] | None = None

    def perceive(self, text: str, parser_now_fn: Callable[[], date] | None) -> PerceptionResult:
        try:
            command_perception = parse_assistant_command(text, now_fn=parser_now_fn)
        except AgentToolError as err:
            self.trace = build_perception_trace(
                decision="command_error",
                selected_source=None,
                selected_perception=None,
                candidates=[
                    error_candidate("command", err),
                    skipped_candidate("permission_response", "command_error"),
                    skipped_candidate("agent_loop", "command_error"),
                ],
            )
            raise
        if command_perception is not None:
            self.route = "command"
            self.trace = build_perception_trace(
                decision="command_selected",
                selected_source="command",
                selected_perception=command_perception,
                candidates=[
                    accepted_candidate("command", command_perception),
                    skipped_candidate("permission_response", "command_selected"),
                    skipped_candidate("agent_loop", "command_selected"),
                ],
            )
            return command_perception

        operation_store = InboundOperationStore(self._audit_store.path)
        try:
            permission_perception = parse_permission_response(
                text,
                request=self._request,
                store=operation_store,
            )
        except AgentToolError as err:
            self.route = "permission_response"
            self.trace = build_perception_trace(
                decision="permission_response_error",
                selected_source=None,
                selected_perception=None,
                candidates=[
                    skipped_candidate("command", "not_command"),
                    error_candidate("permission_response", err),
                    skipped_candidate("agent_loop", "permission_response_error"),
                ],
            )
            raise
        if permission_perception is not None:
            self.route = "permission_response"
            self.llm_trace = skipped_llm_trace(self._settings.llm, reason="permission_response")
            self.trace = build_perception_trace(
                decision="permission_response_selected",
                selected_source="permission_response",
                selected_perception=permission_perception,
                candidates=[
                    skipped_candidate("command", "not_command"),
                    accepted_candidate("permission_response", permission_perception),
                    skipped_candidate("agent_loop", "permission_response_selected"),
                ],
            )
            return permission_perception

        if not self._agent_loop_available():
            err = AgentToolError(
                code="AGENT_LOOP_DISABLED",
                message="自然语言请求需要进入 AgentLoop，但当前未启用。",
                hint="启用 assistant.agent_loop.enabled，或使用明确 slash command，例如 /status、/pending。",
            )
            self.route = "agent_loop_disabled"
            self.llm_trace = skipped_llm_trace(self._settings.llm, reason="agent_loop_disabled")
            self.trace = build_perception_trace(
                decision="agent_loop_disabled",
                selected_source=None,
                selected_perception=None,
                candidates=[
                    skipped_candidate("command", "not_command"),
                    skipped_candidate("permission_response", "not_permission_response"),
                    error_candidate("agent_loop", err),
                ],
            )
            raise err
        return self._perceive_agent_loop(text, parser_now_fn)

    def _initial_route(self, text: str) -> str:
        if looks_like_command(text):
            return "command"
        if self._agent_loop_available():
            return "agent_loop"
        return "agent_loop_disabled"

    def _agent_loop_available(self) -> bool:
        return bool(
            self._settings.agent_loop_enabled
            or self._generate_reply_fn is not None
        )

    def _perceive_agent_loop(self, text: str, parser_now_fn: Callable[[], date] | None) -> PerceptionResult:
        conversation_context = self._conversation_context()
        copilot_result = self._plan_with_copilot(text, conversation_context=conversation_context, now_fn=parser_now_fn)
        if "context" not in self.llm_trace:
            self.llm_trace["context"] = context_trace(conversation_context)
        if copilot_result.perception is not None:
            return self._handle_agent_loop_perception(
                copilot_result.perception,
                text=text,
                now_fn=parser_now_fn,
            )
        if copilot_result.error is not None:
            details = copilot_result.error.details if isinstance(copilot_result.error.details, dict) else {}
            if (
                copilot_result.error.code == "NEEDS_CLARIFICATION"
                and details.get("missing_capability") == "read_tool_or_required_slots"
            ):
                self.trace = build_perception_trace(
                    decision="needs_clarification",
                    selected_source=None,
                    selected_perception=None,
                    candidates=[
                        error_candidate("agent_loop", copilot_result.error, reason=str(self.llm_trace.get("reason") or "no_intent")),
                        skipped_candidate("command", "not_command"),
                        skipped_candidate("permission_response", "not_permission_response"),
                    ],
                )
                raise copilot_result.error
            return self._handle_agent_loop_error(text, copilot_result.error, conversation_context)
        err = AgentToolError(code="NEEDS_CLARIFICATION", message="AgentLoop 没有识别出可执行请求。")
        self.trace = build_perception_trace(
            decision="needs_clarification",
            selected_source=None,
            selected_perception=None,
            candidates=[
                error_candidate("agent_loop", err, reason=str(self.llm_trace.get("reason") or "no_intent")),
                skipped_candidate("command", "not_command"),
                skipped_candidate("permission_response", "not_permission_response"),
            ],
        )
        raise err

    def _conversation_context(self) -> dict[str, Any] | None:
        if (
            not self._agent_loop_available()
            and self._generate_reply_fn is None
        ):
            return None
        try:
            self.last_conversation_context = build_conversation_context(
                self._request,
                audit_store=self._audit_store,
                max_messages=self._settings.context_window_messages,
            )
        except Exception as exc:
            self.last_conversation_context = {
                "scope": {
                    "channel": str(self._request.channel or "local").strip().lower() or "local",
                    "sender_id": str(self._request.sender_id or "").strip(),
                    "conversation_id": str(self._request.conversation_id or "").strip(),
                },
                "window_messages": 0,
                "limits": {
                    "max_recent_messages": 0,
                    "max_pending_operations": 0,
                },
                "semantics": {
                    "explicit_message_wins": True,
                    "context_is_hint_only": True,
                    "confirmation_must_be_permission_response": True,
                },
                "recent_messages": [],
                "last_successful_read": None,
                "pending_operations": [],
                "degraded": True,
                "error": {
                    "stage": "conversation_context",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            }
        return self.last_conversation_context

    def _plan_with_copilot(
        self,
        text: str,
        *,
        conversation_context: dict[str, Any] | None,
        now_fn: Callable[[], date] | None,
    ) -> AgentLoopPlanningOutcome:
        if not self._agent_loop_available():
            copilot_result = AgentLoopPlanningOutcome(
                perception=None,
                trace=skipped_llm_trace(self._settings.llm, reason="agent_loop_disabled"),
            )
            self.llm_trace = dict(copilot_result.trace)
            return copilot_result
        conversation_context = _with_perception_temporal_context(conversation_context, now_fn=now_fn)
        self.last_conversation_context = conversation_context
        loop_result = run_read_only_agent_loop(
            text,
            settings=self._settings,
            conversation_context=conversation_context,
            request=self._request,
            execute_tool_fn=self._execute_tool_fn,
            now_fn=now_fn,
        )
        self.llm_trace = dict(loop_result.trace)
        self.last_tool_loop_result = dict(loop_result.tool_loop_result) if isinstance(loop_result.tool_loop_result, dict) else None
        return loop_result.planning

    def _handle_agent_loop_perception(
        self,
        perception: PerceptionResult,
        *,
        text: str = "",
        now_fn: Callable[[], date] | None = None,
    ) -> PerceptionResult:
        self.route = "agent_loop"
        perception = _reconcile_month_slot_from_text(
            perception,
            text=text,
            today=now_fn() if now_fn is not None else date.today(),
        )
        agent_loop_candidate = accepted_candidate(self.route, perception)
        candidates = [
            agent_loop_candidate,
            skipped_candidate("command", "not_command"),
            skipped_candidate("permission_response", "not_permission_response"),
        ]
        try:
            ensure_llm_perception_allowed(perception)
        except AgentToolError as policy_err:
            self.trace = build_perception_trace(
                decision="agent_loop_denied_by_policy",
                selected_source=None,
                selected_perception=None,
                candidates=[*candidates, error_candidate("policy", policy_err)],
            )
            raise
        self.trace = build_perception_trace(
            decision="agent_loop_selected",
            selected_source="agent_loop",
            selected_perception=perception,
            candidates=candidates,
        )
        return perception

    def _handle_agent_loop_error(
        self,
        text: str,
        llm_error: AgentToolError,
        conversation_context: dict[str, Any] | None,
    ) -> PerceptionResult:
        llm_error_candidate = error_candidate(
            "agent_loop",
            llm_error,
            reason=str(self.llm_trace.get("reason") or ""),
        )
        reply_result = self._maybe_generate_general_reply(
            text,
            planning_error=llm_error,
            conversation_context=conversation_context,
        )
        if reply_result.response_text:
            self.route = "llm_reply"
            self.llm_trace = dict(reply_result.trace)
            reply_perception = PerceptionResult(
                intent_name="small_talk",
                arguments={
                    "kind": "llm_reply",
                    "response_text": reply_result.response_text,
                },
                source="llm_reply",
                confidence=1.0,
            )
            self.trace = build_perception_trace(
                decision="llm_reply_selected",
                selected_source="llm_reply",
                selected_perception=reply_perception,
                candidates=[
                    llm_error_candidate,
                    accepted_candidate("llm_reply", reply_perception),
                    skipped_candidate("command", "not_command"),
                    skipped_candidate("permission_response", "not_permission_response"),
                ],
            )
            return reply_perception
        self.trace = build_perception_trace(
            decision="agent_loop_error",
            selected_source=None,
            selected_perception=None,
            candidates=[
                llm_error_candidate,
                skipped_candidate("command", "not_command"),
                skipped_candidate("permission_response", "not_permission_response"),
            ],
        )
        raise llm_error

    def _maybe_generate_general_reply(
        self,
        text: str,
        *,
        planning_error: AgentToolError,
        conversation_context: dict[str, Any] | None,
    ) -> LlmReplyResult:
        if not general_reply_allowed(text, planning_error=planning_error):
            return LlmReplyResult(
                response_text=None,
                trace={
                    **dict(self.llm_trace),
                    "reply": {
                        "attempted": False,
                        "reason": "blocked_by_safety_filter",
                    },
                },
                error=planning_error,
            )
        if self._generate_reply_fn is not None:
            reply_result = self._generate_reply_fn(text, self._settings, conversation_context)
        else:
            reply_result = generate_general_reply(
                text,
                settings=self._settings.llm,
                conversation_context=conversation_context,
            )
        trace = dict(reply_result.trace)
        trace["intent_router"] = dict(self.llm_trace)
        if "context" not in trace:
            trace["context"] = context_trace(conversation_context)
        return LlmReplyResult(response_text=reply_result.response_text, trace=trace, error=reply_result.error)


def looks_like_command(text: str) -> bool:
    return str(text or "").lstrip().startswith("/")


def general_reply_allowed(text: str, *, planning_error: AgentToolError) -> bool:
    if planning_error.code in {"PERMISSION_DENIED", "INPUT_ERROR"}:
        return False
    details = planning_error.details if isinstance(planning_error.details, dict) else {}
    if isinstance(details.get("context_validation"), dict):
        return False
    compact = str(text or "").strip().lower()
    if not compact:
        return False
    return not any(token in compact for token in _BUSINESS_OR_WRITE_TOKENS)


def ensure_llm_perception_allowed(perception: PerceptionResult) -> None:
    if perception.intent_name == "tool_loop" and perception.source == "agent_loop_events":
        return
    spec = _COMMAND_SPECS_BY_INTENT.get(perception.intent_name)
    if perception.source in {"agent_loop_plan", "agent_loop_events"} and spec is not None and is_llm_planner_preview_spec(spec):
        return
    if spec is None or not spec.llm_allowed or not (spec.read_only or _is_llm_preview_perception_allowed(spec)):
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message=f"AgentLoop is not allowed to route intent: {perception.intent_name}",
            hint="AgentLoop planning is restricted to read-only capabilities plus explicitly allowed preview capabilities; confirm/apply operations require permission responses.",
            details={"intent_name": perception.intent_name},
        )


def _llm_preview_intent_name(perception: PerceptionResult) -> str | None:
    if perception.intent_name != "tool_loop":
        return str(perception.intent_name or "")
    events = perception.arguments.get("events") if isinstance(perception.arguments, dict) else None
    if not isinstance(events, list):
        return None
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event_type") or "")
        candidate = ""
        if event_type == "model_tool_call":
            candidate = str(event.get("tool_name") or "")
        spec = _COMMAND_SPECS_BY_INTENT.get(candidate)
        if spec is not None and _is_llm_preview_perception_allowed(spec):
            return candidate
    return None


def _is_llm_preview_perception_allowed(spec: Any) -> bool:
    return bool(is_llm_planner_preview_spec(spec))


def _reconcile_month_slot_from_text(perception: PerceptionResult, *, text: str, today: date) -> PerceptionResult:
    if perception.intent_name != "monthly_income_report":
        return perception
    month = extract_month_filter(text, today=today)
    if not month:
        return perception
    arguments = dict(perception.arguments or {})
    current = arguments.get("month")
    if current == month:
        return perception
    arguments["month"] = month
    evidence = dict(perception.evidence or {})
    evidence["argument_reconciliation"] = {
        "source": "text_month_filter",
        "filled": {"month": month} if _is_empty_slot(current) else {},
        "overridden": {} if _is_empty_slot(current) else {"month": {"llm": current, "text": month}},
    }
    return PerceptionResult(
        intent_name=perception.intent_name,
        arguments=arguments,
        source=perception.source,
        confidence=perception.confidence,
        evidence=evidence,
    )


def _with_perception_temporal_context(
    conversation_context: dict[str, Any] | None,
    *,
    now_fn: Callable[[], date] | None,
) -> dict[str, Any] | None:
    if not isinstance(conversation_context, dict):
        return conversation_context
    today = now_fn() if now_fn is not None else date.today()
    context = dict(conversation_context)
    context["temporal_context"] = {
        "current_date": today.isoformat(),
        "timezone": "Asia/Shanghai",
    }
    return context


def _is_empty_slot(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, dict) and not any(not _is_empty_slot(item) for item in value.values()):
        return True
    return False


__all__ = [
    "GenerateReplyFn",
    "PerceptionEngine",
    "ensure_llm_perception_allowed",
    "general_reply_allowed",
    "looks_like_command",
]
