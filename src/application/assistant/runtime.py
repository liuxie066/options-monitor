from __future__ import annotations

from datetime import date
from typing import Any, Callable

from src.application.assistant.agent_loop import build_tool_observation
from src.application.assistant.intent_arbitration import (
    IntentArbitration,
    build_assistant_decision,
)
from src.application.assistant.intent_arbitrator import GenerateReplyFn, IntentArbitrator, TranslateIntentFn
from src.application.assistant.llm_translator import skipped_llm_trace
from src.application.assistant.settings import AssistantSettings
from src.application.assistant.tool_policy import DEFAULT_TOOL_POLICY
from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.commands import spec_by_intent
from src.application.assistant.contracts import AssistantRequest, AssistantToolCall, SemanticFrame
from src.application.assistant.audit import InboundAuditStore
from src.application.assistant.router import ExecuteToolFn, handle_assistant_request
from src.application.tool_execution import execute_tool

_COMMAND_SPECS_BY_INTENT = spec_by_intent()


def handle_assistant_message(
    request: AssistantRequest,
    *,
    audit_store: InboundAuditStore | None = None,
    execute_tool_fn: ExecuteToolFn = execute_tool,
    allowed_senders: str | None = None,
    now_fn: Callable[[], date] | None = None,
    settings: AssistantSettings | None = None,
    translate_intent_fn: TranslateIntentFn | None = None,
    generate_reply_fn: GenerateReplyFn | None = None,
) -> dict[str, Any]:
    runtime_settings = settings or AssistantSettings()
    request = _request_with_default_market_scope(request, runtime_settings)
    store = audit_store or InboundAuditStore(request.audit_db)
    if not runtime_settings.enabled:
        response = handle_assistant_request(
            request,
            audit_store=store,
            execute_tool_fn=execute_tool_fn,
            allowed_senders=allowed_senders,
            now_fn=now_fn,
        )
        response = _with_assistant_meta(
            response,
            route="disabled",
            settings=runtime_settings,
            llm_trace=skipped_llm_trace(runtime_settings.llm, reason="runtime_disabled"),
        )
        _update_audit_response(store=store, response=response)
        return response

    agent_loop_tool_events: list[dict[str, Any]] = []
    agent_loop_observations: list[dict[str, Any]] = []
    arbitrator = IntentArbitrator(
        request=request,
        audit_store=store,
        settings=runtime_settings,
        translate_intent_fn=translate_intent_fn,
        generate_reply_fn=generate_reply_fn,
    )

    router_execute_tool_fn = execute_tool_fn
    if runtime_settings.mode == "agent_loop":
        router_execute_tool_fn = _agent_loop_execute_tool_fn(
            execute_tool_fn=execute_tool_fn,
            tool_events=agent_loop_tool_events,
            observations=agent_loop_observations,
            should_trace=lambda: arbitrator.route == "agent_loop",
        )

    response = handle_assistant_request(
        request,
        audit_store=store,
        execute_tool_fn=router_execute_tool_fn,
        allowed_senders=allowed_senders,
        now_fn=now_fn,
        parse_intent_fn=arbitrator.parse,
    )
    llm_trace = arbitrator.llm_trace
    if agent_loop_tool_events:
        llm_trace = _merge_agent_loop_tool_events(llm_trace, agent_loop_tool_events, agent_loop_observations)
    response = _with_assistant_meta(
        response,
        route=arbitrator.route,
        settings=runtime_settings,
        llm_trace=llm_trace,
        arbitration=arbitrator.arbitration,
    )
    _update_audit_response(store=store, response=response)
    return response


def _request_with_default_market_scope(request: AssistantRequest, settings: AssistantSettings) -> AssistantRequest:
    if request.config_path or request.config_key:
        return request
    scope = str(settings.default_market_scope or "").strip().lower()
    if scope not in {"us", "hk"}:
        return request
    return AssistantRequest(
        text=request.text,
        sender_id=request.sender_id,
        channel=request.channel,
        message_id=request.message_id,
        conversation_id=request.conversation_id,
        config_key=scope,
        config_path=request.config_path,
        audit_db=request.audit_db,
        assistant_config_path=request.assistant_config_path,
    )


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
        call = AssistantToolCall(tool_name=tool_name, payload=dict(payload or {}))
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
    merged: dict[str, Any] = dict(llm_trace or {})
    loop_raw = merged.get("agent_loop")
    loop: dict[str, Any] = dict(loop_raw) if isinstance(loop_raw, dict) else {}
    loop.setdefault("enabled", True)
    loop.setdefault("planner", "llm_read_only_intent")
    loop["tool_events"] = [dict(item) for item in tool_events]
    loop["observations"] = [dict(item) for item in observations]
    loop["tool_calls_used"] = sum(1 for item in tool_events if item.get("phase") == "observe_tool_result")
    loop["writes_allowed"] = False
    final_response_raw = loop.get("final_response")
    final_response: dict[str, Any] = dict(final_response_raw) if isinstance(final_response_raw, dict) else {}
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


def _update_audit_response(*, store: InboundAuditStore, response: dict[str, Any]) -> None:
    meta = response.get("meta")
    if isinstance(meta, dict) and bool(meta.get("idempotent_replay")):
        return
    data = response.get("data")
    command_id = data.get("command_id") if isinstance(data, dict) else None
    if not command_id:
        return
    store.update_response(command_id=str(command_id), response=response)


def _with_assistant_meta(
    response: dict[str, Any],
    *,
    route: str,
    settings: AssistantSettings,
    llm_trace: dict[str, Any],
    arbitration: IntentArbitration | None = None,
) -> dict[str, Any]:
    meta_raw = response.get("meta")
    meta = dict(meta_raw) if isinstance(meta_raw, dict) else {}
    llm_meta = dict(llm_trace)
    context_meta = llm_meta.pop("context", {"provided": False})
    assistant_meta = {
        "enabled": bool(settings.enabled),
        "mode": settings.mode,
        "route": route,
        "llm": llm_meta,
        "context": dict(context_meta) if isinstance(context_meta, dict) else {"provided": False},
        "langgraph": "optional" if settings.mode == "agent_loop" else "disabled",
    }
    if arbitration is not None:
        assistant_meta["arbitration"] = arbitration.public_payload()
    assistant_meta["decision"] = build_assistant_decision(
        route=route,
        arbitration=arbitration,
        llm_trace=llm_meta,
        intent_metadata=_intent_metadata(arbitration.selected_intent if arbitration else None),
    ).public_payload()
    meta["assistant"] = assistant_meta
    return {**response, "meta": meta}


def _intent_metadata(intent: SemanticFrame | None) -> dict[str, Any]:
    if intent is None:
        return {}
    spec = _COMMAND_SPECS_BY_INTENT.get(intent.name)
    if spec is None:
        return {}
    return {
        "read_only": bool(spec.read_only),
        "risk_level": spec.risk_level or ("read_only" if spec.read_only else "unknown"),
        "operation_action": spec.operation_action,
        "operation_target": spec.operation_target,
        "llm_allowed": bool(spec.llm_allowed),
    }
