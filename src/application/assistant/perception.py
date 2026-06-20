from __future__ import annotations

from datetime import date
import re
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.agent_loop import AgentLoopPlanFn, AgentLoopPlanningOutcome, run_read_only_agent_loop, skipped_llm_trace
from src.application.assistant.audit import InboundAuditStore
from src.application.assistant.command_parser import parse_assistant_command
from src.application.assistant.capability_catalog import is_llm_planner_preview_spec, spec_by_intent
from src.application.assistant.contracts import AssistantRequest, PerceptionResult
from src.application.assistant.conversation_context import build_conversation_context, context_trace
from src.application.assistant.deterministic_commands import parse_deterministic_text
from src.application.assistant.llm_reply import LlmReplyResult, generate_general_reply
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
        plan_tools_fn: AgentLoopPlanFn | None = None,
        generate_reply_fn: GenerateReplyFn | None = None,
    ) -> None:
        self._request = request
        self._audit_store = audit_store
        self._settings = settings
        self._plan_tools_fn = plan_tools_fn
        self._generate_reply_fn = generate_reply_fn
        self.route = self._initial_route(request.text)
        skipped_reason = "command" if self.route == "command" else "not_needed"
        self.llm_trace = skipped_llm_trace(settings.llm, reason=skipped_reason)
        self.trace: PerceptionTrace | None = None
        self.last_conversation_context: dict[str, Any] | None = None

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
                    skipped_candidate("deterministic", "command_error"),
                    skipped_candidate("llm", "command_error"),
                ],
            )
            raise
        if command_perception is not None:
            self.trace = build_perception_trace(
                decision="command_selected",
                selected_source="command",
                selected_perception=command_perception,
                candidates=[
                    accepted_candidate("command", command_perception),
                    skipped_candidate("deterministic", "command_selected"),
                    skipped_candidate("llm", "command_selected"),
                ],
            )
            return command_perception
        if self._llm_first_enabled():
            return self._perceive_llm_first(text, parser_now_fn)
        return self._perceive_deterministic_first(text, parser_now_fn)

    def _initial_route(self, text: str) -> str:
        if looks_like_command(text):
            return "command"
        if self._settings.planner_enabled:
            return "agent_loop"
        return "deterministic"

    def _llm_first_enabled(self) -> bool:
        return self._settings.planner_enabled

    def _perceive_deterministic_first(self, text: str, parser_now_fn: Callable[[], date] | None) -> PerceptionResult:
        try:
            deterministic_perception = parse_deterministic_text(text, now_fn=parser_now_fn)
            self.trace = build_perception_trace(
                decision="deterministic_selected",
                selected_source="deterministic",
                selected_perception=deterministic_perception,
                candidates=[
                    accepted_candidate("deterministic", deterministic_perception),
                    skipped_candidate("llm", "deterministic_selected"),
                ],
            )
            return deterministic_perception
        except AgentToolError as err:
            return self._handle_deterministic_error(text, err, parser_now_fn=parser_now_fn)

    def _perceive_llm_first(self, text: str, parser_now_fn: Callable[[], date] | None) -> PerceptionResult:
        deterministic_candidate, deterministic_perception, deterministic_error = self._deterministic_candidate(
            text,
            parser_now_fn,
        )
        if deterministic_perception is not None and _deterministic_operation_command_has_priority(deterministic_perception):
            llm_source = self._llm_source()
            self.llm_trace = skipped_llm_trace(self._settings.llm, reason="deterministic_operation_command")
            return self._handle_deterministic_fallback(
                deterministic_perception,
                candidates=[
                    deterministic_candidate,
                    skipped_candidate(llm_source, "deterministic_operation_command"),
                ],
            )

        conversation_context = self._conversation_context()
        llm_result = self._plan_with_llm(text, conversation_context=conversation_context, now_fn=parser_now_fn)
        if "context" not in self.llm_trace:
            self.llm_trace["context"] = context_trace(conversation_context)
        if llm_result.perception is not None:
            return self._handle_llm_perception(
                llm_result.perception,
                deterministic_candidate,
                llm_first=True,
                text=text,
                now_fn=parser_now_fn,
            )
        if llm_result.error is not None:
            return self._handle_llm_first_error(
                text,
                llm_result.error,
                deterministic_candidate,
                deterministic_perception,
                deterministic_error,
                conversation_context,
            )
        llm_source = self._llm_source()
        llm_candidate = skipped_candidate(llm_source, str(self.llm_trace.get("reason") or "no_intent"))
        if deterministic_perception is not None:
            return self._handle_deterministic_fallback(
                deterministic_perception,
                candidates=[llm_candidate, deterministic_candidate],
            )
        self.trace = build_perception_trace(
            decision="needs_clarification",
            selected_source=None,
            selected_perception=None,
            candidates=[llm_candidate, deterministic_candidate],
        )
        if deterministic_error is not None:
            raise deterministic_error
        raise AgentToolError(code="NEEDS_CLARIFICATION", message="没有识别出可执行的只读命令。")

    def _deterministic_candidate(
        self,
        text: str,
        parser_now_fn: Callable[[], date] | None,
    ) -> tuple[Any, PerceptionResult | None, AgentToolError | None]:
        try:
            perception = parse_deterministic_text(text, now_fn=parser_now_fn)
        except AgentToolError as err:
            return error_candidate("deterministic", err), None, err
        return accepted_candidate("deterministic", perception), perception, None

    def _handle_deterministic_fallback(
        self,
        perception: PerceptionResult,
        *,
        candidates: list[Any],
    ) -> PerceptionResult:
        self.route = "deterministic"
        self.trace = build_perception_trace(
            decision="deterministic_fallback_selected",
            selected_source="deterministic",
            selected_perception=perception,
            candidates=candidates,
        )
        return perception

    def _handle_llm_first_error(
        self,
        text: str,
        llm_error: AgentToolError,
        deterministic_candidate: Any,
        deterministic_perception: PerceptionResult | None,
        deterministic_error: AgentToolError | None,
        conversation_context: dict[str, Any] | None,
    ) -> PerceptionResult:
        llm_source = self._llm_source()
        llm_error_candidate = error_candidate(llm_source, llm_error, reason=str(self.llm_trace.get("reason") or ""))
        if deterministic_perception is not None and _llm_error_allows_deterministic_fallback(
            llm_error,
            deterministic_perception,
        ):
            return self._handle_deterministic_fallback(
                deterministic_perception,
                candidates=[llm_error_candidate, deterministic_candidate],
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
                    deterministic_candidate,
                    accepted_candidate("llm_reply", reply_perception),
                ],
            )
            return reply_perception
        llm_error_details = llm_error.details if isinstance(llm_error.details, dict) else {}
        if (
            deterministic_error is not None
            and llm_error.code == "NEEDS_CLARIFICATION"
            and "clarification_request" not in llm_error_details
        ):
            self.route = "deterministic"
            self.trace = build_perception_trace(
                decision="needs_clarification",
                selected_source=None,
                selected_perception=None,
                candidates=[llm_error_candidate, deterministic_candidate],
            )
            raise deterministic_error
        self.trace = build_perception_trace(
            decision="llm_error",
            selected_source=None,
            selected_perception=None,
            candidates=[llm_error_candidate, deterministic_candidate],
        )
        raise llm_error

    def _handle_deterministic_error(
        self,
        text: str,
        err: AgentToolError,
        *,
        parser_now_fn: Callable[[], date] | None,
    ) -> PerceptionResult:
        deterministic_candidate = error_candidate("deterministic", err)
        if err.code != "NEEDS_CLARIFICATION":
            self.trace = build_perception_trace(
                decision="deterministic_error",
                selected_source=None,
                selected_perception=None,
                candidates=[
                    deterministic_candidate,
                    skipped_candidate("llm", "deterministic_error"),
                ],
            )
            raise err
        if looks_like_command(text):
            self.trace = build_perception_trace(
                decision="command_error",
                selected_source=None,
                selected_perception=None,
                candidates=[
                    skipped_candidate("command", "command_prefix"),
                    deterministic_candidate,
                    skipped_candidate("llm", "command_error"),
                ],
            )
            raise err
        conversation_context = self._conversation_context()
        llm_result = self._plan_with_llm(text, conversation_context=conversation_context, now_fn=parser_now_fn)
        if "context" not in self.llm_trace:
            self.llm_trace["context"] = context_trace(conversation_context)
        if llm_result.perception is not None:
            return self._handle_llm_perception(
                llm_result.perception,
                deterministic_candidate,
                text=text,
                now_fn=parser_now_fn,
            )
        if llm_result.error is not None:
            return self._handle_llm_error(text, llm_result.error, deterministic_candidate, conversation_context)
        self.trace = build_perception_trace(
            decision="needs_clarification",
            selected_source=None,
            selected_perception=None,
            candidates=[
                deterministic_candidate,
                skipped_candidate("agent_loop", str(self.llm_trace.get("reason") or "not_available")),
            ],
        )
        raise err

    def _conversation_context(self) -> dict[str, Any] | None:
        if (
            not self._settings.planner_enabled
            and self._plan_tools_fn is None
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
                    "confirmation_must_be_deterministic": True,
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

    def _plan_with_llm(
        self,
        text: str,
        *,
        conversation_context: dict[str, Any] | None,
        now_fn: Callable[[], date] | None,
    ) -> AgentLoopPlanningOutcome:
        if not self._settings.planner_enabled and self._plan_tools_fn is None:
            llm_result = AgentLoopPlanningOutcome(
                perception=None,
                trace=skipped_llm_trace(self._settings.llm, reason="planner_disabled"),
            )
            self.llm_trace = dict(llm_result.trace)
            return llm_result
        conversation_context = _with_perception_temporal_context(conversation_context, now_fn=now_fn)
        self.last_conversation_context = conversation_context
        loop_result = run_read_only_agent_loop(
            text,
            settings=self._settings,
            conversation_context=conversation_context,
            plan_tools_fn=self._plan_tools_fn,
            now_fn=now_fn,
        )
        self.llm_trace = dict(loop_result.trace)
        return loop_result.planning

    def _llm_source(self) -> str:
        return "agent_loop"

    def _handle_llm_perception(
        self,
        perception: PerceptionResult,
        deterministic_candidate: Any,
        *,
        llm_first: bool = False,
        text: str = "",
        now_fn: Callable[[], date] | None = None,
    ) -> PerceptionResult:
        self.route = "agent_loop"
        if llm_first:
            perception = _reconcile_llm_read_perception(
                perception,
                deterministic_candidate,
                text=text,
                today=now_fn() if now_fn is not None else date.today(),
            )
        llm_candidate = accepted_candidate(self.route, perception)
        candidates = [llm_candidate, deterministic_candidate] if llm_first else [deterministic_candidate, llm_candidate]
        try:
            ensure_llm_perception_allowed(perception)
        except AgentToolError as policy_err:
            self.trace = build_perception_trace(
                decision="llm_denied_by_policy",
                selected_source=None,
                selected_perception=None,
                candidates=[*candidates, error_candidate("policy", policy_err)],
            )
            raise
        conflict_err = _llm_preview_conflict_error(perception, deterministic_candidate)
        if conflict_err is not None:
            self.trace = build_perception_trace(
                decision="llm_conflict_needs_clarification",
                selected_source=None,
                selected_perception=None,
                candidates=[*candidates, error_candidate("policy", conflict_err)],
            )
            raise conflict_err
        self.trace = build_perception_trace(
            decision=f"{self.route}_selected",
            selected_source=self.route,
            selected_perception=perception,
            candidates=candidates,
        )
        return perception

    def _handle_llm_error(
        self,
        text: str,
        llm_error: AgentToolError,
        deterministic_candidate: Any,
        conversation_context: dict[str, Any] | None,
    ) -> PerceptionResult:
        llm_source = "agent_loop"
        llm_error_candidate = error_candidate(llm_source, llm_error, reason=str(self.llm_trace.get("reason") or ""))
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
                    deterministic_candidate,
                    llm_error_candidate,
                    accepted_candidate("llm_reply", reply_perception),
                ],
            )
            return reply_perception
        self.trace = build_perception_trace(
            decision="llm_error",
            selected_source=None,
            selected_perception=None,
            candidates=[deterministic_candidate, llm_error_candidate],
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
            message=f"LLM is not allowed to route intent: {perception.intent_name}",
            hint="AgentLoop planning is restricted to read-only capabilities plus explicitly allowed preview capabilities; confirm/apply operations require deterministic commands.",
            details={"intent_name": perception.intent_name},
        )


