from __future__ import annotations

from datetime import date
import re
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.agent_loop import run_read_only_agent_loop
from src.application.assistant.audit import InboundAuditStore
from src.application.assistant.command_parser import parse_assistant_command
from src.application.assistant.commands import spec_by_intent
from src.application.assistant.contracts import AssistantIntent, AssistantRequest
from src.application.assistant.conversation_context import build_conversation_context, context_trace
from src.application.assistant.intent_arbitration import (
    IntentArbitration,
    accepted_candidate,
    build_intent_arbitration,
    error_candidate,
    skipped_candidate,
)
from src.application.assistant.llm_reply import LlmReplyResult, generate_general_reply
from src.application.assistant.llm_translator import LlmTranslationResult, skipped_llm_trace, translate_inbound_intent
from src.application.assistant.parser import parse_inbound_text
from src.application.assistant.settings import AssistantSettings

TranslateIntentFn = Callable[[str, AssistantSettings, dict[str, Any] | None], LlmTranslationResult]
GenerateReplyFn = Callable[[str, AssistantSettings, dict[str, Any] | None], LlmReplyResult]

_COMMAND_SPECS_BY_INTENT = spec_by_intent()
_OPERATION_ID_RE = re.compile(r"\bin_[A-Za-z0-9_.:-]+\b")
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
)


