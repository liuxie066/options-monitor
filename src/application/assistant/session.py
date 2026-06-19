from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any

from src.application.assistant.contracts import AssistantRequest
from src.application.assistant.evidence import EvidenceBundle
from src.application.assistant.operation_lifecycle import build_action_lifecycle
from src.application.assistant.operation_store import operation_summary


AGENT_SESSION_SCHEMA_VERSION = "om-agent-session-v1"
AGENT_PROGRESS_SCHEMA_VERSION = "om-agent-progress-v1"
AGENT_CAPABILITY_SELECTION_SCHEMA_VERSION = "om-agent-capability-selection-v1"


@dataclass(frozen=True)
class AgentSessionSnapshot:
    session_id: str
    request: dict[str, Any]
    goal: str
    task_state: str
    capability_selection: dict[str, Any]
    progress: dict[str, Any]
    plan_revisions: tuple[dict[str, Any], ...]
    tool_transcript: tuple[dict[str, Any], ...]
    task_contract: dict[str, Any]
    evidence_bundle: dict[str, Any]
    coverage: dict[str, Any]
    permission_state: dict[str, Any]
    answer_trace: dict[str, Any]
    audit_ref: dict[str, Any]
    schema_version: str = AGENT_SESSION_SCHEMA_VERSION

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "request": dict(self.request),
            "goal": self.goal,
            "task_state": self.task_state,
            "capability_selection": dict(self.capability_selection),
            "progress": dict(self.progress),
            "plan_revisions": [dict(item) for item in self.plan_revisions],
            "tool_transcript": [dict(item) for item in self.tool_transcript],
            "task_contract": dict(self.task_contract),
            "evidence_bundle": dict(self.evidence_bundle),
            "coverage": dict(self.coverage),
            "permission_state": dict(self.permission_state),
            "answer_trace": dict(self.answer_trace),
            "audit_ref": dict(self.audit_ref),
        }


def build_agent_session_snapshot(
    *,
    request: AssistantRequest,
    command_id: str | None = None,
    question: str,
    plan: dict[str, Any],
    plan_revisions: list[dict[str, Any]] | None = None,
    tool_events: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    task_contract: dict[str, Any] | None = None,
    evidence_bundle: EvidenceBundle,
    coverage: dict[str, Any] | None = None,
    final_response: dict[str, Any],
    synthesis_trace: dict[str, Any],
    ok: bool,
) -> AgentSessionSnapshot:
    goal = str(plan.get("goal") or question or "").strip()
    session_id = _session_id(request=request, command_id=command_id, goal=goal)
    revisions = tuple(
        dict(item)
        for item in (
            plan_revisions
            if plan_revisions is not None
            else [
                {
                    "revision": 1,
                    "source": "agent_loop",
                    "reason": "initial bounded plan",
                    "plan": dict(plan),
                }
            ]
        )
    )
    task_state = _task_state(ok=ok, final_response=final_response)
    tool_transcript = tuple(_tool_transcript(tool_events=tool_events, observations=observations))
    permission_state = {
        "writes_allowed": False,
        "preview_operations_allowed": True,
        "pending_operation_ids": [],
        "apply_allowed": False,
    }
    followup_decisions = _followup_decisions(tool_events)
    answer_trace = {
        "final_response": dict(final_response),
        "synthesis": dict(synthesis_trace),
        "followup_decisions": followup_decisions,
        "answer_route": _answer_route(final_response=final_response, synthesis_trace=synthesis_trace),
        "scope_source": _scope_source(task_contract or {}),
        "loop_stop_reason": _loop_stop_reason(
            final_response=final_response,
            synthesis_trace=synthesis_trace,
            followup_decisions=followup_decisions,
            coverage=coverage,
            tool_events=tool_events,
        ),
        "repair_attempted": _repair_attempted(followup_decisions),
    }
    clarification_reason = _clarification_reason(final_response=final_response, followup_decisions=followup_decisions)
    if clarification_reason:
        answer_trace["clarification_reason"] = clarification_reason
    return AgentSessionSnapshot(
        session_id=session_id,
        request=request.public_payload(),
        goal=goal,
        task_state=task_state,
        capability_selection=_capability_selection_payload(plan_revisions=revisions, tool_events=tool_events),
        progress=_progress_payload(
            task_state=task_state,
            plan_revisions=revisions,
            tool_transcript=tool_transcript,
            coverage=coverage,
            permission_state=permission_state,
            answer_trace=answer_trace,
            tool_events=tool_events,
        ),
        plan_revisions=revisions,
        tool_transcript=tool_transcript,
        task_contract=dict(task_contract or {}),
        evidence_bundle=evidence_bundle.trace_payload(),
        coverage=dict(coverage or {}),
        permission_state=permission_state,
        answer_trace=answer_trace,
        audit_ref={
            "channel": request.channel,
            "message_id": request.message_id,
            "conversation_id": request.conversation_id,
        },
    )


