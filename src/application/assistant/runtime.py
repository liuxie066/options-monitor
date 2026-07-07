from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.llm_trace import skipped_llm_trace
from src.application.assistant.perception_trace import (
    PerceptionTrace,
    build_assistant_decision,
)
from src.application.assistant.perception import PerceptionEngine
from src.application.assistant.settings import AssistantSettings
from src.application.assistant.capability_catalog import spec_by_intent
from src.application.assistant.contracts import AssistantRequest, AssistantTurnResult, PerceptionResult
from src.application.assistant.memory_proposals import (
    MEMORY_PROPOSAL_SCHEMA_VERSION,
    suggest_memory_proposals_from_text,
)
from src.application.assistant.audit import InboundAuditStore
from src.application.assistant.router import ExecuteToolFn, handle_assistant_request
from src.application.assistant.session import (
    build_operation_readback_agent_session_snapshot,
    build_preview_agent_session_snapshot,
)
from src.application.assistant.operation_lifecycle import build_action_lifecycle
from src.application.assistant.session_store import AgentSessionStore
from src.application.assistant.turn_result import (
    assistant_turn_result_from_response_payload,
    with_assistant_turn_result,
)
from src.application.assistant.verifier_hooks import hook_results_from_tool_check
from src.application.tool_execution import execute_tool

_COMMAND_SPECS_BY_INTENT = spec_by_intent()
TOOL_CHECK_SCHEMA_VERSION = "om-agent-tool-check-v1"


def handle_assistant_turn(
    request: AssistantRequest,
    *,
    audit_store: InboundAuditStore | None = None,
    execute_tool_fn: ExecuteToolFn = execute_tool,
    allowed_senders: str | None = None,
    now_fn: Callable[[], date] | None = None,
    settings: AssistantSettings | None = None,
    memory_suggestion_dir: str | Path | None = None,
) -> AssistantTurnResult:
    response = _run_assistant_turn_response(
        request,
        audit_store=audit_store,
        execute_tool_fn=execute_tool_fn,
        allowed_senders=allowed_senders,
        now_fn=now_fn,
        settings=settings,
        memory_suggestion_dir=memory_suggestion_dir,
    )
    return assistant_turn_result_from_response_payload(response)


