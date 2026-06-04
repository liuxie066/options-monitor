from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response
from src.application.agent_tool_registry import get_tool_definition
from src.application.assistant.commands import (
    ACCOUNT_VALUES,
    is_llm_planner_preview_spec,
    llm_planner_preview_specs,
    spec_by_intent,
)
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
from src.application.assistant.parser import extract_month_filter
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
AGENT_LOOP_READ_TOOLS = frozenset(
    {
        "monthly_income_report",
        "option_positions_read",
        "runtime_status",
        "runtime_runs",
        "runtime_logs",
        "close_advice_read",
    }
)
AGENT_LOOP_PREVIEW_CAPABILITIES = frozenset(spec.intent_name for spec in llm_planner_preview_specs())
AGENT_LOOP_ALLOWED_TOOLS = AGENT_LOOP_READ_TOOLS | AGENT_LOOP_PREVIEW_CAPABILITIES
_COMMAND_SPECS_BY_INTENT = spec_by_intent()
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
    settings: AssistantSettings,
    conversation_context: dict[str, Any] | None,
    execute_tool_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
    synthesize_response_fn: AgentLoopSynthesizeFn | None = None,
) -> dict[str, Any]:
    plan = parse_tool_plan_payload(plan_payload)
    plan = _normalize_tool_plan(plan, question=question, today=_planner_today(None))
    validate_tool_plan(plan, allow_preview=False)
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
        schema_version=schema_version,
    )


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
        banned = sorted(key for key in step.arguments if str(key) in _BANNED_PLAN_ARGUMENTS)
        if banned:
            raise AgentToolError(
                code="PERMISSION_DENIED",
                message="tool plan contains system-scoped or path arguments",
                hint="config/path/runtime fields are injected by the system, not by LLM plans.",
                details={"tool_name": step.tool_name, "banned_arguments": banned},
            )
        allowed_args = _allowed_plan_arguments(step.tool_name)
        allowed_args.difference_update(_BANNED_PLAN_ARGUMENTS)
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
        return set(definition.input_schema) if definition is not None else set()
    spec = _COMMAND_SPECS_BY_INTENT.get(tool_name)
    if spec is None or not is_llm_planner_preview_spec(spec):
        return set()
    allowed = set(spec.arguments)
    if tool_name in {"manual_trade_open", "manual_trade_close"}:
        allowed.add("account")
    return allowed


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
        schema_version=plan.schema_version,
    )


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
        guard = _verify_answer_guard(synthesis.response_text, observations=synthesis_observations)
        if not guard["violations"]:
            return LlmSynthesisResult(
                response_text=synthesis.response_text,
                trace={**dict(synthesis.trace), "answer_guard": {"status": "passed"}},
                error=synthesis.error,
            )
        retry_observations = _with_answer_guard_feedback(synthesis_observations, guard)
        retry = synthesizer(question, settings, plan, retry_observations, conversation_context)
        if retry.response_text:
            retry_guard = _verify_answer_guard(retry.response_text, observations=synthesis_observations)
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
    if len(plan.steps) == 1 and synthesis_observations:
        text = _canonical_response(plan.steps[0], synthesis_observations[0])
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
    if reason == "canonical_renderer_fallback":
        return FinalResponsePlan(
            status="rendered",
            reason="canonical renderer used after synthesis was unavailable",
            canonical_renderer_required=True,
            llm_may_summarize=False,
        ).public_payload()
    if reason in {"synthesized", "synthesized_after_answer_guard"}:
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


