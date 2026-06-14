from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from domain.domain.symbol_identity import symbol_market
from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response
from src.application.agent_tool_registry import get_tool_definition
from src.application.agent_tools.analysis import VIEW_SPECS as ANALYSIS_VIEW_SPECS
from src.application.assistant.capability_catalog import (
    ACCOUNT_VALUES,
    is_llm_planner_preview_spec,
    planner_preview_specs,
    planner_read_specs,
    spec_by_intent,
)
from src.application.assistant.answer_verifier import verify_response_against_evidence
from src.application.assistant.contracts import AssistantRequest, PerceptionResult, ToolCall
from src.application.assistant.evidence import build_evidence_bundle
from src.application.assistant.llm_common import (
    CreateStructuredResponseFn,
    is_supported_llm_provider,
    llm_api_key_value,
    missing_llm_config,
    normalize_llm_provider,
    provider_create_response_fn,
    strip_json_code_fence,
    unsupported_llm_provider_error,
)
from src.application.assistant.llm_translator import LlmTranslationResult
from src.application.assistant.renderer import render_canonical_tool_result, render_inbound_text
from src.application.assistant.session import build_agent_session_snapshot
from src.application.assistant.settings import AssistantSettings
from src.application.assistant.time_filters import extract_month_filter
from src.application.assistant.tool_policy import DEFAULT_TOOL_POLICY
from src.application.assistant.user_profile import user_profile_trace
from src.infrastructure.openai_chat_completions import (
    OpenAIChatCompletionsError,
    extract_chat_completion_text,
)
from src.infrastructure.openai_responses import OpenAIResponsesError, extract_response_text

AGENT_LOOP_SCHEMA_VERSION = "om-agent-loop-v1"
TOOL_PLAN_SCHEMA_VERSION = "om-tool-plan-v1"
TOOL_PLAN_SYNTHESIS_SCHEMA_VERSION = "om-tool-plan-synthesis-v1"
FOLLOWUP_DECISION_SCHEMA_VERSION = "om-agent-loop-followup-decision-v1"
INTERNAL_TOOL_PLAN_NAME = "assistant.tool_plan"
MAX_TOOL_PLAN_STEPS = 3
MAX_AGENT_LOOP_ITERATIONS = 3
MAX_AGENT_LOOP_TOOL_CALLS = 5
AGENT_LOOP_READ_TOOLS = frozenset(
    str(spec.tool_name)
    for spec in planner_read_specs()
    if spec.tool_name is not None and get_tool_definition(str(spec.tool_name)) is not None
)
AGENT_LOOP_PREVIEW_CAPABILITIES = frozenset(spec.intent_name for spec in planner_preview_specs())
AGENT_LOOP_ALLOWED_TOOLS = AGENT_LOOP_READ_TOOLS | AGENT_LOOP_PREVIEW_CAPABILITIES
_COMMAND_SPECS_BY_INTENT = spec_by_intent()
_BANNED_PLAN_ARGUMENTS = frozenset(
    {
        "audit_db",
        "accounts_root",
        "candidate_paths",
        "candidate_reject_log_paths",
        "candidate_report_dir",
        "candidate_trace_paths",
        "config_key",
        "config_path",
        "csv_path",
        "data_config",
        "delivery",
        "delivery_mode",
        "env_file",
        "file",
        "include_service_status",
        "log_file",
        "logs_root",
        "max_notification_chars",
        "max_run_age_minutes",
        "opend_telnet_host",
        "opend_telnet_port",
        "output_dir",
        "profile_path",
        "report_dir",
        "report_path",
        "run_dir",
        "runs_root",
        "shared_state_dir",
        "state_dir",
        "timeout_sec",
        "timeoutSeconds",
        "trigger_job_id",
        "trigger_source",
    }
)
_BANNED_PLAN_ARGUMENT_PREFIXES = ("audit", "config", "delivery", "env", "service", "system", "trigger")
_BANNED_PLAN_ARGUMENT_EXACT = frozenset(
    {
        "dir",
        "dirs",
        "file",
        "files",
        "host",
        "path",
        "paths",
        "port",
        "root",
        "roots",
        "service",
        "services",
        "system",
    }
)
_BANNED_PLAN_ARGUMENT_SUFFIXES = ("_path", "_paths", "_root", "_roots", "_dir", "_dirs", "_file", "_host", "_port")
_BANNED_PLAN_ARGUMENT_CONTAINS = ("_path_",)
_CONFIG_SCOPED_PLAN_TOOLS = frozenset(
    {
        "analysis_catalog",
        "analysis_query",
        "monthly_income_report",
        "option_positions_read",
        "runtime_status",
        "close_advice_read",
        "symbol_config_read",
    }
)


@dataclass(frozen=True)
class AgentLoopResult:
    translation: LlmTranslationResult
    trace: dict[str, Any]
    steps: tuple["AgentLoopStep", ...] = ()


@dataclass(frozen=True)
class AgentLoopStep:
    index: int
    phase: str
    status: str
    intent_name: str | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] | None = None
    purpose: str | None = None

    def public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "index": int(self.index),
            "phase": self.phase,
            "status": self.status,
            "intent_name": self.intent_name,
            "tool_name": self.tool_name,
            "arguments": _safe_tool_payload(dict(self.arguments or {})),
        }
        if self.purpose:
            payload["purpose"] = self.purpose
        return payload


@dataclass(frozen=True)
class PlannerPlanStep:
    id: str
    tool_name: str
    arguments: dict[str, Any]
    purpose: str = ""

    def public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "tool_name": self.tool_name,
            "arguments": _safe_tool_payload(self.arguments),
        }
        if self.purpose:
            payload["purpose"] = self.purpose
        return payload


@dataclass(frozen=True)
class PlannerPlan:
    goal: str
    steps: tuple[PlannerPlanStep, ...]
    response_mode: str = "synthesis"
    required_capabilities: tuple[str, ...] = ()
    schema_version: str = TOOL_PLAN_SCHEMA_VERSION

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "goal": self.goal,
            "response_mode": self.response_mode,
            "required_capabilities": list(self.required_capabilities),
            "steps": [step.public_payload() for step in self.steps],
        }


@dataclass(frozen=True)
class LlmPlannerResult:
    plan: PlannerPlan | None
    trace: dict[str, Any]
    error: AgentToolError | None = None


@dataclass(frozen=True)
class LlmSynthesisResult:
    response_text: str | None
    trace: dict[str, Any]
    error: AgentToolError | None = None


@dataclass(frozen=True)
class ToolObservation:
    index: int
    tool_name: str
    payload: dict[str, Any]
    ok: bool
    error_code: str | None
    summary: dict[str, Any]

    def public_payload(self) -> dict[str, Any]:
        return {
            "index": int(self.index),
            "tool_name": self.tool_name,
            "payload": dict(self.payload),
            "ok": bool(self.ok),
            "error_code": self.error_code,
            "summary": dict(self.summary),
        }


@dataclass(frozen=True)
class FinalResponsePlan:
    status: str
    reason: str
    canonical_renderer_required: bool = True
    llm_may_summarize: bool = False

    def public_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "canonical_renderer_required": bool(self.canonical_renderer_required),
            "llm_may_summarize": bool(self.llm_may_summarize),
        }


@dataclass(frozen=True)
class AnswerEvidence:
    """Internal evidence package used by the Agent composer.

    The deterministic renderer remains the fallback/evidence source. The LLM
    owns user-facing expression only when the answer guard accepts it.
    """

    enabled: bool
    observations: tuple[dict[str, Any], ...] = ()
    fallback_text: str = ""
    provenance_lines: tuple[str, ...] = ()
    trace: dict[str, Any] | None = None


AgentLoopTranslateFn = Callable[[str, AssistantSettings, dict[str, Any] | None], LlmTranslationResult]
AgentLoopPlanFn = Callable[[str, AssistantSettings, dict[str, Any] | None], LlmPlannerResult]
AgentLoopSynthesizeFn = Callable[
    [str, AssistantSettings, PlannerPlan, list[dict[str, Any]], dict[str, Any] | None],
    LlmSynthesisResult,
]


def run_read_only_agent_loop(
    text: str,
    *,
    settings: AssistantSettings,
    conversation_context: dict[str, Any] | None,
    translate_intent_fn: AgentLoopTranslateFn | None = None,
    plan_tools_fn: AgentLoopPlanFn | None = None,
    now_fn: Callable[[], date] | None = None,
    max_steps: int = MAX_TOOL_PLAN_STEPS,
) -> AgentLoopResult:
    """Build a bounded assistant plan.

    Pure-read plans route through the internal pseudo-tool. A single preview
    operation plan routes back into the existing deterministic operation path
    and still requires a later explicit confirm/apply step.
    """
    steps = max(1, min(int(max_steps), MAX_TOOL_PLAN_STEPS))
    today = _planner_today(now_fn)
    loop_context = _with_temporal_context(conversation_context, today=today)
    if plan_tools_fn is not None or translate_intent_fn is None:
        plan_result = (plan_tools_fn or plan_read_only_tools)(text, settings, loop_context)
        plan_result = _normalize_tool_plan_result(plan_result, question=text, today=today)
        translation, planned_steps = _translation_from_tool_plan_result(plan_result, question=text)
        trace = dict(translation.trace or plan_result.trace)
    else:
        translator = translate_intent_fn
        translation = translator(text, settings, loop_context)
        planned_steps = _planned_steps_from_translation(translation)
        trace = dict(translation.trace)
    trace["agent_loop"] = {
        "schema_version": AGENT_LOOP_SCHEMA_VERSION,
        "enabled": True,
        "planner": "llm_tool_plan" if translate_intent_fn is None or plan_tools_fn is not None else "llm_read_only_intent",
        "max_steps": steps,
        "steps_used": len(planned_steps),
        "writes_allowed": False,
        "preview_operations_allowed": True,
        "steps": [step.public_payload() for step in planned_steps],
        "final_response": FinalResponsePlan(
            status="pending_tool_execution" if translation.intent is not None else "no_plan",
            reason="canonical renderer will own factual output"
            if translation.intent is not None
            else "planner did not produce an executable assistant capability",
        ).public_payload(),
    }
    return AgentLoopResult(translation=translation, trace=trace, steps=planned_steps)


def build_tool_observation(*, index: int, tool_name: str, payload: dict[str, Any], result: dict[str, Any]) -> ToolObservation:
    error = result.get("error") if isinstance(result, dict) else None
    error_code = error.get("code") if isinstance(error, dict) else None
    return ToolObservation(
        index=index,
        tool_name=str(tool_name or ""),
        payload=_safe_tool_payload(payload),
        ok=bool(result.get("ok", False)) if isinstance(result, dict) else False,
        error_code=str(error_code) if error_code else None,
        summary=_safe_result_summary(result),
    )


def execute_tool_plan(
    *,
    question: str,
    request: AssistantRequest,
    plan_payload: dict[str, Any],
    command_id: str | None = None,
    settings: AssistantSettings,
    conversation_context: dict[str, Any] | None,
    execute_tool_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
    plan_tools_fn: AgentLoopPlanFn | None = None,
    synthesize_response_fn: AgentLoopSynthesizeFn | None = None,
) -> dict[str, Any]:
    plan = parse_tool_plan_payload(plan_payload)
    plan = _normalize_tool_plan(plan, question=question, today=_planner_today(None))
    validate_tool_plan(plan, allow_preview=False)
    plan_revisions: list[dict[str, Any]] = [_plan_revision_payload(1, plan=plan, reason="initial bounded plan")]
    tool_events: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    fact_observations: list[dict[str, Any]] = []
    llm_observations: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    ok = True
    error_payload: dict[str, Any] | None = None
    capability_status: dict[str, Any] = {"required": [], "satisfied": [], "gaps": []}
    evidence_gaps: list[dict[str, Any]] = []
    followup_decisions: list[dict[str, Any]] = []
    iteration = 1

    while True:
        ok, error_payload = _execute_read_plan_steps(
            request=request,
            plan=plan,
            execute_tool_fn=execute_tool_fn,
            tool_events=tool_events,
            observations=observations,
            fact_observations=fact_observations,
            llm_observations=llm_observations,
            tool_results=tool_results,
            ok=ok,
            error_payload=error_payload,
        )
        capability_status = _append_capability_observation(
            plan=plan,
            fact_observations=fact_observations,
            llm_observations=llm_observations,
            tool_events=tool_events,
            ok=ok,
        )
        evidence_bundle = build_evidence_bundle(
            question=question,
            plan=plan.public_payload(),
            observations=fact_observations,
        )
        evidence_gaps = _assess_evidence_gaps(plan=plan, evidence_bundle=evidence_bundle, observations=fact_observations)
        if not _should_replan_read_only(
            ok=ok,
            plan_tools_fn=plan_tools_fn,
            evidence_gaps=evidence_gaps,
            iteration=iteration,
            tool_call_count=len(observations),
        ):
            stop_decision = _followup_stop_decision(
                ok=ok,
                plan_tools_fn=plan_tools_fn,
                evidence_gaps=evidence_gaps,
                iteration=iteration,
                tool_call_count=len(observations),
                revision=len(plan_revisions) + 1,
            )
            if stop_decision:
                followup_decisions.append(stop_decision)
                tool_events.append({"phase": "followup_decision", **stop_decision})
            break
        next_plan = _plan_followup_read_steps(
            question=question,
            settings=settings,
            conversation_context=conversation_context,
            plan_tools_fn=plan_tools_fn,
            evidence_gaps=evidence_gaps,
            prior_plan=plan,
            observations=observations,
            revision=len(plan_revisions) + 1,
            followup_decisions=followup_decisions,
            tool_events=tool_events,
        )
        if next_plan is None:
            break
        if _can_recover_execution_failure(ok=ok, evidence_gaps=evidence_gaps):
            ok = True
            error_payload = None
            tool_events.append(
                {
                    "phase": "recover_tool_error",
                    "status": "accepted_followup",
                    "evidence_gaps": [dict(item) for item in evidence_gaps],
                }
            )
        plan = next_plan
        plan_revisions.append(_plan_revision_payload(len(plan_revisions) + 1, plan=plan, reason="follow-up evidence-gap plan"))
        iteration += 1

    synthesis = _build_final_response(
        question=question,
        settings=settings,
        plan=plan,
        evidence_bundle=evidence_bundle,
        fact_observations=fact_observations,
        llm_observations=llm_observations,
        conversation_context=conversation_context,
        synthesize_response_fn=synthesize_response_fn,
        ok=ok,
        error_payload=error_payload,
        followup_decisions=followup_decisions,
    )
    final_response_payload = _final_response_payload(synthesis)
    agent_session = build_agent_session_snapshot(
        request=request,
        command_id=command_id,
        question=question,
        plan=plan.public_payload(),
        plan_revisions=plan_revisions,
        tool_events=tool_events,
        observations=observations,
        evidence_bundle=evidence_bundle,
        final_response=final_response_payload,
        synthesis_trace=dict(synthesis.trace),
        ok=ok,
    )
    data = {
        "response_text": synthesis.response_text or "",
        "plan": plan.public_payload(),
        "observations": observations,
        "synthesis_observations": llm_observations,
        "evidence_bundle": evidence_bundle.public_payload(),
        "agent_session": agent_session.public_payload(),
        "tool_events": tool_events,
        "tool_calls_used": len(observations),
        "writes_allowed": False,
        "final_response": final_response_payload,
        "synthesis": dict(synthesis.trace),
        "tool_results": tool_results,
        "capability_status": capability_status,
        "evidence_gaps": evidence_gaps,
        "followup_decisions": followup_decisions,
        "plan_revisions": plan_revisions,
    }
    return build_response(
        tool_name=INTERNAL_TOOL_PLAN_NAME,
        ok=ok,
        data=data,
        error=error_payload if not ok else None,
    )


def _execute_read_plan_steps(
    *,
    request: AssistantRequest,
    plan: PlannerPlan,
    execute_tool_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
    tool_events: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    fact_observations: list[dict[str, Any]],
    llm_observations: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
    ok: bool,
    error_payload: dict[str, Any] | None,
) -> tuple[bool, dict[str, Any] | None]:
    if not ok:
        return ok, error_payload
    for step in plan.steps:
        if len(observations) >= MAX_AGENT_LOOP_TOOL_CALLS:
            error = AgentToolError(
                code="TOOL_BUDGET_EXHAUSTED",
                message=f"Agent loop 工具调用预算已用完（最多 {MAX_AGENT_LOOP_TOOL_CALLS} 次），未执行完整计划。",
                hint="请缩小问题范围，或拆分为更小的查询。",
                details={
                    "max_tool_calls": MAX_AGENT_LOOP_TOOL_CALLS,
                    "skipped_tool_name": step.tool_name,
                    "calls_used": len(observations),
                },
            )
            tool_events.append(
                {
                    "phase": "tool_budget_exhausted",
                    "max_tool_calls": MAX_AGENT_LOOP_TOOL_CALLS,
                    "skipped_tool_name": step.tool_name,
                }
            )
            return False, build_error_payload(error)
        index = len(observations) + 1
        payload = _inject_system_fields(step.arguments, request=request, tool_name=step.tool_name)
        call = ToolCall(tool_name=step.tool_name, payload=payload)
        try:
            decision = DEFAULT_TOOL_POLICY.authorize_read_tool(call, source="agent_loop")
        except AgentToolError as err:
            tool_events.append(
                {
                    "phase": "authorize_tool",
                    "tool_name": step.tool_name,
                    "allowed": False,
                    "error_code": err.code,
                }
            )
            return False, build_error_payload(err)
        tool_events.append(
            {
                "phase": "authorize_tool",
                "tool_name": step.tool_name,
                "allowed": True,
                "decision": decision.public_payload(),
            }
        )
        result = execute_tool_fn(step.tool_name, payload)
        tool_results.append(
            {
                "index": index,
                "tool_name": step.tool_name,
                "ok": bool(result.get("ok", False)) if isinstance(result, dict) else False,
                "error": result.get("error") if isinstance(result, dict) else None,
            }
        )
        observations.append(
            build_tool_observation(index=index, tool_name=step.tool_name, payload=payload, result=result).public_payload()
        )
        fact_observations.append(
            build_fact_observation(index=index, tool_name=step.tool_name, payload=payload, result=result)
        )
        llm_observations.append(
            build_synthesis_observation(index=index, tool_name=step.tool_name, payload=payload, result=result)
        )
        error = result.get("error") if isinstance(result, dict) else None
        error_code = error.get("code") if isinstance(error, dict) else None
        step_ok = bool(result.get("ok", False)) if isinstance(result, dict) else False
        tool_events.append(
            {
                "phase": "observe_tool_result",
                "tool_name": step.tool_name,
                "ok": step_ok,
                "error_code": str(error_code) if error_code else None,
            }
        )
        if not step_ok:
            return False, dict(error) if isinstance(error, dict) else {"code": "TOOL_FAILED", "message": "tool call failed"}
    return ok, error_payload


