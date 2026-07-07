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


def build_preview_agent_session_snapshot(
    *,
    request: AssistantRequest,
    command_id: str | None,
    question: str,
    response: dict[str, Any],
) -> AgentSessionSnapshot | None:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    permission_request = data.get("permission_request") if isinstance(data.get("permission_request"), dict) else {}
    if not permission_request or str(data.get("status") or "").strip() != "previewed":
        return None

    operation_id = str(data.get("operation_id") or permission_request.get("operation_id") or command_id or "").strip()
    operation_type = str(data.get("operation_type") or permission_request.get("operation_type") or "").strip()
    lifecycle = _safe_action_lifecycle(data.get("action_lifecycle")) or build_action_lifecycle(
        operation_id=operation_id,
        operation_type=operation_type,
        status="previewed",
        source="agent_session_preview",
    )
    receipt = {
        "operation_id": operation_id,
        "operation_type": operation_type,
        "status": data.get("status"),
        "target_summary": permission_request.get("target_summary"),
        "confirm_required": permission_request.get("confirm_required"),
        "apply_allowed": permission_request.get("apply_allowed"),
        "action_lifecycle": lifecycle,
    }
    goal = _preview_goal(question=question, data=data, receipt=receipt)
    safe_payload = _safe_preview_payload(data.get("payload"))
    tool_name = str(response.get("tool_name") or "operation.preview")
    plan_revisions = (
        _single_step_plan_revision(
            source="assistant_runtime",
            reason="preview operation plan",
            goal=goal,
            plan_kind="preview",
            tool_name=tool_name,
            payload=safe_payload,
            purpose="preview operation",
        ),
    )
    tool_transcript = (
        {
            "index": 1,
            "tool_name": tool_name,
            "payload": safe_payload,
            "authorized": True,
            "authorization_reason": "operator confirmation required",
            "risk_class": "SOFT_WRITE_PREVIEW",
            "precheck": {},
            "postcheck": {"status": "pass", "code": "previewed"},
            "hook_results": [],
            "evidence_summary": {
                "source_label": "OM 本地交易预览",
                "row_count": 1,
                "missing_data_count": 0,
            },
            "action_lifecycle": lifecycle,
            "ok": bool(response.get("ok", False)),
            "error_code": None,
            "summary": {
                "status": receipt.get("status"),
                "operation_type": operation_type,
                "confirm_required": bool(receipt.get("confirm_required")),
                "apply_allowed": bool(receipt.get("apply_allowed")),
            },
        },
    )
    permission_state = {
        "writes_allowed": False,
        "preview_operations_allowed": True,
        "pending_operation_ids": [operation_id] if operation_id else [],
        "apply_allowed": False,
        "action_lifecycle": lifecycle,
        "permission_request": _safe_permission_request(data.get("permission_request")),
    }
    answer_trace = {
        "final_response": {
            "status": "preview",
            "reason": "pending operator confirmation",
            "action_lifecycle": lifecycle,
            "hook_results": [],
        },
        "synthesis": {"reason": "preview_permission_request", "hook_results": []},
        "followup_decisions": [],
        "answer_route": "preview",
        "scope_source": "not_recorded",
        "loop_stop_reason": "preview_requested",
        "repair_attempted": False,
    }
    return AgentSessionSnapshot(
        session_id=_session_id(request=request, command_id=operation_id or command_id, goal=goal),
        request=request.public_payload(),
        goal=goal,
        task_state="waiting_for_permission",
        capability_selection=_capability_selection(
            tool_name=tool_name,
            effect="preview",
            risk_class="SOFT_WRITE_PREVIEW",
            reason="preview operation",
        ),
        progress=_progress(
            state="waiting_for_permission",
            plan_revisions=plan_revisions,
            tool_transcript=tool_transcript,
            permission_state=permission_state,
        ),
        plan_revisions=plan_revisions,
        tool_transcript=tool_transcript,
        task_contract={},
        evidence_bundle=_empty_evidence_bundle(),
        coverage={},
        permission_state=permission_state,
        answer_trace=answer_trace,
        audit_ref=_audit_ref(request),
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
    safe_payload = _safe_operation_payload(
        operation_id=operation_id,
        operation_type=operation_type,
        status=status,
        payload=payload,
    )
    goal = _operation_goal(operation_type=operation_type, payload=payload, preview=preview)
    clean_hooks = [dict(item) for item in hook_results if isinstance(item, dict)]
    postcheck_ok = str(postcheck.get("status") or "") == "pass"
    ok = bool(response.get("ok", False)) and postcheck_ok
    task_state = "done" if status in {"applied", "cancelled", "canceled"} else "failed"
    plan_revisions = (
        _single_step_plan_revision(
            source="deterministic_operation",
            reason="operation readback",
            goal=goal,
            plan_kind="readback",
            tool_name=tool_name,
            payload=safe_payload,
            purpose="record final operation state",
        ),
    )
    tool_transcript = (
        {
            "index": 1,
            "tool_name": tool_name,
            "payload": safe_payload,
            "authorized": True,
            "authorization_reason": "deterministic_operation_readback",
            "risk_class": "READ_AUTO",
            "precheck": {},
            "postcheck": dict(postcheck),
            "hook_results": clean_hooks,
            "evidence_summary": {
                "source_label": "OM 本地操作回执",
                "row_count": 1,
                "missing_data_count": 0,
            },
            "action_lifecycle": lifecycle,
            "ok": ok,
            "error_code": None,
            "summary": {
                "status": status,
                "operation_id": operation_id,
                "operation_type": operation_type,
                "resolved_operation_id": data.get("resolved_operation_id"),
            },
        },
    )
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
        "final_response": {
            "status": status,
            "reason": "operation readback",
            "action_lifecycle": lifecycle,
            "hook_results": clean_hooks,
        },
        "synthesis": {"reason": "operation_readback", "hook_results": clean_hooks},
        "followup_decisions": [],
        "answer_route": status,
        "scope_source": "not_recorded",
        "loop_stop_reason": "operation_readback",
        "repair_attempted": False,
    }
    return AgentSessionSnapshot(
        session_id=_session_id(request=request, command_id=operation_id, goal=goal),
        request=request.public_payload(),
        goal=goal,
        task_state=task_state,
        capability_selection=_capability_selection(
            tool_name=tool_name,
            effect="readback",
            risk_class="READ_AUTO",
            reason="record final operation state",
        ),
        progress=_progress(
            state=task_state,
            plan_revisions=plan_revisions,
            tool_transcript=tool_transcript,
            permission_state=permission_state,
        ),
        plan_revisions=plan_revisions,
        tool_transcript=tool_transcript,
        task_contract={},
        evidence_bundle=_empty_evidence_bundle(),
        coverage={},
        permission_state=permission_state,
        answer_trace=answer_trace,
        audit_ref=_audit_ref(request),
    )


