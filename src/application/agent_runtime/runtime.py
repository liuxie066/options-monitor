from __future__ import annotations

from datetime import date
import re
from typing import Any, Callable

from src.application.agent_runtime.agent_loop import build_tool_observation, run_read_only_agent_loop
from src.application.agent_runtime.command_catalog import spec_by_intent
from src.application.agent_runtime.command_parser import parse_agent_command
from src.application.agent_runtime.conversation_context import build_conversation_context, context_trace
from src.application.agent_runtime.llm_reply import LlmReplyResult, generate_general_reply
from src.application.agent_runtime.llm_translator import LlmTranslationResult, skipped_llm_trace, translate_inbound_intent
from src.application.agent_runtime.settings import AgentRuntimeSettings
from src.application.agent_runtime.tool_policy import DEFAULT_TOOL_POLICY
from src.application.agent_tool_contracts import AgentToolError
from src.application.inbound.audit import InboundAuditStore
from src.application.inbound.contracts import InboundIntent, InboundRequest, InboundToolCall
from src.application.inbound.parser import parse_inbound_text
from src.application.inbound.router import ExecuteToolFn, handle_inbound_request
from src.application.tool_execution import execute_tool

TranslateIntentFn = Callable[[str, AgentRuntimeSettings, dict[str, Any] | None], LlmTranslationResult]
GenerateReplyFn = Callable[[str, AgentRuntimeSettings, dict[str, Any] | None], LlmReplyResult]
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


