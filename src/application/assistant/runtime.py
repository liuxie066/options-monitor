from __future__ import annotations

from datetime import date
from typing import Any, Callable

from src.application.assistant.agent_loop import (
    AgentLoopPlanFn,
    AgentLoopSynthesizeFn,
    INTERNAL_TOOL_PLAN_NAME,
    ToolExecutor,
    execute_tool_plan,
)
from src.application.assistant.perception_trace import (
    PerceptionTrace,
    build_assistant_decision,
)
from src.application.assistant.perception import GenerateReplyFn, PerceptionEngine, TranslateIntentFn
from src.application.assistant.llm_translator import skipped_llm_trace
from src.application.assistant.settings import AssistantSettings
from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.commands import spec_by_intent
from src.application.assistant.contracts import AssistantRequest, PerceptionResult
from src.application.assistant.audit import InboundAuditStore
from src.application.assistant.router import ExecuteToolFn, handle_assistant_request
from src.application.assistant.session_store import AgentSessionStore
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
    plan_tools_fn: AgentLoopPlanFn | None = None,
    synthesize_response_fn: AgentLoopSynthesizeFn | None = None,
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
    perception_engine = PerceptionEngine(
        request=request,
        audit_store=store,
        settings=runtime_settings,
        translate_intent_fn=translate_intent_fn,
        plan_tools_fn=plan_tools_fn,
        generate_reply_fn=generate_reply_fn,
    )

    router_execute_tool_fn = execute_tool_fn
    if runtime_settings.planner_enabled:
        router_execute_tool_fn = _agent_loop_execute_tool_fn(
            execute_tool_fn=execute_tool_fn,
            request=request,
            settings=runtime_settings,
            plan_tools_fn=plan_tools_fn,
            conversation_context_fn=lambda: perception_engine.last_conversation_context,
            synthesize_response_fn=synthesize_response_fn,
            tool_events=agent_loop_tool_events,
            observations=agent_loop_observations,
            should_trace=lambda: perception_engine.route == "agent_loop",
        )

    response = handle_assistant_request(
        request,
        audit_store=store,
        execute_tool_fn=router_execute_tool_fn,
        allowed_senders=allowed_senders,
        now_fn=now_fn,
        parse_perception_fn=perception_engine.perceive,
    )
    llm_trace = perception_engine.llm_trace
    if agent_loop_tool_events:
        llm_trace = _merge_agent_loop_tool_events(llm_trace, agent_loop_tool_events, agent_loop_observations)
    llm_trace = _merge_agent_loop_preview_receipt(llm_trace, response)
    response = _with_assistant_meta(
        response,
        route=perception_engine.route,
        settings=runtime_settings,
        llm_trace=llm_trace,
        perception_trace=perception_engine.trace,
    )
    _persist_agent_session(store=store, request=request, response=response)
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
        reply_context=dict(request.reply_context) if isinstance(request.reply_context, dict) else None,
    )


def _agent_loop_execute_tool_fn(
    *,
    execute_tool_fn: ExecuteToolFn,
    request: AssistantRequest,
    settings: AssistantSettings,
    plan_tools_fn: AgentLoopPlanFn | None,
    conversation_context_fn: Callable[[], dict[str, Any] | None],
    synthesize_response_fn: AgentLoopSynthesizeFn | None,
    tool_events: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    should_trace: Callable[[], bool],
) -> ExecuteToolFn:
    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not should_trace():
            return execute_tool_fn(tool_name, dict(payload or {}))
        if str(tool_name or "") == INTERNAL_TOOL_PLAN_NAME:
            result = execute_tool_plan(
                question=str((payload or {}).get("question") or request.text),
                request=request,
                plan_payload=dict((payload or {}).get("plan") or {}),
                command_id=str((payload or {}).get("_command_id") or "").strip() or None,
                settings=settings,
                conversation_context=conversation_context_fn(),
                execute_tool_fn=execute_tool_fn,
                plan_tools_fn=plan_tools_fn,
                synthesize_response_fn=synthesize_response_fn,
            )
            data = result.get("data") if isinstance(result, dict) else {}
            if isinstance(data, dict):
                tool_events.extend([dict(item) for item in data.get("tool_events") or [] if isinstance(item, dict)])
                observations.extend([dict(item) for item in data.get("observations") or [] if isinstance(item, dict)])
                final_response = data.get("final_response")
                if isinstance(final_response, dict):
                    tool_events.append({"phase": "final_response", **dict(final_response)})
            return result
        outcome = ToolExecutor(execute_tool_fn=execute_tool_fn, source="agent_loop").execute_read_tool(
            request=request,
            task_contract=None,
            index=len(observations) + 1,
            tool_name=tool_name,
            payload=dict(payload or {}),
        )
        tool_events.append(outcome.authorization_event)
        if not outcome.allowed:
            if outcome.error is not None:
                raise outcome.error
            raise AgentToolError(code="PERMISSION_DENIED", message=f"{tool_name} is not allowed through action policy")
        if outcome.observation is not None:
            observations.append(outcome.observation)
        if outcome.result_event is not None:
            tool_events.append(outcome.result_event)
        return outcome.result_payload or {}

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
    final_event = next((item for item in reversed(tool_events) if item.get("phase") == "final_response"), None)
    if isinstance(final_event, dict):
        final_response.update({key: value for key, value in final_event.items() if key != "phase"})
    else:
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