def _llm_preview_conflict_error(perception: PerceptionResult, deterministic_candidate: Any) -> AgentToolError | None:
    spec = _COMMAND_SPECS_BY_INTENT.get(perception.intent_name)
    if spec is None or not _is_llm_preview_perception_allowed(spec):
        return None
    deterministic_perception = getattr(deterministic_candidate, "perception", None)
    if not isinstance(deterministic_perception, PerceptionResult):
        return None
    if deterministic_perception.intent_name == perception.intent_name:
        return None
    return AgentToolError(
        code="NEEDS_CLARIFICATION",
        message="这句话同时像写入预览和其他操作，请确认要创建哪一种预览，或改成只读查询。",
        hint="写入预览只能创建待确认操作；确认、取消和应用必须使用明确的确认/取消命令。",
        details={
            "llm_intent_name": perception.intent_name,
            "deterministic_intent_name": deterministic_perception.intent_name,
        },
    )


def _is_llm_preview_perception_allowed(spec: Any) -> bool:
    return bool(is_llm_planner_preview_spec(spec))


def _reconcile_llm_read_perception(
    perception: PerceptionResult,
    deterministic_candidate: Any,
    *,
    text: str,
    today: date,
) -> PerceptionResult:
    deterministic_perception = getattr(deterministic_candidate, "perception", None)
    if not isinstance(deterministic_perception, PerceptionResult):
        return _reconcile_month_slot_from_text(perception, text=text, today=today)
    if deterministic_perception.intent_name != perception.intent_name:
        return _reconcile_month_slot_from_text(perception, text=text, today=today)
    spec = _COMMAND_SPECS_BY_INTENT.get(perception.intent_name)
    if spec is None or not spec.read_only:
        return _reconcile_month_slot_from_text(perception, text=text, today=today)

    merged, changes = _merge_argument_slots(
        llm_arguments=dict(perception.arguments or {}),
        deterministic_arguments=dict(deterministic_perception.arguments or {}),
    )
    if not changes:
        return _reconcile_month_slot_from_text(perception, text=text, today=today)
    evidence = dict(perception.evidence or {})
    evidence["argument_reconciliation"] = {
        "source": "deterministic_shadow",
        "filled": changes["filled"],
        "overridden": changes["overridden"],
    }
    return PerceptionResult(
        intent_name=perception.intent_name,
        arguments=merged,
        source=perception.source,
        confidence=perception.confidence,
        evidence=evidence,
    )


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