def build_preview_agent_session_snapshot(
    *,
    request: AssistantRequest,
    command_id: str | None,
    question: str,
    agent_loop: dict[str, Any],
    response: dict[str, Any],
) -> AgentSessionSnapshot | None:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    receipt = agent_loop.get("preview_receipt") if isinstance(agent_loop.get("preview_receipt"), dict) else {}
    if not receipt:
        return None
    steps = [dict(item) for item in agent_loop.get("steps") or [] if isinstance(item, dict)]
    step = _preview_session_step(steps=steps, receipt=receipt)
    if not step:
        return None
    operation_id = str(receipt.get("operation_id") or data.get("operation_id") or command_id or "").strip()
    goal = _preview_goal(question=question, data=data, receipt=receipt)
    lifecycle = _safe_action_lifecycle(data.get("action_lifecycle")) or build_action_lifecycle(
        operation_id=operation_id,
        operation_type=str(receipt.get("operation_type") or data.get("operation_type") or ""),
        status="previewed",
        source="agent_session_preview",
    )
    plan = {
        "goal": goal,
        "plan_kind": "preview",
        "steps": [_preview_plan_step(step)],
    }
    final_response = {
        "status": "preview",
        "reason": "pending operator confirmation",
        "action_lifecycle": lifecycle,
        "hook_results": [],
    }
    task_state = "waiting_for_permission"
    plan_revisions = (
        {
            "revision": 1,
            "source": "agent_loop",
            "reason": "preview operation plan",
            "plan": plan,
        },
    )
    tool_transcript = (_preview_tool_transcript_item(step=step, response=response, receipt=receipt),)
    coverage: dict[str, Any] = {}
    permission_state = {
        "writes_allowed": False,
        "preview_operations_allowed": True,
        "pending_operation_ids": [operation_id] if operation_id else [],
        "apply_allowed": False,
        "action_lifecycle": lifecycle,
        "permission_request": _safe_permission_request(data.get("permission_request")),
    }
    answer_trace = {
        "final_response": final_response,
        "synthesis": {
            "reason": "preview_permission_request",
            "hook_results": [],
        },
        "followup_decisions": [],
        "answer_route": _answer_route(final_response=final_response, synthesis_trace={"reason": "preview_permission_request"}),
        "scope_source": _scope_source({}),
        "loop_stop_reason": "preview_requested",
        "repair_attempted": False,
    }
    return AgentSessionSnapshot(
        session_id=_session_id(request=request, command_id=operation_id or command_id, goal=goal),
        request=request.public_payload(),
        goal=goal,
        task_state=task_state,
        capability_selection=_capability_selection_payload(plan_revisions=plan_revisions, tool_events=[]),
        progress=_progress_payload(
            task_state=task_state,
            plan_revisions=plan_revisions,
            tool_transcript=tool_transcript,
            coverage=coverage,
            permission_state=permission_state,
            answer_trace=answer_trace,
            tool_events=[],
        ),
        plan_revisions=plan_revisions,
        tool_transcript=tool_transcript,
        task_contract={},
        evidence_bundle=EvidenceBundle(scope={}, facts=(), datasets=()).trace_payload(),
        coverage=coverage,
        permission_state=permission_state,
        answer_trace=answer_trace,
        audit_ref={
            "channel": request.channel,
            "message_id": request.message_id,
            "conversation_id": request.conversation_id,
        },
    )