def _append_capability_observation(
    *,
    plan: PlannerPlan,
    fact_observations: list[dict[str, Any]],
    llm_observations: list[dict[str, Any]],
    tool_events: list[dict[str, Any]],
    ok: bool,
) -> dict[str, Any]:
    capability_status = _assess_plan_capabilities(plan, fact_observations) if ok else {"required": [], "satisfied": [], "gaps": []}
    if capability_status["required"]:
        capability_observation = {
            "index": len(llm_observations) + 1,
            "tool_name": "assistant.capability_check",
            "payload": {},
            "ok": not bool(capability_status["gaps"]),
            "error": None,
            "data": {"capability_status": capability_status},
        }
        fact_observations.append(capability_observation)
        llm_observations.append(capability_observation)
        tool_events.append(
            {
                "phase": "assess_capabilities",
                "required": list(capability_status["required"]),
                "satisfied": list(capability_status["satisfied"]),
                "gaps": list(capability_status["gaps"]),
            }
        )
    return capability_status


def _should_replan_read_only(
    *,
    ok: bool,
    plan_tools_fn: AgentLoopPlanFn | None,
    evidence_gaps: list[dict[str, Any]],
    iteration: int,
    tool_call_count: int,
) -> bool:
    return (
        (ok or _can_recover_execution_failure(ok=ok, evidence_gaps=evidence_gaps))
        and plan_tools_fn is not None
        and bool(evidence_gaps)
        and iteration < MAX_AGENT_LOOP_ITERATIONS
        and tool_call_count < MAX_AGENT_LOOP_TOOL_CALLS
    )


def _can_recover_execution_failure(*, ok: bool, evidence_gaps: list[dict[str, Any]]) -> bool:
    if ok:
        return False
    return any(
        isinstance(gap, dict)
        and str(gap.get("kind") or "") == "analysis_preflight_repair"
        and str(gap.get("recoverable_by") or "") == "analysis_query"
        for gap in evidence_gaps
    )


def _followup_stop_decision(
    *,
    ok: bool,
    plan_tools_fn: AgentLoopPlanFn | None,
    evidence_gaps: list[dict[str, Any]],
    iteration: int,
    tool_call_count: int,
    revision: int,
) -> dict[str, Any]:
    if not evidence_gaps:
        return {}
    reason = ""
    if not ok:
        reason = "tool execution failed before recoverable evidence gaps could be closed"
    elif plan_tools_fn is None:
        reason = "follow-up planner is unavailable"
    elif iteration >= MAX_AGENT_LOOP_ITERATIONS:
        reason = "max follow-up iterations reached"
    elif tool_call_count >= MAX_AGENT_LOOP_TOOL_CALLS:
        reason = "max tool calls reached"
    if not reason:
        return {}
    return _followup_decision_payload(
        revision=revision,
        decision="stop_with_gap",
        status="stopped",
        reason=reason,
        evidence_gaps=evidence_gaps,
    )


def _plan_followup_read_steps(
    *,
    question: str,
    settings: AssistantSettings,
    conversation_context: dict[str, Any] | None,
    plan_tools_fn: AgentLoopPlanFn | None,
    evidence_gaps: list[dict[str, Any]],
    prior_plan: PlannerPlan,
    observations: list[dict[str, Any]],
    revision: int,
    followup_decisions: list[dict[str, Any]],
    tool_events: list[dict[str, Any]],
) -> PlannerPlan | None:
    if plan_tools_fn is None:
        return None
    context = dict(conversation_context or {})
    context["agent_loop_followup"] = {
        "revision": revision,
        "prior_plan": prior_plan.public_payload(),
        "evidence_gaps": [dict(item) for item in evidence_gaps],
        "decision_contract": _followup_decision_contract(evidence_gaps=evidence_gaps),
        "instruction": "Plan only read-only follow-up steps that directly close recoverable evidence gaps.",
    }
    plan_result = plan_tools_fn(question, settings, context)
    if plan_result.error is not None:
        if plan_result.error.code == "NEEDS_CLARIFICATION":
            clarification = str(plan_result.error.message or "需要补充范围后才能继续分析。").strip()
            decision = _followup_decision_payload(
                revision=revision,
                decision="ask_clarification",
                status="stopped",
                reason=clarification,
                evidence_gaps=evidence_gaps,
                clarification=clarification,
            )
            followup_decisions.append(decision)
            tool_events.append({"phase": "followup_decision", **decision})
            tool_events.append(
                {
                    "phase": "replan",
                    "status": "needs_clarification",
                    "error_code": plan_result.error.code,
                }
            )
            return None
        decision = _followup_decision_payload(
            revision=revision,
            decision="stop_with_gap",
            status="failed",
            reason=f"follow-up planner failed: {plan_result.error.code}",
            evidence_gaps=evidence_gaps,
        )
        followup_decisions.append(decision)
        tool_events.append({"phase": "followup_decision", **decision})
        tool_events.append(
            {
                "phase": "replan",
                "status": "failed",
                "error_code": plan_result.error.code,
            }
        )
        return None
    if plan_result.plan is None or not plan_result.plan.steps:
        decision = _followup_decision_payload(
            revision=revision,
            decision="stop_with_gap",
            status="no_followup_plan",
            reason="follow-up planner did not produce a tool call",
            evidence_gaps=evidence_gaps,
        )
        followup_decisions.append(decision)
        tool_events.append({"phase": "followup_decision", **decision})
        tool_events.append({"phase": "replan", "status": "no_followup_plan"})
        return None
    try:
        next_plan = _normalize_tool_plan(plan_result.plan, question=question, today=_planner_today_from_context(context))
        validate_tool_plan(next_plan, question=question, allow_preview=False)
    except AgentToolError as err:
        decision = _followup_decision_payload(
            revision=revision,
            decision="call_tool",
            status="rejected",
            reason=f"invalid follow-up plan: {err.code}",
            evidence_gaps=evidence_gaps,
        )
        followup_decisions.append(decision)
        tool_events.append({"phase": "followup_decision", **decision})
        tool_events.append(
            {
                "phase": "replan",
                "status": "invalid",
                "error_code": err.code,
            }
        )
        return None
    duplicate_rejection = _followup_duplicate_rejection(next_plan, prior_plan=prior_plan, observations=observations)
    if duplicate_rejection:
        decision = _followup_decision_payload(
            revision=revision,
            decision="call_tool",
            status="rejected",
            reason=duplicate_rejection,
            evidence_gaps=evidence_gaps,
            plan=next_plan,
        )
        followup_decisions.append(decision)
        tool_events.append({"phase": "followup_decision", **decision})
        tool_events.append({"phase": "replan", "status": "duplicate_plan"})
        return None
    gap_rejection = _followup_gap_rejection(next_plan, evidence_gaps=evidence_gaps)
    if gap_rejection:
        decision = _followup_decision_payload(
            revision=revision,
            decision="call_tool",
            status="rejected",
            reason=gap_rejection,
            evidence_gaps=evidence_gaps,
            plan=next_plan,
        )
        followup_decisions.append(decision)
        tool_events.append({"phase": "followup_decision", **decision})
        tool_events.append(
            {
                "phase": "replan",
                "status": "unrelated_to_gap",
                "reason": gap_rejection,
                "evidence_gaps": [dict(item) for item in evidence_gaps],
                "steps": [step.public_payload() for step in next_plan.steps],
            }
        )
        return None
    decision = _followup_decision_payload(
        revision=revision,
        decision="call_tool",
        status="accepted",
        reason="follow-up plan directly addresses recoverable evidence gaps",
        evidence_gaps=evidence_gaps,
        plan=next_plan,
    )
    followup_decisions.append(decision)
    tool_events.append({"phase": "followup_decision", **decision})
    tool_events.append(
        {
            "phase": "replan",
            "status": "planned",
            "revision": revision,
            "evidence_gaps": [dict(item) for item in evidence_gaps],
            "steps": [step.public_payload() for step in next_plan.steps],
        }
    )
    return next_plan


def _followup_decision_contract(*, evidence_gaps: list[dict[str, Any]]) -> dict[str, Any]:
    suggested_tools = sorted(
        {
            str(gap.get("suggested_tool") or "")
            for gap in evidence_gaps
            if isinstance(gap, dict) and str(gap.get("suggested_tool") or "").strip()
        }
    )
    allowed_tools = sorted({"analysis_catalog", "analysis_query", *suggested_tools})
    return {
        "schema_version": FOLLOWUP_DECISION_SCHEMA_VERSION,
        "allowed_decisions": ["call_tool", "final_answer", "ask_clarification", "stop_with_gap"],
        "allowed_tools": allowed_tools,
        "required_fields": ["decision", "reason"],
        "call_tool_fields": ["tool_name", "arguments", "expected_evidence"],
        "safety_rules": [
            "tool must be allowlisted",
            "analysis_query must remain SELECT-only over whitelisted views",
            "query must not duplicate an earlier query",
            "query must use suggested views or explicitly close the evidence gap",
            "scope must not broaden beyond the user question or evidence gap",
        ],
    }


def _followup_decision_payload(
    *,
    revision: int,
    decision: str,
    status: str,
    reason: str,
    evidence_gaps: list[dict[str, Any]],
    plan: PlannerPlan | None = None,
    clarification: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": FOLLOWUP_DECISION_SCHEMA_VERSION,
        "revision": int(revision),
        "decision": str(decision or "stop_with_gap"),
        "status": str(status or ""),
        "reason": str(reason or "").strip(),
        "expected_evidence": _followup_expected_evidence(evidence_gaps),
        "evidence_gaps": [dict(item) for item in evidence_gaps if isinstance(item, dict)],
    }
    if clarification:
        payload["clarification"] = str(clarification).strip()
    if plan is not None:
        payload["steps"] = [step.public_payload() for step in plan.steps]
        if len(plan.steps) == 1:
            payload["tool_name"] = plan.steps[0].tool_name
            payload["arguments"] = _safe_tool_payload(plan.steps[0].arguments)
    return payload


def _followup_expected_evidence(evidence_gaps: list[dict[str, Any]]) -> list[str]:
    expected: list[str] = []
    for gap in evidence_gaps:
        if not isinstance(gap, dict):
            continue
        kind = str(gap.get("kind") or "").strip()
        if kind:
            expected.append(kind)
        for view in gap.get("suggested_views") or []:
            if str(view).strip():
                expected.append(f"view:{view}")
        for field in gap.get("suggested_fields") or []:
            if str(field).strip():
                expected.append(f"field:{field}")
        for account in gap.get("missing_accounts") or []:
            if str(account).strip():
                expected.append(f"account:{account}")
        for symbol in gap.get("symbols") or []:
            if str(symbol).strip():
                expected.append(f"symbol:{symbol}")
    return _unique_strings(expected)[:12]