def _single_step_plan_revision(
    *,
    source: str,
    reason: str,
    goal: str,
    plan_kind: str,
    tool_name: str,
    payload: dict[str, Any],
    purpose: str,
) -> dict[str, Any]:
    return {
        "revision": 1,
        "source": source,
        "reason": reason,
        "plan": {
            "goal": goal,
            "plan_kind": plan_kind,
            "steps": [
                {
                    "tool_name": tool_name,
                    "arguments": dict(payload),
                    "purpose": purpose,
                }
            ],
        },
    }


def _capability_selection(*, tool_name: str, effect: str, risk_class: str, reason: str) -> dict[str, Any]:
    selected = [
        {
            "tool_name": tool_name,
            "revision": 1,
            "effect": effect,
            "risk_class": risk_class,
            "selection_source": "message_text" if effect == "preview" else "deterministic_operation",
            "reason": reason,
        }
    ]
    return {
        "schema_version": AGENT_CAPABILITY_SELECTION_SCHEMA_VERSION,
        "selected": selected,
        "required": [],
        "satisfied": [],
        "rejected": [],
        "selected_tools": [tool_name],
        "risk_classes": [risk_class],
        "selection_sources": [selected[0]["selection_source"]],
        "explanation": {
            "selection_source": "deterministic_contract",
            "plan_sources": ["assistant_runtime" if effect == "preview" else "deterministic_operation"],
            "revision_count": 1,
            "followup_revision_count": 0,
            "selected_tool_count": 1,
            "selected_effects": [effect],
            "risk_classes": [risk_class],
            "selection_sources": [selected[0]["selection_source"]],
            "required_count": 0,
            "satisfied_count": 0,
            "rejected_count": 0,
            "decision_basis": ["known_command_contract"],
        },
    }