def _merge_argument_slots(
    *,
    llm_arguments: dict[str, Any],
    deterministic_arguments: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]] | None]:
    merged = dict(llm_arguments)
    filled: dict[str, Any] = {}
    overridden: dict[str, Any] = {}
    for key, deterministic_value in deterministic_arguments.items():
        if _is_empty_slot(deterministic_value):
            continue
        if key not in merged or _is_empty_slot(merged.get(key)):
            merged[key] = deterministic_value
            filled[key] = deterministic_value
            continue
        llm_value = merged.get(key)
        if isinstance(llm_value, dict) and isinstance(deterministic_value, dict):
            nested, nested_changes = _merge_argument_slots(
                llm_arguments=llm_value,
                deterministic_arguments=deterministic_value,
            )
            if nested_changes:
                merged[key] = nested
                if nested_changes["filled"]:
                    filled[key] = nested_changes["filled"]
                if nested_changes["overridden"]:
                    overridden[key] = nested_changes["overridden"]
            continue
        if llm_value != deterministic_value:
            merged[key] = deterministic_value
            overridden[key] = {"llm": llm_value, "deterministic": deterministic_value}

    if not filled and not overridden:
        return merged, None
    return merged, {"filled": filled, "overridden": overridden}


def _is_empty_slot(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, dict) and not any(not _is_empty_slot(item) for item in value.values()):
        return True
    return False


def _llm_error_allows_deterministic_fallback(
    err: AgentToolError,
    deterministic_perception: PerceptionResult,
) -> bool:
    details = err.details if isinstance(err.details, dict) else {}
    deterministic_spec = _COMMAND_SPECS_BY_INTENT.get(deterministic_perception.intent_name)
    if err.code == "NEEDS_CLARIFICATION":
        if "clarification_request" in details:
            return False
        if deterministic_spec is not None and is_llm_planner_preview_spec(deterministic_spec):
            return False
        return True
    if err.code in {"LLM_UNAVAILABLE", "LLM_PROVIDER_ERROR"}:
        return True
    return (
        err.code == "PERMISSION_DENIED"
        and details.get("llm_rejected_reason") == "known_non_executable_intent"
        and details.get("intent_name") == deterministic_perception.intent_name
    )


def _deterministic_operation_command_has_priority(perception: PerceptionResult) -> bool:
    spec = _COMMAND_SPECS_BY_INTENT.get(perception.intent_name)
    return bool(spec is not None and spec.operation_action in {"confirm", "cancel"})


__all__ = [
    "GenerateReplyFn",
    "PerceptionEngine",
    "ensure_llm_perception_allowed",
    "general_reply_allowed",
    "looks_like_command",
]
