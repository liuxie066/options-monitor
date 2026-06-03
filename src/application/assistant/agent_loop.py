from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response
from src.application.agent_tool_registry import get_tool_definition
from src.application.assistant.contracts import AssistantRequest, PerceptionResult, ToolCall
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
from src.application.assistant.llm_translator import LlmTranslationResult, translate_inbound_intent
from src.application.assistant.renderer import render_inbound_text
from src.application.assistant.settings import AssistantSettings
from src.application.assistant.tool_policy import DEFAULT_TOOL_POLICY
from src.infrastructure.openai_chat_completions import (
    OpenAIChatCompletionsError,
    extract_chat_completion_text,
)
from src.infrastructure.openai_responses import OpenAIResponsesError, extract_response_text

AGENT_LOOP_SCHEMA_VERSION = "om-agent-loop-v1"
TOOL_PLAN_SCHEMA_VERSION = "om-tool-plan-v1"
TOOL_PLAN_SYNTHESIS_SCHEMA_VERSION = "om-tool-plan-synthesis-v1"
INTERNAL_TOOL_PLAN_NAME = "assistant.tool_plan"
MAX_TOOL_PLAN_STEPS = 3
AGENT_LOOP_ALLOWED_TOOLS = frozenset(
    {
        "monthly_income_report",
        "option_positions_read",
        "runtime_status",
        "runtime_runs",
        "runtime_logs",
        "close_advice_read",
    }
)
_BANNED_PLAN_ARGUMENTS = frozenset(
    {
        "audit_db",
        "config_key",
        "config_path",
        "csv_path",
        "data_config",
        "env_file",
        "file",
        "log_file",
        "output_dir",
        "profile_path",
        "report_path",
        "run_dir",
        "runs_root",
    }
)
_CONFIG_SCOPED_PLAN_TOOLS = frozenset(
    {
        "monthly_income_report",
        "option_positions_read",
        "runtime_status",
        "close_advice_read",
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
    schema_version: str = TOOL_PLAN_SCHEMA_VERSION

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "goal": self.goal,
            "response_mode": self.response_mode,
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
    max_steps: int = MAX_TOOL_PLAN_STEPS,
) -> AgentLoopResult:
    """Build a bounded read-only tool plan.

    The plan itself is an internal pseudo-tool routed through the existing
    inbound router. Individual step execution still goes through the read-only
    tool policy at action time.
    """
    steps = max(1, min(int(max_steps), MAX_TOOL_PLAN_STEPS))
    if plan_tools_fn is not None or translate_intent_fn is None:
        plan_result = (plan_tools_fn or plan_read_only_tools)(text, settings, conversation_context)
        translation, planned_steps = _translation_from_tool_plan_result(plan_result)
        trace = dict(translation.trace or plan_result.trace)
    else:
        translator = translate_intent_fn
        translation = translator(text, settings, conversation_context)
        planned_steps = _planned_steps_from_translation(translation)
        trace = dict(translation.trace)
    trace["agent_loop"] = {
        "schema_version": AGENT_LOOP_SCHEMA_VERSION,
        "enabled": True,
        "planner": "llm_tool_plan" if translate_intent_fn is None or plan_tools_fn is not None else "llm_read_only_intent",
        "max_steps": steps,
        "steps_used": len(planned_steps),
        "writes_allowed": False,
        "steps": [step.public_payload() for step in planned_steps],
        "final_response": FinalResponsePlan(
            status="pending_tool_execution" if translation.intent is not None else "no_plan",
            reason="canonical renderer will own factual output"
            if translation.intent is not None
            else "translator did not produce an executable read-only intent",
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
    settings: AssistantSettings,
    conversation_context: dict[str, Any] | None,
    execute_tool_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
    synthesize_response_fn: AgentLoopSynthesizeFn | None = None,
) -> dict[str, Any]:
    plan = parse_tool_plan_payload(plan_payload)
    validate_tool_plan(plan)
    tool_events: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    synthesis_observations: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    ok = True
    error_payload: dict[str, Any] | None = None

    for index, step in enumerate(plan.steps, start=1):
        payload = _inject_system_fields(step.arguments, request=request, tool_name=step.tool_name)
        call = ToolCall(tool_name=step.tool_name, payload=payload)
        try:
            decision = DEFAULT_TOOL_POLICY.authorize_read_tool(call, source="agent_loop")
        except AgentToolError as err:
            ok = False
            error_payload = build_error_payload(err)
            tool_events.append(
                {
                    "phase": "authorize_tool",
                    "tool_name": step.tool_name,
                    "allowed": False,
                    "error_code": err.code,
                }
            )
            break
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
        synthesis_observations.append(
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
            ok = False
            error_payload = dict(error) if isinstance(error, dict) else {"code": "TOOL_FAILED", "message": "tool call failed"}
            break

    synthesis = _build_final_response(
        question=question,
        settings=settings,
        plan=plan,
        synthesis_observations=synthesis_observations,
        conversation_context=conversation_context,
        synthesize_response_fn=synthesize_response_fn,
        ok=ok,
        error_payload=error_payload,
    )
    data = {
        "response_text": synthesis.response_text or "",
        "plan": plan.public_payload(),
        "observations": observations,
        "synthesis_observations": synthesis_observations,
        "tool_events": tool_events,
        "tool_calls_used": len(observations),
        "writes_allowed": False,
        "final_response": _final_response_payload(synthesis),
        "synthesis": dict(synthesis.trace),
        "tool_results": tool_results,
    }
    return build_response(
        tool_name=INTERNAL_TOOL_PLAN_NAME,
        ok=ok,
        data=data,
        error=error_payload if not ok else None,
    )


def _default_translate(
    text: str,
    settings: AssistantSettings,
    conversation_context: dict[str, Any] | None,
) -> LlmTranslationResult:
    return translate_inbound_intent(text, settings=settings.llm, conversation_context=conversation_context)


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
                hint="Set assistant.llm.provider, assistant.llm.model, and assistant.llm.api_key_env, or use assistant.mode=deterministic.",
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
        validate_tool_plan(plan)
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
        schema_version=schema_version,
    )


def validate_tool_plan(plan: PlannerPlan) -> None:
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
    for step in plan.steps:
        if step.tool_name not in AGENT_LOOP_ALLOWED_TOOLS:
            raise AgentToolError(
                code="PERMISSION_DENIED",
                message=f"{step.tool_name} is not allowed in assistant planner",
                hint="Planner MVP only allows bounded pure-read analysis tools.",
                details={"allowed_tools": sorted(AGENT_LOOP_ALLOWED_TOOLS), "tool_name": step.tool_name},
            )
        banned = sorted(key for key in step.arguments if str(key) in _BANNED_PLAN_ARGUMENTS)
        if banned:
            raise AgentToolError(
                code="PERMISSION_DENIED",
                message="tool plan contains system-scoped or path arguments",
                hint="config/path/runtime fields are injected by the system, not by LLM plans.",
                details={"tool_name": step.tool_name, "banned_arguments": banned},
            )
        definition = get_tool_definition(step.tool_name)
        allowed_args = set(definition.input_schema) if definition is not None else set()
        allowed_args.difference_update(_BANNED_PLAN_ARGUMENTS)
        extra = sorted(str(key) for key in step.arguments if allowed_args and str(key) not in allowed_args)
        if extra:
            raise AgentToolError(
                code="INPUT_ERROR",
                message=f"tool plan has unsupported arguments for {step.tool_name}: {', '.join(extra)}",
                details={"tool_name": step.tool_name, "allowed_arguments": sorted(allowed_args), "extra_arguments": extra},
            )


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


def _translation_from_tool_plan_result(result: LlmPlannerResult) -> tuple[LlmTranslationResult, tuple[AgentLoopStep, ...]]:
    if result.error is not None:
        return LlmTranslationResult(intent=None, trace=dict(result.trace), error=result.error), ()
    if result.plan is None:
        return LlmTranslationResult(intent=None, trace=dict(result.trace)), ()
    if not result.plan.steps:
        return LlmTranslationResult(intent=None, trace={**dict(result.trace), "reason": "no_plan"}, error=_no_tool_plan_error()), ()
    try:
        validate_tool_plan(result.plan)
    except AgentToolError as err:
        return LlmTranslationResult(intent=None, trace={**dict(result.trace), "reason": "invalid_plan", "error_code": err.code}, error=err), ()
    planned_steps = tuple(
        AgentLoopStep(
            index=index,
            phase="plan_tool",
            status="planned",
            tool_name=step.tool_name,
            arguments=dict(step.arguments),
            purpose=step.purpose,
        )
        for index, step in enumerate(result.plan.steps, start=1)
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
        "month",
        "include_rows",
        "run_id",
        "kind",
        "limit",
        "lines",
        "query",
        "symbol",
        "option_type",
        "side",
        "strike",
        "expiration",
    }
    return {key: payload[key] for key in sorted(allowed) if key in payload}


def build_synthesis_observation(*, index: int, tool_name: str, payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    error = result.get("error") if isinstance(result, dict) else None
    data = result.get("data") if isinstance(result, dict) else None
    item: dict[str, Any] = {
        "index": int(index),
        "tool_name": str(tool_name or ""),
        "payload": _safe_tool_payload(payload),
        "ok": bool(result.get("ok", False)) if isinstance(result, dict) else False,
        "error": dict(error) if isinstance(error, dict) else None,
    }
    if isinstance(data, dict):
        item["data"] = _synthesis_data(tool_name, data)
    return item


def tool_plan_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_version": {"type": "string", "enum": [TOOL_PLAN_SCHEMA_VERSION]},
            "goal": {"type": "string"},
            "response_mode": {"type": "string", "enum": ["canonical", "synthesis"]},
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
        "required": ["schema_version", "goal", "response_mode", "steps"],
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
    synthesis_observations: list[dict[str, Any]],
    conversation_context: dict[str, Any] | None,
    synthesize_response_fn: AgentLoopSynthesizeFn | None,
    ok: bool,
    error_payload: dict[str, Any] | None,
) -> LlmSynthesisResult:
    if not ok:
        return LlmSynthesisResult(
            response_text=_fallback_response(plan=plan, observations=synthesis_observations, error_payload=error_payload),
            trace={"attempted": False, "reason": "tool_error", "schema_version": TOOL_PLAN_SYNTHESIS_SCHEMA_VERSION},
        )
    if plan.response_mode == "canonical" and len(plan.steps) == 1 and synthesis_observations:
        text = _canonical_response(plan.steps[0], synthesis_observations[0])
        if text:
            return LlmSynthesisResult(
                response_text=text,
                trace={"attempted": False, "reason": "canonical_renderer", "schema_version": TOOL_PLAN_SYNTHESIS_SCHEMA_VERSION},
            )
    synthesizer = synthesize_response_fn or synthesize_tool_plan_response
    synthesis = synthesizer(question, settings, plan, synthesis_observations, conversation_context)
    if synthesis.response_text:
        return synthesis
    return LlmSynthesisResult(
        response_text=_fallback_response(plan=plan, observations=synthesis_observations, error_payload=error_payload),
        trace={
            **dict(synthesis.trace),
            "fallback": "structured_observation_summary",
            "error_code": synthesis.error.code if synthesis.error else None,
        },
        error=synthesis.error,
    )


def _final_response_payload(synthesis: LlmSynthesisResult) -> dict[str, Any]:
    reason = str(synthesis.trace.get("reason") or "")
    if reason == "canonical_renderer":
        return FinalResponsePlan(
            status="rendered",
            reason="canonical renderer produced the factual response",
            canonical_renderer_required=True,
            llm_may_summarize=False,
        ).public_payload()
    if reason == "synthesized":
        return FinalResponsePlan(
            status="synthesized",
            reason="LLM synthesized the response from tool observations",
            canonical_renderer_required=False,
            llm_may_summarize=True,
        ).public_payload()
    return FinalResponsePlan(
        status="fallback",
        reason="structured fallback response used after synthesis was unavailable",
        canonical_renderer_required=False,
        llm_may_summarize=False,
    ).public_payload()


def _canonical_response(step: PlannerPlanStep, observation: dict[str, Any]) -> str:
    if not bool(observation.get("ok", False)):
        return ""
    data = observation.get("data")
    if not isinstance(data, dict):
        return ""
    intent_name = _intent_name_for_tool(step.tool_name)
    if not intent_name:
        return ""
    return render_inbound_text(
        intent=PerceptionResult(intent_name=intent_name, arguments=dict(step.arguments), source="agent_loop_plan"),
        tool_result=build_response(tool_name=step.tool_name, ok=True, data=data),
    )


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


def _inject_system_fields(arguments: dict[str, Any], *, request: AssistantRequest, tool_name: str) -> dict[str, Any]:
    payload = dict(arguments or {})
    if tool_name in _CONFIG_SCOPED_PLAN_TOOLS:
        if request.config_path:
            payload["config_path"] = request.config_path
        elif request.config_key:
            payload["config_key"] = request.config_key
    if tool_name == "option_positions_read":
        payload.setdefault("action", "list")
    if tool_name == "runtime_runs":
        payload.setdefault("limit", 10)
    if tool_name == "runtime_logs":
        payload.setdefault("kind", "all")
        payload.setdefault("lines", 50)
    return payload


def _planner_input_text(text: str, *, conversation_context: dict[str, Any] | None) -> str:
    payload: dict[str, Any] = {
        "message": str(text or ""),
        "tools": _planner_tool_manifest(),
    }
    if isinstance(conversation_context, dict):
        payload["context"] = {
            "window_messages": int(conversation_context.get("window_messages") or 0),
            "semantics": conversation_context.get("semantics") if isinstance(conversation_context.get("semantics"), dict) else {},
            "last_successful_read": conversation_context.get("last_successful_read")
            if isinstance(conversation_context.get("last_successful_read"), dict)
            else None,
        }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _planner_instructions() -> str:
    return """\
You are the options-monitor read-only tool planner.
Return only JSON that matches the requested schema.

Rules:
- Produce 1 to 3 tool calls. If the question needs more, choose the most direct useful plan, or fail by returning no unsupported tool.
- Use only tools in the provided tool manifest.
- Never plan write/admin/notification/config-change actions.
- Do not include system-scoped or path arguments such as config_key, config_path, data_config, output_dir, report_path, run_dir, or audit_db. The system injects those.
- For monthly income summary questions, use monthly_income_report with account/month when available.
- For cashflow detail, net cashflow composition, net inflow source, "明细", "组成", "构成", "来源", or "由什么组成", use monthly_income_report with include_rows=true and response_mode=synthesis.
- Use response_mode=canonical only for direct status/summary/list queries that do not require explanation, comparison, or composition.
- Use response_mode=synthesis for analysis, detail, comparison, why/how, or composition questions.
- If there is no safe read-only plan, or required slots are missing and the tool cannot safely handle them, return steps=[] instead of guessing.
"""


def _planner_tool_manifest() -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for name in sorted(AGENT_LOOP_ALLOWED_TOOLS):
        definition = get_tool_definition(name)
        if definition is None:
            continue
        input_schema = {
            key: value
            for key, value in definition.input_schema.items()
            if key not in _BANNED_PLAN_ARGUMENTS
        }
        notes: list[str] = []
        if name == "monthly_income_report":
            notes.append("Set include_rows=true for cashflow details, composition, source, 明细, 组成, 构成, 来源, or 由什么组成.")
            notes.append("Data comes from OM local ledger, not broker realtime cash statements.")
        tools.append(
            {
                "name": name,
                "description": definition.description,
                "capabilities": list(definition.capabilities),
                "input_schema": input_schema,
                "safe_default_input": dict(definition.safe_default_input),
                "planner_notes": notes,
            }
        )
    return tools


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
        }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


_SYNTHESIS_INSTRUCTIONS = """\
You answer options-monitor chat questions using only provided tool observations.
Return only JSON.

Rules:
- Do not invent facts beyond observations.
- If required data is missing, say it cannot be confirmed and name the missing data.
- Do not downgrade a detail/composition question into a nearby summary.
- Keep Chinese output concise and Markdown-friendly.
- Mention the data scope when relevant, for example OM 本地账本.
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
        }
    return payload


def _synthesis_data(tool_name: str, data: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "monthly_income_report":
        out = {
            "summary": _clip_list(data.get("summary"), limit=8),
            "return_summary": _clip_list(data.get("return_summary"), limit=8),
            "diagnostics": _clip_list(data.get("diagnostics"), limit=4),
            "filters": dict(data.get("filters") or {}) if isinstance(data.get("filters"), dict) else {},
            "calculation_method": data.get("calculation_method"),
            "row_count": data.get("row_count"),
            "premium_row_count": data.get("premium_row_count"),
        }
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
            "rows": _clip_list(data.get("rows"), limit=20),
            "row_count": data.get("row_count"),
        }
    return _clip_mapping(data, limit=20)


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


def _tool_name_for_intent(intent_name: str) -> str | None:
    return {
        "monthly_income_report": "monthly_income_report",
        "position_query": "option_positions_read",
        "position_exit_analysis": "close_advice_read",
        "runtime_status": "runtime_status",
        "runtime_runs": "runtime_runs",
        "runtime_logs": "runtime_logs",
    }.get(str(intent_name or ""))


def _intent_name_for_tool(tool_name: str) -> str | None:
    return {
        "monthly_income_report": "monthly_income_report",
        "option_positions_read": "position_query",
        "close_advice_read": "position_exit_analysis",
        "runtime_status": "runtime_status",
        "runtime_runs": "runtime_runs",
        "runtime_logs": "runtime_logs",
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
        message="没有识别出可安全执行的只读工具计划。",
        hint="请明确要查收益、持仓、运行状态、最近任务、日志或平仓建议。",
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