def _unique_strings(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        out.append(item)
        seen.add(item)
    return out


def _followup_duplicate_rejection(
    plan: PlannerPlan,
    *,
    prior_plan: PlannerPlan,
    observations: list[dict[str, Any]],
) -> str:
    if plan.public_payload() == prior_plan.public_payload():
        return "follow-up plan duplicates the previous plan"
    attempted = {
        signature
        for observation in observations
        if isinstance(observation, dict)
        for signature in [_followup_observation_signature(observation)]
        if signature
    }
    for step in plan.steps:
        signature = _followup_step_signature(step.tool_name, step.arguments)
        if signature and signature in attempted:
            return "follow-up query repeats an earlier tool call"
    return ""


def _followup_observation_signature(observation: dict[str, Any]) -> str:
    payload = observation.get("payload") if isinstance(observation.get("payload"), dict) else {}
    return _followup_step_signature(str(observation.get("tool_name") or ""), payload)


def _followup_step_signature(tool_name: str, arguments: dict[str, Any]) -> str:
    if tool_name == "analysis_query":
        sql = _normalized_sql(str(arguments.get("sql") or arguments.get("query") or ""))
        return f"analysis_query:{sql}" if sql else ""
    if tool_name == "analysis_catalog":
        views = arguments.get("views") or arguments.get("view") or ""
        if isinstance(views, str):
            view_items = sorted(item.strip() for item in views.split(",") if item.strip())
        elif isinstance(views, (list, tuple, set)):
            view_items = sorted(str(item).strip() for item in views if str(item).strip())
        else:
            view_items = []
        return f"analysis_catalog:{','.join(view_items)}"
    comparable = {
        key: value
        for key, value in arguments.items()
        if key not in {"config_key", "audit_db", "message_id", "command_id"}
    }
    if not comparable:
        return str(tool_name or "")
    return f"{tool_name}:{json.dumps(comparable, ensure_ascii=False, sort_keys=True, default=str)}"


def _normalized_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", str(sql or "").strip().lower())


def _followup_gap_rejection(plan: PlannerPlan, *, evidence_gaps: list[dict[str, Any]]) -> str:
    quote_gaps = [
        gap
        for gap in evidence_gaps
        if isinstance(gap, dict)
        and str(gap.get("kind") or "") == "recoverable_missing_quote"
        and str(gap.get("recoverable_by") or "") == "refresh_quotes"
    ]
    if quote_gaps:
        for gap in quote_gaps:
            if any(_step_closes_quote_gap(step, gap=gap) for step in plan.steps):
                continue
            return "follow-up plan did not include assigned-stock quote refresh for the recoverable missing quote gap"
    analysis_gaps = [
        gap
        for gap in evidence_gaps
        if isinstance(gap, dict)
        and str(gap.get("recoverable_by") or "") == "analysis_query"
    ]
    if not analysis_gaps:
        return ""
    if not any(step.tool_name in {"analysis_query", "analysis_catalog"} for step in plan.steps):
        return "follow-up plan did not include analysis_catalog or analysis_query for the analysis evidence gap"
    for step in plan.steps:
        if step.tool_name not in {"analysis_query", "analysis_catalog"}:
            return "follow-up plan for analysis evidence gap used a non-analysis tool"
    for gap in analysis_gaps:
        if str(gap.get("kind") or "") == "analysis_preflight_repair":
            if any(_step_addresses_preflight_repair_gap(step, gap=gap) for step in plan.steps):
                continue
            return "follow-up analysis query did not use a suggested field or view for the preflight repair gap"
        suggested_views = {str(item) for item in gap.get("suggested_views") or [] if str(item).strip()}
        if not suggested_views:
            continue
        if any(_step_uses_any_analysis_view(step, suggested_views) for step in plan.steps):
            continue
        return "follow-up analysis query did not use a suggested view for the evidence gap"
    return ""


def _step_addresses_preflight_repair_gap(step: PlannerPlanStep, *, gap: dict[str, Any]) -> bool:
    if step.tool_name == "analysis_catalog":
        return True
    if step.tool_name != "analysis_query":
        return False
    sql = str(step.arguments.get("sql") or step.arguments.get("query") or "")
    normalized = _normalized_sql(sql)
    suggested_views = {str(item).strip() for item in gap.get("suggested_views") or [] if str(item).strip()}
    suggested_fields = {str(item).strip() for item in gap.get("suggested_fields") or [] if str(item).strip()}
    error_code = str(gap.get("error_code") or "").strip().upper()
    if error_code == "UNKNOWN_VIEW":
        return bool(suggested_views) and any(re.search(rf"(?is)\b{re.escape(view)}\b", sql) for view in suggested_views)
    if error_code == "UNKNOWN_COLUMN":
        uses_field = bool(suggested_fields) and any(
            re.search(rf"(?is)\b{re.escape(field)}\b", sql) for field in suggested_fields
        )
        if not uses_field:
            return False
        return not suggested_views or any(re.search(rf"(?is)\b{re.escape(view)}\b", sql) for view in suggested_views)
    return bool(normalized)


def _step_closes_quote_gap(step: PlannerPlanStep, *, gap: dict[str, Any]) -> bool:
    if step.tool_name != str(gap.get("suggested_tool") or "option_positions_read"):
        return False
    if _tool_plan_step_action(step.arguments) != "assigned-stock":
        return False
    if step.arguments.get("refresh_quotes") is not True:
        return False
    gap_accounts = {str(item).strip() for item in gap.get("accounts") or [] if str(item).strip()}
    step_account = str(step.arguments.get("account") or "").strip()
    if gap_accounts and step_account and step_account not in gap_accounts:
        return False
    gap_symbols = {str(item).strip().upper() for item in gap.get("symbols") or [] if str(item).strip()}
    step_symbol = str(step.arguments.get("symbol") or "").strip().upper()
    if gap_symbols and step_symbol and step_symbol not in gap_symbols:
        return False
    return True


def _step_uses_any_analysis_view(step: PlannerPlanStep, views: set[str]) -> bool:
    if step.tool_name == "analysis_catalog":
        requested_views = step.arguments.get("views") or step.arguments.get("view")
        if isinstance(requested_views, str):
            return requested_views in views or any(item.strip() in views for item in requested_views.split(","))
        if isinstance(requested_views, (list, tuple, set)):
            return any(str(item) in views for item in requested_views)
        return True
    if step.tool_name != "analysis_query":
        return False
    sql = str(step.arguments.get("sql") or step.arguments.get("query") or "")
    return any(re.search(rf"(?is)\b{re.escape(view)}\b", sql) for view in views)


def _assess_evidence_gaps(*, plan: PlannerPlan, evidence_bundle: Any, observations: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    payload = evidence_bundle.public_payload() if hasattr(evidence_bundle, "public_payload") else {}
    missing_data = payload.get("missing_data") if isinstance(payload, dict) else []
    if not isinstance(missing_data, list):
        missing_data = []
    gaps: list[dict[str, Any]] = []
    needs_assigned_stock_quote_refresh = any(
        step.tool_name == "option_positions_read"
        and _tool_plan_step_action(step.arguments) == "assigned-stock"
        and step.arguments.get("refresh_quotes") is not True
        for step in plan.steps
    )
    if needs_assigned_stock_quote_refresh:
        quote_records = [
            item
            for item in missing_data
            if isinstance(item, dict) and str(item.get("recoverable_by") or "") == "refresh_quotes"
        ]
        if quote_records:
            symbols: set[str] = set()
            accounts: set[str] = set()
            for record in quote_records:
                if str(record.get("symbol") or "").strip():
                    symbols.add(str(record.get("symbol")))
                for symbol in record.get("symbols") or []:
                    if str(symbol).strip():
                        symbols.add(str(symbol))
                if str(record.get("account") or "").strip():
                    accounts.add(str(record.get("account")))
            gaps.append(
                {
                    "kind": "recoverable_missing_quote",
                    "recoverable_by": "refresh_quotes",
                    "suggested_tool": "option_positions_read",
                    "suggested_arguments": {"action": "assigned-stock", "refresh_quotes": True},
                    "symbols": sorted(symbols),
                    "accounts": sorted(accounts),
                    "reason": "assigned-stock quote dependent facts are missing and the executed plan did not request quote refresh",
                }
            )
    gaps.extend(_analysis_evidence_gaps(plan=plan, evidence_payload=payload))
    gaps.extend(_analysis_preflight_repair_gaps(observations or []))
    return gaps


def _analysis_preflight_repair_gaps(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    analysis_observations = [
        observation
        for observation in observations
        if isinstance(observation, dict) and str(observation.get("tool_name") or "") == "analysis_query"
    ]
    if not analysis_observations:
        return []
    latest = analysis_observations[-1]
    if bool(latest.get("ok", False)):
        return []
    error = latest.get("error") if isinstance(latest.get("error"), dict) else {}
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    preflight = details.get("preflight") if isinstance(details.get("preflight"), dict) else {}
    if preflight.get("ok") is not False:
        return []
    error_code = str(details.get("error_code") or preflight.get("error_code") or "").strip().upper()
    if error_code not in {"UNKNOWN_COLUMN", "UNKNOWN_VIEW"}:
        return []
    suggestions = _unique_strings([str(item) for item in (details.get("suggestions") or preflight.get("suggestions") or [])])
    referenced_views = _unique_strings([str(item) for item in details.get("referenced_views") or []])
    suggested_views = referenced_views
    suggested_fields: list[str] = []
    if error_code == "UNKNOWN_COLUMN":
        suggested_fields = suggestions
    else:
        suggested_views = suggestions
    gap: dict[str, Any] = {
        "kind": "analysis_preflight_repair",
        "recoverable_by": "analysis_query",
        "suggested_tool": "analysis_query",
        "error_code": error_code,
        "suggested_views": suggested_views,
        "suggested_fields": suggested_fields,
        "reason": "analysis_query preflight failed with repairable catalog diagnostics",
    }
    if latest.get("index") is not None:
        gap["source_observation_index"] = latest.get("index")
    if details.get("unknown_column"):
        gap["unknown_column"] = details.get("unknown_column")
    if details.get("unknown_view"):
        gap["unknown_view"] = details.get("unknown_view")
    if suggestions:
        gap["suggestions"] = suggestions
    if referenced_views:
        gap["referenced_views"] = referenced_views
    return [gap]


def _analysis_evidence_gaps(*, plan: PlannerPlan, evidence_payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not any(step.tool_name == "analysis_query" for step in plan.steps):
        return []
    datasets = evidence_payload.get("datasets") if isinstance(evidence_payload, dict) else []
    if not isinstance(datasets, list):
        return []
    covered_views: set[str] = set()
    covered_accounts: set[str] = set()
    has_non_empty_analysis_result = False
    for dataset in datasets:
        if not isinstance(dataset, dict) or dataset.get("tool_name") != "analysis_query" or dataset.get("ok") is not True:
            continue
        if dataset.get("row_count") != 0:
            has_non_empty_analysis_result = True
        analysis_evidence = dataset.get("analysis_evidence") if isinstance(dataset.get("analysis_evidence"), dict) else {}
        coverage = analysis_evidence.get("coverage") if isinstance(analysis_evidence.get("coverage"), dict) else {}
        covered_views.update(str(item) for item in coverage.get("views") or [] if str(item).strip())
        covered_accounts.update(str(item).strip().lower() for item in coverage.get("accounts") or [] if str(item).strip())
    has_breakdown_detail = bool(covered_views & {"account_monthly_income_components", "symbol_income_attribution"})
    requested_accounts = _analysis_requested_accounts(plan.goal)
    gaps: list[dict[str, Any]] = []
    if requested_accounts and covered_accounts:
        missing_accounts = sorted(set(requested_accounts) - covered_accounts)
        if missing_accounts:
            gaps.append(
                {
                    "kind": "analysis_missing_account_coverage",
                    "recoverable_by": "analysis_query",
                    "suggested_tool": "analysis_query",
                    "suggested_views": sorted(covered_views) or ["account_monthly_performance"],
                    "missing_accounts": missing_accounts,
                    "covered_accounts": sorted(covered_accounts),
                    "reason": "the question names accounts that are not covered by the current analysis evidence",
                }
            )
    for dataset in datasets:
        if not isinstance(dataset, dict) or dataset.get("tool_name") != "analysis_query" or dataset.get("ok") is not True:
            continue
        row_count = dataset.get("row_count")
        if row_count == 0 and not has_non_empty_analysis_result:
            gaps.append(
                {
                    "kind": "analysis_empty_result",
                    "recoverable_by": "analysis_query",
                    "suggested_tool": "analysis_query",
                    "suggested_views": ["runtime_tick_status", "quote_freshness", "candidate_filter_diagnostics"],
                    "reason": "analysis query returned no rows; a narrower or diagnostic follow-up query may be needed",
                }
            )
            continue
        analysis_evidence = dataset.get("analysis_evidence") if isinstance(dataset.get("analysis_evidence"), dict) else {}
        coverage = analysis_evidence.get("coverage") if isinstance(analysis_evidence.get("coverage"), dict) else {}
        views = {str(item) for item in coverage.get("views") or [] if str(item).strip()}
        if not has_breakdown_detail and _analysis_question_needs_breakdown(plan.goal) and views and views.issubset(
            {"account_monthly_performance", "monthly_income_return_summary", "monthly_income_combined_return_summary"}
        ):
            gaps.append(
                {
                    "kind": "analysis_breakdown_needed",
                    "recoverable_by": "analysis_query",
                    "suggested_tool": "analysis_query",
                    "suggested_views": ["account_monthly_income_components", "symbol_income_attribution"],
                    "reason": "the question asks for source/cause/breakdown but the query only returned account-level summary evidence",
                }
            )
    return gaps


def _analysis_requested_accounts(goal: str) -> set[str]:
    compact = str(goal or "").lower()
    requested: set[str] = set()
    for account in ACCOUNT_VALUES:
        label = str(account or "").strip().lower()
        if not label:
            continue
        if re.search(rf"(?<![a-z0-9_]){re.escape(label)}(?![a-z0-9_])", compact):
            requested.add(label)
    return requested


def _analysis_question_needs_breakdown(goal: str) -> bool:
    text = re.sub(r"\s+", "", str(goal or "").lower())
    return any(
        token in text
        for token in (
            "为什么",
            "原因",
            "来源",
            "组成",
            "构成",
            "主要来自",
            "差异主要",
            "不同在哪里",
            "亏在哪里",
            "breakdown",
            "source",
            "driver",
            "why",
        )
    )


def _plan_revision_payload(revision: int, *, plan: PlannerPlan, reason: str) -> dict[str, Any]:
    return {
        "revision": int(revision),
        "source": "agent_loop",
        "reason": reason,
        "plan": plan.public_payload(),
    }


def plan_read_only_tools(
    text: str,
    settings: AssistantSettings,
    conversation_context: dict[str, Any] | None,
    *,
    create_response_fn: CreateStructuredResponseFn | None = None,
    environ: dict[str, str] | None = None,
) -> LlmPlannerResult:
    llm_settings = settings.llm
    if not llm_settings.enabled:
        return LlmPlannerResult(plan=None, trace=_llm_trace(llm_settings, attempted=False, reason="disabled"))

    missing = missing_llm_config(llm_settings)
    if missing:
        return LlmPlannerResult(
            plan=None,
            trace=_llm_trace(llm_settings, attempted=False, reason="missing_config", missing=missing),
            error=AgentToolError(
                code="LLM_UNAVAILABLE",
                message="LLM planner is enabled but not fully configured.",
                hint="Set assistant.llm.provider, assistant.llm.model, and assistant.llm.api_key_env, or disable assistant.planner.enabled.",
                details={"missing": missing},
            ),
        )

    provider = normalize_llm_provider(llm_settings.provider)
    if not is_supported_llm_provider(provider):
        return LlmPlannerResult(
            plan=None,
            trace=_llm_trace(llm_settings, attempted=False, reason="unsupported_provider"),
            error=unsupported_llm_provider_error(llm_settings, component="planner"),
        )

    api_key = llm_api_key_value(llm_settings, environ=environ)
    if not api_key:
        return LlmPlannerResult(
            plan=None,
            trace=_llm_trace(llm_settings, attempted=False, reason="missing_api_key", missing=["api_key"]),
            error=AgentToolError(
                code="LLM_UNAVAILABLE",
                message="LLM planner API key is not configured.",
                hint=f"Set {llm_settings.api_key_env} in the local env file or process environment.",
                details={"api_key_env": llm_settings.api_key_env},
            ),
        )

    try:
        response = (create_response_fn or provider_create_response_fn(provider))(
            api_key=api_key,
            base_url=llm_settings.base_url,
            model=llm_settings.model,
            input_text=_planner_input_text(text, conversation_context=conversation_context),
            instructions=_planner_instructions(),
            json_schema=tool_plan_json_schema(),
            timeout=int(llm_settings.timeout_seconds),
            max_output_tokens=int(llm_settings.max_output_tokens),
        )
    except (OpenAIResponsesError, OpenAIChatCompletionsError) as err:
        return LlmPlannerResult(
            plan=None,
            trace=_llm_trace(
                llm_settings,
                attempted=True,
                reason="provider_error",
                error_code="LLM_PROVIDER_ERROR",
                conversation_context=conversation_context,
            ),
            error=AgentToolError(
                code="LLM_PROVIDER_ERROR",
                message=str(err),
                details={"provider": provider, "http_status": err.http_status},
            ),
        )
    except Exception as err:
        return LlmPlannerResult(
            plan=None,
            trace=_llm_trace(
                llm_settings,
                attempted=True,
                reason="provider_error",
                error_code="LLM_PROVIDER_ERROR",
                conversation_context=conversation_context,
            ),
            error=AgentToolError(
                code="LLM_PROVIDER_ERROR",
                message=f"LLM planner provider failed: {type(err).__name__}: {err}",
                details={"provider": provider},
            ),
        )

    payload = _parse_provider_payload(response)
    if payload is None:
        return LlmPlannerResult(
            plan=None,
            trace=_llm_trace(
                llm_settings,
                attempted=True,
                reason="invalid_provider_output",
                error_code="LLM_PROVIDER_ERROR",
                conversation_context=conversation_context,
            ),
            error=AgentToolError(
                code="LLM_PROVIDER_ERROR",
                message="LLM planner returned invalid JSON.",
                details={"provider": provider},
            ),
        )
    try:
        plan = parse_tool_plan_payload(payload)
        plan = _normalize_tool_plan(plan, question=text, today=_planner_today_from_context(conversation_context))
        if not plan.steps:
            return LlmPlannerResult(
                plan=None,
                trace=_llm_trace(
                    llm_settings,
                    attempted=True,
                    reason="no_plan",
                    error_code="NEEDS_CLARIFICATION",
                    schema_version=TOOL_PLAN_SCHEMA_VERSION,
                    conversation_context=conversation_context,
                ),
                error=_no_tool_plan_error(),
            )
        validate_tool_plan(plan, question=text)
    except AgentToolError as err:
        return LlmPlannerResult(
            plan=None,
            trace=_llm_trace(
                llm_settings,
                attempted=True,
                reason="invalid_plan",
                error_code=err.code,
                conversation_context=conversation_context,
            ),
            error=err,
        )
    return LlmPlannerResult(
        plan=plan,
        trace=_llm_trace(
            llm_settings,
            attempted=True,
            reason="accepted",
            schema_version=TOOL_PLAN_SCHEMA_VERSION,
            conversation_context=conversation_context,
        ),
    )


def synthesize_tool_plan_response(
    question: str,
    settings: AssistantSettings,
    plan: PlannerPlan,
    observations: list[dict[str, Any]],
    conversation_context: dict[str, Any] | None,
    *,
    create_response_fn: CreateStructuredResponseFn | None = None,
    environ: dict[str, str] | None = None,
) -> LlmSynthesisResult:
    llm_settings = settings.llm
    if not llm_settings.enabled:
        return LlmSynthesisResult(
            response_text=None,
            trace=_llm_trace(llm_settings, attempted=False, reason="disabled"),
            error=AgentToolError(code="LLM_UNAVAILABLE", message="LLM synthesis is disabled."),
        )
    missing = missing_llm_config(llm_settings)
    if missing:
        return LlmSynthesisResult(
            response_text=None,
            trace=_llm_trace(llm_settings, attempted=False, reason="missing_config", missing=missing),
            error=AgentToolError(code="LLM_UNAVAILABLE", message="LLM synthesis is not fully configured.", details={"missing": missing}),
        )
    provider = normalize_llm_provider(llm_settings.provider)
    if not is_supported_llm_provider(provider):
        return LlmSynthesisResult(
            response_text=None,
            trace=_llm_trace(llm_settings, attempted=False, reason="unsupported_provider"),
            error=unsupported_llm_provider_error(llm_settings, component="synthesis"),
        )
    api_key = llm_api_key_value(llm_settings, environ=environ)
    if not api_key:
        return LlmSynthesisResult(
            response_text=None,
            trace=_llm_trace(llm_settings, attempted=False, reason="missing_api_key", missing=["api_key"]),
            error=AgentToolError(
                code="LLM_UNAVAILABLE",
                message="LLM synthesis API key is not configured.",
                hint=f"Set {llm_settings.api_key_env} in the local env file or process environment.",
            ),
        )
    try:
        response = (create_response_fn or provider_create_response_fn(provider))(
            api_key=api_key,
            base_url=llm_settings.base_url,
            model=llm_settings.model,
            input_text=_synthesis_input_text(
                question,
                plan=plan,
                observations=observations,
                conversation_context=conversation_context,
            ),
            instructions=_SYNTHESIS_INSTRUCTIONS,
            json_schema=_SYNTHESIS_JSON_SCHEMA,
            timeout=int(llm_settings.timeout_seconds),
            max_output_tokens=int(llm_settings.max_output_tokens),
        )
    except (OpenAIResponsesError, OpenAIChatCompletionsError) as err:
        return LlmSynthesisResult(
            response_text=None,
            trace=_llm_trace(llm_settings, attempted=True, reason="provider_error", error_code="LLM_PROVIDER_ERROR"),
            error=AgentToolError(code="LLM_PROVIDER_ERROR", message=str(err), details={"provider": provider, "http_status": err.http_status}),
        )
    except Exception as err:
        return LlmSynthesisResult(
            response_text=None,
            trace=_llm_trace(llm_settings, attempted=True, reason="provider_error", error_code="LLM_PROVIDER_ERROR"),
            error=AgentToolError(
                code="LLM_PROVIDER_ERROR",
                message=f"LLM synthesis provider failed: {type(err).__name__}: {err}",
                details={"provider": provider},
            ),
        )
    payload = _parse_provider_payload(response)
    response_text = str(payload.get("response_text") or "").strip() if isinstance(payload, dict) else ""
    if not response_text:
        return LlmSynthesisResult(
            response_text=None,
            trace=_llm_trace(llm_settings, attempted=True, reason="invalid_provider_output", error_code="LLM_PROVIDER_ERROR"),
            error=AgentToolError(code="LLM_PROVIDER_ERROR", message="LLM synthesis returned invalid JSON."),
        )
    return LlmSynthesisResult(
        response_text=response_text,
        trace=_llm_trace(llm_settings, attempted=True, reason="synthesized", schema_version=TOOL_PLAN_SYNTHESIS_SCHEMA_VERSION),
    )


def parse_tool_plan_payload(payload: dict[str, Any]) -> PlannerPlan:
    if not isinstance(payload, dict):
        raise AgentToolError(code="INPUT_ERROR", message="tool plan must be a JSON object")
    schema_version = str(payload.get("schema_version") or "").strip()
    if schema_version != TOOL_PLAN_SCHEMA_VERSION:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="unsupported tool plan schema version",
            details={"schema_version": schema_version, "expected": TOOL_PLAN_SCHEMA_VERSION},
        )
    response_mode = str(payload.get("response_mode") or "synthesis").strip().lower()
    if response_mode not in {"canonical", "synthesis"}:
        raise AgentToolError(code="INPUT_ERROR", message="tool plan response_mode must be canonical or synthesis")
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        raise AgentToolError(code="INPUT_ERROR", message="tool plan steps must be a JSON array")
    if len(raw_steps) > MAX_TOOL_PLAN_STEPS:
        raise AgentToolError(
            code="PLAN_TOO_MANY_STEPS",
            message=f"这个问题需要超过 {MAX_TOOL_PLAN_STEPS} 次工具调用，请拆分问题。",
            details={"max_steps": MAX_TOOL_PLAN_STEPS, "steps": len(raw_steps)},
        )
    steps: list[PlannerPlanStep] = []
    for index, raw in enumerate(raw_steps, start=1):
        if not isinstance(raw, dict):
            raise AgentToolError(code="INPUT_ERROR", message="tool plan step must be a JSON object")
        tool_name = str(raw.get("tool_name") or "").strip()
        arguments = raw.get("arguments")
        steps.append(
            PlannerPlanStep(
                id=str(raw.get("id") or f"step_{index}").strip() or f"step_{index}",
                tool_name=tool_name,
                arguments=dict(arguments) if isinstance(arguments, dict) else {},
                purpose=str(raw.get("purpose") or "").strip(),
            )
        )
    return PlannerPlan(
        goal=str(payload.get("goal") or "").strip(),
        steps=tuple(steps),
        response_mode=response_mode,
        required_capabilities=_normalized_capabilities(payload.get("required_capabilities")),
        schema_version=schema_version,
    )


def _normalized_capabilities(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    out: list[str] = []
    for item in value:
        capability = str(item or "").strip()
        if capability and capability not in out:
            out.append(capability)
    return tuple(out)


def validate_tool_plan(plan: PlannerPlan, *, question: str | None = None, allow_preview: bool = True) -> None:
    if plan.schema_version != TOOL_PLAN_SCHEMA_VERSION:
        raise AgentToolError(code="INPUT_ERROR", message="unsupported tool plan schema version")
    if not plan.steps:
        raise _no_tool_plan_error()
    if len(plan.steps) > MAX_TOOL_PLAN_STEPS:
        raise AgentToolError(
            code="PLAN_TOO_MANY_STEPS",
            message=f"这个问题需要超过 {MAX_TOOL_PLAN_STEPS} 次工具调用，请拆分问题。",
            details={"max_steps": MAX_TOOL_PLAN_STEPS, "steps": len(plan.steps)},
        )
    kinds: list[str] = []
    for step in plan.steps:
        step_kind = _plan_step_kind(step.tool_name)
        if step_kind is None:
            raise AgentToolError(
                code="PERMISSION_DENIED",
                message=f"{step.tool_name} is not allowed in assistant planner",
                hint="Planner only allows bounded read tools and explicit preview-only capabilities.",
                details={"allowed_tools": sorted(AGENT_LOOP_ALLOWED_TOOLS), "tool_name": step.tool_name},
            )
        kinds.append(step_kind)
        banned = sorted(_banned_plan_argument_paths(step.arguments))
        if banned:
            raise AgentToolError(
                code="PERMISSION_DENIED",
                message="tool plan contains system-scoped or path arguments",
                hint="config/path/runtime fields are injected by the system, not by LLM plans.",
                details={"tool_name": step.tool_name, "banned_arguments": banned},
            )
        allowed_args = _allowed_plan_arguments(step.tool_name)
        extra = sorted(str(key) for key in step.arguments if allowed_args and str(key) not in allowed_args)
        if extra:
            raise AgentToolError(
                code="INPUT_ERROR",
                message=f"tool plan has unsupported arguments for {step.tool_name}: {', '.join(extra)}",
                details={"tool_name": step.tool_name, "allowed_arguments": sorted(allowed_args), "extra_arguments": extra},
            )
    preview_count = sum(1 for kind in kinds if kind == "preview")
    read_count = sum(1 for kind in kinds if kind == "read")
    if preview_count:
        if not allow_preview:
            raise AgentToolError(
                code="PERMISSION_DENIED",
                message="preview-write capabilities cannot be executed through assistant.tool_plan",
                hint="LLM planner may create a pending preview, but direct tool execution remains read-only.",
                details={"preview_capabilities": sorted(step.tool_name for step in plan.steps if _plan_step_kind(step.tool_name) == "preview")},
            )
        if preview_count > 1 or read_count:
            raise AgentToolError(
                code="PLAN_UNSUPPORTED_COMPOSITION",
                message="一次聊天计划只能包含纯只读分析，或一个写入预览操作。",
                hint="请把查询分析和写入预览拆成两条消息。",
                details={"read_steps": read_count, "preview_steps": preview_count},
            )
    elif question is not None and _question_requests_preview_operation(question):
        raise AgentToolError(
            code="PLAN_RISK_MISMATCH",
            message="这句话像写入预览请求，但 LLM 规划成了只读查询。",
            hint="请重新规划为对应的 preview capability；不要把记录交易、成交提醒或配置修改降级成持仓/收益查询。",
            details={"planned_tools": [step.tool_name for step in plan.steps]},
        )


def _plan_step_kind(tool_name: str) -> str | None:
    name = str(tool_name or "")
    if name in AGENT_LOOP_READ_TOOLS:
        return "read"
    if name in AGENT_LOOP_PREVIEW_CAPABILITIES:
        spec = _COMMAND_SPECS_BY_INTENT.get(name)
        if spec is not None and is_llm_planner_preview_spec(spec):
            return "preview"
    return None


def _single_preview_step(plan: PlannerPlan) -> PlannerPlanStep | None:
    preview_steps = [step for step in plan.steps if _plan_step_kind(step.tool_name) == "preview"]
    if len(preview_steps) == 1 and len(plan.steps) == 1:
        return preview_steps[0]
    return None


def _allowed_plan_arguments(tool_name: str) -> set[str]:
    if tool_name in AGENT_LOOP_READ_TOOLS:
        definition = get_tool_definition(tool_name)
        return _filter_plan_arguments(definition.input_schema) if definition is not None else set()
    spec = _COMMAND_SPECS_BY_INTENT.get(tool_name)
    if spec is None or not is_llm_planner_preview_spec(spec):
        return set()
    allowed = _filter_plan_arguments(spec.arguments)
    if tool_name in {"manual_trade_open", "manual_trade_close"}:
        allowed.add("account")
    return allowed


def _filter_plan_arguments(arguments: Any) -> set[str]:
    return {str(arg) for arg in arguments if not _is_banned_plan_argument(str(arg))}


def _banned_plan_argument_paths(value: Any, *, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        hits: list[str] = []
        for raw_key, item in value.items():
            key = str(raw_key)
            path = f"{prefix}.{key}" if prefix else key
            if _is_banned_plan_argument(key):
                hits.append(path)
            hits.extend(_banned_plan_argument_paths(item, prefix=path))
        return hits
    if isinstance(value, list):
        hits = []
        for index, item in enumerate(value):
            path = f"{prefix}[{index}]" if prefix else f"[{index}]"
            hits.extend(_banned_plan_argument_paths(item, prefix=path))
        return hits
    return []


def _is_banned_plan_argument(argument: str) -> bool:
    name = str(argument or "").strip()
    if not name:
        return False
    normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", name).replace("-", "_").lower()
    if name in _BANNED_PLAN_ARGUMENTS or normalized in _BANNED_PLAN_ARGUMENTS:
        return True
    if normalized in _BANNED_PLAN_ARGUMENT_EXACT:
        return True
    if normalized.startswith("timeout"):
        return True
    if normalized.startswith(_BANNED_PLAN_ARGUMENT_PREFIXES):
        return True
    if normalized.endswith(_BANNED_PLAN_ARGUMENT_SUFFIXES):
        return True
    return any(token in normalized for token in _BANNED_PLAN_ARGUMENT_CONTAINS)


def _question_requests_preview_operation(question: str) -> bool:
    compact = re.sub(r"\s+", "", str(question or "").strip().lower())
    if not compact:
        return False
    high_confidence_tokens = (
        "记录开仓",
        "记录平仓",
        "记录交易",
        "写入交易",
        "成交提醒",
        "委托已全部成交",
        "成功卖出",
        "成功买入",
        "recordopen",
        "recordclose",
    )
    if any(token in compact for token in high_confidence_tokens):
        return True
    symbol_setting_tokens = ("coveredcall", "sellcall", "sellput", "minstrike", "maxstrike", "min_strike", "max_strike")
    if any(token in compact for token in ("是多少", "多少", "是什么", "当前", "现在", "目前", "查询", "查看")):
        return False
    if ("设置" in compact or "修改监控" in compact or "配置标的" in compact) and any(token in compact for token in symbol_setting_tokens):
        return True
    return "立即升级" in compact or "切换模型" in compact or "使用模型" in compact


def _planned_steps_from_translation(translation: LlmTranslationResult) -> tuple[AgentLoopStep, ...]:
    if translation.intent is None:
        return ()
    return (
        AgentLoopStep(
            index=1,
            phase="plan_tool",
            status="planned",
            intent_name=translation.intent.intent_name,
            tool_name=_tool_name_for_intent(translation.intent.intent_name),
            arguments=dict(translation.intent.arguments),
        ),
    )


def _translation_from_tool_plan_result(result: LlmPlannerResult, *, question: str) -> tuple[LlmTranslationResult, tuple[AgentLoopStep, ...]]:
    if result.error is not None:
        return LlmTranslationResult(intent=None, trace=dict(result.trace), error=result.error), ()
    if result.plan is None:
        return LlmTranslationResult(intent=None, trace=dict(result.trace)), ()
    if not result.plan.steps:
        return LlmTranslationResult(intent=None, trace={**dict(result.trace), "reason": "no_plan"}, error=_no_tool_plan_error()), ()
    try:
        validate_tool_plan(result.plan, question=question)
    except AgentToolError as err:
        return LlmTranslationResult(intent=None, trace={**dict(result.trace), "reason": "invalid_plan", "error_code": err.code}, error=err), ()
    planned_steps = tuple(
        AgentLoopStep(
            index=index,
            phase="plan_tool",
            status="planned",
            intent_name=step.tool_name if _plan_step_kind(step.tool_name) == "preview" else None,
            tool_name=step.tool_name,
            arguments=dict(step.arguments),
            purpose=step.purpose,
        )
        for index, step in enumerate(result.plan.steps, start=1)
    )
    preview_step = _single_preview_step(result.plan)
    if preview_step is not None:
        return (
            LlmTranslationResult(
                intent=PerceptionResult(
                    intent_name=preview_step.tool_name,
                    arguments=dict(preview_step.arguments),
                    source="agent_loop_plan",
                    confidence=1.0,
                ),
                trace=dict(result.trace),
            ),
            planned_steps,
        )
    return (
        LlmTranslationResult(
            intent=PerceptionResult(
                intent_name="tool_plan",
                arguments={"plan": result.plan.public_payload(), "response_mode": result.plan.response_mode},
                source="agent_loop_plan",
                confidence=1.0,
            ),
            trace=dict(result.trace),
        ),
        planned_steps,
    )


def _safe_tool_payload(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "action",
        "broker",
        "config_key",
        "account",
        "status",
        "assigned_stock_status",
        "stock_lot_id",
        "refresh_quotes",
        "month",
        "include_rows",
        "run_id",
        "kind",
        "limit",
        "lines",
        "sql",
        "query",
        "view",
        "views",
        "symbol",
        "strategy",
        "field",
        "option_type",
        "side",
        "strike",
        "expiration",
    }
    return {key: payload[key] for key in sorted(allowed) if key in payload}


def _planner_today(now_fn: Callable[[], date] | None) -> date:
    if now_fn is not None:
        return now_fn()
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _planner_today_from_context(conversation_context: dict[str, Any] | None) -> date:
    temporal = conversation_context.get("temporal_context") if isinstance(conversation_context, dict) else None
    current_date = temporal.get("current_date") if isinstance(temporal, dict) else None
    if isinstance(current_date, str):
        try:
            return date.fromisoformat(current_date)
        except ValueError:
            pass
    return _planner_today(None)


def _with_temporal_context(conversation_context: dict[str, Any] | None, *, today: date) -> dict[str, Any]:
    context = dict(conversation_context or {})
    context["temporal_context"] = {
        "current_date": today.isoformat(),
        "timezone": "Asia/Shanghai",
    }
    return context


def _normalize_tool_plan_result(result: LlmPlannerResult, *, question: str, today: date) -> LlmPlannerResult:
    if result.plan is None:
        return result
    normalized_plan = _normalize_tool_plan(result.plan, question=question, today=today)
    if normalized_plan is result.plan:
        return result
    return LlmPlannerResult(plan=normalized_plan, trace=result.trace, error=result.error)


def _normalize_tool_plan(plan: PlannerPlan, *, question: str, today: date) -> PlannerPlan:
    months = _extract_month_filters(question, today=today)
    month = months[0] if len(months) == 1 else None
    detail_requested = _question_requests_income_detail(question)
    all_history_requested = _question_requests_all_income_history(question)
    all_accounts_requested = _question_requests_all_accounts(question)
    response_mode = plan.response_mode
    changed = False
    steps: list[PlannerPlanStep] = []
    monthly_step_index = 0
    monthly_step_count = sum(1 for step in plan.steps if step.tool_name == "monthly_income_report")
    for step in plan.steps:
        arguments = dict(step.arguments)
        if "response_mode" in arguments:
            misplaced_response_mode = arguments.pop("response_mode")
            normalized_response_mode = str(misplaced_response_mode or "").strip().lower()
            if normalized_response_mode in {"canonical", "synthesis"}:
                if response_mode != normalized_response_mode:
                    response_mode = normalized_response_mode
                changed = True
            else:
                arguments["response_mode"] = misplaced_response_mode
        if step.tool_name == "monthly_income_report":
            monthly_step_index += 1
            purpose_months = _extract_month_filters(step.purpose, today=today)
            if all_accounts_requested and "account" in arguments:
                arguments.pop("account", None)
                changed = True
            if all_history_requested and not months and "month" in arguments:
                arguments.pop("month", None)
                changed = True
                if response_mode != "synthesis":
                    response_mode = "synthesis"
            elif len(months) > 1 and monthly_step_count == 1 and "month" in arguments:
                arguments.pop("month", None)
                changed = True
                if response_mode != "synthesis":
                    response_mode = "synthesis"
            elif len(months) > 1 and monthly_step_index <= len(months) and arguments.get("month") != months[monthly_step_index - 1]:
                arguments["month"] = months[monthly_step_index - 1]
                changed = True
            elif purpose_months and arguments.get("month") not in {None, purpose_months[0]}:
                arguments["month"] = purpose_months[0]
                changed = True
            elif month and arguments.get("month") != month:
                arguments["month"] = month
                changed = True
            if detail_requested and arguments.get("include_rows") is not True:
                arguments["include_rows"] = True
                changed = True
            if detail_requested and response_mode != "synthesis":
                response_mode = "synthesis"
                changed = True
        if step.tool_name == "option_positions_read" and _tool_plan_step_action(arguments) == "assigned-stock":
            if response_mode != "synthesis":
                response_mode = "synthesis"
                changed = True
        if arguments == step.arguments:
            steps.append(step)
        else:
            steps.append(PlannerPlanStep(id=step.id, tool_name=step.tool_name, arguments=arguments, purpose=step.purpose))
    if not changed:
        return plan
    return PlannerPlan(
        goal=plan.goal,
        steps=tuple(steps),
        response_mode=response_mode,
        required_capabilities=plan.required_capabilities,
        schema_version=plan.schema_version,
    )


def _tool_plan_step_action(arguments: dict[str, Any]) -> str:
    return str(arguments.get("action") or "").strip().lower()


def _question_requests_income_detail(question: str) -> bool:
    text = str(question or "")
    return any(token in text for token in ("明细", "组成", "构成", "来源", "由什么组成"))


def _question_requests_all_income_history(question: str) -> bool:
    compact = re.sub(r"\s+", "", str(question or ""))
    return (
        any(token in compact for token in ("历史以来", "历史至今", "从开始", "全部历史"))
        or ("累计" in compact and "净现金流" in compact)
        or ("以来" in compact and "净现金流" in compact)
        or ("今年以来" in compact and ("收益" in compact or "现金流" in compact))
    )


def _question_requests_all_accounts(question: str) -> bool:
    compact = re.sub(r"\s+", "", str(question or ""))
    return any(token in compact for token in ("所有账户", "全部账户", "各账户", "全账户"))


_MONTH_FILTER_RE = re.compile(r"(?<!\d)(20\d{2})[-/.](0[1-9]|1[0-2])(?!\d)")
_YEAR_MONTH_CN_FILTER_RE = re.compile(r"(?<!\d)(20\d{2})年(1[0-2]|0?[1-9]|十[一二]?|[一二三四五六七八九])月")
_MONTH_CN_FILTER_RE = re.compile(r"(?<!\d)(1[0-2]|0?[1-9]|十[一二]?|[一二三四五六七八九])月")
_CN_MONTH_NUMBERS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
}


def _extract_month_filters(text: str, *, today: date) -> list[str]:
    raw = str(text or "")
    compact = re.sub(r"\s+", "", raw)
    found: list[tuple[int, str]] = []
    for match in _MONTH_FILTER_RE.finditer(raw):
        found.append((match.start(), f"{match.group(1)}-{match.group(2)}"))
    occupied = [(match.start(), match.end()) for match in _YEAR_MONTH_CN_FILTER_RE.finditer(compact)]
    for match in _YEAR_MONTH_CN_FILTER_RE.finditer(compact):
        month = _month_filter_number(match.group(2))
        if month:
            found.append((match.start(), f"{int(match.group(1)):04d}-{month:02d}"))
    for match in _MONTH_CN_FILTER_RE.finditer(compact):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        month = _month_filter_number(match.group(1))
        if month:
            found.append((match.start(), f"{today.year:04d}-{month:02d}"))
    if not found:
        month = extract_month_filter(raw, today=today)
        return [month] if month else []
    out: list[str] = []
    seen: set[str] = set()
    for _position, month in sorted(found, key=lambda item: item[0]):
        if month not in seen:
            out.append(month)
            seen.add(month)
    return out


def _month_filter_number(raw: str) -> int | None:
    if raw.isdigit():
        value = int(raw)
    else:
        value = _CN_MONTH_NUMBERS.get(raw)
    return value if value is not None and 1 <= value <= 12 else None


def build_synthesis_observation(*, index: int, tool_name: str, payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    error = result.get("error") if isinstance(result, dict) else None
    data = result.get("data") if isinstance(result, dict) else None
    output_contract = _output_contract_for_tool(tool_name, payload)
    item: dict[str, Any] = {
        "index": int(index),
        "tool_name": str(tool_name or ""),
        "payload": _safe_tool_payload(payload),
        "ok": bool(result.get("ok", False)) if isinstance(result, dict) else False,
        "error": dict(error) if isinstance(error, dict) else None,
    }
    if output_contract:
        item["output_contract"] = output_contract
    if isinstance(data, dict):
        item["data"] = _synthesis_data(tool_name, data)
    return item


def build_fact_observation(*, index: int, tool_name: str, payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    error = result.get("error") if isinstance(result, dict) else None
    data = result.get("data") if isinstance(result, dict) else None
    output_contract = _output_contract_for_tool(tool_name, payload)
    item: dict[str, Any] = {
        "index": int(index),
        "tool_name": str(tool_name or ""),
        "payload": _safe_tool_payload(payload),
        "ok": bool(result.get("ok", False)) if isinstance(result, dict) else False,
        "error": dict(error) if isinstance(error, dict) else None,
    }
    if output_contract:
        item["output_contract"] = output_contract
    if isinstance(data, dict):
        item["data"] = _fact_data(tool_name, data)
    return item


def tool_plan_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_version": {"type": "string", "enum": [TOOL_PLAN_SCHEMA_VERSION]},
            "goal": {"type": "string"},
            "response_mode": {"type": "string", "enum": ["canonical", "synthesis"]},
            "required_capabilities": {
                "type": "array",
                "items": {"type": "string"},
            },
            "steps": {
                "type": "array",
                "minItems": 0,
                "maxItems": MAX_TOOL_PLAN_STEPS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "tool_name": {"type": "string", "enum": sorted(AGENT_LOOP_ALLOWED_TOOLS)},
                        "arguments": {"type": "object"},
                        "purpose": {"type": "string"},
                    },
                    "required": ["id", "tool_name", "arguments", "purpose"],
                },
            },
        },
        "required": ["schema_version", "goal", "response_mode", "required_capabilities", "steps"],
    }


def _safe_result_summary(result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"result_type": type(result).__name__}
    data = result.get("data")
    warnings = result.get("warnings")
    summary: dict[str, Any] = {
        "tool_name": result.get("tool_name"),
        "warning_count": len(warnings) if isinstance(warnings, list) else 0,
    }
    if isinstance(data, dict):
        if isinstance(data.get("summary"), dict):
            summary["summary"] = _clip_mapping(data["summary"])
        elif isinstance(data.get("summary"), list):
            summary["summary_count"] = len(data["summary"])
        if "response_text" in data:
            summary["response_text_chars"] = len(str(data.get("response_text") or ""))
        for key in ("status", "pending_count", "symbol_count"):
            if key in data:
                summary[key] = data[key]
    return {key: value for key, value in summary.items() if value is not None}


def _build_final_response(
    *,
    question: str,
    settings: AssistantSettings,
    plan: PlannerPlan,
    evidence_bundle: Any | None = None,
    fact_observations: list[dict[str, Any]],
    llm_observations: list[dict[str, Any]],
    conversation_context: dict[str, Any] | None,
    synthesize_response_fn: AgentLoopSynthesizeFn | None,
    ok: bool,
    error_payload: dict[str, Any] | None,
    followup_decisions: list[dict[str, Any]] | None = None,
) -> LlmSynthesisResult:
    clarification = _followup_clarification_text(followup_decisions or [])
    if clarification:
        return LlmSynthesisResult(
            response_text=clarification,
            trace={"attempted": False, "reason": "ask_clarification", "schema_version": TOOL_PLAN_SYNTHESIS_SCHEMA_VERSION},
        )
    if not ok:
        return LlmSynthesisResult(
            response_text=_fallback_response(plan=plan, observations=llm_observations, error_payload=error_payload),
            trace={"attempted": False, "reason": "tool_error", "schema_version": TOOL_PLAN_SYNTHESIS_SCHEMA_VERSION},
        )
    capability_gap = _capability_gap_response(fact_observations)
    if capability_gap:
        return LlmSynthesisResult(
            response_text=capability_gap,
            trace={
                "attempted": False,
                "reason": "capability_gap",
                "schema_version": TOOL_PLAN_SYNTHESIS_SCHEMA_VERSION,
            },
        )
    evidence = _build_answer_evidence(plan=plan, fact_observations=fact_observations, llm_observations=llm_observations)
    if _agent_composer_required(plan, evidence=evidence):
        return _compose_agent_response(
            question=question,
            settings=settings,
            plan=plan,
            evidence=evidence,
            evidence_bundle=evidence_bundle,
            fact_observations=fact_observations,
            conversation_context=conversation_context,
            synthesize_response_fn=synthesize_response_fn,
            error_payload=error_payload,
        )
    if _grounded_facts_response_required(plan) and fact_observations:
        fact_text = _canonical_response(plan.steps[0], _first_tool_observation(plan.steps[0], fact_observations))
        if fact_text:
            synthesizer = synthesize_response_fn or synthesize_tool_plan_response
            grounded_observations = _with_grounded_facts_observation(llm_observations, fact_text)
            synthesis = synthesizer(question, settings, plan, grounded_observations, conversation_context)
            if synthesis.response_text:
                guard = _verify_answer_guard(synthesis.response_text, observations=fact_observations, evidence_bundle=evidence_bundle)
                if not guard["violations"]:
                    return LlmSynthesisResult(
                        response_text=_combine_grounded_response(fact_text, synthesis.response_text),
                        trace={
                            **dict(synthesis.trace),
                            "reason": "grounded_renderer_with_analysis",
                            "grounded_facts": True,
                            "answer_guard": {"status": "passed"},
                        },
                        error=synthesis.error,
                    )
                retry_observations = _with_answer_guard_feedback(grounded_observations, guard)
                retry = synthesizer(question, settings, plan, retry_observations, conversation_context)
                if retry.response_text:
                    retry_guard = _verify_answer_guard(retry.response_text, observations=fact_observations, evidence_bundle=evidence_bundle)
                    if not retry_guard["violations"]:
                        return LlmSynthesisResult(
                            response_text=_combine_grounded_response(fact_text, retry.response_text),
                            trace={
                                **dict(retry.trace),
                                "reason": "grounded_renderer_with_analysis",
                                "grounded_facts": True,
                                "answer_guard": {
                                    "status": "failed_then_rewritten",
                                    "violations": guard["violations"],
                                },
                            },
                            error=retry.error,
                        )
                    guard = {
                        "violations": guard["violations"],
                        "retry_violations": retry_guard["violations"],
                    }
            trace = {
                **dict(synthesis.trace),
                "attempted": bool(synthesis.trace.get("attempted", False)),
                "reason": "grounded_renderer",
                "grounded_facts": True,
                "schema_version": TOOL_PLAN_SYNTHESIS_SCHEMA_VERSION,
                "fallback": "canonical_facts",
                "error_code": synthesis.error.code if synthesis.error else None,
            }
            if "guard" in locals():
                trace["answer_guard"] = {"status": "failed_then_fallback", **guard}
            return LlmSynthesisResult(response_text=fact_text, trace=trace)
    if plan.response_mode == "canonical" and len(plan.steps) == 1 and fact_observations:
        text = _canonical_response(plan.steps[0], _first_tool_observation(plan.steps[0], fact_observations))
        if text:
            return LlmSynthesisResult(
                response_text=text,
                trace={"attempted": False, "reason": "canonical_renderer", "schema_version": TOOL_PLAN_SYNTHESIS_SCHEMA_VERSION},
            )
    synthesizer = synthesize_response_fn or synthesize_tool_plan_response
    synthesis = synthesizer(question, settings, plan, llm_observations, conversation_context)
    if synthesis.response_text:
        guard = _verify_answer_guard(synthesis.response_text, observations=fact_observations, evidence_bundle=evidence_bundle)
        if not guard["violations"]:
            return LlmSynthesisResult(
                response_text=synthesis.response_text,
                trace={**dict(synthesis.trace), "answer_guard": {"status": "passed"}},
                error=synthesis.error,
            )
        retry_observations = _with_answer_guard_feedback(llm_observations, guard)
        retry = synthesizer(question, settings, plan, retry_observations, conversation_context)
        if retry.response_text:
            retry_guard = _verify_answer_guard(retry.response_text, observations=fact_observations, evidence_bundle=evidence_bundle)
            if not retry_guard["violations"]:
                return LlmSynthesisResult(
                    response_text=retry.response_text,
                    trace={
                        **dict(retry.trace),
                        "reason": "synthesized_after_answer_guard",
                        "answer_guard": {
                            "status": "failed_then_rewritten",
                            "violations": guard["violations"],
                        },
                    },
                    error=retry.error,
                )
            guard = {
                "violations": guard["violations"],
                "retry_violations": retry_guard["violations"],
            }
    if len(plan.steps) == 1 and fact_observations:
        text = _canonical_response(plan.steps[0], _first_tool_observation(plan.steps[0], fact_observations))
        if text:
            trace = {
                **dict(synthesis.trace),
                "reason": "canonical_renderer_fallback",
                "fallback": "canonical_renderer",
                "error_code": synthesis.error.code if synthesis.error else None,
            }
            if "guard" in locals():
                trace["answer_guard"] = {"status": "failed_then_fallback", **guard}
            return LlmSynthesisResult(
                response_text=text,
                trace=trace,
                error=synthesis.error,
            )
    analysis_fallback = _analysis_query_fallback_response(fact_observations)
    if analysis_fallback:
        trace = {
            **dict(synthesis.trace),
            "reason": "analysis_result_fallback",
            "fallback": "analysis_result_renderer",
            "error_code": synthesis.error.code if synthesis.error else None,
        }
        if "guard" in locals():
            trace["answer_guard"] = {"status": "failed_then_fallback", **guard}
        return LlmSynthesisResult(
            response_text=analysis_fallback,
            trace=trace,
            error=synthesis.error,
        )
    return LlmSynthesisResult(
        response_text=_fallback_response(plan=plan, observations=llm_observations, error_payload=error_payload),
        trace={
            **dict(synthesis.trace),
            "fallback": "structured_observation_summary",
            "error_code": synthesis.error.code if synthesis.error else None,
        },
        error=synthesis.error,
    )


def _followup_clarification_text(followup_decisions: list[dict[str, Any]]) -> str:
    for decision in reversed(followup_decisions):
        if not isinstance(decision, dict):
            continue
        if str(decision.get("decision") or "") != "ask_clarification":
            continue
        text = str(decision.get("clarification") or decision.get("reason") or "").strip()
        return text or "需要补充范围后才能继续分析。"
    return ""


def _build_answer_evidence(
    *,
    plan: PlannerPlan,
    fact_observations: list[dict[str, Any]],
    llm_observations: list[dict[str, Any]],
) -> AnswerEvidence:
    if len(plan.steps) != 1 or not fact_observations:
        return AnswerEvidence(enabled=False)
    step = plan.steps[0]
    observation = _first_tool_observation(step, fact_observations)
    if not observation or not bool(observation.get("ok", False)):
        return AnswerEvidence(enabled=False)
    output_contract = _contract_from_observation(observation) or _output_contract_for_step(step)
    if not output_contract:
        return AnswerEvidence(enabled=False)
    fallback_text = _canonical_response(step, observation)
    provenance_lines = _provenance_lines_for_observation(step, observation, output_contract=output_contract)
    if not fallback_text and not provenance_lines:
        return AnswerEvidence(enabled=False)
    evidence_observation = _answer_evidence_observation(
        observations=llm_observations,
        fallback_text=fallback_text,
        provenance_lines=provenance_lines,
        output_contract=output_contract,
        step=step,
    )
    return AnswerEvidence(
        enabled=True,
        observations=tuple([*llm_observations, evidence_observation]),
        fallback_text=fallback_text,
        provenance_lines=tuple(provenance_lines),
        trace={
            "output_contract": {
                "schema_version": output_contract.get("schema_version"),
                "canonical_renderer": output_contract.get("canonical_renderer"),
                "source_label": output_contract.get("source_label"),
                "guard_profile": output_contract.get("guard_profile"),
            },
            "fallback_renderer": bool(fallback_text),
            "provenance_lines": len(provenance_lines),
        },
    )


def _agent_composer_required(plan: PlannerPlan, *, evidence: AnswerEvidence) -> bool:
    if not evidence.enabled or len(plan.steps) != 1:
        return False
    step = plan.steps[0]
    if _answer_policy_for_step(step) == "facts_then_analysis":
        return True
    return plan.response_mode == "synthesis"


def _compose_agent_response(
    *,
    question: str,
    settings: AssistantSettings,
    plan: PlannerPlan,
    evidence: AnswerEvidence,
    evidence_bundle: Any | None,
    fact_observations: list[dict[str, Any]],
    conversation_context: dict[str, Any] | None,
    synthesize_response_fn: AgentLoopSynthesizeFn | None,
    error_payload: dict[str, Any] | None,
) -> LlmSynthesisResult:
    synthesizer = synthesize_response_fn or synthesize_tool_plan_response
    observations = [dict(item) for item in evidence.observations]
    synthesis = synthesizer(question, settings, plan, observations, conversation_context)
    if synthesis.response_text:
        guard = _verify_answer_guard(synthesis.response_text, observations=fact_observations, evidence_bundle=evidence_bundle)
        if not guard["violations"]:
            return LlmSynthesisResult(
                response_text=_append_provenance(synthesis.response_text, evidence.provenance_lines),
                trace={
                    **dict(synthesis.trace),
                    "reason": "agent_composed_response",
                    "answer_guard": {"status": "passed"},
                    "answer_evidence": dict(evidence.trace or {}),
                    "provenance_appended": bool(evidence.provenance_lines),
                },
                error=synthesis.error,
            )
        retry_observations = _with_answer_guard_feedback(observations, guard)
        retry = synthesizer(question, settings, plan, retry_observations, conversation_context)
        if retry.response_text:
            retry_guard = _verify_answer_guard(retry.response_text, observations=fact_observations, evidence_bundle=evidence_bundle)
            if not retry_guard["violations"]:
                return LlmSynthesisResult(
                    response_text=_append_provenance(retry.response_text, evidence.provenance_lines),
                    trace={
                        **dict(retry.trace),
                        "reason": "agent_composed_response",
                        "answer_guard": {
                            "status": "failed_then_rewritten",
                            "violations": guard["violations"],
                        },
                        "answer_evidence": dict(evidence.trace or {}),
                        "provenance_appended": bool(evidence.provenance_lines),
                    },
                    error=retry.error,
                )
            guard = {
                "violations": guard["violations"],
                "retry_violations": retry_guard["violations"],
            }
    fallback_text = evidence.fallback_text or _fallback_response(
        plan=plan,
        observations=observations,
        error_payload=error_payload,
    )
    trace = {
        **dict(synthesis.trace),
        "attempted": bool(synthesis.trace.get("attempted", False)),
        "reason": "agent_renderer_fallback",
        "fallback": "canonical_renderer" if evidence.fallback_text else "structured_observation_summary",
        "error_code": synthesis.error.code if synthesis.error else None,
        "answer_evidence": dict(evidence.trace or {}),
    }
    if "guard" in locals():
        trace["answer_guard"] = {"status": "failed_then_fallback", **guard}
    return LlmSynthesisResult(
        response_text=_append_provenance(fallback_text, evidence.provenance_lines),
        trace=trace,
        error=synthesis.error,
    )


def _final_response_payload(synthesis: LlmSynthesisResult) -> dict[str, Any]:
    reason = str(synthesis.trace.get("reason") or "")
    if reason == "agent_composed_response":
        return FinalResponsePlan(
            status="synthesized",
            reason="LLM composed the response from guarded tool evidence",
            canonical_renderer_required=False,
            llm_may_summarize=True,
        ).public_payload()
    if reason == "ask_clarification":
        return FinalResponsePlan(
            status="needs_clarification",
            reason="follow-up planning determined that user scope is required",
            canonical_renderer_required=False,
            llm_may_summarize=False,
        ).public_payload()
    if reason == "agent_renderer_fallback":
        return FinalResponsePlan(
            status="rendered",
            reason="deterministic fallback renderer used after agent composition was unavailable or unsafe",
            canonical_renderer_required=True,
            llm_may_summarize=True,
        ).public_payload()
    if reason == "canonical_renderer":
        return FinalResponsePlan(
            status="rendered",
            reason="canonical renderer produced the factual response",
            canonical_renderer_required=True,
            llm_may_summarize=False,
        ).public_payload()
    if reason == "canonical_renderer_fallback":
        return FinalResponsePlan(
            status="rendered",
            reason="canonical renderer used after synthesis was unavailable",
            canonical_renderer_required=True,
            llm_may_summarize=False,
        ).public_payload()
    if reason == "analysis_result_fallback":
        return FinalResponsePlan(
            status="rendered",
            reason="analysis result renderer used after synthesis was unavailable or unsafe",
            canonical_renderer_required=True,
            llm_may_summarize=True,
        ).public_payload()
    if reason == "grounded_renderer_with_analysis":
        return FinalResponsePlan(
            status="synthesized",
            reason="canonical facts rendered first; LLM added verified analysis",
            canonical_renderer_required=True,
            llm_may_summarize=True,
        ).public_payload()
    if reason == "grounded_renderer":
        return FinalResponsePlan(
            status="rendered",
            reason="canonical facts rendered after analysis was unavailable or unsafe",
            canonical_renderer_required=True,
            llm_may_summarize=True,
        ).public_payload()
    if reason in {"synthesized", "synthesized_after_answer_guard"}:
        return FinalResponsePlan(
            status="synthesized",
            reason="LLM synthesized the response from tool observations",
            canonical_renderer_required=False,
            llm_may_summarize=True,
        ).public_payload()
    if reason == "capability_gap":
        return FinalResponsePlan(
            status="partial",
            reason="tool observations did not satisfy all requested capabilities",
            canonical_renderer_required=False,
            llm_may_summarize=False,
        ).public_payload()
    return FinalResponsePlan(
        status="fallback",
        reason="structured fallback response used after synthesis was unavailable",
        canonical_renderer_required=False,
        llm_may_summarize=False,
    ).public_payload()


def _first_tool_observation(step: PlannerPlanStep, observations: list[dict[str, Any]]) -> dict[str, Any]:
    for observation in reversed(observations):
        if str(observation.get("tool_name") or "") == step.tool_name:
            return observation
    return {}


def _canonical_response(step: PlannerPlanStep, observation: dict[str, Any]) -> str:
    if not bool(observation.get("ok", False)):
        return ""
    data = observation.get("data")
    if not isinstance(data, dict):
        return ""
    tool_result = build_response(tool_name=step.tool_name, ok=True, data=data)
    output_contract = _contract_from_observation(observation) or _output_contract_for_step(step)
    renderer_key = str(output_contract.get("canonical_renderer") or "").strip()
    if renderer_key:
        rendered = render_canonical_tool_result(renderer_key=renderer_key, data=data, tool_result=tool_result)
        if rendered:
            return rendered
    intent_name = _intent_name_for_tool(step.tool_name)
    if not intent_name:
        return ""
    return render_inbound_text(
        intent=PerceptionResult(intent_name=intent_name, arguments=dict(step.arguments), source="agent_loop_plan"),
        tool_result=tool_result,
    )


def _analysis_query_fallback_response(observations: list[dict[str, Any]]) -> str:
    for observation in reversed(observations):
        if str(observation.get("tool_name") or "") != "analysis_query":
            continue
        if not bool(observation.get("ok", False)):
            continue
        payload = observation.get("payload") if isinstance(observation.get("payload"), dict) else {}
        step = PlannerPlanStep(
            id="analysis_fallback",
            tool_name="analysis_query",
            arguments=dict(payload),
            purpose="render analysis query result as task-shaped fallback",
        )
        rendered = _canonical_response(step, observation)
        if rendered:
            return rendered
    return ""


def _grounded_facts_response_required(plan: PlannerPlan) -> bool:
    if len(plan.steps) != 1:
        return False
    step = plan.steps[0]
    return plan.response_mode == "synthesis" and _answer_policy_for_step(step) == "facts_then_analysis"


def _answer_policy_for_step(step: PlannerPlanStep) -> str:
    definition = get_tool_definition(step.tool_name)
    if definition is None:
        return "default"
    return definition.resolve_answer_policy(dict(step.arguments))


def _output_contract_for_step(step: PlannerPlanStep) -> dict[str, Any]:
    return _output_contract_for_tool(step.tool_name, dict(step.arguments))


def _output_contract_for_tool(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    definition = get_tool_definition(str(tool_name or ""))
    if definition is None:
        return {}
    contract = definition.resolve_output_contract(dict(payload or {}))
    return contract if isinstance(contract, dict) else {}


def _contract_from_observation(observation: dict[str, Any]) -> dict[str, Any]:
    contract = observation.get("output_contract")
    return dict(contract) if isinstance(contract, dict) else {}


def _answer_evidence_observation(
    *,
    observations: list[dict[str, Any]],
    fallback_text: str,
    provenance_lines: list[str],
    output_contract: dict[str, Any],
    step: PlannerPlanStep,
) -> dict[str, Any]:
    renderer_key = str(output_contract.get("canonical_renderer") or "").strip()
    instruction = (
        "Answer the user naturally in Chinese using only the tool observations. "
        "Start with the direct conclusion, then add only the key evidence needed to support it. "
        "Do not expose internal ids such as stock_lot_id, record_id, event_id, or source_deal_id. "
        "Do not expose SQL, tool names, artifact paths, trace ids, canonical/synthesis mode names, or raw tool receipts. "
        "Do not use a forced 事实/分析 split. "
        "For direct summary questions, prefer a short synthesis; for detail/list questions, include compact rows only when useful. "
        "Do not append 数据来源 or 口径 lines; the system appends deterministic provenance. "
        "If quote_status or quote_refresh reports missing_quote, say the affected symbol cannot compute realtime floating PnL."
    )
    return {
        "index": len(observations) + 1,
        "tool_name": "assistant.answer_evidence",
        "payload": {},
        "ok": True,
        "error": None,
        "data": {
            "renderer_key": renderer_key,
            "tool_name": step.tool_name,
            "fallback_renderer_text": fallback_text,
            "provenance_lines": list(provenance_lines),
            "composition_instruction": instruction,
        },
    }


def _provenance_lines_for_observation(
    step: PlannerPlanStep,
    observation: dict[str, Any],
    *,
    output_contract: dict[str, Any],
) -> list[str]:
    data = observation.get("data") if isinstance(observation.get("data"), dict) else {}
    source_label = str(output_contract.get("source_label") or "").strip()
    lines: list[str] = []
    if source_label:
        source_line = f"数据来源：{source_label}"
        quote_refresh = data.get("quote_refresh") if isinstance(data, dict) else None
        if step.tool_name == "option_positions_read" and _tool_plan_step_action(step.arguments) == "assigned-stock":
            quote = quote_refresh if isinstance(quote_refresh, dict) else {}
            quote_source = str(quote.get("quote_source") or "").strip()
            quote_status = str(quote.get("status") or "").strip()
            if quote_source:
                source_line += f"；spot={quote_source}"
            if quote_status:
                source_line += f"（{quote_status}）"
        lines.append(source_line)
    policy_line = _accounting_policy_line(step, data)
    if policy_line:
        lines.append(policy_line)
    return lines


def _accounting_policy_line(step: PlannerPlanStep, data: dict[str, Any]) -> str:
    if step.tool_name == "option_positions_read" and _tool_plan_step_action(step.arguments) == "assigned-stock":
        return "口径：正股成本按真实交割价记录，不扣除 Sell Put 权利金；生命周期PnL 才包含权利金归因。"
    if step.tool_name == "monthly_income_report":
        if isinstance(data.get("combined_return_summary"), list) and data.get("combined_return_summary"):
            return "口径：合并现金流率=sum(净现金流CNY)/sum(当前现金担保CNY)，不是账户收益率平均值。"
        return "口径：现金流率=净现金流/当前现金担保，不是账户总资产收益率。"
    return ""


def _append_provenance(response_text: str, provenance_lines: tuple[str, ...]) -> str:
    text = str(response_text or "").strip()
    additions: list[str] = []
    for raw_line in provenance_lines:
        line = str(raw_line or "").strip()
        if not line:
            continue
        if line in text:
            continue
        if line.startswith("数据来源：") and ("数据来源：" in text or "数据源：" in text):
            continue
        if line.startswith("口径：") and "口径：" in text:
            continue
        additions.append(line)
    if not additions:
        return text
    return f"{text}\n\n" + "\n".join(additions) if text else "\n".join(additions)


def _with_grounded_facts_observation(observations: list[dict[str, Any]], fact_text: str) -> list[dict[str, Any]]:
    return [
        *observations,
        {
            "index": len(observations) + 1,
            "tool_name": "assistant.grounded_facts",
            "payload": {},
            "ok": True,
            "error": None,
            "data": {
                "canonical_response": fact_text,
                "analysis_instruction": (
                    "The canonical_response is the factual answer block and will be sent first. "
                    "Return only a concise analysis block in Chinese. Do not repeat the factual rows, "
                    "and do not restate or alter accounts, symbols, dates, contract quantities, currencies, or amounts."
                ),
            },
        },
    ]


def _combine_grounded_response(fact_text: str, analysis_text: str) -> str:
    facts = str(fact_text or "").strip()
    analysis = str(analysis_text or "").strip()
    if not analysis:
        return facts
    if analysis.startswith("分析"):
        return f"{facts}\n\n{analysis}".strip()
    return f"{facts}\n\n分析\n{analysis}".strip()


def _fallback_response(
    *,
    plan: PlannerPlan,
    observations: list[dict[str, Any]],
    error_payload: dict[str, Any] | None,
) -> str:
    if error_payload:
        message = str(error_payload.get("message") or "工具调用失败").strip()
        hint = str(error_payload.get("hint") or "").strip()
        return f"无法完成分析：{message}{(' ' + hint) if hint else ''}".strip()
    lines = [f"{plan.goal or '分析'} 已完成工具查询，但 LLM 生成不可用。"]
    for item in observations:
        tool_name = str(item.get("tool_name") or "-")
        ok = "ok" if bool(item.get("ok", False)) else "failed"
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        counts: list[str] = []
        for key in ("row_count", "premium_row_count", "cashflow_row_count", "realized_row_count"):
            if key in data:
                counts.append(f"{key}={data.get(key)}")
        lines.append(f"- {tool_name}: {ok}{(' | ' + ', '.join(counts)) if counts else ''}")
    return "\n".join(lines)


def _verify_answer_guard(
    response_text: str,
    *,
    observations: list[dict[str, Any]],
    evidence_bundle: Any | None = None,
) -> dict[str, Any]:
    text = str(response_text or "")
    compact = re.sub(r"\s+", "", text.lower())
    facts = _answer_guard_facts(observations)
    violations: list[dict[str, Any]] = []
    violations.extend(_normal_answer_ux_violations(text))
    if facts["all_tools_ok"] and any(token in compact for token in ("工具查询失败", "工具调用失败", "查询失败")):
        violations.append(
            {
                "type": "contradicts_tool_status",
                "claim": "工具查询失败",
                "evidence": "all observed tools returned ok=true",
            }
        )
    if facts["complete_for_query_scope"]:
        missing_scope_tokens = (
            "无法直接确认",
            "无法确认",
            "不能确认",
            "无法提供完整",
            "需要提供完整",
            "请提供完整",
            "缺少所有月份",
            "缺少完整月份",
            "缺少所有账户",
        )
        if any(token in compact for token in missing_scope_tokens):
            violations.append(
                {
                    "type": "contradicts_query_coverage",
                    "claim": "缺少查询范围内的数据",
                    "evidence": f"coverage is complete for query scope; months={facts['months']}; accounts={facts['accounts']}",
                }
            )
    for account in facts["accounts"]:
        account_text = str(account).lower()
        if not account_text:
            continue
        if re.search(rf"(未包含|不包含|缺少|没有|未提供)[^，。；;\n]{{0,12}}{re.escape(account_text)}", compact) or re.search(
            rf"{re.escape(account_text)}[^，。；;\n]{{0,12}}(未包含|不包含|缺少|没有|未提供)",
            compact,
        ):
            violations.append(
                {
                    "type": "contradicts_account_coverage",
                    "claim": f"缺少账户 {account}",
                    "evidence": f"coverage.accounts includes {account}",
                }
            )
    for month in facts["months"]:
        month_text = str(month)
        month_cn = _month_label_cn(month_text)
        month_tokens = [month_text, month_text.replace("-", "年") + "月", month_cn]
        for token in month_tokens:
            if not token:
                continue
            normalized = token.lower()
            if re.search(rf"(未提供|缺少|没有)[^，。；;\n]{{0,12}}{re.escape(normalized)}", compact) or re.search(
                rf"{re.escape(normalized)}[^，。；;\n]{{0,12}}(未提供|缺少|没有|数据缺失)",
                compact,
            ):
                violations.append(
                    {
                        "type": "contradicts_month_coverage",
                        "claim": f"缺少月份 {month}",
                        "evidence": f"coverage.months includes {month}",
                    }
                )
                break
    if facts["cashflow_row_count"] > 0 and any(token in compact for token in ("没有明细", "明细为空", "无明细")):
        violations.append(
            {
                "type": "contradicts_detail_rows",
                "claim": "没有明细",
                "evidence": f"cashflow_row_count={facts['cashflow_row_count']}",
            }
        )
    for fact in facts["contract_facts"]:
        expected_contracts = _safe_float(fact.get("contracts"))
        if expected_contracts is None or expected_contracts == 1:
            continue
        for segment in _answer_guard_segments(text):
            if not _segment_matches_contract_fact(segment, fact):
                continue
            if _contains_singular_contract_claim(segment):
                violations.append(
                    {
                        "type": "contradicts_contract_quantity",
                        "claim": "一手/1张",
                        "evidence": (
                            f"{fact.get('account') or '-'} {fact.get('symbol') or '-'} "
                            f"{fact.get('option_type') or '-'} expected contracts={_format_contract_count(expected_contracts)} "
                            f"from {fact.get('row_type') or 'monthly_income_report'}."
                        ),
                    }
                )
                break
    violations.extend(_unsupported_assigned_stock_numeric_claims(text, observations=observations))
    contract_verification = verify_response_against_evidence(text, evidence_bundle=evidence_bundle)
    violations.extend(contract_verification.violations)
    return {"facts": facts, "contract_verifier": contract_verification.public_payload(), "violations": violations}


_UX_FORCED_SECTION_RE = re.compile(r"(?im)^\s*(?:事实|分析)\s*[:：]?\s*$")
_UX_INTERNAL_MODE_RE = re.compile(
    r"(?i)\b(?:canonical|synthesis|fact\s*mode|analysis\s*mode|tool_plan|output_contract|evidencebundle|assistant\.answer_evidence)\b"
)
_UX_TOOL_NAME_RE = re.compile(r"(?i)\b(?:analysis_query|analysis_catalog)\b")
_UX_SQL_RE = re.compile(r"(?is)(?:\bsql\b|\bselect\b.{0,240}\bfrom\b|\bwith\b.{0,240}\bselect\b)")
_UX_INTERNAL_ID_RE = re.compile(
    r"(?i)\b(?:stock_lot_id|record_id|event_id|source_deal_id|position_key|trace_id|artifact_path)\b"
)
_UX_INTERNAL_PATH_RE = re.compile(
    r"(?i)(?:/Volumes/|/Users/|output_runs/|output_shared/|candidate_filter_trace\.jsonl|\.(?:sqlite3|jsonl)\b)"
)


def _normal_answer_ux_violations(response_text: str) -> list[dict[str, Any]]:
    text = str(response_text or "")
    checks: tuple[tuple[str, str, re.Pattern[str]], ...] = (
        ("unsupported_internal_mode_leak", "internal answer mode leaked", _UX_INTERNAL_MODE_RE),
        ("unsupported_internal_tool_leak", "internal tool name leaked", _UX_TOOL_NAME_RE),
        ("unsupported_internal_sql_leak", "SQL detail leaked", _UX_SQL_RE),
        ("unsupported_internal_id_leak", "internal identifier leaked", _UX_INTERNAL_ID_RE),
        ("unsupported_internal_path_leak", "internal artifact path leaked", _UX_INTERNAL_PATH_RE),
        ("unsupported_forced_fact_analysis_split", "forced fact/analysis section split leaked", _UX_FORCED_SECTION_RE),
    )
    violations: list[dict[str, Any]] = []
    for violation_type, evidence, pattern in checks:
        match = pattern.search(text)
        if not match:
            continue
        claim = str(match.group(0) or "").strip()
        violations.append(
            {
                "type": violation_type,
                "claim": claim[:80],
                "evidence": evidence,
            }
        )
    return violations


def _with_answer_guard_feedback(observations: list[dict[str, Any]], guard: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        *observations,
        {
            "index": len(observations) + 1,
            "tool_name": "assistant.answer_guard",
            "payload": {},
            "ok": True,
            "error": None,
            "data": {
                "violations": guard.get("violations") or [],
                "rewrite_instruction": (
                    "Your previous response contradicted tool observations. Rewrite using only observations. "
                    "When monthly_income_report query_scope.month=all_available, answer over the OM local ledger coverage. "
                    "Do not claim missing months/accounts unless coverage or diagnostics explicitly says so. "
                    "Answer as one natural user-facing Agent response; do not expose canonical/synthesis/fact/analysis modes, "
                    "SQL, tool names, internal ids, artifact paths, or raw tool receipts. "
                    "For factual rows, use contracts/contracts_open/contracts_closed as the trade quantity; "
                    "do not treat one row or one lot as one contract, and do not alter symbols, dates, strikes, or accounts."
                ),
            },
        },
    ]


def _answer_guard_facts(observations: list[dict[str, Any]]) -> dict[str, Any]:
    months: set[str] = set()
    accounts: set[str] = set()
    all_tools_ok = True
    complete_for_query_scope = False
    cashflow_row_count = 0
    contract_facts: list[dict[str, Any]] = []
    for item in observations:
        if not bool(item.get("ok", False)):
            all_tools_ok = False
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        if not isinstance(data, dict):
            continue
        contract = item.get("output_contract") if isinstance(item.get("output_contract"), dict) else {}
        guard_profile = str(contract.get("guard_profile") or "").strip()
        coverage = data.get("coverage") if isinstance(data.get("coverage"), dict) else {}
        for month in coverage.get("months") or []:
            if str(month).strip():
                months.add(str(month))
        for account in coverage.get("accounts") or []:
            if str(account).strip():
                accounts.add(str(account))
        if bool(coverage.get("complete_for_query_scope")):
            complete_for_query_scope = True
        count = data.get("cashflow_row_count")
        if count is None:
            rows = data.get("cashflow_rows")
            count = len(rows) if isinstance(rows, list) else 0
        try:
            cashflow_row_count = max(cashflow_row_count, int(count or 0))
        except Exception:
            pass
        if guard_profile in {"income_summary", "income_rows"} or str(item.get("tool_name") or "") == "monthly_income_report":
            contract_facts.extend(_monthly_income_contract_facts(data))
        if guard_profile == "position_rows" or str(item.get("tool_name") or "") == "option_positions_read":
            contract_facts.extend(_position_contract_facts(data))
    return {
        "months": sorted(months),
        "accounts": sorted(accounts),
        "all_tools_ok": all_tools_ok,
        "complete_for_query_scope": complete_for_query_scope,
        "cashflow_row_count": cashflow_row_count,
        "contract_facts": contract_facts,
    }


def _monthly_income_contract_facts(data: dict[str, Any]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for row_type, rows_key, quantity_key in (
        ("cashflow_rows", "cashflow_rows", "contracts"),
        ("premium_rows", "premium_rows", "contracts"),
        ("realized_rows", "realized_rows", "contracts_closed"),
    ):
        rows = data.get(rows_key)
        if not isinstance(rows, list):
            continue
        for row_raw in rows:
            if not isinstance(row_raw, dict):
                continue
            contracts = _safe_float(row_raw.get(quantity_key))
            if contracts is None or contracts <= 0:
                continue
            facts.append(
                {
                    "row_type": row_type,
                    "quantity_field": quantity_key,
                    "contracts": contracts,
                    "month": row_raw.get("month"),
                    "account": row_raw.get("account"),
                    "symbol": row_raw.get("symbol"),
                    "option_type": row_raw.get("option_type"),
                    "trade_action": row_raw.get("trade_action"),
                    "close_type": row_raw.get("close_type"),
                    "realized_gross": row_raw.get("realized_gross"),
                    "net_cashflow_gross": row_raw.get("net_cashflow_gross"),
                    "premium_received_gross": row_raw.get("premium_received_gross"),
                    "currency": row_raw.get("currency"),
                }
            )
    return facts


def _position_contract_facts(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = data.get("rows")
    if not isinstance(rows, list):
        rows = data.get("positions")
    if not isinstance(rows, list):
        return []
    facts: list[dict[str, Any]] = []
    for row_raw in rows:
        if not isinstance(row_raw, dict):
            continue
        contracts = _safe_float(row_raw.get("contracts_open") if row_raw.get("contracts_open") is not None else row_raw.get("contracts"))
        if contracts is None or contracts <= 0:
            continue
        facts.append(
            {
                "row_type": "option_positions_read.rows",
                "quantity_field": "contracts_open",
                "contracts": contracts,
                "account": row_raw.get("account"),
                "symbol": row_raw.get("symbol"),
                "option_type": row_raw.get("option_type"),
                "side": row_raw.get("side"),
                "strike": row_raw.get("strike"),
                "expiration_ymd": row_raw.get("expiration_ymd"),
                "expiration": row_raw.get("expiration"),
            }
        )
    return facts


_CURRENCY_NUMERIC_CLAIM_RE = re.compile(r"\b(?:USD|HKD|CNY)\s*([-+]?\d[\d,]*(?:\.\d+)?)", re.IGNORECASE)
_UNIT_NUMERIC_CLAIM_RE = re.compile(r"(?<![\w.])([-+]?\d[\d,]*(?:\.\d+)?)\s*(股|条|笔|张)")
_PERCENT_NUMERIC_CLAIM_RE = re.compile(r"(?<![\w.])([-+]?\d[\d,]*(?:\.\d+)?)\s*%")


def _unsupported_assigned_stock_numeric_claims(
    response_text: str,
    *,
    observations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    allowed_values = _assigned_stock_allowed_numeric_values(observations)
    if not allowed_values:
        return []
    violations: list[dict[str, Any]] = []
    for raw in _CURRENCY_NUMERIC_CLAIM_RE.findall(response_text):
        value = _parse_claim_number(raw)
        if value is not None and not _numeric_value_allowed(value, allowed_values):
            violations.append(
                {
                    "type": "unsupported_assigned_stock_number",
                    "claim": raw,
                    "evidence": "number is not present in assigned-stock tool rows or currency totals",
                }
            )
    for raw, unit in _UNIT_NUMERIC_CLAIM_RE.findall(response_text):
        value = _parse_claim_number(raw)
        if value is not None and not _numeric_value_allowed(value, allowed_values):
            violations.append(
                {
                    "type": "unsupported_assigned_stock_number",
                    "claim": f"{raw}{unit}",
                    "evidence": "number is not present in assigned-stock tool rows, counts, or share quantities",
                }
            )
    for raw in _PERCENT_NUMERIC_CLAIM_RE.findall(response_text):
        violations.append(
            {
                "type": "unsupported_assigned_stock_percent",
                "claim": f"{raw}%",
                "evidence": "assigned-stock tool output does not provide percentage return facts",
            }
        )
    return violations[:5]


def _assigned_stock_allowed_numeric_values(observations: list[dict[str, Any]]) -> list[float]:
    values: list[float] = []
    numeric_fields = (
        "shares_remaining",
        "shares_sold",
        "stock_cost_per_share",
        "remaining_stock_cost_basis",
        "remaining_market_value",
        "spot",
        "assigned_stock_unrealized_pnl",
        "assigned_stock_realized_pnl",
        "option_premium_attribution",
        "assignment_lifecycle_pnl",
    )
    summed_fields = (
        "remaining_stock_cost_basis",
        "remaining_market_value",
        "assigned_stock_unrealized_pnl",
        "assigned_stock_realized_pnl",
        "option_premium_attribution",
        "assignment_lifecycle_pnl",
    )
    for item in observations:
        if str(item.get("tool_name") or "") != "option_positions_read":
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        if str(data.get("action") or "").strip().lower() != "assigned-stock":
            contract = item.get("output_contract") if isinstance(item.get("output_contract"), dict) else {}
            if str(contract.get("canonical_renderer") or "") != "assigned_stock_lifecycle":
                continue
        rows = data.get("rows") or data.get("assigned_stock_lots")
        if not isinstance(rows, list):
            continue
        _append_numeric_value(values, data.get("row_count"))
        _append_numeric_value(values, len(rows))
        counts_by_symbol: dict[str, int] = {}
        counts_by_currency: dict[str, int] = {}
        sums_by_currency: dict[str, dict[str, float]] = {}
        for row_raw in rows:
            if not isinstance(row_raw, dict):
                continue
            symbol = str(row_raw.get("symbol") or "").strip()
            currency = str(row_raw.get("currency") or "").strip().upper()
            if symbol:
                counts_by_symbol[symbol] = counts_by_symbol.get(symbol, 0) + 1
            if currency:
                counts_by_currency[currency] = counts_by_currency.get(currency, 0) + 1
            for field in numeric_fields:
                _append_numeric_value(values, row_raw.get(field))
            bucket = sums_by_currency.setdefault(currency, {})
            for field in summed_fields:
                amount = _safe_float(row_raw.get(field))
                if amount is not None:
                    bucket[field] = bucket.get(field, 0.0) + amount
        for count in [*counts_by_symbol.values(), *counts_by_currency.values()]:
            _append_numeric_value(values, count)
        for bucket in sums_by_currency.values():
            for amount in bucket.values():
                _append_numeric_value(values, amount)
    return values


def _append_numeric_value(values: list[float], value: Any) -> None:
    number = _safe_float(value)
    if number is not None:
        values.append(number)


def _parse_claim_number(raw: str) -> float | None:
    try:
        return float(str(raw or "").replace(",", ""))
    except Exception:
        return None


def _numeric_value_allowed(value: float, allowed_values: list[float]) -> bool:
    for allowed in allowed_values:
        tolerance = max(0.01, abs(allowed) * 0.00001)
        if abs(value - allowed) <= tolerance:
            return True
    return False


def _answer_guard_segments(text: str) -> list[str]:
    return [segment for segment in re.split(r"[\n。！？!?]+", str(text or "")) if segment.strip()]


def _segment_matches_contract_fact(segment: str, fact: dict[str, Any]) -> bool:
    compact = re.sub(r"\s+", "", str(segment or "").lower())
    if not compact:
        return False
    account = str(fact.get("account") or "").strip().lower()
    if account and account not in compact:
        return False
    symbol = str(fact.get("symbol") or "").strip().lower()
    if symbol:
        symbol_tokens = {symbol, symbol.replace(".", "")}
        if "." in symbol:
            symbol_tokens.add(symbol.split(".", 1)[0])
        if not any(token and token in compact for token in symbol_tokens):
            return False
    option_type = str(fact.get("option_type") or "").strip().lower()
    if option_type == "put" and not any(token in compact for token in ("put", "沽", "认沽")):
        return False
    if option_type == "call" and not any(token in compact for token in ("call", "购", "认购")):
        return False
    return True


def _contains_singular_contract_claim(segment: str) -> bool:
    compact = re.sub(r"\s+", "", str(segment or "").lower())
    return bool(re.search(r"(^|[^\d])1(手|张)", compact) or "一手" in compact or "一张" in compact)


def _format_contract_count(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return str(value)


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _return_summary_row_is_calculable(row: dict[str, Any]) -> bool:
    cash = _safe_float(row.get("cash_secured_cny"))
    if cash is None or cash <= 0:
        return False
    return any(
        row.get(key) is not None
        for key in (
            "net_return_rate",
            "premium_return_rate",
            "realized_return_rate",
            "net_income_cny",
            "premium_income_cny",
            "realized_pnl_cny",
        )
    )


def _assess_plan_capabilities(plan: PlannerPlan, observations: list[dict[str, Any]]) -> dict[str, Any]:
    required = list(plan.required_capabilities)
    satisfied: list[str] = []
    gaps: list[str] = []
    for capability in required:
        checker = _MONTHLY_INCOME_CAPABILITY_CHECKS.get(capability)
        if checker is not None:
            if checker(observations):
                satisfied.append(capability)
            else:
                gaps.append(capability)
        elif _observations_have_tool_capability(observations, capability):
            satisfied.append(capability)
        else:
            gaps.append(capability)
    return {"required": required, "satisfied": satisfied, "gaps": gaps}


def _observations_have_tool_capability(observations: list[dict[str, Any]], capability: str) -> bool:
    required = str(capability or "").strip()
    if not required:
        return False
    for item in observations:
        if not bool(item.get("ok", False)):
            continue
        tool_name = str(item.get("tool_name") or "")
        definition = get_tool_definition(tool_name)
        if definition is not None and required in definition.capabilities:
            return True
    return False


def _observations_have_monthly_income_report_result(observations: list[dict[str, Any]]) -> bool:
    for item in _monthly_income_observations(observations):
        data = item.get("data")
        if not isinstance(data, dict):
            continue
        if any(isinstance(data.get(key), list) and data.get(key) for key in ("summary", "return_summary", "diagnostics")):
            return True
    return False


def _observations_have_account_return(observations: list[dict[str, Any]]) -> bool:
    for item in _monthly_income_observations(observations):
        data = item.get("data")
        if not isinstance(data, dict):
            continue
        rows = data.get("return_summary")
        if isinstance(rows, list) and any(isinstance(row, dict) and _return_summary_row_is_calculable(row) for row in rows):
            return True
    return False


def _observations_have_all_accounts_breakdown(observations: list[dict[str, Any]]) -> bool:
    for item in _monthly_income_observations(observations):
        data = item.get("data")
        if not isinstance(data, dict):
            continue
        rows = data.get("return_summary")
        if not isinstance(rows, list):
            continue
        accounts = {
            str(row.get("account") or "").strip()
            for row in rows
            if isinstance(row, dict) and str(row.get("account") or "").strip() and _return_summary_row_is_calculable(row)
        }
        if accounts:
            return True
    return False


def _observations_have_combined_account_return(observations: list[dict[str, Any]]) -> bool:
    for item in _monthly_income_observations(observations):
        data = item.get("data")
        if not isinstance(data, dict):
            continue
        rows = data.get("combined_return_summary")
        if isinstance(rows, list) and any(isinstance(row, dict) and _return_summary_row_is_calculable(row) for row in rows):
            return True
    return False


def _observations_have_cashflow_detail(observations: list[dict[str, Any]]) -> bool:
    for item in _monthly_income_observations(observations):
        data = item.get("data")
        if not isinstance(data, dict):
            continue
        rows = data.get("cashflow_rows")
        if isinstance(rows, list) and any(isinstance(row, dict) for row in rows):
            return True
    return False


def _observations_have_assigned_stock_positions(observations: list[dict[str, Any]]) -> bool:
    for item in observations:
        if str(item.get("tool_name") or "") != "option_positions_read" or not bool(item.get("ok", False)):
            continue
        data = item.get("data")
        if not isinstance(data, dict):
            continue
        if str(data.get("action") or "").strip().lower() == "assigned-stock":
            return True
        filters = data.get("filters")
        if isinstance(filters, dict) and filters.get("refresh_quotes") is not None and isinstance(data.get("rows"), list):
            return True
    return False


def _monthly_income_observations(observations: list[dict[str, Any]]):
    for item in observations:
        if str(item.get("tool_name") or "") == "monthly_income_report" and bool(item.get("ok", False)):
            yield item


_MONTHLY_INCOME_CAPABILITY_CHECKS = {
    "income_report": _observations_have_monthly_income_report_result,
    "account_return": _observations_have_account_return,
    "all_accounts_breakdown": _observations_have_all_accounts_breakdown,
    "combined_account_return": _observations_have_combined_account_return,
    "cashflow_detail": _observations_have_cashflow_detail,
    "assigned_stock_positions": _observations_have_assigned_stock_positions,
    "assigned_stock_pnl": _observations_have_assigned_stock_positions,
}


def _capability_gap_response(observations: list[dict[str, Any]]) -> str:
    for item in reversed(observations):
        if str(item.get("tool_name") or "") != "assistant.capability_check":
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        status = data.get("capability_status") if isinstance(data, dict) else None
        if not isinstance(status, dict):
            continue
        gaps = [str(value) for value in status.get("gaps") or [] if str(value).strip()]
        if not gaps:
            continue
        if "combined_account_return" in gaps:
            return (
                "当前只能部分满足：工具已完成查询，但没有可用的合并账户收益率结果。"
                "不能把分账户收益率直接平均成合并收益率；需要 combined_return_summary "
                "按 sum(净现金流CNY)/sum(当前现金担保CNY) 输出后才能确认。"
            )
        return "当前只能部分满足：工具结果缺少请求所需能力：" + "、".join(gaps)
    return ""


def _month_label_cn(month: str) -> str:
    parts = str(month or "").split("-")
    if len(parts) != 2:
        return ""
    try:
        return f"{int(parts[1])}月"
    except Exception:
        return ""


def _inject_system_fields(arguments: dict[str, Any], *, request: AssistantRequest, tool_name: str) -> dict[str, Any]:
    payload = dict(arguments or {})
    if tool_name in _CONFIG_SCOPED_PLAN_TOOLS:
        config_path = _config_path_for_tool_payload(tool_name=tool_name, payload=payload, default=request.config_path)
        if config_path:
            payload["config_path"] = config_path
        elif request.config_key:
            payload["config_key"] = _config_key_for_tool_payload(tool_name=tool_name, payload=payload, default=request.config_key)
    if tool_name == "option_positions_read":
        payload.setdefault("action", "list")
    if tool_name == "runtime_runs":
        payload.setdefault("limit", 10)
    if tool_name == "runtime_logs":
        payload.setdefault("kind", "all")
        payload.setdefault("lines", 50)
    return payload


def _config_path_for_tool_payload(*, tool_name: str, payload: dict[str, Any], default: str | None) -> str | None:
    if not default:
        return None
    if tool_name != "symbol_config_read":
        return default
    market_key = _market_config_key(payload.get("symbol"))
    if market_key is None:
        return default
    path = Path(str(default))
    if path.name not in {"config.us.json", "config.hk.json"}:
        return default
    return str(path.with_name(f"config.{market_key}.json"))


def _config_key_for_tool_payload(*, tool_name: str, payload: dict[str, Any], default: str) -> str:
    if tool_name != "symbol_config_read":
        return default
    market_key = _market_config_key(payload.get("symbol"))
    if market_key is not None:
        return market_key
    return default


def _market_config_key(symbol: Any) -> str | None:
    market = str(symbol_market(symbol) or "").strip().upper()
    if market == "HK":
        return "hk"
    if market == "US":
        return "us"
    return None


def _planner_input_text(text: str, *, conversation_context: dict[str, Any] | None) -> str:
    payload: dict[str, Any] = {
        "message": str(text or ""),
        "tools": _planner_tool_manifest(),
    }
    if isinstance(conversation_context, dict):
        payload["context"] = {
            "window_messages": int(conversation_context.get("window_messages") or 0),
            "temporal_context": conversation_context.get("temporal_context")
            if isinstance(conversation_context.get("temporal_context"), dict)
            else {},
            "semantics": conversation_context.get("semantics") if isinstance(conversation_context.get("semantics"), dict) else {},
            "last_successful_read": conversation_context.get("last_successful_read")
            if isinstance(conversation_context.get("last_successful_read"), dict)
            else None,
            "user_profile": conversation_context.get("user_profile")
            if isinstance(conversation_context.get("user_profile"), dict)
            else {"provided": False},
        }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _planner_instructions() -> str:
    return """\
You are the options-monitor assistant capability planner.
Return only JSON that matches the requested schema.

Rules:
- Produce 1 to 3 read-only tool calls, or exactly 1 preview-write capability call.
- Use only tools/capabilities in the provided manifest.
- Fill required_capabilities with the user's required answer capabilities from the tool manifest. Use [] only when the request needs no special capability beyond the planned tool call.
- Preview-write capabilities only create a pending preview. They never apply writes, confirm pending operations, notify users externally, or mutate config/ledger directly.
- Never plan confirm/cancel/apply actions. Confirm/cancel must be handled by deterministic user commands bound to a pending operation.
- Do not include system-scoped, path, config, audit, host, port, timeout, service, delivery, or trigger arguments such as config_key, config_path, data_config, output_dir, report_path, run_dir, logs_root, state_dir, opend_telnet_host, timeout_sec, or audit_db. The system injects those.
- Resolve relative dates using context.temporal_context.current_date in Asia/Shanghai. For a month without a year such as "6月", use the current_date year.
- For monthly income summary questions, use monthly_income_report with account/month when available.
- For combined/all-account return questions, include required_capabilities=["combined_account_return"] and use monthly_income_report without account.
- For cashflow detail, net cashflow composition, net inflow source, "明细", "组成", "构成", "来源", or "由什么组成", use monthly_income_report with include_rows=true; the Agent composer will write the final user response from tool evidence.
- For assigned stock / 被指派正股 / 指派正股 holding PnL, floating PnL, spot, cost basis, or lifecycle PnL questions, use option_positions_read with action="assigned-stock", status="open" unless the user asks all/closed, refresh_quotes=true for current holding PnL. Use response_mode=synthesis so the Agent composer can summarize the holding PnL from tool evidence.
- For all-history, cumulative, or total net cashflow questions, omit month so monthly_income_report reads all OM local ledger months.
- For multiple explicit months, either call monthly_income_report once per month with matching arguments, or omit month and synthesize from all available rows; never duplicate one month while claiming another.
- For "记录开仓", "记录平仓", Futu 成交提醒, 成功卖出/买入 option fills, use manual_trade_open or manual_trade_close with raw_text set to the original user message.
- For current monitored-symbol config questions such as "max strike 是多少", "当前配置", or "查询 sell_put.max_strike", use symbol_config_read with symbol plus optional strategy/field.
- For open-ended analytical questions such as 对比, 有什么不同, 排名, 趋势, 组成, 来源, 按账户/月份/标的汇总, or cross-domain questions across income/positions/trades/assigned stock/config, prefer analysis_query over narrow business renderers. Use analysis_catalog first only when fields/views are unknown.
- For analysis_query, use only columns listed in the tool manifest analysis_views. Never invent SQL columns. If the needed fields are not clear from the manifest, plan analysis_catalog before analysis_query.
- For monitored-symbol setting changes such as covered call min strike 85, use symbol_edit. Do not use symbol_edit for questions about the current value.
- For model switch requests, use model_use. For immediate software upgrade requests, use upgrade_now.
- response_mode is a top-level plan field only. Never include response_mode inside any step.arguments.
- response_mode is an internal compatibility field, not a user-visible mode. Prefer response_mode=synthesis for financial answers that should be composed from tool evidence; deterministic renderers remain fallback.
- Use response_mode=canonical only for narrow status/config lookups where a direct deterministic value is the whole answer.
- If there is no safe plan, or required slots are missing and the capability cannot safely handle them, return steps=[] instead of guessing.
"""


def _planner_tool_manifest() -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for name in sorted(AGENT_LOOP_READ_TOOLS):
        definition = get_tool_definition(name)
        if definition is None:
            continue
        input_schema = {
            key: value
            for key, value in definition.input_schema.items()
            if not _is_banned_plan_argument(str(key))
        }
        notes: list[str] = []
        semantics: dict[str, Any] = {}
        if name == "monthly_income_report":
            notes.append("Set include_rows=true for cashflow details, composition, source, 明细, 组成, 构成, 来源, or 由什么组成.")
            notes.append("When include_rows=true, canonical factual rows are rendered by the system; synthesis should only add analysis.")
            notes.append("Data comes from OM local ledger, not broker realtime cash statements.")
            notes.append("If month is omitted, the tool reads all months currently available in the OM local ledger.")
            notes.append("If account is omitted, the tool reads all ledger accounts available for the selected broker/config.")
            notes.append("For combined/all-account return questions, require capability combined_account_return; the response must use combined_return_summary when available.")
            semantics = {
                "data_source": "OM local ledger",
                "answer_capabilities": {
                    "account_return": "single-account monthly return_summary rows",
                    "all_accounts_breakdown": "per-account return_summary rows when account is omitted",
                    "combined_account_return": "combined_return_summary rows; compute rates as summed CNY numerator divided by summed CNY cash-secured denominator",
                    "cashflow_detail": "cashflow_rows when include_rows=true",
                },
                "scope_semantics": {
                    "month omitted": "all months currently available in the OM local ledger",
                    "account omitted": "all available ledger accounts for the selected broker/config",
                    "include_rows": "include detail rows for composition/source questions",
                },
                "not_promised": [
                    "complete broker account history before OM ledger ingestion",
                    "realtime broker cash statement",
                ],
                "answer_rules": [
                    "For 历史以来, 累计, or 总净现金流, answer over the OM local ledger coverage returned by the tool.",
                    "Do not claim missing history solely because coverage contains only some months.",
                    "Do not claim an account is missing if coverage.accounts includes it.",
                ],
            }
        if name == "analysis_catalog":
            notes.append("Use when the user asks what data/fields can be analyzed, or before analysis_query if view names are unknown.")
            notes.append("This is a pure-read catalog; it does not answer the business question by itself.")
            semantics = {
                "data_source": "OM Tool OS catalog",
                "answer_capabilities": {
                    "analysis_catalog": "lists whitelisted read-only views and SQL rules",
                    "read_only": "catalog only; no ledger mutation",
                },
                "scope_semantics": {
                    "view omitted": "return all available analysis views",
                },
            }
        if name == "analysis_query":
            notes.append("Use for open-ended analysis: 对比, 有什么不同, 排名, 趋势, 组成, 来源, 按账户/月份/标的汇总, 差额, 收益率差.")
            notes.append("Generate one SELECT or WITH query over analysis_catalog views; never include writes, PRAGMA, ATTACH, paths, config, or system arguments.")
            notes.append("Use only the columns listed in semantics.analysis_views. Do not invent columns such as net_cashflow, total_return, return_rate, or open_basis_pnl unless they are listed.")
            notes.append("For account income/performance comparison, prefer account_monthly_performance columns month, account, net_income_cny, net_return_rate, realized_pnl_cny, premium_income_cny, and cash_secured_cny.")
            notes.append("For account income composition or source questions, use account_monthly_income_components; filter included_in_net_income=1 when explaining return numerator components.")
            notes.append("For assigned-stock PnL analysis, prefer assigned_stock_position_pnl; use assigned_stock_sale_events when the question asks about sold shares or realized sale PnL.")
            notes.append("For current option exposure or expiry concentration, use open_option_exposure and expiration_risk_buckets; for symbol-level income drivers, use symbol_income_attribution.")
            notes.append("For strategy setting comparisons by symbol/account, use strategy_config_by_symbol_account instead of raw config rows.")
            notes.append("For candidate diagnostics, close-advice questions, runtime push/scan diagnostics, or quote freshness gaps, use candidate_filter_diagnostics, close_advice_snapshot, runtime_tick_status, and quote_freshness.")
            notes.append("Do not avg/sum return-rate fields directly; recompute weighted rates from money numerator and cash_secured_cny when aggregating rows.")
            notes.append("Tool result rows/cell_refs are evidence; if synthesis fails, the analysis_result renderer preserves the task-shaped table.")
            semantics = {
                "data_source": "OM read-only analysis workspace backed by local ledger/config/runtime read tools",
                "analysis_views": _analysis_views_for_planner_manifest(),
                "query_templates": {
                    "lx_sy_income_comparison": (
                        "select month, "
                        "round(sum(case when account = 'lx' then net_income_cny else 0 end), 2) as lx_income_cny, "
                        "round(sum(case when account = 'sy' then net_income_cny else 0 end), 2) as sy_income_cny, "
                        "case when sum(case when account = 'lx' then net_income_cny else 0 end) >= "
                        "sum(case when account = 'sy' then net_income_cny else 0 end) then 'lx' else 'sy' end as higher_account, "
                        "round(abs(sum(case when account = 'lx' then net_income_cny else 0 end) - "
                        "sum(case when account = 'sy' then net_income_cny else 0 end)), 2) as income_diff_cny, "
                        "round(max(case when account = 'lx' then net_return_rate end) - "
                        "max(case when account = 'sy' then net_return_rate end), 6) as return_rate_diff "
                        "from account_monthly_performance where account in ('lx','sy') group by month order by month"
                    ),
                },
                "answer_capabilities": {
                    "analysis_query": "comparison, ranking, breakdown, trend, grouping, and cross-domain read-only analysis",
                    "read_only": "SELECT-only in-memory SQLite over whitelisted views",
                },
                "scope_semantics": {
                    "account/month omitted": "materialize available local OM ledger coverage, then SQL can filter/group rows",
                    "limit": "caps returned rows; tool reports truncation",
                },
                "not_promised": [
                    "arbitrary Python execution",
                    "database writes",
                    "broker realtime statement outside existing read tools",
                ],
                "answer_rules": [
                    "Use SQL result rows as the source for user-visible amounts, accounts, symbols, dates, quantities, and statuses.",
                    "Do not expose internal canonical/synthesis mode names.",
                    "If a requested comparison is unsupported by available rows, say what data is missing instead of returning a nearby raw report.",
                ],
            }
        if name == "option_positions_read":
            notes.append("Use for current option position list/detail requests, including 持仓明细, 持仓明晰, 持仓详情, 当前仓位, or current positions.")
            notes.append("For assigned stock / 被指派正股 / 指派正股 holding PnL, use action=assigned-stock with status=open by default and refresh_quotes=true when the user asks current 盈亏, spot, 浮盈亏, or 持仓盈亏; use synthesis so the Agent composer can answer from tool evidence.")
            notes.append("For ordinary position list/detail requests, required_capabilities should be [] because option_positions_read itself provides option_positions/read_only.")
            notes.append("Use action=list for current lots; use action=history or action=inspect only when the user explicitly asks for event history, projection, repair, or ledger diagnostics.")
            notes.append("For action=list or action=assigned-stock, tool rows are evidence; deterministic renderers are fallback/provenance, not the default user-visible mode.")
            semantics = {
                "data_source": "local option position ledger",
                "answer_capabilities": {
                    "option_positions": "successful option_positions_read observations provide option position rows",
                    "assigned_stock_positions": "action=assigned-stock provides Sell Put assignment stock lots, cost basis, spot status, realized/unrealized stock PnL, and lifecycle PnL",
                    "read_only": "option_positions_read is registry-declared read-only",
                    "ledger_diagnostics": "history or inspect actions provide ledger diagnostic context when explicitly requested",
                },
                "scope_semantics": {
                    "status omitted": "open option positions",
                    "assigned-stock status omitted": "use open assigned-stock lots for holding PnL unless the user asks all/closed",
                    "account omitted": "all available accounts for the selected config",
                    "detail words": "明细, 明晰, 详情, and current positions are ordinary list/detail reads",
                },
                "not_promised": [
                    "broker realtime statement outside the local OM ledger",
                    "ordinary option profit or return calculations; use monthly_income_report for monthly income questions",
                    "close advice; use close_advice_read for should-close or take-profit analysis",
                ],
            }
        if name == "symbol_config_read":
            notes.append("Use for current monitored-symbol config questions, for example sell_put.max_strike, covered_call.min_strike, or enabled state.")
            notes.append("symbol is required; strategy can be sell_put, sell_call/covered_call, or combo_yield; field can be enabled, min_strike, max_strike, min_dte, or max_dte.")
            notes.append("Do not use this for setting changes; use the symbol_edit preview capability for changes.")
            semantics = {
                "data_source": "selected runtime config",
                "answer_capabilities": {
                    "symbol_config_read": "reads current monitored-symbol strategy config without mutating config",
                },
                "missing_behavior": "If symbol, strategy, or field is not configured, return the missing reason instead of planning a weakly related tool.",
            }
        item = {
            "name": name,
            "description": definition.description,
            "capabilities": list(definition.capabilities),
            "input_schema": input_schema,
            "safe_default_input": dict(definition.safe_default_input),
            "planner_notes": notes,
        }
        if semantics:
            item["semantics"] = semantics
        tools.append(item)
    for spec in planner_preview_specs():
        item = {
            "name": spec.intent_name,
            "description": spec.summary,
            "capabilities": ["preview_operation"],
            "input_schema": _planner_preview_input_schema(spec.intent_name),
            "safe_default_input": {},
            "risk_level": spec.risk_level,
            "operation_action": "preview",
            "operation_target": spec.operation_target,
            "planner_notes": _planner_preview_notes(spec.intent_name),
        }
        tools.append(item)
    return tools


def _analysis_views_for_planner_manifest() -> dict[str, dict[str, Any]]:
    views: dict[str, dict[str, Any]] = {}
    for name, spec in ANALYSIS_VIEW_SPECS.items():
        field_semantics = spec.get("field_semantics") if isinstance(spec.get("field_semantics"), dict) else {}
        views[name] = {
            "description": str(spec.get("description") or ""),
            "fields": [str(field) for field in spec.get("fields") or []],
            "row_grain": str(spec.get("row_grain") or ""),
            "primary_keys": [str(item) for item in spec.get("primary_keys") or ()],
            "time_grain": str(spec.get("time_grain") or ""),
            "source_tools": [str(item) for item in spec.get("source_tools") or ()],
            "freshness": str(spec.get("freshness") or ""),
            "recommended_filters": [str(item) for item in spec.get("recommended_filters") or ()],
            "safe_join_keys": [str(item) for item in spec.get("safe_join_keys") or ()],
            "alias_of": str(spec.get("alias_of") or ""),
            "field_semantics": _analysis_field_semantics_for_planner(field_semantics),
        }
    return views


def _analysis_field_semantics_for_planner(field_semantics: dict[str, Any]) -> dict[str, dict[str, Any]]:
    allowed_keys = {
        "type",
        "unit",
        "currency",
        "formula",
        "aggregation",
        "null_meaning",
        "source",
        "freshness",
        "do_not",
    }
    out: dict[str, dict[str, Any]] = {}
    for field, raw_meta in field_semantics.items():
        if not isinstance(raw_meta, dict):
            continue
        meta = {key: value for key, value in raw_meta.items() if key in allowed_keys and value not in (None, "", [], {})}
        out[str(field)] = meta
    return out


def _planner_preview_input_schema(intent_name: str) -> dict[str, Any]:
    if intent_name in {"manual_trade_open", "manual_trade_close"}:
        return {
            "raw_text": {
                "type": "string",
                "description": "Original user message, including account label and broker fill text when present.",
            },
            "account": {
                "type": ["string", "null"],
                "enum": [*ACCOUNT_VALUES, None],
                "description": "Optional account label if explicitly present. Keep raw_text complete regardless.",
            },
        }
    if intent_name == "manual_trade_update":
        return {
            "operation_id": {"type": ["string", "null"]},
            "operation_resolution": {"type": ["string", "null"]},
            "updates": {"type": "object"},
        }
    if intent_name == "symbol_edit":
        return {
            "symbol": {"type": "string"},
            "set": {"type": "object"},
            "ensure_use": {"type": ["array", "null"], "items": {"type": "string"}},
        }
    if intent_name == "model_use":
        return {"model_profile": {"type": "string"}}
    if intent_name == "upgrade_now":
        return {"target_version": {"type": ["string", "null"]}}
    return {}


def _planner_preview_notes(intent_name: str) -> list[str]:
    if intent_name == "manual_trade_open":
        return [
            "Use for 记录开仓, Futu 成交提醒, 成功卖出/买入 option opening fills.",
            "Set raw_text to the original user message so the deterministic trade parser can extract symbol, expiration, strike, contracts, premium, and account.",
            "Creates only a pending preview; it never writes the ledger until a deterministic confirm command is received.",
        ]
    if intent_name == "manual_trade_close":
        return [
            "Use for 记录平仓 and closing fill reminders.",
            "Set raw_text to the original user message.",
            "Creates only a pending preview; it never writes the ledger until a deterministic confirm command is received.",
        ]
    if intent_name == "manual_trade_update":
        return [
            "Use only to modify an existing pending manual trade preview.",
            "Creates an updated pending preview; it does not confirm or write the ledger.",
        ]
    if intent_name == "symbol_edit":
        return [
            "Use for monitored-symbol setting changes such as covered call min strike or sell put thresholds.",
            "Creates only a pending config-change preview.",
        ]
    if intent_name == "model_use":
        return ["Use for assistant model switch requests. Creates only a pending model-switch preview."]
    if intent_name == "upgrade_now":
        return ["Use for immediate software upgrade requests. Creates only a pending admin preview."]
    return []


def _synthesis_input_text(
    question: str,
    *,
    plan: PlannerPlan,
    observations: list[dict[str, Any]],
    conversation_context: dict[str, Any] | None,
) -> str:
    payload: dict[str, Any] = {
        "question": str(question or ""),
        "plan": plan.public_payload(),
        "observations": observations,
    }
    if isinstance(conversation_context, dict):
        payload["context"] = {
            "window_messages": int(conversation_context.get("window_messages") or 0),
            "user_profile": conversation_context.get("user_profile")
            if isinstance(conversation_context.get("user_profile"), dict)
            else {"provided": False},
        }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


_SYNTHESIS_INSTRUCTIONS = """\
You answer options-monitor chat questions using only provided tool observations.
Return only JSON.

Rules:
- Do not invent facts beyond observations.
- If required data is missing, say it cannot be confirmed and name the missing data.
- If observation.capability_status has gaps, say the request is only partially satisfied and name the gap; do not present a nearby result as complete.
- Do not downgrade a detail/composition question into a nearby summary.
- Keep Chinese output concise and Markdown-friendly.
- Start with the direct answer. Use a short "关键依据：" bullet block only when it improves readability.
- Do not force a 事实/分析 section split. Write one natural answer; use compact bullets only when the user asked for details or the rows are necessary.
- Do not expose SQL, tool names, raw tool receipts, artifact paths, trace ids, or internal ids such as stock_lot_id, record_id, event_id, source_deal_id, or position_key.
- Do not mention internal answer modes such as canonical, synthesis, fact mode, or analysis mode.
- If observations include assistant.answer_evidence, follow its composition_instruction. Do not append 数据来源 or 口径; the system appends deterministic provenance.
- Mention the data scope only when it changes the answer; do not duplicate deterministic provenance.
- For monthly_income_report, "历史以来", "累计", and "总净现金流" mean the OM local ledger coverage returned by the tool.
- Do not claim missing months/accounts when observation.coverage includes them or complete_for_query_scope=true.
- For monthly_income_report detail rows, contract quantity must come from contracts or contracts_closed. Do not infer one row as one contract; if contracts/contracts_closed=2, say 2张/2手, never 一手.
- If observations include assistant.grounded_facts, canonical_response is already the factual answer block. Return only a concise analysis block; do not repeat, restate, or alter factual rows, amounts, contract quantities, accounts, dates, symbols, or currencies.
"""

_SYNTHESIS_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "response_text": {"type": "string"},
    },
    "required": ["response_text"],
}


def _parse_provider_payload(response: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(response, dict):
        return None
    text = extract_response_text(response) or extract_chat_completion_text(response)
    if not text:
        return None
    try:
        parsed = json.loads(strip_json_code_fence(text))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _llm_trace(
    settings: Any,
    *,
    attempted: bool,
    reason: str,
    missing: list[str] | None = None,
    error_code: str | None = None,
    schema_version: str | None = None,
    conversation_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "enabled": bool(settings.enabled),
        "attempted": bool(attempted),
        "reason": str(reason),
        "provider": settings.provider,
        "base_url": settings.base_url,
        "model": settings.model,
        "api_key_env": settings.api_key_env,
        "confidence_min": float(settings.confidence_min),
        "timeout_seconds": int(settings.timeout_seconds),
        "max_output_tokens": int(settings.max_output_tokens),
    }
    if missing:
        payload["missing"] = list(missing)
    if error_code:
        payload["error_code"] = str(error_code)
    if schema_version:
        payload["schema_version"] = str(schema_version)
    if conversation_context is not None:
        recent = conversation_context.get("recent_messages") if isinstance(conversation_context, dict) else None
        pending = conversation_context.get("pending_operations") if isinstance(conversation_context, dict) else None
        payload["context"] = {
            "provided": True,
            "window_messages": int(conversation_context.get("window_messages") or 0) if isinstance(conversation_context, dict) else 0,
            "recent_count": len(recent) if isinstance(recent, list) else 0,
            "pending_count": len(pending) if isinstance(pending, list) else 0,
            "user_profile": user_profile_trace(
                conversation_context.get("user_profile") if isinstance(conversation_context, dict) else None
            ),
        }
    return payload


def _fact_data(tool_name: str, data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    if tool_name == "monthly_income_report":
        coverage = _monthly_income_coverage(data)
        out.setdefault("data_scope", "OM 本地账本")
        out.setdefault("query_scope", coverage.get("query_scope"))
        out.setdefault("coverage", coverage.get("coverage"))
    if tool_name == "analysis_query":
        out.setdefault("data_scope", "OM read-only analysis workspace")
    return out


def _synthesis_data(tool_name: str, data: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "analysis_query":
        return {
            "source_label": data.get("source_label") or "OM read-only analysis workspace",
            "query": dict(data.get("query") or {}) if isinstance(data.get("query"), dict) else {},
            "columns": list(data.get("columns") or []),
            "rows": _clip_list(data.get("rows"), limit=30),
            "row_count": data.get("row_count"),
            "truncated": bool(data.get("truncated", False)),
            "views_used": list(data.get("views_used") or []),
            "cell_refs": _clip_mapping(data.get("cell_refs"), limit=120) if isinstance(data.get("cell_refs"), dict) else {},
            "fallback_text": data.get("fallback_text"),
        }
    if tool_name == "monthly_income_report":
        coverage = _monthly_income_coverage(data)
        out = {
            "summary": _clip_list(data.get("summary"), limit=8),
            "return_summary": _clip_list(data.get("return_summary"), limit=8),
            "combined_return_summary": _clip_list(data.get("combined_return_summary"), limit=8),
            "diagnostics": _clip_list(data.get("diagnostics"), limit=4),
            "filters": dict(data.get("filters") or {}) if isinstance(data.get("filters"), dict) else {},
            "data_scope": "OM 本地账本",
            "query_scope": coverage.get("query_scope"),
            "coverage": coverage.get("coverage"),
            "calculation_method": data.get("calculation_method"),
        }
        for key in ("row_count", "premium_row_count", "cashflow_row_count", "realized_row_count"):
            if data.get(key) is not None:
                out[key] = data.get(key)
        for key in ("cashflow_rows", "realized_rows", "open_basis_rows", "premium_rows", "enhancement_rows"):
            rows = data.get(key)
            if isinstance(rows, list):
                out[key] = _clip_list(rows, limit=20)
                out[f"{key[:-1]}_count" if key.endswith("s") else f"{key}_count"] = len(rows)
                if len(rows) > 20:
                    out[f"{key}_truncated"] = True
        return out
    if tool_name == "option_positions_read":
        return {
            "summary": _clip_mapping(data.get("summary"), limit=16) if isinstance(data.get("summary"), dict) else data.get("summary"),
            "rows": _strip_internal_identifiers(_clip_list(data.get("rows"), limit=20)),
            "row_count": data.get("row_count"),
            "filters": dict(data.get("filters") or {}) if isinstance(data.get("filters"), dict) else {},
            "quote_refresh": _clip_mapping(data.get("quote_refresh"), limit=16)
            if isinstance(data.get("quote_refresh"), dict)
            else data.get("quote_refresh"),
            "assigned_stock_sale_rows": _strip_internal_identifiers(_clip_list(data.get("assigned_stock_sale_rows"), limit=20)),
            "assigned_stock_review_rows": _strip_internal_identifiers(_clip_list(data.get("assigned_stock_review_rows"), limit=20)),
            "warnings": _clip_list(data.get("warnings"), limit=8),
        }
    return _clip_mapping(data, limit=20)


def _monthly_income_coverage(data: dict[str, Any]) -> dict[str, Any]:
    filters = data.get("filters") if isinstance(data.get("filters"), dict) else {}
    rows: list[dict[str, Any]] = []
    for key in ("summary", "return_summary", "combined_return_summary", "cashflow_rows", "realized_rows", "premium_rows"):
        value = data.get(key)
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, dict))
    months = sorted({str(row.get("month")) for row in rows if str(row.get("month") or "").strip()})
    accounts = sorted({str(row.get("account")) for row in rows if str(row.get("account") or "").strip()})
    warnings = data.get("report_warnings")
    if not isinstance(warnings, list):
        warnings = data.get("warnings") if isinstance(data.get("warnings"), list) else []
    month_filter = filters.get("month")
    account_filter = filters.get("account")
    return {
        "query_scope": {
            "data_source": "OM local ledger",
            "month": str(month_filter) if month_filter else "all_available",
            "account": str(account_filter) if account_filter else "all",
            "broker": filters.get("broker"),
        },
        "coverage": {
            "months": months,
            "month_start": months[0] if months else None,
            "month_end": months[-1] if months else None,
            "accounts": accounts,
            "summary_count": len(data.get("summary") or []) if isinstance(data.get("summary"), list) else 0,
            "return_summary_count": len(data.get("return_summary") or []) if isinstance(data.get("return_summary"), list) else 0,
            "combined_return_summary_count": len(data.get("combined_return_summary") or [])
            if isinstance(data.get("combined_return_summary"), list)
            else 0,
            "warnings": [str(item) for item in warnings if str(item).strip()][:8],
            "complete_for_query_scope": bool(months or accounts) and not warnings,
        },
    }


def _clip_list(value: Any, *, limit: int) -> list[Any]:
    if not isinstance(value, list):
        return []
    out: list[Any] = []
    for item in value[:limit]:
        if isinstance(item, dict):
            out.append(_clip_mapping(item, limit=20))
        elif isinstance(item, (str, int, float, bool)) or item is None:
            out.append(item)
        else:
            out.append({"type": type(item).__name__})
    return out


_INTERNAL_SYNTHESIS_KEYS = frozenset(
    {
        "record_id",
        "lot_id",
        "stock_lot_id",
        "stock_event_id",
        "event_id",
        "source_deal_id",
        "source_option_lot_id",
        "position_key",
    }
)


def _strip_internal_identifiers(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_internal_identifiers(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _strip_internal_identifiers(item)
            for key, item in value.items()
            if str(key) not in _INTERNAL_SYNTHESIS_KEYS
        }
    return value


def _tool_name_for_intent(intent_name: str) -> str | None:
    return {
        "monthly_income_report": "monthly_income_report",
        "position_query": "option_positions_read",
        "assigned_stock_position_query": "option_positions_read",
        "position_exit_analysis": "close_advice_read",
        "runtime_status": "runtime_status",
        "runtime_runs": "runtime_runs",
        "runtime_logs": "runtime_logs",
        "symbol_config_query": "symbol_config_read",
        "analysis_catalog": "analysis_catalog",
        "analysis_query": "analysis_query",
    }.get(str(intent_name or ""))


def _intent_name_for_tool(tool_name: str) -> str | None:
    return {
        "monthly_income_report": "monthly_income_report",
        "option_positions_read": "position_query",
        "close_advice_read": "position_exit_analysis",
        "runtime_status": "runtime_status",
        "runtime_runs": "runtime_runs",
        "runtime_logs": "runtime_logs",
        "symbol_config_read": "symbol_config_query",
        "analysis_catalog": "analysis_catalog",
        "analysis_query": "analysis_query",
    }.get(str(tool_name or ""))


def _clip_mapping(value: dict[str, Any], *, limit: int = 12) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for index, (key, item) in enumerate(value.items()):
        if index >= limit:
            out["..."] = "truncated"
            break
        if isinstance(item, (str, int, float, bool)) or item is None:
            out[str(key)] = item
        elif isinstance(item, list):
            out[str(key)] = {"type": "list", "count": len(item)}
        elif isinstance(item, dict):
            out[str(key)] = {"type": "object", "keys": sorted(str(k) for k in item.keys())[:limit]}
        else:
            out[str(key)] = {"type": type(item).__name__}
    return out


def _no_tool_plan_error() -> AgentToolError:
    return AgentToolError(
        code="NEEDS_CLARIFICATION",
        message="缺少可安全执行的只读工具或必填信息，无法完成这个请求。",
        hint="我不会降级到弱相关查询；请明确要查的对象、字段、账户、run_id，或说明需要新增哪类只读能力。",
        details={"missing_capability": "read_tool_or_required_slots", "weak_downgrade_allowed": False},
    )


__all__ = [
    "AGENT_LOOP_SCHEMA_VERSION",
    "AGENT_LOOP_ALLOWED_TOOLS",
    "AgentLoopResult",
    "AgentLoopStep",
    "AgentLoopPlanFn",
    "AgentLoopSynthesizeFn",
    "AgentLoopTranslateFn",
    "FinalResponsePlan",
    "FOLLOWUP_DECISION_SCHEMA_VERSION",
    "INTERNAL_TOOL_PLAN_NAME",
    "LlmSynthesisResult",
    "LlmPlannerResult",
    "MAX_TOOL_PLAN_STEPS",
    "TOOL_PLAN_SCHEMA_VERSION",
    "TOOL_PLAN_SYNTHESIS_SCHEMA_VERSION",
    "PlannerPlan",
    "PlannerPlanStep",
    "ToolObservation",
    "build_tool_observation",
    "build_synthesis_observation",
    "execute_tool_plan",
    "parse_tool_plan_payload",
    "plan_read_only_tools",
    "run_read_only_agent_loop",
    "synthesize_tool_plan_response",
    "tool_plan_json_schema",
    "validate_tool_plan",
]
