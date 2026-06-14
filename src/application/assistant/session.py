from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any

from src.application.assistant.contracts import AssistantRequest
from src.application.assistant.evidence import EvidenceBundle


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
]
