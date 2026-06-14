from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any

from src.application.assistant.contracts import AssistantRequest
from src.application.assistant.evidence import EvidenceBundle
from src.application.assistant.operation_store import operation_summary


AGENT_SESSION_SCHEMA_VERSION = "om-agent-session-v1"


@dataclass(frozen=True)
class AgentSessionSnapshot:
    session_id: str
    request: dict[str, Any]
    goal: str
    task_state: str
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
    return AgentSessionSnapshot(
        session_id=session_id,
        request=request.public_payload(),
        goal=goal,
        task_state=_task_state(ok=ok, final_response=final_response),
        plan_revisions=revisions,
        tool_transcript=tuple(_tool_transcript(tool_events=tool_events, observations=observations)),
        task_contract=dict(task_contract or {}),
        evidence_bundle=evidence_bundle.trace_payload(),
        coverage=dict(coverage or {}),
        permission_state={
            "writes_allowed": False,
            "preview_operations_allowed": True,
            "pending_operation_ids": [],
            "apply_allowed": False,
        },
        answer_trace={
            "final_response": dict(final_response),
            "synthesis": dict(synthesis_trace),
            "followup_decisions": _followup_decisions(tool_events),
        },
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
    plan = {
        "goal": goal,
        "response_mode": "preview",
        "steps": [_preview_plan_step(step)],
    }
    final_response = {
        "status": "preview",
        "reason": "pending operator confirmation",
        "hook_results": [],
    }
    return AgentSessionSnapshot(
        session_id=_session_id(request=request, command_id=operation_id or command_id, goal=goal),
        request=request.public_payload(),
        goal=goal,
        task_state="waiting_for_permission",
        plan_revisions=(
            {
                "revision": 1,
                "source": "agent_loop",
                "reason": "preview operation plan",
                "plan": plan,
            },
        ),
        tool_transcript=(_preview_tool_transcript_item(step=step, response=response, receipt=receipt),),
        task_contract={},
        evidence_bundle=EvidenceBundle(scope={}, facts=(), datasets=()).trace_payload(),
        coverage={},
        permission_state={
            "writes_allowed": False,
            "preview_operations_allowed": True,
            "pending_operation_ids": [operation_id] if operation_id else [],
            "apply_allowed": False,
            "permission_request": _safe_permission_request(data.get("permission_request")),
        },
        answer_trace={
            "final_response": final_response,
            "synthesis": {
                "reason": "preview_permission_request",
                "hook_results": [],
            },
            "followup_decisions": [],
        },
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
    tool_name = _operation_tool_name(response=response, data=data)
    safe_payload = _safe_operation_payload(operation_id=operation_id, operation_type=operation_type, status=status, payload=payload)
    goal = _operation_goal(operation_type=operation_type, payload=payload, preview=preview)
    final_response = {
        "status": status,
        "reason": "operation readback",
        "hook_results": [dict(item) for item in hook_results if isinstance(item, dict)],
    }
    return AgentSessionSnapshot(
        session_id=_session_id(request=request, command_id=operation_id, goal=goal),
        request=request.public_payload(),
        goal=goal,
        task_state="done" if status in {"applied", "cancelled", "canceled"} else "failed",
        plan_revisions=(
            {
                "revision": 1,
                "source": "deterministic_operation",
                "reason": "operation readback",
                "plan": {
                    "goal": goal,
                    "response_mode": "readback",
                    "steps": [
                        {
                            "tool_name": tool_name,
                            "arguments": safe_payload,
                            "purpose": "record final operation state",
                        }
                    ],
                },
            },
        ),
        tool_transcript=(
            {
                "index": 1,
                "tool_name": tool_name,
                "payload": safe_payload,
                "authorized": True,
                "authorization_reason": "deterministic_operation_readback",
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
                "ok": bool(response.get("ok", False)) and str(postcheck.get("status") or "") == "pass",
                "error_code": None,
                "summary": {
                    "status": status,
                    "operation_id": operation_id,
                    "operation_type": operation_type,
                    "resolved_operation_id": data.get("resolved_operation_id"),
                },
            },
        ),
        task_contract={},
        evidence_bundle=EvidenceBundle(scope={}, facts=(), datasets=()).trace_payload(),
        coverage={},
        permission_state={
            "writes_allowed": False,
            "preview_operations_allowed": True,
            "pending_operation_ids": [],
            "apply_allowed": False,
            "operation_id": operation_id,
            "resolved_operation_id": data.get("resolved_operation_id"),
            "operation_status": status,
        },
        answer_trace={
            "final_response": final_response,
            "synthesis": {
                "reason": "operation_readback",
                "hook_results": [dict(item) for item in hook_results if isinstance(item, dict)],
            },
            "followup_decisions": [],
        },
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


__all__ = [
    "AGENT_SESSION_SCHEMA_VERSION",
    "AgentSessionSnapshot",
    "build_agent_session_snapshot",
    "build_operation_readback_agent_session_snapshot",
    "build_preview_agent_session_snapshot",
]