def _progress(
    *,
    state: str,
    plan_revisions: tuple[dict[str, Any], ...],
    tool_transcript: tuple[dict[str, Any], ...],
    permission_state: dict[str, Any],
) -> dict[str, Any]:
    pending_operation_ids = _string_list(permission_state.get("pending_operation_ids"))
    blocked_by: list[dict[str, Any]] = []
    if pending_operation_ids:
        blocked_by.append({"kind": "permission", "next_action": "confirm_or_cancel", "count": len(pending_operation_ids)})
    failed_tools = [item for item in tool_transcript if isinstance(item, dict) and item.get("ok") is False]
    for tool in failed_tools:
        blocked_by.append(
            {
                "kind": "tool_failure",
                "tool_name": str(tool.get("tool_name") or ""),
                "code": str(tool.get("error_code") or "TOOL_FAILED"),
            }
        )
    return {
        "schema_version": AGENT_PROGRESS_SCHEMA_VERSION,
        "state": state,
        "summary": _progress_summary(state=state, blocked_by=blocked_by),
        "plan_revision_count": len(plan_revisions),
        "planned_step_count": _planned_step_count(plan_revisions),
        "tool_call_count": len(tool_transcript),
        "completed_step_count": sum(1 for item in tool_transcript if item.get("ok") is True),
        "failed_step_count": len(failed_tools),
        "denied_step_count": 0,
        "coverage_status": "not_applicable",
        "coverage_next_action": "not_applicable",
        "pending_operation_ids": pending_operation_ids,
        "blocked_by": _dedupe_progress_blockers(blocked_by),
        "next_action": _next_action(state=state, pending_operation_ids=pending_operation_ids, failed_tools=failed_tools),
    }


def _progress_summary(*, state: str, blocked_by: list[dict[str, Any]]) -> str:
    if state == "waiting_for_permission":
        return "等待人工确认或取消"
    if state == "failed":
        return "执行失败，需要查看阻塞项"
    if blocked_by:
        return "已记录回执，但仍有阻塞项"
    return "已完成"


def _next_action(*, state: str, pending_operation_ids: list[str], failed_tools: list[dict[str, Any]]) -> str:
    if pending_operation_ids:
        return "confirm_or_cancel"
    if failed_tools:
        return "inspect_tool_failure"
    if state == "waiting_for_permission":
        return "confirm_or_cancel"
    if state == "failed":
        return "inspect_failure"
    return "none"


def _planned_step_count(plan_revisions: tuple[dict[str, Any], ...]) -> int:
    count = 0
    for revision in plan_revisions:
        plan = revision.get("plan") if isinstance(revision.get("plan"), dict) else {}
        steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
        count += len([item for item in steps if isinstance(item, dict)])
    return count


def _preview_goal(*, question: str, data: dict[str, Any], receipt: dict[str, Any]) -> str:
    permission_request = data.get("permission_request") if isinstance(data.get("permission_request"), dict) else {}
    target = str(permission_request.get("target_summary") or receipt.get("target_summary") or "").strip()
    operation_type = str(receipt.get("operation_type") or data.get("operation_type") or "").strip()
    label = {
        "manual_open": "记录开仓预览",
        "manual_close": "记录平仓预览",
        "monitor_run_now": "监控执行预览",
    }.get(operation_type, "写入预览")
    if target:
        return f"{label}：{target}"
    return label if question else "写入预览"


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
        "monitor_run_now": "监控执行预览",
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


def _safe_preview_payload(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
    if arguments:
        payload = {**payload, **arguments}
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


def _empty_evidence_bundle() -> dict[str, Any]:
    return EvidenceBundle(scope={}, facts=(), datasets=()).trace_payload()


def _audit_ref(request: AssistantRequest) -> dict[str, Any]:
    return {
        "channel": request.channel,
        "message_id": request.message_id,
        "conversation_id": request.conversation_id,
    }


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


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _dedupe_progress_blockers(blockers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for blocker in blockers:
        clean = {k: v for k, v in blocker.items() if v not in (None, "", [], {})}
        key = tuple(sorted((str(k), str(v)) for k, v in clean.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


__all__ = [
    "AGENT_SESSION_SCHEMA_VERSION",
    "AGENT_CAPABILITY_SELECTION_SCHEMA_VERSION",
    "AGENT_PROGRESS_SCHEMA_VERSION",
    "AgentSessionSnapshot",
    "build_operation_readback_agent_session_snapshot",
    "build_preview_agent_session_snapshot",
]
