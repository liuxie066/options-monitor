from __future__ import annotations

from datetime import date
from typing import Any, Callable

from src.application.agent_runtime.command_parser import parse_agent_command
from src.application.agent_runtime.conversation_context import build_conversation_context, context_trace
from src.application.agent_runtime.llm_translator import LlmTranslationResult, skipped_llm_trace, translate_inbound_intent
from src.application.agent_runtime.settings import AgentRuntimeSettings
from src.application.agent_tool_contracts import AgentToolError
from src.application.inbound.audit import InboundAuditStore
from src.application.inbound.contracts import InboundIntent, InboundRequest
from src.application.inbound.parser import parse_inbound_text
from src.application.inbound.router import ExecuteToolFn, handle_inbound_request
from src.application.tool_execution import execute_tool

TranslateIntentFn = Callable[[str, AgentRuntimeSettings, dict[str, Any] | None], LlmTranslationResult]


def handle_agent_message(
    request: InboundRequest,
    *,
    audit_store: InboundAuditStore | None = None,
    execute_tool_fn: ExecuteToolFn = execute_tool,
    allowed_senders: str | None = None,
    now_fn: Callable[[], date] | None = None,
    settings: AgentRuntimeSettings | None = None,
    translate_intent_fn: TranslateIntentFn | None = None,
) -> dict[str, Any]:
    runtime_settings = settings or AgentRuntimeSettings()
    store = audit_store or InboundAuditStore(request.audit_db)
    if not runtime_settings.enabled:
        response = handle_inbound_request(
            request,
            audit_store=store,
            execute_tool_fn=execute_tool_fn,
            allowed_senders=allowed_senders,
            now_fn=now_fn,
        )
        return _with_agent_runtime_meta(
            response,
            route="disabled",
            settings=runtime_settings,
            llm_trace=skipped_llm_trace(runtime_settings.llm, reason="runtime_disabled"),
        )

    route = "command" if _looks_like_command(request.text) else "deterministic"
    llm_trace = skipped_llm_trace(runtime_settings.llm, reason="command" if route == "command" else "not_needed")

    def _parse(text: str, parser_now_fn: Callable[[], date] | None) -> InboundIntent:
        nonlocal route, llm_trace
        command_intent = parse_agent_command(text, now_fn=parser_now_fn)
        if command_intent is not None:
            return command_intent
        try:
            return parse_inbound_text(text, now_fn=parser_now_fn)
        except AgentToolError as err:
            if err.code != "NEEDS_CLARIFICATION":
                raise
            conversation_context = (
                build_conversation_context(
                    request,
                    audit_store=store,
                    max_messages=runtime_settings.context_window_messages,
                )
                if runtime_settings.llm.enabled or translate_intent_fn is not None
                else None
            )
            llm_result = _translate_intent(
                text,
                settings=runtime_settings,
                translate_intent_fn=translate_intent_fn,
                conversation_context=conversation_context,
            )
            llm_trace = dict(llm_result.trace)
            if "context" not in llm_trace:
                llm_trace["context"] = context_trace(conversation_context)
            if llm_result.intent is not None:
                route = "llm"
                return llm_result.intent
            if llm_result.error is not None:
                raise llm_result.error
            raise

    response = handle_inbound_request(
        request,
        audit_store=store,
        execute_tool_fn=execute_tool_fn,
        allowed_senders=allowed_senders,
        now_fn=now_fn,
        parse_intent_fn=_parse,
    )
    return _with_agent_runtime_meta(response, route=route, settings=runtime_settings, llm_trace=llm_trace)


def _looks_like_command(text: str) -> bool:
    return str(text or "").lstrip().startswith("/")


def _translate_intent(
    text: str,
    *,
    settings: AgentRuntimeSettings,
    translate_intent_fn: TranslateIntentFn | None,
    conversation_context: dict[str, Any] | None,
) -> LlmTranslationResult:
    if translate_intent_fn is not None:
        return translate_intent_fn(text, settings, conversation_context)
    return translate_inbound_intent(text, settings=settings.llm, conversation_context=conversation_context)


def _with_agent_runtime_meta(
    response: dict[str, Any],
    *,
    route: str,
    settings: AgentRuntimeSettings,
    llm_trace: dict[str, Any],
) -> dict[str, Any]:
    meta_raw = response.get("meta")
    meta = dict(meta_raw) if isinstance(meta_raw, dict) else {}
    llm_meta = dict(llm_trace)
    context_meta = llm_meta.pop("context", {"provided": False})
    meta["agent_runtime"] = {
        "enabled": bool(settings.enabled),
        "route": route,
        "llm": llm_meta,
        "context": dict(context_meta) if isinstance(context_meta, dict) else {"provided": False},
        "langgraph": "disabled",
    }
    return {**response, "meta": meta}