def _merge_agent_loop_preview_receipt(llm_trace: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    perception = data.get("perception") if isinstance(data.get("perception"), dict) else {}
    if perception.get("source") != "agent_loop_plan":
        return llm_trace
    permission_request = data.get("permission_request") if isinstance(data.get("permission_request"), dict) else None
    if permission_request is None:
        return llm_trace
    receipt = {
        "schema_version": "om-agent-preview-receipt-v1",
        "status": str(data.get("status") or "previewed"),
        "operation_id": str(data.get("operation_id") or permission_request.get("operation_id") or ""),
        "operation_type": str(permission_request.get("operation_type") or ""),
        "permission_request_schema": str(permission_request.get("schema_version") or ""),
        "risk_class": str(permission_request.get("risk_class") or ""),
        "safety_class": str(permission_request.get("safety_class") or ""),
        "confirm_required": bool(permission_request.get("confirm_required")),
        "apply_allowed": bool(permission_request.get("apply_allowed")),
        "handler_tool": str(response.get("tool_name") or ""),
        "receipt_source": "permission_request",
    }
    if permission_request.get("target_summary"):
        receipt["target_summary"] = str(permission_request.get("target_summary") or "")

    merged: dict[str, Any] = dict(llm_trace or {})
    loop_raw = merged.get("agent_loop")
    loop: dict[str, Any] = dict(loop_raw) if isinstance(loop_raw, dict) else {}
    if not loop:
        return llm_trace
    loop["preview_receipt"] = receipt
    steps: list[Any] = list(loop.get("steps") or [])
    selected_intent = str(perception.get("intent_name") or "")
    attached = False
    updated_steps: list[Any] = []
    for raw_step in steps:
        step = dict(raw_step) if isinstance(raw_step, dict) else raw_step
        if isinstance(step, dict) and not attached:
            if step.get("intent_name") == selected_intent or step.get("tool_name") == selected_intent:
                step["preview_receipt"] = receipt
                attached = True
        updated_steps.append(step)
    if not attached and len(updated_steps) == 1 and isinstance(updated_steps[0], dict):
        updated_steps[0]["preview_receipt"] = receipt
    if updated_steps:
        loop["steps"] = updated_steps
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


def _persist_agent_session(*, store: InboundAuditStore, request: AssistantRequest, response: dict[str, Any]) -> None:
    meta = response.get("meta")
    if isinstance(meta, dict) and bool(meta.get("idempotent_replay")):
        return
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    command_id = data.get("command_id")
    session = _agent_session_payload_from_response(data)
    if not session:
        return
    try:
        AgentSessionStore(store.path).upsert_snapshot(
            snapshot=session,
            command_id=str(command_id or ""),
            request=request,
            response=response,
        )
    except Exception as exc:
        meta = response.setdefault("meta", {})
        if isinstance(meta, dict):
            meta["agent_session_store_warning"] = f"{type(exc).__name__}: {exc}"


def _agent_session_payload_from_response(data: dict[str, Any]) -> dict[str, Any] | None:
    action = data.get("action") if isinstance(data.get("action"), dict) else {}
    result = action.get("result") if isinstance(action.get("result"), dict) else {}
    result_data = result.get("data") if isinstance(result.get("data"), dict) else {}
    session = result_data.get("agent_session") if isinstance(result_data.get("agent_session"), dict) else None
    return dict(session) if isinstance(session, dict) else None


def _with_assistant_meta(
    response: dict[str, Any],
    *,
    route: str,
    settings: AssistantSettings,
    llm_trace: dict[str, Any],
    perception_trace: PerceptionTrace | None = None,
) -> dict[str, Any]:
    meta_raw = response.get("meta")
    meta = dict(meta_raw) if isinstance(meta_raw, dict) else {}
    llm_meta = dict(llm_trace)
    context_meta = llm_meta.pop("context", {"provided": False})
    assistant_meta = {
        "enabled": bool(settings.enabled),
        "mode": settings.mode,
        "planner": settings.planner.public_payload(),
        "route": route,
        "llm": llm_meta,
        "context": dict(context_meta) if isinstance(context_meta, dict) else {"provided": False},
        "langgraph": "optional" if settings.planner_enabled else "disabled",
    }
    if perception_trace is not None:
        assistant_meta["perception_trace"] = perception_trace.public_payload()
    assistant_meta["decision"] = build_assistant_decision(
        route=route,
        perception_trace=perception_trace,
        llm_trace=llm_meta,
        intent_metadata=_intent_metadata(perception_trace.selected_perception if perception_trace else None),
    ).public_payload()
    meta["assistant"] = assistant_meta
    return {**response, "meta": meta}


def _intent_metadata(perception: PerceptionResult | None) -> dict[str, Any]:
    if perception is None:
        return {}
    spec = _COMMAND_SPECS_BY_INTENT.get(perception.intent_name)
    if spec is None:
        return {}
    return {
        "read_only": bool(spec.read_only),
        "risk_level": spec.risk_level or ("read_only" if spec.read_only else "unknown"),
        "operation_action": spec.operation_action,
        "operation_target": spec.operation_target,
        "llm_allowed": bool(spec.llm_allowed),
        "supported": bool(spec.supported),
    }