def _run_assistant_turn_response(
    request: AssistantRequest,
    *,
    audit_store: InboundAuditStore | None = None,
    execute_tool_fn: ExecuteToolFn = execute_tool,
    allowed_senders: str | None = None,
    now_fn: Callable[[], date] | None = None,
    settings: AssistantSettings | None = None,
    memory_suggestion_dir: str | Path | None = None,
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
        response = with_assistant_turn_result(response, route="disabled")
        _update_audit_response(store=store, response=response)
        return response

    perception_engine = PerceptionEngine(
        request=request,
        audit_store=store,
        settings=runtime_settings,
    )

    response = handle_assistant_request(
        request,
        audit_store=store,
        execute_tool_fn=execute_tool_fn,
        allowed_senders=allowed_senders,
        now_fn=now_fn,
        parse_perception_fn=perception_engine.perceive,
    )
    llm_trace = perception_engine.llm_trace
    response = _with_assistant_meta(
        response,
        route=perception_engine.route,
        settings=runtime_settings,
        llm_trace=llm_trace,
        perception_trace=perception_engine.trace,
    )
    response = _with_memory_suggestion(
        response,
        request=request,
        memory_dir=memory_suggestion_dir,
    )
    response = with_assistant_turn_result(response, route=perception_engine.route)
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


def _with_memory_suggestion(
    response: dict[str, Any],
    *,
    request: AssistantRequest,
    memory_dir: str | Path | None,
) -> dict[str, Any]:
    meta = response.get("meta") if isinstance(response.get("meta"), dict) else {}
    if bool(meta.get("idempotent_replay")):
        return response
    if not _memory_suggestion_allowed_response(response):
        return response
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    command_id = str(data.get("command_id") or "").strip() or None
    try:
        suggestion = suggest_memory_proposals_from_text(
            text=request.text,
            memory_dir=memory_dir,
            source_turn=command_id,
            max_suggestions=1,
            write=True,
        )
    except AgentToolError as exc:
        public = {
            "schema_version": MEMORY_PROPOSAL_SCHEMA_VERSION,
            "source": "explicit_turn_text",
            "status": "failed",
            "error": {
                "code": exc.code,
                "message": exc.message,
            },
        }
    except Exception as exc:
        public = {
            "schema_version": MEMORY_PROPOSAL_SCHEMA_VERSION,
            "source": "explicit_turn_text",
            "status": "failed",
            "error": {
                "code": type(exc).__name__,
                "message": str(exc),
            },
        }
    else:
        public = _public_memory_suggestion(suggestion)
        if public is None:
            return response

    updated = dict(response)
    updated_data = dict(data)
    updated_data["memory_suggestion"] = public
    updated_text = _response_text_with_memory_suggestion(
        str(updated_data.get("response_text") or ""),
        public,
    )
    if updated_text:
        updated_data["response_text"] = updated_text
        observation = updated_data.get("observation")
        if isinstance(observation, dict):
            updated_observation = dict(observation)
            updated_observation["response_text"] = updated_text
            updated_data["observation"] = updated_observation
    updated["data"] = updated_data
    updated_meta = dict(meta)
    assistant_meta = dict(updated_meta.get("assistant") or {})
    assistant_meta["memory_suggestion"] = public
    updated_meta["assistant"] = assistant_meta
    updated["meta"] = updated_meta
    return updated


def _memory_suggestion_allowed_response(response: dict[str, Any]) -> bool:
    if not bool(response.get("ok")):
        return False
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    decision = data.get("decision") if isinstance(data.get("decision"), dict) else {}
    sender = decision.get("sender") if isinstance(decision.get("sender"), dict) else {}
    if sender and bool(sender.get("allowed")) is False:
        return False
    return True


def _public_memory_suggestion(suggestion: dict[str, Any]) -> dict[str, Any] | None:
    skipped = suggestion.get("skipped") if isinstance(suggestion.get("skipped"), list) else []
    reasons = [
        str(item.get("reason") or "").strip()
        for item in skipped
        if isinstance(item, dict) and str(item.get("reason") or "").strip()
    ]
    if reasons == ["missing_explicit_memory_signal"]:
        return None
    proposals = suggestion.get("proposals") if isinstance(suggestion.get("proposals"), list) else []
    public_proposals = [
        {
            "proposal_id": str(item.get("proposal_id") or ""),
            "memory_id": str(item.get("memory_id") or ""),
            "type": str(item.get("type") or ""),
            "title": str(item.get("title") or ""),
            "status": str(item.get("status") or ""),
        }
        for item in proposals
        if isinstance(item, dict)
    ]
    status = "proposed" if public_proposals else "skipped"
    return {
        "schema_version": MEMORY_PROPOSAL_SCHEMA_VERSION,
        "source": "explicit_turn_text",
        "status": status,
        "proposal_count": len(public_proposals),
        "proposals": public_proposals,
        "skipped_reasons": reasons,
        "write_applied": bool(suggestion.get("write_applied")),
        "requires_accept": True,
        "accept_hint": "./om assistant memory accept <proposal_id>",
    }


def _response_text_with_memory_suggestion(response_text: str, suggestion: dict[str, Any]) -> str:
    note = _memory_suggestion_response_note(suggestion)
    if not note:
        return response_text
    text = str(response_text or "").strip()
    return f"{text}\n\n{note}".strip() if text else note


def _memory_suggestion_response_note(suggestion: dict[str, Any]) -> str:
    status = str(suggestion.get("status") or "").strip()
    proposals = suggestion.get("proposals") if isinstance(suggestion.get("proposals"), list) else []
    if status == "proposed" and proposals:
        proposal = proposals[0] if isinstance(proposals[0], dict) else {}
        proposal_id = str(proposal.get("proposal_id") or "").strip()
        memory_type = str(proposal.get("type") or "").strip()
        if proposal_id:
            return (
                f"记忆建议已创建：{proposal_id}"
                f"{f'（{memory_type}）' if memory_type else ''}。"
                "需显式 accept 后才会生效。"
            )
    if status == "skipped":
        reasons = suggestion.get("skipped_reasons") if isinstance(suggestion.get("skipped_reasons"), list) else []
        reason = str(reasons[0] if reasons else "").strip()
        if reason:
            return f"未创建记忆建议：{_memory_suggestion_skip_reason_text(reason)}。"
    if status == "failed":
        error = suggestion.get("error") if isinstance(suggestion.get("error"), dict) else {}
        code = str(error.get("code") or "failed").strip()
        return f"记忆建议创建失败：{code}。"
    return ""


def _memory_suggestion_skip_reason_text(reason: str) -> str:
    return {
        "runtime_or_market_fact": "内容像当前市场或运行态事实，应通过 OM 工具查询",
        "config_or_parameter_value": "内容像具体配置或参数值，不应作为长期记忆",
        "sensitive_material": "内容包含疑似敏感材料",
        "too_short": "内容过短",
        "limit_zero": "建议数量限制为 0",
    }.get(reason, reason)


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
    command_id = _agent_session_command_id(data)
    session = _agent_session_payload_from_response(request=request, response=response)
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


def _agent_session_command_id(data: dict[str, Any]) -> Any:
    status = str(data.get("status") or "").strip().lower()
    if status in {"previewed", "applied", "cancelled", "canceled", "failed", "expired"} and data.get("operation_id"):
        return data.get("operation_id")
    return data.get("command_id") or data.get("operation_id")


def _agent_session_payload_from_response(*, request: AssistantRequest, response: dict[str, Any]) -> dict[str, Any] | None:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    action = data.get("action") if isinstance(data.get("action"), dict) else {}
    result = action.get("result") if isinstance(action.get("result"), dict) else {}
    result_data = result.get("data") if isinstance(result.get("data"), dict) else {}
    session = result_data.get("agent_session") if isinstance(result_data.get("agent_session"), dict) else None
    if isinstance(session, dict):
        return dict(session)
    operation_postcheck = _operation_readback_postcheck(response=response)
    if operation_postcheck:
        operation_session = build_operation_readback_agent_session_snapshot(
            request=request,
            command_id=str(data.get("operation_id") or data.get("command_id") or "").strip() or None,
            response=response,
            postcheck=operation_postcheck,
            hook_results=hook_results_from_tool_check(operation_postcheck),
        )
        if operation_session is not None:
            return operation_session.public_payload()
    preview_session = build_preview_agent_session_snapshot(
        request=request,
        command_id=str(data.get("operation_id") or data.get("command_id") or "").strip() or None,
        question=str(request.text or ""),
        response=response,
    )
    return preview_session.public_payload() if preview_session is not None else None


def _operation_readback_postcheck(*, response: dict[str, Any]) -> dict[str, Any] | None:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    status = str(data.get("status") or "").strip().lower()
    final_statuses = {"applied", "cancelled", "canceled", "failed", "expired"}
    if status not in final_statuses:
        return None
    operation_id = str(data.get("operation_id") or data.get("resolved_operation_id") or "").strip()
    operation_type = str(data.get("operation_type") or "").strip()
    response_ok = bool(response.get("ok", False))
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    result_status = str(result.get("status") or status).strip().lower()
    lifecycle = build_action_lifecycle(
        operation_id=operation_id,
        operation_type=operation_type,
        status=status,
        result=result,
        source="operation_readback_postcheck",
    )
    checks = [
        {
            "name": "result_status",
            "status": "pass" if response_ok else "fail",
            "code": "ok" if response_ok else "response_failed",
        },
        {
            "name": "operation_readback",
            "status": "pass" if operation_id and status in final_statuses else "fail",
            "code": status if operation_id else "missing_operation_id",
        },
        {
            "name": "operation_identity",
            "status": "pass" if operation_id and operation_type else "fail",
            "code": "complete" if operation_id and operation_type else "missing_operation_identity",
        },
        {
            "name": "final_status",
            "status": "pass" if result_status in final_statuses else "fail",
            "code": result_status or "missing_result_status",
        },
        {
            "name": "action_lifecycle",
            "status": "pass"
            if lifecycle.get("schema_version") == "om-agent-action-lifecycle-v1"
            and lifecycle.get("status") in final_statuses
            and lifecycle.get("operation_id") == operation_id
            else "fail",
            "code": str(lifecycle.get("phase") or "missing_action_lifecycle"),
        },
    ]
    return {
        "schema_version": TOOL_CHECK_SCHEMA_VERSION,
        "stage": "post_tool",
        "status": "pass" if all(item["status"] == "pass" for item in checks) else "fail",
        "checks": checks,
        "tool_name": str(response.get("tool_name") or ""),
        "operation_id_present": bool(operation_id),
        "operation_type": operation_type,
        "operation_status": status,
        "result_status": result_status,
        "action_lifecycle": lifecycle,
    }


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
        "freeform_runtime": {**settings.freeform_runtime.public_payload(), "execution_enabled": False},
        "planner": settings.planner.public_payload(),
        "route": route,
        "llm": llm_meta,
        "context": dict(context_meta) if isinstance(context_meta, dict) else {"provided": False},
        "freeform_execution": "disabled",
    }
    if perception_trace is not None:
        assistant_meta["perception_trace"] = perception_trace.public_payload()
    selected_perception = perception_trace.selected_perception if perception_trace else None
    effective_perception = _effective_perception_from_response(response, fallback=selected_perception)
    decision_trace = perception_trace
    if perception_trace is not None and effective_perception is not None and effective_perception != selected_perception:
        decision_trace = PerceptionTrace(
            decision=perception_trace.decision,
            selected_source=perception_trace.selected_source,
            selected_perception=effective_perception,
            candidates=perception_trace.candidates,
        )
    assistant_meta["decision"] = build_assistant_decision(
        route=route,
        perception_trace=decision_trace,
        llm_trace=llm_meta,
        intent_metadata=_intent_metadata(effective_perception),
    ).public_payload()
    meta["assistant"] = assistant_meta
    return {**response, "meta": meta}


def _effective_perception_from_response(
    response: dict[str, Any],
    *,
    fallback: PerceptionResult | None,
) -> PerceptionResult | None:
    data = response.get("data") if isinstance(response, dict) else None
    payload = data.get("perception") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        return fallback
    intent_name = str(payload.get("intent_name") or "").strip()
    if not intent_name:
        return fallback
    arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else None
    try:
        confidence = float(payload.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 1.0
    return PerceptionResult(
        intent_name=intent_name,
        arguments=dict(arguments),
        source=str(payload.get("source") or "unknown"),
        confidence=confidence,
        evidence=dict(evidence) if evidence is not None else None,
    )


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