def handle_agent_message(
    request: InboundRequest,
    *,
    audit_store: InboundAuditStore | None = None,
    execute_tool_fn: ExecuteToolFn = execute_tool,
    allowed_senders: str | None = None,
    now_fn: Callable[[], date] | None = None,
    settings: AgentRuntimeSettings | None = None,
    translate_intent_fn: TranslateIntentFn | None = None,
    generate_reply_fn: GenerateReplyFn | None = None,
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
        response = _with_agent_runtime_meta(
            response,
            route="disabled",
            settings=runtime_settings,
            llm_trace=skipped_llm_trace(runtime_settings.llm, reason="runtime_disabled"),
        )
        _update_audit_response(store=store, response=response)
        return response

    route = "command" if _looks_like_command(request.text) else "deterministic"
    llm_trace = skipped_llm_trace(runtime_settings.llm, reason="command" if route == "command" else "not_needed")
    agent_loop_tool_events: list[dict[str, Any]] = []
    agent_loop_observations: list[dict[str, Any]] = []

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
            llm_mode = runtime_settings.mode in {"llm_router", "agent_loop"}
            conversation_context = (
                build_conversation_context(
                    request,
                    audit_store=store,
                    max_messages=runtime_settings.context_window_messages,
                )
                if llm_mode or translate_intent_fn is not None
                else None
            )
            if runtime_settings.mode == "agent_loop":
                loop_result = run_read_only_agent_loop(
                    text,
                    settings=runtime_settings,
                    conversation_context=conversation_context,
                    translate_intent_fn=translate_intent_fn,
                )
                llm_result = loop_result.translation
                llm_trace = dict(loop_result.trace)
            else:
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
                route = "agent_loop" if runtime_settings.mode == "agent_loop" else "llm"
                _ensure_llm_intent_allowed(llm_result.intent)
                return llm_result.intent
            if llm_result.error is not None:
                reply_result = _maybe_generate_general_reply(
                    text,
                    settings=runtime_settings,
                    translate_trace=llm_trace,
                    translate_error=llm_result.error,
                    generate_reply_fn=generate_reply_fn,
                    conversation_context=conversation_context,
                )
                if reply_result.response_text:
                    route = "llm_reply"
                    llm_trace = dict(reply_result.trace)
                    return InboundIntent(
                        name="small_talk",
                        arguments={
                            "kind": "llm_reply",
                            "response_text": reply_result.response_text,
                        },
                        parser="llm_reply",
                        confidence=1.0,
                    )
                raise llm_result.error
            raise

    router_execute_tool_fn = execute_tool_fn
    if runtime_settings.mode == "agent_loop":
        router_execute_tool_fn = _agent_loop_execute_tool_fn(
            execute_tool_fn=execute_tool_fn,
            tool_events=agent_loop_tool_events,
            observations=agent_loop_observations,
            should_trace=lambda: route == "agent_loop",
        )

    response = handle_inbound_request(
        request,
        audit_store=store,
        execute_tool_fn=router_execute_tool_fn,
        allowed_senders=allowed_senders,
        now_fn=now_fn,
        parse_intent_fn=_parse,
    )
    if agent_loop_tool_events:
        llm_trace = _merge_agent_loop_tool_events(llm_trace, agent_loop_tool_events, agent_loop_observations)
    response = _with_agent_runtime_meta(response, route=route, settings=runtime_settings, llm_trace=llm_trace)
    _update_audit_response(store=store, response=response)
    return response


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


def _maybe_generate_general_reply(
    text: str,
    *,
    settings: AgentRuntimeSettings,
    translate_trace: dict[str, Any],
    translate_error: AgentToolError,
    generate_reply_fn: GenerateReplyFn | None,
    conversation_context: dict[str, Any] | None,
) -> LlmReplyResult:
    if not _general_reply_allowed(text, translate_error=translate_error):
        return LlmReplyResult(
            response_text=None,
            trace={
                **dict(translate_trace),
                "reply": {
                    "attempted": False,
                    "reason": "blocked_by_safety_filter",
                },
            },
            error=translate_error,
        )
    if generate_reply_fn is not None:
        reply_result = generate_reply_fn(text, settings, conversation_context)
    else:
        reply_result = generate_general_reply(
            text,
            settings=settings.llm,
            conversation_context=conversation_context,
        )
    trace = dict(reply_result.trace)
    trace["intent_router"] = dict(translate_trace)
    if "context" not in trace:
        trace["context"] = context_trace(conversation_context)
    return LlmReplyResult(response_text=reply_result.response_text, trace=trace, error=reply_result.error)


def _general_reply_allowed(text: str, *, translate_error: AgentToolError) -> bool:
    if translate_error.code != "NEEDS_CLARIFICATION":
        return False
    raw = str(text or "").strip()
    if not raw or _looks_like_command(raw) or _OPERATION_ID_RE.search(raw):
        return False
    compact = re.sub(r"\s+", "", raw).lower()
    if any(token in compact for token in _BUSINESS_OR_WRITE_TOKENS):
        return False
    return True


def _agent_loop_execute_tool_fn(
    *,
    execute_tool_fn: ExecuteToolFn,
    tool_events: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    should_trace: Callable[[], bool],
) -> ExecuteToolFn:
    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not should_trace():
            return execute_tool_fn(tool_name, dict(payload or {}))
        call = InboundToolCall(tool_name=tool_name, payload=dict(payload or {}))
        try:
            decision = DEFAULT_TOOL_POLICY.authorize_read_tool(call, source="agent_loop")
        except AgentToolError as err:
            tool_events.append(
                {
                    "phase": "authorize_tool",
                    "tool_name": tool_name,
                    "allowed": False,
                    "error_code": err.code,
                }
            )
            raise
        tool_events.append(
            {
                "phase": "authorize_tool",
                "tool_name": tool_name,
                "allowed": True,
                "decision": decision.public_payload(),
            }
        )
        result = execute_tool_fn(tool_name, dict(payload or {}))
        observations.append(
            build_tool_observation(
                index=len(observations) + 1,
                tool_name=tool_name,
                payload=dict(payload or {}),
                result=result,
            ).public_payload()
        )
        error = result.get("error") if isinstance(result, dict) else None
        error_code = error.get("code") if isinstance(error, dict) else None
        tool_events.append(
            {
                "phase": "observe_tool_result",
                "tool_name": tool_name,
                "ok": bool(result.get("ok", False)) if isinstance(result, dict) else False,
                "error_code": str(error_code) if error_code else None,
            }
        )
        return result

    return _execute


def _merge_agent_loop_tool_events(
    llm_trace: dict[str, Any],
    tool_events: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    merged = dict(llm_trace)
    loop = dict(merged.get("agent_loop") if isinstance(merged.get("agent_loop"), dict) else {})
    loop.setdefault("enabled", True)
    loop.setdefault("planner", "llm_read_only_intent")
    loop["tool_events"] = [dict(item) for item in tool_events]
    loop["observations"] = [dict(item) for item in observations]
    loop["tool_calls_used"] = sum(1 for item in tool_events if item.get("phase") == "observe_tool_result")
    loop["writes_allowed"] = False
    final_response = dict(loop.get("final_response") if isinstance(loop.get("final_response"), dict) else {})
    final_response.update(
        {
            "status": "rendered",
            "reason": "canonical renderer produced the factual response",
            "canonical_renderer_required": True,
            "llm_may_summarize": False,
        }
    )
    loop["final_response"] = final_response
    merged["agent_loop"] = loop
    return merged


def _ensure_llm_intent_allowed(intent: InboundIntent) -> None:
    spec = _COMMAND_SPECS_BY_INTENT.get(intent.name)
    if spec is None or not spec.read_only or not spec.llm_allowed:
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message=f"LLM is not allowed to route intent: {intent.name}",
            hint="LLM routing is restricted to read-only command intents; write actions must use deterministic preview and confirm flows.",
            details={"intent_name": intent.name},
        )


def _update_audit_response(*, store: InboundAuditStore, response: dict[str, Any]) -> None:
    meta = response.get("meta")
    if isinstance(meta, dict) and bool(meta.get("idempotent_replay")):
        return
    data = response.get("data")
    command_id = data.get("command_id") if isinstance(data, dict) else None
    if not command_id:
        return
    store.update_response(command_id=str(command_id), response=response)


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
        "mode": settings.mode,
        "route": route,
        "llm": llm_meta,
        "context": dict(context_meta) if isinstance(context_meta, dict) else {"provided": False},
        "langgraph": "optional" if settings.mode == "agent_loop" else "disabled",
    }
    return {**response, "meta": meta}