def _verify_answer_guard(response_text: str, *, observations: list[dict[str, Any]]) -> dict[str, Any]:
    text = str(response_text or "")
    compact = re.sub(r"\s+", "", text.lower())
    facts = _answer_guard_facts(observations)
    violations: list[dict[str, Any]] = []
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
    return {"facts": facts, "violations": violations}


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
                    "Do not claim missing months/accounts unless coverage or diagnostics explicitly says so."
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
    for item in observations:
        if not bool(item.get("ok", False)):
            all_tools_ok = False
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        if not isinstance(data, dict):
            continue
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
    return {
        "months": sorted(months),
        "accounts": sorted(accounts),
        "all_tools_ok": all_tools_ok,
        "complete_for_query_scope": complete_for_query_scope,
        "cashflow_row_count": cashflow_row_count,
    }


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
            "temporal_context": conversation_context.get("temporal_context")
            if isinstance(conversation_context.get("temporal_context"), dict)
            else {},
            "semantics": conversation_context.get("semantics") if isinstance(conversation_context.get("semantics"), dict) else {},
            "last_successful_read": conversation_context.get("last_successful_read")
            if isinstance(conversation_context.get("last_successful_read"), dict)
            else None,
        }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _planner_instructions() -> str:
    return """\
You are the options-monitor assistant capability planner.
Return only JSON that matches the requested schema.

Rules:
- Produce 1 to 3 read-only tool calls, or exactly 1 preview-write capability call.
- Use only tools/capabilities in the provided manifest.
- Preview-write capabilities only create a pending preview. They never apply writes, confirm pending operations, notify users externally, or mutate config/ledger directly.
- Never plan confirm/cancel/apply actions. Confirm/cancel must be handled by deterministic user commands bound to a pending operation.
- Do not include system-scoped or path arguments such as config_key, config_path, data_config, output_dir, report_path, run_dir, or audit_db. The system injects those.
- Resolve relative dates using context.temporal_context.current_date in Asia/Shanghai. For a month without a year such as "6月", use the current_date year.
- For monthly income summary questions, use monthly_income_report with account/month when available.
- For cashflow detail, net cashflow composition, net inflow source, "明细", "组成", "构成", "来源", or "由什么组成", use monthly_income_report with include_rows=true.
- For all-history, cumulative, or total net cashflow questions, omit month so monthly_income_report reads all OM local ledger months.
- For multiple explicit months, either call monthly_income_report once per month with matching arguments, or omit month and synthesize from all available rows; never duplicate one month while claiming another.
- For "记录开仓", "记录平仓", Futu 成交提醒, 成功卖出/买入 option fills, use manual_trade_open or manual_trade_close with raw_text set to the original user message.
- For monitored-symbol setting requests such as covered call min strike, use symbol_edit.
- For model switch requests, use model_use. For immediate software upgrade requests, use upgrade_now.
- response_mode is a top-level plan field only. Never include response_mode inside any step.arguments.
- Use response_mode=canonical only for direct status/summary/list queries that do not require explanation, comparison, or composition.
- Use response_mode=synthesis for analysis, detail, comparison, why/how, or composition questions.
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
            if key not in _BANNED_PLAN_ARGUMENTS
        }
        notes: list[str] = []
        semantics: dict[str, Any] = {}
        if name == "monthly_income_report":
            notes.append("Set include_rows=true for cashflow details, composition, source, 明细, 组成, 构成, 来源, or 由什么组成.")
            notes.append("Data comes from OM local ledger, not broker realtime cash statements.")
            notes.append("If month is omitted, the tool reads all months currently available in the OM local ledger.")
            notes.append("If account is omitted, the tool reads all ledger accounts available for the selected broker/config.")
            semantics = {
                "data_source": "OM local ledger",
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
    for spec in llm_planner_preview_specs():
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
- For monthly_income_report, "历史以来", "累计", and "总净现金流" mean the OM local ledger coverage returned by the tool.
- Do not claim missing months/accounts when observation.coverage includes them or complete_for_query_scope=true.
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
        coverage = _monthly_income_coverage(data)
        out = {
            "summary": _clip_list(data.get("summary"), limit=8),
            "return_summary": _clip_list(data.get("return_summary"), limit=8),
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
            "rows": _clip_list(data.get("rows"), limit=20),
            "row_count": data.get("row_count"),
        }
    return _clip_mapping(data, limit=20)


def _monthly_income_coverage(data: dict[str, Any]) -> dict[str, Any]:
    filters = data.get("filters") if isinstance(data.get("filters"), dict) else {}
    rows: list[dict[str, Any]] = []
    for key in ("summary", "return_summary", "cashflow_rows", "realized_rows", "premium_rows"):
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
        message="没有识别出可安全执行的能力计划。",
        hint="请明确要查收益、持仓、运行状态、最近任务、日志、平仓建议，或要创建哪类写入预览。",
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