def build_operation_readback_agent_session_snapshot(
    *,
    request: AssistantRequest,
    command_id: str | None,
    response: dict[str, Any],
    postcheck: dict[str, Any],
    hook_results: list[dict[str, Any]],
) -> AgentSessionSnapshot | None:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    status = str(data.get("status") or "").strip().lower()
    if status not in {"applied", "cancelled", "canceled", "failed", "expired"}:
        return None
    operation_id = str(data.get("operation_id") or data.get("resolved_operation_id") or command_id or "").strip()
    if not operation_id:
        return None
    operation_type = str(data.get("operation_type") or "").strip()
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    preview = data.get("preview") if isinstance(data.get("preview"), dict) else {}
    lifecycle = _safe_action_lifecycle(data.get("action_lifecycle")) or build_action_lifecycle(
        operation_id=operation_id,
        operation_type=operation_type,
        status=status,
        result=data.get("result") if isinstance(data.get("result"), dict) else None,
        source="agent_session_readback",
    )
    tool_name = _operation_tool_name(response=response, data=data)
    safe_payload = _safe_operation_payload(operation_id=operation_id, operation_type=operation_type, status=status, payload=payload)
    goal = _operation_goal(operation_type=operation_type, payload=payload, preview=preview)
    final_response = {
        "status": status,
        "reason": "operation readback",
        "action_lifecycle": lifecycle,
        "hook_results": [dict(item) for item in hook_results if isinstance(item, dict)],
    }
    task_state = "done" if status in {"applied", "cancelled", "canceled"} else "failed"
    plan_revisions = (
        {
            "revision": 1,
            "source": "deterministic_operation",
            "reason": "operation readback",
            "plan": {
                "goal": goal,
                "plan_kind": "readback",
                "steps": [
                    {
                        "tool_name": tool_name,
                        "arguments": safe_payload,
                        "purpose": "record final operation state",
                    }
                ],
            },
        },
    )
    tool_transcript = (
        {
            "index": 1,
            "tool_name": tool_name,
            "payload": safe_payload,
            "authorized": True,
            "authorization_reason": "deterministic_operation_readback",
            "risk_class": "READ_AUTO",
            "action_policy": {
                "allowed": True,
                "decision": "readback",
                "allowed_effect": "readback",
                "requires_confirmation": False,
                "apply_allowed": False,
                "authority": "deterministic_operation_response",
            },
            "action_safety": {},
            "precheck": {},
            "postcheck": dict(postcheck),
            "hook_results": [dict(item) for item in hook_results if isinstance(item, dict)],
            "evidence_summary": {
                "source_label": "OM 本地操作回执",
                "row_count": 1,
                "missing_data_count": 0,
            },
            "action_lifecycle": lifecycle,
            "ok": bool(response.get("ok", False)) and str(postcheck.get("status") or "") == "pass",
            "error_code": None,
            "summary": {
                "status": status,
                "operation_id": operation_id,
                "operation_type": operation_type,
                "resolved_operation_id": data.get("resolved_operation_id"),
            },
        },
    )
    coverage: dict[str, Any] = {}
    permission_state = {
        "writes_allowed": False,
        "preview_operations_allowed": True,
        "pending_operation_ids": [],
        "apply_allowed": False,
        "operation_id": operation_id,
        "resolved_operation_id": data.get("resolved_operation_id"),
        "operation_status": status,
        "action_lifecycle": lifecycle,
    }
    answer_trace = {
        "final_response": final_response,
        "synthesis": {
            "reason": "operation_readback",
            "hook_results": [dict(item) for item in hook_results if isinstance(item, dict)],
        },
        "followup_decisions": [],
        "answer_route": _answer_route(final_response=final_response, synthesis_trace={"reason": "operation_readback"}),
        "scope_source": _scope_source({}),
        "loop_stop_reason": "operation_readback",
        "repair_attempted": False,
    }
    return AgentSessionSnapshot(
        session_id=_session_id(request=request, command_id=operation_id, goal=goal),
        request=request.public_payload(),
        goal=goal,
        task_state=task_state,
        capability_selection=_capability_selection_payload(plan_revisions=plan_revisions, tool_events=[]),
        progress=_progress_payload(
            task_state=task_state,
            plan_revisions=plan_revisions,
            tool_transcript=tool_transcript,
            coverage=coverage,
            permission_state=permission_state,
            answer_trace=answer_trace,
            tool_events=[],
        ),
        plan_revisions=plan_revisions,
        tool_transcript=tool_transcript,
        task_contract={},
        evidence_bundle=EvidenceBundle(scope={}, facts=(), datasets=()).trace_payload(),
        coverage=coverage,
        permission_state=permission_state,
        answer_trace=answer_trace,
        audit_ref={
            "channel": request.channel,
            "message_id": request.message_id,
            "conversation_id": request.conversation_id,
        },
    )


def _preview_session_step(*, steps: list[dict[str, Any]], receipt: dict[str, Any]) -> dict[str, Any]:
    for step in steps:
        step_receipt = step.get("preview_receipt")
        if isinstance(step_receipt, dict) and step_receipt.get("operation_id") == receipt.get("operation_id"):
            return step
    return steps[0] if len(steps) == 1 else {}


def _preview_goal(*, question: str, data: dict[str, Any], receipt: dict[str, Any]) -> str:
    permission_request = data.get("permission_request") if isinstance(data.get("permission_request"), dict) else {}
    target = str(permission_request.get("target_summary") or receipt.get("target_summary") or "").strip()
    operation_type = str(receipt.get("operation_type") or data.get("operation_type") or "").strip()
    label = {
        "manual_open": "记录开仓预览",
        "manual_close": "记录平仓预览",
    }.get(operation_type, "写入预览")
    if target:
        return f"{label}：{target}"
    return label if question else "写入预览"


def _preview_plan_step(step: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool_name": step.get("tool_name"),
        "arguments": _safe_preview_payload(step.get("arguments")),
        "purpose": step.get("purpose"),
    }


def _preview_tool_transcript_item(
    *,
    step: dict[str, Any],
    response: dict[str, Any],
    receipt: dict[str, Any],
) -> dict[str, Any]:
    action_policy = step.get("action_policy") if isinstance(step.get("action_policy"), dict) else {}
    postcheck = step.get("postcheck") if isinstance(step.get("postcheck"), dict) else {}
    return {
        "index": step.get("index"),
        "tool_name": str(step.get("tool_name") or ""),
        "payload": _safe_preview_payload(step.get("arguments")),
        "authorized": True,
        "authorization_reason": action_policy.get("reason"),
        "risk_class": "SOFT_WRITE_PREVIEW",
        "action_policy": dict(action_policy),
        "action_safety": dict(step.get("action_safety") or {}) if isinstance(step.get("action_safety"), dict) else {},
        "precheck": dict(step.get("precheck") or {}) if isinstance(step.get("precheck"), dict) else {},
        "postcheck": dict(postcheck),
        "hook_results": [dict(item) for item in step.get("hook_results") or [] if isinstance(item, dict)],
        "evidence_summary": {
            "source_label": "OM 本地交易预览",
            "row_count": 1,
            "missing_data_count": 0,
        },
        "action_lifecycle": _safe_action_lifecycle(receipt.get("action_lifecycle")),
        "ok": bool(response.get("ok", False)) and str(postcheck.get("status") or "") == "pass",
        "error_code": None,
        "summary": {
            "status": receipt.get("status"),
            "operation_type": receipt.get("operation_type"),
            "confirm_required": bool(receipt.get("confirm_required")),
            "apply_allowed": bool(receipt.get("apply_allowed")),
        },
    }