class IntentArbitrator:
    def __init__(
        self,
        *,
        request: AssistantRequest,
        audit_store: InboundAuditStore,
        settings: AssistantSettings,
        translate_intent_fn: TranslateIntentFn | None = None,
        generate_reply_fn: GenerateReplyFn | None = None,
    ) -> None:
        self._request = request
        self._audit_store = audit_store
        self._settings = settings
        self._translate_intent_fn = translate_intent_fn
        self._generate_reply_fn = generate_reply_fn
        self.route = "command" if looks_like_command(request.text) else "deterministic"
        self.llm_trace = skipped_llm_trace(settings.llm, reason="command" if self.route == "command" else "not_needed")
        self.arbitration: IntentArbitration | None = None

    def parse(self, text: str, parser_now_fn: Callable[[], date] | None) -> AssistantIntent:
        try:
            command_intent = parse_assistant_command(text, now_fn=parser_now_fn)
        except AgentToolError as err:
            self.arbitration = build_intent_arbitration(
                decision="command_error",
                selected_source=None,
                selected_intent=None,
                candidates=[
                    error_candidate("command", err),
                    skipped_candidate("deterministic", "command_error"),
                    skipped_candidate("llm", "command_error"),
                ],
            )
            raise
        if command_intent is not None:
            self.arbitration = build_intent_arbitration(
                decision="command_selected",
                selected_source="command",
                selected_intent=command_intent,
                candidates=[
                    accepted_candidate("command", command_intent),
                    skipped_candidate("deterministic", "command_selected"),
                    skipped_candidate("llm", "command_selected"),
                ],
            )
            return command_intent
        try:
            deterministic_intent = parse_inbound_text(text, now_fn=parser_now_fn)
            self.arbitration = build_intent_arbitration(
                decision="deterministic_selected",
                selected_source="deterministic",
                selected_intent=deterministic_intent,
                candidates=[
                    accepted_candidate("deterministic", deterministic_intent),
                    skipped_candidate("llm", "deterministic_selected"),
                ],
            )
            return deterministic_intent
        except AgentToolError as err:
            return self._handle_deterministic_error(text, err)

    def _handle_deterministic_error(self, text: str, err: AgentToolError) -> AssistantIntent:
        deterministic_candidate = error_candidate("deterministic", err)
        if err.code != "NEEDS_CLARIFICATION":
            self.arbitration = build_intent_arbitration(
                decision="deterministic_error",
                selected_source=None,
                selected_intent=None,
                candidates=[
                    deterministic_candidate,
                    skipped_candidate("llm", "deterministic_error"),
                ],
            )
            raise err
        if looks_like_command(text):
            self.arbitration = build_intent_arbitration(
                decision="command_error",
                selected_source=None,
                selected_intent=None,
                candidates=[
                    skipped_candidate("command", "command_prefix"),
                    deterministic_candidate,
                    skipped_candidate("llm", "command_error"),
                ],
            )
            raise err
        conversation_context = self._conversation_context()
        llm_result = self._translate(text, conversation_context=conversation_context)
        if "context" not in self.llm_trace:
            self.llm_trace["context"] = context_trace(conversation_context)
        if llm_result.intent is not None:
            return self._handle_llm_intent(llm_result.intent, deterministic_candidate)
        if llm_result.error is not None:
            return self._handle_llm_error(text, llm_result.error, deterministic_candidate, conversation_context)
        self.arbitration = build_intent_arbitration(
            decision="needs_clarification",
            selected_source=None,
            selected_intent=None,
            candidates=[
                deterministic_candidate,
                skipped_candidate(
                    "agent_loop" if self._settings.mode == "agent_loop" else "llm",
                    str(self.llm_trace.get("reason") or "not_available"),
                ),
            ],
        )
        raise err

    def _conversation_context(self) -> dict[str, Any] | None:
        llm_mode = self._settings.mode in {"llm_router", "agent_loop"}
        if not llm_mode and self._translate_intent_fn is None:
            return None
        return build_conversation_context(
            self._request,
            audit_store=self._audit_store,
            max_messages=self._settings.context_window_messages,
        )

    def _translate(self, text: str, *, conversation_context: dict[str, Any] | None) -> LlmTranslationResult:
        if self._settings.mode == "agent_loop":
            loop_result = run_read_only_agent_loop(
                text,
                settings=self._settings,
                conversation_context=conversation_context,
                translate_intent_fn=self._translate_intent_fn,
            )
            self.llm_trace = dict(loop_result.trace)
            return loop_result.translation
        if self._translate_intent_fn is not None:
            llm_result = self._translate_intent_fn(text, self._settings, conversation_context)
        else:
            llm_result = translate_inbound_intent(text, settings=self._settings.llm, conversation_context=conversation_context)
        self.llm_trace = dict(llm_result.trace)
        return llm_result

    def _handle_llm_intent(self, intent: AssistantIntent, deterministic_candidate: Any) -> AssistantIntent:
        self.route = "agent_loop" if self._settings.mode == "agent_loop" else "llm"
        llm_candidate = accepted_candidate(self.route, intent)
        try:
            ensure_llm_intent_allowed(intent)
        except AgentToolError as policy_err:
            self.arbitration = build_intent_arbitration(
                decision="llm_denied_by_policy",
                selected_source=None,
                selected_intent=None,
                candidates=[
                    deterministic_candidate,
                    llm_candidate,
                    error_candidate("policy", policy_err),
                ],
            )
            raise
        self.arbitration = build_intent_arbitration(
            decision=f"{self.route}_selected",
            selected_source=self.route,
            selected_intent=intent,
            candidates=[deterministic_candidate, llm_candidate],
        )
        return intent

    def _handle_llm_error(
        self,
        text: str,
        llm_error: AgentToolError,
        deterministic_candidate: Any,
        conversation_context: dict[str, Any] | None,
    ) -> AssistantIntent:
        llm_source = "agent_loop" if self._settings.mode == "agent_loop" else "llm"
        llm_error_candidate = error_candidate(llm_source, llm_error, reason=str(self.llm_trace.get("reason") or ""))
        reply_result = self._maybe_generate_general_reply(
            text,
            translate_error=llm_error,
            conversation_context=conversation_context,
        )
        if reply_result.response_text:
            self.route = "llm_reply"
            self.llm_trace = dict(reply_result.trace)
            reply_intent = AssistantIntent(
                name="small_talk",
                arguments={
                    "kind": "llm_reply",
                    "response_text": reply_result.response_text,
                },
                parser="llm_reply",
                confidence=1.0,
            )
            self.arbitration = build_intent_arbitration(
                decision="llm_reply_selected",
                selected_source="llm_reply",
                selected_intent=reply_intent,
                candidates=[
                    deterministic_candidate,
                    llm_error_candidate,
                    accepted_candidate("llm_reply", reply_intent),
                ],
            )
            return reply_intent
        self.arbitration = build_intent_arbitration(
            decision="llm_error",
            selected_source=None,
            selected_intent=None,
            candidates=[deterministic_candidate, llm_error_candidate],
        )
        raise llm_error

    def _maybe_generate_general_reply(
        self,
        text: str,
        *,
        translate_error: AgentToolError,
        conversation_context: dict[str, Any] | None,
    ) -> LlmReplyResult:
        if not general_reply_allowed(text, translate_error=translate_error):
            return LlmReplyResult(
                response_text=None,
                trace={
                    **dict(self.llm_trace),
                    "reply": {
                        "attempted": False,
                        "reason": "blocked_by_safety_filter",
                    },
                },
                error=translate_error,
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


def general_reply_allowed(text: str, *, translate_error: AgentToolError) -> bool:
    if translate_error.code != "NEEDS_CLARIFICATION":
        return False
    raw = str(text or "").strip()
    if not raw or looks_like_command(raw) or _OPERATION_ID_RE.search(raw):
        return False
    compact = re.sub(r"\s+", "", raw).lower()
    if any(token in compact for token in _BUSINESS_OR_WRITE_TOKENS):
        return False
    return True


def ensure_llm_intent_allowed(intent: AssistantIntent) -> None:
    spec = _COMMAND_SPECS_BY_INTENT.get(intent.name)
    if spec is None or not spec.read_only or not spec.llm_allowed:
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message=f"LLM is not allowed to route intent: {intent.name}",
            hint="LLM routing is restricted to read-only command intents; write actions must use deterministic preview and confirm flows.",
            details={"intent_name": intent.name},
        )


__all__ = [
    "GenerateReplyFn",
    "IntentArbitrator",
    "TranslateIntentFn",
    "ensure_llm_intent_allowed",
    "general_reply_allowed",
    "looks_like_command",
]