def _safe_preview_payload(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    allowed = {
        "account",
        "symbol",
        "currency",
        "option_type",
        "side",
        "contracts",
        "strike",
        "expiration_ymd",
        "status",
    }
    return {key: payload.get(key) for key in sorted(allowed) if key in payload}


def _safe_permission_request(value: Any) -> dict[str, Any]:
    request = value if isinstance(value, dict) else {}
    allowed = {
        "schema_version",
        "operation_id",
        "operation_type",
        "risk_class",
        "safety_class",
        "confirm_required",
        "apply_allowed",
        "confirm_hint",
        "cancel_hint",
        "target_summary",
    }
    return {key: request.get(key) for key in sorted(allowed) if key in request}


def _safe_action_lifecycle(value: Any) -> dict[str, Any]:
    lifecycle = value if isinstance(value, dict) else {}
    if not lifecycle:
        return {}
    allowed = {
        "schema_version",
        "operation_id",
        "operation_type",
        "status",
        "phase",
        "stages",
        "required_next_action",
        "verify_status",
        "audit_status",
        "source",
        "result_status",
    }
    return {key: lifecycle.get(key) for key in sorted(allowed) if key in lifecycle}


def _operation_tool_name(*, response: dict[str, Any], data: dict[str, Any]) -> str:
    reasoning = data.get("reasoning") if isinstance(data.get("reasoning"), dict) else {}
    tool_call = reasoning.get("tool_call") if isinstance(reasoning.get("tool_call"), dict) else {}
    return str(response.get("tool_name") or tool_call.get("tool_name") or "operation.readback")


def _operation_goal(*, operation_type: str, payload: dict[str, Any], preview: dict[str, Any]) -> str:
    target = _operation_target(operation_type=operation_type, payload=payload, preview=preview)
    label = {
        "manual_open": "记录开仓预览",
        "manual_close": "记录平仓预览",
        "symbol_add": "监控标的预览",
        "symbol_edit": "监控标的预览",
        "symbol_remove": "监控标的预览",
        "model_use": "模型切换预览",
        "upgrade_now": "升级预览",
    }.get(operation_type, "写入预览")
    return f"{label}：{target}" if target else label


def _operation_target(*, operation_type: str, payload: dict[str, Any], preview: dict[str, Any]) -> str:
    args = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
    if args or preview:
        operation = {"operation_type": operation_type, "payload": dict(payload)}
        if preview:
            operation["preview"] = dict(preview)
        summary = operation_summary(operation).get("summary")
        summary_text = str(summary or "").strip()
        if summary_text and summary_text != "-":
            return summary_text
    account = str(args.get("account") or "").strip()
    symbol = str(args.get("symbol") or "").strip()
    if account and symbol:
        return f"{account} {symbol}"
    record_id = str(args.get("record_id") or "").strip()
    if record_id:
        return f"record_id {record_id}"
    model_name = str(args.get("model_name") or args.get("active_model") or "").strip()
    if model_name:
        return model_name
    target_version = str(args.get("target_version") or "").strip()
    if target_version:
        return target_version
    return ""


def _safe_operation_payload(
    *,
    operation_id: str,
    operation_type: str,
    status: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    out = {
        "operation_id": operation_id,
        "operation_type": operation_type,
        "status": status,
    }
    args = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
    allowed = {
        "account",
        "symbol",
        "currency",
        "option_type",
        "side",
        "position_side",
        "contracts",
        "contracts_to_close",
        "strike",
        "expiration_ymd",
        "record_id",
    }
    out.update({key: args.get(key) for key in sorted(allowed) if key in args})
    return out


def _session_id(*, request: AssistantRequest, command_id: str | None, goal: str) -> str:
    command_ref = str(command_id or "").strip()
    source = "\x1f".join(
        [
            str(request.channel or "").strip().lower(),
            str(request.sender_id or "").strip(),
            command_ref or str(request.message_id or "").strip(),
            str(request.conversation_id or "").strip(),
            goal,
        ]
    )
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]
    return f"as_{digest}"


def _task_state(*, ok: bool, final_response: dict[str, Any]) -> str:
    if not ok:
        return "failed"
    status = str(final_response.get("status") or "").strip()
    if status in {"needs_clarification", "clarify"}:
        return "asking_clarification"
    if status in {"pending_permission", "preview"}:
        return "waiting_for_permission"
    return "done"


def _answer_route(*, final_response: dict[str, Any], synthesis_trace: dict[str, Any]) -> str:
    status = str(final_response.get("status") or "").strip()
    if status in {"needs_clarification", "clarify"}:
        return "clarification"
    if status in {"pending_permission", "preview"}:
        return "preview"
    if status == "denied":
        return "denied"
    reason = str(synthesis_trace.get("reason") or "").strip()
    fallback = str(synthesis_trace.get("fallback") or "").strip()
    guard = synthesis_trace.get("answer_guard") if isinstance(synthesis_trace.get("answer_guard"), dict) else {}
    guard_status = str(guard.get("status") or "").strip()
    if guard_status == "failed_then_rewritten":
        return "llm_composer"
    deterministic_reasons = {
        "agent_renderer_fallback",
        "analysis_result_fallback",
        "task_contract_fallback",
        "grounded_renderer",
    }
    if fallback or reason in deterministic_reasons or "fallback" in reason:
        return "deterministic_renderer"
    if reason in {
        "agent_composed_response",
        "synthesized",
        "synthesized_after_answer_guard",
        "grounded_renderer_with_analysis",
    }:
        return "llm_composer"
    if status == "rendered":
        return "deterministic_renderer"
    if status == "synthesized":
        return "llm_composer"
    return status or "unknown"


def _scope_source(task_contract: dict[str, Any]) -> str:
    scope = task_contract.get("scope") if isinstance(task_contract.get("scope"), dict) else {}
    requested = [
        *_string_list(scope.get("requested_accounts")),
        *_string_list(scope.get("requested_symbols")),
        *_string_list(scope.get("requested_months")),
    ]
    if requested:
        return "user_text"
    planned = [
        *_string_list(scope.get("planned_accounts")),
        *_string_list(scope.get("planned_symbols")),
        *_string_list(scope.get("planned_months")),
    ]
    if planned:
        return "planner_scope"
    if _string_list(scope.get("config_keys")):
        return "request_context"
    if scope:
        return "contract"
    return "not_recorded"


def _clarification_reason(*, final_response: dict[str, Any], followup_decisions: list[dict[str, Any]]) -> str:
    for decision in reversed(followup_decisions):
        if not isinstance(decision, dict):
            continue
        reason = str(decision.get("clarification_reason") or "").strip()
        if reason:
            return reason
    request = final_response.get("clarification_request")
    if isinstance(request, dict):
        questions = request.get("questions") if isinstance(request.get("questions"), list) else []
        for question in questions:
            if not isinstance(question, dict):
                continue
            slot = str(question.get("slot") or "").strip()
            if slot == "scope":
                return "missing_scope"
            if slot:
                return f"missing_{slot}_scope"
    status = str(final_response.get("status") or "").strip()
    if status in {"needs_clarification", "clarify"}:
        return "needs_clarification"
    return ""


def _tool_transcript(*, tool_events: list[dict[str, Any]], observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    authorize_by_tool: dict[str, dict[str, Any]] = {}
    result_by_tool: dict[str, dict[str, Any]] = {}
    for event in tool_events:
        if not isinstance(event, dict):
            continue
        if event.get("phase") == "authorize_tool":
            tool_name = str(event.get("tool_name") or "")
            if tool_name:
                authorize_by_tool[tool_name] = dict(event)
        elif event.get("phase") == "observe_tool_result":
            tool_name = str(event.get("tool_name") or "")
            if tool_name:
                result_by_tool[tool_name] = dict(event)
    transcript: list[dict[str, Any]] = []
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        tool_name = str(observation.get("tool_name") or "")
        authorization = authorize_by_tool.get(tool_name, {})
        action_policy = authorization.get("action_policy")
        if not isinstance(action_policy, dict):
            action_policy = authorization.get("decision")
        if not isinstance(action_policy, dict):
            action_policy = {}
        action_safety = authorization.get("action_safety")
        if not isinstance(action_safety, dict):
            action_safety = {}
        result_event = result_by_tool.get(tool_name, {})
        precheck = authorization.get("precheck")
        postcheck = result_event.get("postcheck")
        evidence_summary = result_event.get("evidence_summary")
        authorization_hooks = authorization.get("hook_results") if isinstance(authorization.get("hook_results"), list) else []
        result_hooks = result_event.get("hook_results") if isinstance(result_event.get("hook_results"), list) else []
        hook_results = [
            dict(item)
            for item in [*authorization_hooks, *result_hooks]
            if isinstance(item, dict)
        ]
        transcript.append(
            {
                "index": observation.get("index"),
                "tool_name": tool_name,
                "payload": dict(observation.get("payload") or {}) if isinstance(observation.get("payload"), dict) else {},
                "authorized": bool(authorization.get("allowed", True)),
                "authorization_reason": action_policy.get("reason") or authorization.get("reason"),
                "risk_class": _risk_class(action_policy),
                "action_policy": dict(action_policy),
                "action_safety": dict(action_safety),
                "precheck": dict(precheck) if isinstance(precheck, dict) else {},
                "postcheck": dict(postcheck) if isinstance(postcheck, dict) else {},
                "hook_results": hook_results,
                "evidence_summary": dict(evidence_summary) if isinstance(evidence_summary, dict) else {},
                "ok": bool(observation.get("ok", False)),
                "error_code": observation.get("error_code"),
                "summary": dict(observation.get("summary") or {}) if isinstance(observation.get("summary"), dict) else {},
            }
        )
    return transcript


def _followup_decisions(tool_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for event in tool_events:
        if not isinstance(event, dict) or event.get("phase") != "followup_decision":
            continue
        payload = {key: value for key, value in event.items() if key != "phase"}
        decisions.append(dict(payload))
    return decisions


def _capability_selection_payload(
    *,
    plan_revisions: tuple[dict[str, Any], ...],
    tool_events: list[dict[str, Any]],
) -> dict[str, Any]:
    selected_capabilities: list[dict[str, Any]] = []
    selected_tools: set[str] = set()
    required: set[str] = set()
    satisfied: set[str] = set()
    rejected: list[dict[str, Any]] = []
    for revision in plan_revisions:
        if not isinstance(revision, dict):
            continue
        plan = revision.get("plan") if isinstance(revision.get("plan"), dict) else {}
        revision_id = revision.get("revision")
        for capability in plan.get("required_capabilities") or []:
            text = str(capability or "").strip()
            if text:
                required.add(text)
        effect = _plan_effect(plan)
        for step in plan.get("steps") or []:
            if not isinstance(step, dict):
                continue
            tool_name = str(step.get("tool_name") or "").strip()
            if not tool_name:
                continue
            selected_tools.add(tool_name)
            selected_capabilities.append(
                {
                    "tool_name": tool_name,
                    "revision": revision_id,
                    "effect": effect,
                    "risk_class": _risk_class_for_plan_step(tool_name=tool_name, effect=effect, tool_events=tool_events),
                    "selection_source": _selection_source_for_revision(revision),
                    "reason": _clip_text(step.get("purpose"), 160),
                }
            )
    for event in tool_events:
        if not isinstance(event, dict):
            continue
        if event.get("phase") == "assess_capabilities":
            required.update(str(item) for item in event.get("required") or [] if str(item).strip())
            satisfied.update(str(item) for item in event.get("satisfied") or [] if str(item).strip())
            for gap in event.get("gaps") or []:
                text = str(gap or "").strip()
                if text:
                    rejected.append({"capability_id": text, "reason": "capability gap"})
        if event.get("phase") == "authorize_tool" and event.get("allowed") is False:
            tool_name = str(event.get("tool_name") or "").strip()
            if tool_name:
                rejected.append(
                    {
                        "tool_name": tool_name,
                        "reason": str(event.get("error_code") or "permission denied"),
                    }
                )
    selected = _dedupe_capability_items(selected_capabilities)
    rejected_payload = _dedupe_capability_items(rejected)
    return {
        "schema_version": AGENT_CAPABILITY_SELECTION_SCHEMA_VERSION,
        "selected": selected,
        "required": sorted(required),
        "satisfied": sorted(satisfied),
        "rejected": rejected_payload,
        "selected_tools": sorted(selected_tools),
        "risk_classes": sorted({str(item.get("risk_class") or "") for item in selected if str(item.get("risk_class") or "")}),
        "selection_sources": _unique_strings(str(item.get("selection_source") or "") for item in selected if str(item.get("selection_source") or "")),
        "explanation": _capability_selection_explanation(
            plan_revisions=plan_revisions,
            selected=selected,
            required=required,
            satisfied=satisfied,
            rejected=rejected_payload,
        ),
    }


def _capability_selection_explanation(
    *,
    plan_revisions: tuple[dict[str, Any], ...],
    selected: list[dict[str, Any]],
    required: set[str],
    satisfied: set[str],
    rejected: list[dict[str, Any]],
) -> dict[str, Any]:
    effects = sorted({str(item.get("effect") or "read") for item in selected if isinstance(item, dict)})
    risk_classes = sorted({str(item.get("risk_class") or "") for item in selected if isinstance(item, dict) and str(item.get("risk_class") or "")})
    selection_sources = _unique_strings(str(item.get("selection_source") or "") for item in selected if isinstance(item, dict) and str(item.get("selection_source") or ""))
    sources = _unique_strings(
        str(item.get("source") or "agent_loop")
        for item in plan_revisions
        if isinstance(item, dict)
    )
    followup_count = sum(
        1
        for item in plan_revisions
        if isinstance(item, dict) and str(item.get("reason") or "").startswith("follow-up")
    )
    basis = ["plan_steps"]
    if required or satisfied:
        basis.append("capability_assessment")
    if rejected:
        basis.append("rejections")
    return {
        "selection_source": "plan_revisions",
        "plan_sources": sources,
        "revision_count": len(plan_revisions),
        "followup_revision_count": followup_count,
        "selected_tool_count": len({str(item.get("tool_name") or "") for item in selected if str(item.get("tool_name") or "")}),
        "selected_effects": effects,
        "risk_classes": risk_classes,
        "selection_sources": selection_sources,
        "required_count": len(required),
        "satisfied_count": len(satisfied),
        "rejected_count": len(rejected),
        "decision_basis": basis,
    }


def _plan_effect(plan: dict[str, Any]) -> str:
    kind = str(plan.get("plan_kind") or "").strip().lower()
    if kind in {"preview", "readback"}:
        return kind
    return "read"


def _selection_source_for_revision(revision: dict[str, Any]) -> str:
    reason = str(revision.get("reason") or "").strip().lower()
    if reason.startswith("follow-up"):
        return "evidence_gap"
    if "preview" in reason:
        return "message_text"
    if reason:
        return "task_contract"
    return "plan_revision"


def _risk_class_for_plan_step(*, tool_name: str, effect: str, tool_events: list[dict[str, Any]]) -> str:
    action_policy = _action_policy_for_tool(tool_name=tool_name, tool_events=tool_events)
    if action_policy:
        return _risk_class(action_policy)
    if effect == "preview":
        return "SOFT_WRITE_PREVIEW"
    if effect == "read":
        return "READ_AUTO"
    return "UNKNOWN"


def _action_policy_for_tool(*, tool_name: str, tool_events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(tool_events):
        if not isinstance(event, dict):
            continue
        if event.get("phase") != "authorize_tool":
            continue
        if str(event.get("tool_name") or "") != tool_name:
            continue
        action_policy = event.get("action_policy")
        if isinstance(action_policy, dict):
            return dict(action_policy)
        decision = event.get("decision")
        if isinstance(decision, dict):
            return dict(decision)
    return {}


def _risk_class(action_policy: dict[str, Any]) -> str:
    effect = str(action_policy.get("allowed_effect") or "").strip()
    risk = str(action_policy.get("risk_level") or "").strip()
    if effect in {"read", "none"} and risk in {"read_only", ""}:
        return "READ_AUTO"
    if effect == "preview" or risk == "preview_write":
        return "SOFT_WRITE_PREVIEW"
    if risk in {"confirm_write", "local_write"}:
        return "LEDGER_WRITE_CONFIRM"
    if risk in {"admin", "live_ops"}:
        return "ADMIN_CONFIRM"
    if not risk:
        return "UNKNOWN"
    return risk.upper()


def _progress_payload(
    *,
    task_state: str,
    plan_revisions: tuple[dict[str, Any], ...],
    tool_transcript: tuple[dict[str, Any], ...],
    coverage: dict[str, Any] | None,
    permission_state: dict[str, Any],
    answer_trace: dict[str, Any],
    tool_events: list[dict[str, Any]],
) -> dict[str, Any]:
    coverage_payload = coverage if isinstance(coverage, dict) else {}
    pending_operation_ids = _string_list(permission_state.get("pending_operation_ids"))
    blocked_by = _progress_blockers(
        task_state=task_state,
        tool_transcript=tool_transcript,
        coverage=coverage_payload,
        pending_operation_ids=pending_operation_ids,
        answer_trace=answer_trace,
        tool_events=tool_events,
    )
    next_action = _progress_next_action(
        task_state=task_state,
        coverage=coverage_payload,
        pending_operation_ids=pending_operation_ids,
        blocked_by=blocked_by,
        answer_trace=answer_trace,
    )
    return {
        "schema_version": AGENT_PROGRESS_SCHEMA_VERSION,
        "state": task_state,
        "summary": _progress_summary(task_state=task_state, coverage=coverage_payload, blocked_by=blocked_by),
        "plan_revision_count": len(plan_revisions),
        "planned_step_count": _planned_step_count(plan_revisions),
        "tool_call_count": len(tool_transcript),
        "completed_step_count": sum(1 for item in tool_transcript if item.get("ok") is True),
        "failed_step_count": sum(1 for item in tool_transcript if item.get("ok") is False),
        "denied_step_count": _denied_step_count(tool_transcript=tool_transcript, tool_events=tool_events),
        "coverage_status": str(coverage_payload.get("status") or "not_applicable"),
        "coverage_next_action": str(coverage_payload.get("next_action") or "not_applicable"),
        "pending_operation_ids": pending_operation_ids,
        "blocked_by": blocked_by,
        "next_action": next_action,
    }


def _planned_step_count(plan_revisions: tuple[dict[str, Any], ...]) -> int:
    count = 0
    for revision in plan_revisions:
        if not isinstance(revision, dict):
            continue
        plan = revision.get("plan") if isinstance(revision.get("plan"), dict) else {}
        steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
        count += len([item for item in steps if isinstance(item, dict)])
    return count


def _progress_blockers(
    *,
    task_state: str,
    tool_transcript: tuple[dict[str, Any], ...],
    coverage: dict[str, Any],
    pending_operation_ids: list[str],
    answer_trace: dict[str, Any],
    tool_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if pending_operation_ids:
        blockers.append({"kind": "permission", "next_action": "confirm_or_cancel", "count": len(pending_operation_ids)})
    final_response = answer_trace.get("final_response") if isinstance(answer_trace.get("final_response"), dict) else {}
    if str(final_response.get("status") or "") in {"needs_clarification", "clarify"}:
        blockers.append({"kind": "clarification", "reason": _clip_text(final_response.get("reason"), 120)})
    for event in tool_events:
        if not isinstance(event, dict):
            continue
        if event.get("phase") == "authorize_tool" and event.get("allowed") is False:
            blockers.append(
                {
                    "kind": "permission_denial",
                    "tool_name": str(event.get("tool_name") or ""),
                    "code": str(event.get("error_code") or "PERMISSION_DENIED"),
                }
            )
    for tool in tool_transcript:
        if not isinstance(tool, dict):
            continue
        if tool.get("authorized") is False:
            blockers.append(
                {
                    "kind": "permission_denial",
                    "tool_name": str(tool.get("tool_name") or ""),
                    "code": str(tool.get("error_code") or "PERMISSION_DENIED"),
                }
            )
        elif tool.get("ok") is False:
            blockers.append(
                {
                    "kind": "tool_failure",
                    "tool_name": str(tool.get("tool_name") or ""),
                    "code": str(tool.get("error_code") or "TOOL_FAILED"),
                }
            )
    coverage_status = str(coverage.get("status") or "")
    if coverage_status and coverage_status != "complete":
        for gap in coverage.get("gaps") or []:
            if not isinstance(gap, dict):
                continue
            blockers.append(
                {
                    "kind": "evidence_gap",
                    "gap_kind": str(gap.get("kind") or ""),
                    "recoverable_by": str(gap.get("recoverable_by") or ""),
                    "suggested_tool": str(gap.get("suggested_tool") or ""),
                }
            )
        for missing in coverage.get("missing") or []:
            text = str(missing or "").strip()
            if text:
                blockers.append({"kind": "missing_answer_key", "key": text})
    for decision in answer_trace.get("followup_decisions") or []:
        if not isinstance(decision, dict):
            continue
        status = str(decision.get("status") or "")
        if status == "accepted":
            continue
        blockers.append(
            {
                "kind": "followup_stop",
                "status": status,
                "reason": _clip_text(decision.get("reason"), 160),
            }
        )
    if task_state == "failed" and not blockers:
        blockers.append({"kind": "failed", "reason": "assistant task failed"})
    return _dedupe_progress_blockers(blockers)


def _progress_next_action(
    *,
    task_state: str,
    coverage: dict[str, Any],
    pending_operation_ids: list[str],
    blocked_by: list[dict[str, Any]],
    answer_trace: dict[str, Any],
) -> str:
    if pending_operation_ids:
        return "confirm_or_cancel"
    final_response = answer_trace.get("final_response") if isinstance(answer_trace.get("final_response"), dict) else {}
    if str(final_response.get("status") or "") in {"needs_clarification", "clarify"}:
        return "provide_clarification"
    blocker_kinds = {str(item.get("kind") or "") for item in blocked_by if isinstance(item, dict)}
    if "permission_denial" in blocker_kinds:
        return "inspect_denied_tool"
    if "tool_failure" in blocker_kinds:
        return "inspect_tool_failure"
    if str(coverage.get("next_action") or "") == "followup_tool":
        return "plan_followup_tool"
    if str(coverage.get("status") or "") not in {"", "complete"}:
        return "answer_with_missing_data"
    if task_state == "failed":
        return "inspect_failure"
    if task_state == "waiting_for_permission":
        return "confirm_or_cancel"
    if task_state == "asking_clarification":
        return "provide_clarification"
    return "none"


def _progress_summary(*, task_state: str, coverage: dict[str, Any], blocked_by: list[dict[str, Any]]) -> str:
    if task_state == "waiting_for_permission":
        return "等待人工确认或取消"
    if task_state == "asking_clarification":
        return "等待补充澄清信息"
    if task_state == "failed":
        return "执行失败，需要查看阻塞项"
    if blocked_by:
        return "已生成回答，但仍有证据或权限阻塞项"
    if str(coverage.get("status") or "") == "complete":
        return "已完成，证据覆盖完整"
    return "已完成"


def _denied_step_count(*, tool_transcript: tuple[dict[str, Any], ...], tool_events: list[dict[str, Any]]) -> int:
    count = sum(1 for item in tool_transcript if isinstance(item, dict) and item.get("authorized") is False)
    count += sum(
        1
        for item in tool_events
        if isinstance(item, dict) and item.get("phase") == "authorize_tool" and item.get("allowed") is False
    )
    return count


def _loop_stop_reason(
    *,
    final_response: dict[str, Any],
    synthesis_trace: dict[str, Any],
    followup_decisions: list[dict[str, Any]],
    coverage: dict[str, Any] | None,
    tool_events: list[dict[str, Any]],
) -> str:
    for event in reversed(tool_events):
        if not isinstance(event, dict):
            continue
        phase = str(event.get("phase") or "")
        if phase == "tool_budget_exhausted":
            return "tool_budget_exhausted"
        if phase == "tool_loop_guard" and event.get("allowed") is False:
            return "tool_loop_guard_rejected"
        if phase == "authorize_tool" and event.get("allowed") is False:
            return "permission_denied"
    for decision in reversed(followup_decisions):
        if not isinstance(decision, dict):
            continue
        status = str(decision.get("status") or "")
        if status == "accepted":
            continue
        reason = str(decision.get("reason") or "").strip()
        if status:
            return f"followup_{status}"
        if reason:
            return "followup_stopped"
    status = str(final_response.get("status") or "").strip()
    if status in {"needs_clarification", "clarify"}:
        return "clarification_requested"
    if status in {"pending_permission", "preview"}:
        return "preview_requested"
    coverage_payload = coverage if isinstance(coverage, dict) else {}
    coverage_status = str(coverage_payload.get("status") or "")
    if coverage_status and coverage_status != "complete":
        return "answer_with_evidence_gap"
    fallback = str(synthesis_trace.get("fallback") or "").strip()
    if fallback:
        return "fallback_response"
    return "model_or_renderer_answered"


def _repair_attempted(followup_decisions: list[dict[str, Any]]) -> bool:
    return any(isinstance(item, dict) and str(item.get("status") or "") == "accepted" for item in followup_decisions)


def _dedupe_progress_blockers(blockers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for blocker in blockers:
        key = tuple(sorted((str(k), str(v)) for k, v in blocker.items() if v not in (None, "", [], {})))
        if key in seen:
            continue
        seen.add(key)
        out.append({k: v for k, v in blocker.items() if v not in (None, "", [], {})})
    return out


def _dedupe_capability_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for item in items:
        clean = {key: value for key, value in item.items() if value not in (None, "", [], {})}
        key = tuple(sorted((str(k), str(v)) for k, v in clean.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _unique_strings(values: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _clip_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "..."


__all__ = [
    "AGENT_SESSION_SCHEMA_VERSION",
    "AGENT_CAPABILITY_SELECTION_SCHEMA_VERSION",
    "AGENT_PROGRESS_SCHEMA_VERSION",
    "AgentSessionSnapshot",
    "build_agent_session_snapshot",
    "build_operation_readback_agent_session_snapshot",
    "build_preview_agent_session_snapshot",
]
