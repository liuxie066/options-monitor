from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from domain.domain.symbol_identity import symbol_market
from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response
from src.application.agent_tool_registry import get_tool_definition, pure_read_tool_names
from src.application.agent_tools.analysis import VIEW_SPECS as ANALYSIS_VIEW_SPECS
from src.application.assistant.capability_catalog import (
    ACCOUNT_VALUES,
    is_llm_planner_preview_spec,
    planner_preview_specs,
    planner_read_specs,
    spec_by_intent,
)
from src.application.assistant.action_safety import assess_action_safety
from src.application.assistant.answer_guard import (
    answer_guard_trace_payload as _answer_guard_trace_payload,
    verify_answer_guard as _verify_answer_guard,
    with_answer_guard_feedback as _with_answer_guard_feedback,
)
from src.application.assistant.action_policy import decide_tool_action_policy
from src.application.assistant.contracts import AssistantRequest, PerceptionResult, ToolCall
from src.application.assistant.context_projection import SAFE_SLOT_KEYS, build_context_projection, context_projection_trace
from src.application.assistant.context_validation import context_validation_trace, validate_context_use
from src.application.assistant.coverage_verifier import CoverageResult, verify_coverage
from src.application.assistant.evidence import build_evidence_bundle
from src.application.assistant.llm_common import (
    CreateStructuredResponseFn,
    CreateToolCallPayloadResponseFn,
    CreateToolCallResponseFn,
    is_supported_llm_provider,
    llm_api_key_value,
    chat_completions_payload_options,
    missing_llm_config,
    normalize_llm_provider,
    provider_api_kind,
    provider_create_tool_call_payload_response_fn,
    provider_create_response_fn,
    provider_create_tool_call_response_fn,
    strip_json_code_fence,
    unsupported_llm_provider_error,
)
from src.application.assistant.model_events import (
    AssistantEvent,
    MODEL_EVENT_SCHEMA_VERSION,
    ModelFinalAnswerEvent,
    ModelToolCallEvent,
    ToolGuardDecisionEvent,
    ToolResultAdapterOutput,
    ToolResultEvent,
    adapt_tool_result,
    chat_completions_tools_payload,
    event_transcript_payload,
    model_events_from_provider_response,
    openai_responses_tools_payload,
)
from src.application.assistant.model_continuation import CreateModelContinuationResponseFn, continue_model_after_tool_results
from src.application.assistant.model_evidence import (
    ModelAnswerVerification,
    ModelEvidenceBundle,
    build_model_evidence_bundle,
    canonical_fallback_from_tool_results,
    verify_model_final_answer,
)
from src.application.assistant.renderer import render_canonical_tool_result, render_inbound_text
from src.application.assistant.session import build_agent_session_snapshot
from src.application.assistant.settings import AssistantLlmSettings, AssistantSettings
from src.application.assistant.task_contract import TASK_CONTRACT_SCHEMA_VERSION, build_task_contract, preview_authority_from_text
from src.application.assistant.tool_bindings import (
    planner_binding_for_tool,
    planner_config_scoped_tool_names,
    primary_intent_name_for_tool,
    symbol_market_config_tool_names,
    tool_name_for_intent,
)
from src.application.assistant.tool_contracts import resolve_output_contract
from src.application.assistant.verifier_hooks import (
    hook_results_from_answer_trace,
    hook_results_from_coverage,
    hook_results_from_tool_check,
)
from src.application.assistant.time_filters import extract_month_filter
from src.application.assistant.tool_policy import DEFAULT_TOOL_POLICY
from src.application.assistant.user_profile import user_profile_trace
from src.application.tool_input_schema import build_tool_input_json_schema, validate_tool_input_payload
from src.infrastructure.openai_chat_completions import (
    OpenAIChatCompletionsError,
    extract_chat_completion_text,
)
from src.infrastructure.openai_responses import OpenAIResponsesError, extract_response_text

AGENT_LOOP_SCHEMA_VERSION = "om-agent-loop-v1"
TOOL_PLAN_SCHEMA_VERSION = "om-tool-plan-v2"
PLANNER_CONTEXT_USE_SCHEMA_VERSION = "om-planner-context-use-v1"
FOLLOWUP_DECISION_SCHEMA_VERSION = "om-agent-loop-followup-decision-v1"
TOOL_CHECK_SCHEMA_VERSION = "om-agent-tool-check-v1"
INTERNAL_TOOL_PLAN_NAME = "assistant.tool_plan"
INTERNAL_TOOL_LOOP_NAME = "assistant.tool_loop"
MAX_TOOL_PLAN_STEPS = 5
MAX_AGENT_LOOP_ITERATIONS = 3
MAX_AGENT_LOOP_TOOL_CALLS = 10
MAX_PLANNER_ANALYSIS_VIEWS = 12
_CURRENT_SCOPE_OPTIONAL_FILTER_SLOTS = frozenset({"function", "strategy"})
PLANNER_CONTEXT_USE_MODES = ("none", "carry", "refine", "override", "frame_delta", "ambiguous")
_DEFAULT_PLANNER_ANALYSIS_VIEWS: tuple[str, ...] = (
    "account_monthly_performance",
    "account_monthly_income_components",
    "assigned_stock_position_pnl",
    "open_option_exposure",
    "candidate_filter_diagnostics",
    "runtime_tick_status",
    "strategy_config_by_symbol_account",
    "quote_freshness",
)
_ANALYSIS_VIEW_GROUPS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "income",
        (
            "收益",
            "收入",
            "现金流",
            "净现金流",
            "权利金",
            "来源",
            "组成",
            "构成",
            "主要来自",
            "premium",
            "income",
            "return",
            "pnl",
            "cashflow",
            "source",
            "breakdown",
            "driver",
        ),
        (
            "account_monthly_performance",
            "account_monthly_income_components",
            "monthly_income_summary",
            "monthly_income_return_summary",
            "monthly_income_combined_return_summary",
            "monthly_income_cashflow_rows",
            "monthly_income_realized_rows",
            "monthly_income_premium_rows",
            "symbol_income_attribution",
        ),
    ),
    (
        "assigned_stock",
        ("指派正股", "被指派", "正股", "浮盈亏", "卖股", "assigned", "stock pnl", "stock"),
        (
            "assigned_stock_position_pnl",
            "assigned_stock_sale_events",
            "assigned_stock_lifecycle",
            "assigned_stock_sales",
            "assigned_stock_review",
        ),
    ),
    (
        "position",
        ("持仓", "仓位", "敞口", "到期", "dte", "expiry", "expiration", "exposure", "position"),
        (
            "open_option_exposure",
            "expiration_risk_buckets",
            "position_lots",
            "trade_events",
            "assigned_stock_position_pnl",
        ),
    ),
    (
        "candidate_strategy",
        (
            "候选",
            "过滤",
            "参数",
            "策略",
            "复盘",
            "止盈",
            "平仓",
            "replay",
            "candidate",
            "filter",
            "strategy",
            "close",
        ),
        (
            "candidate_filter_diagnostics",
            "close_advice_snapshot",
            "strategy_config_by_symbol_account",
            "strategy_replay_read_surface",
            "symbol_strategy_config",
            "open_option_exposure",
        ),
    ),
    (
        "runtime",
        (
            "运行",
            "通知",
            "推送",
            "扫描",
            "调度",
            "报价",
            "新鲜",
            "升级",
            "回执",
            "runtime",
            "notification",
            "scheduler",
            "quote",
            "upgrade",
        ),
        (
            "runtime_tick_status",
            "quote_freshness",
            "upgrade_operation_status",
            "candidate_filter_diagnostics",
        ),
    ),
    (
        "config",
        ("配置", "监控标的", "symbol config", "config", "threshold", "min_strike", "max_strike"),
        (
            "strategy_config_by_symbol_account",
            "symbol_strategy_config",
            "candidate_filter_diagnostics",
        ),
    ),
)
_SYMBOL_EDIT_SET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Flat map of supported monitored-symbol setting dot paths to scalar values. "
        "Use sell_put.max_strike, sell_put.enabled, sell_call.min_strike, "
        "sell_call.enabled, covered_call.min_strike, or covered_call.enabled. "
        "Do not nest strategy objects inside set."
    ),
    "additionalProperties": False,
    "minProperties": 1,
    "properties": {
        "sell_put.enabled": {"type": "boolean"},
        "sell_put.max_strike": {"type": "number"},
        "sell_call.enabled": {"type": "boolean"},
        "sell_call.min_strike": {"type": "number"},
        "covered_call.enabled": {"type": "boolean"},
        "covered_call.min_strike": {"type": "number"},
    },
}
_PLANNER_CONTEXT_TOOL_HINTS: dict[str, tuple[str, ...]] = {
    "monthly_income_report": ("income", "收益", "现金流", "权利金", "return", "cashflow"),
    "candidate_filter_explain": ("candidate", "候选", "过滤", "filter", "diagnostic"),
    "candidate_rank_explain": ("candidate", "候选", "排名", "rank"),
    "option_positions_read": ("position", "持仓", "仓位", "敞口", "assigned", "stock"),
    "close_advice_read": ("strategy", "止盈", "平仓", "close", "advice"),
    "symbol_config_read": ("config", "配置", "监控标的", "symbol config"),
    "runtime_status": ("runtime", "运行", "扫描", "通知", "推送", "status"),
    "scheduler_status": ("runtime", "调度", "scheduler"),
    "healthcheck": ("runtime", "健康", "healthcheck"),
    "analysis_query": ("analysis", "分析", "query"),
    "analysis_catalog": ("analysis", "catalog", "views"),
    "operation_timeline": ("operation", "升级", "回执", "audit", "operation"),
}
_BLOCKED_FOLLOWUP_RECOVERABLE_BY = frozenset(
    {
        "apply",
        "confirm",
        "cancel",
        "service",
        "notification",
        "broker",
        "release_workflow",
        "release_workflow_status",
        "opend_service_repair",
        "opend_repair",
        "service_repair",
    }
)
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
        "response_mode",
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
_CONFIG_SCOPED_PLAN_TOOLS = planner_config_scoped_tool_names()
_SYMBOL_MARKET_CONFIG_PLAN_TOOLS = symbol_market_config_tool_names()


@dataclass(frozen=True)
class AgentLoopPlanningOutcome:
    perception: PerceptionResult | None
    trace: dict[str, Any]
    error: AgentToolError | None = None


@dataclass(frozen=True)
class AgentLoopResult:
    planning: AgentLoopPlanningOutcome
    trace: dict[str, Any]
    steps: tuple["AgentLoopStep", ...] = ()
    tool_loop_result: dict[str, Any] | None = None


@dataclass(frozen=True)
class AgentLoopStep:
    index: int
    phase: str
    status: str
    intent_name: str | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] | None = None
    purpose: str | None = None
    action_policy: dict[str, Any] | None = None
    action_safety: dict[str, Any] | None = None
    precheck: dict[str, Any] | None = None
    hook_results: tuple[dict[str, Any], ...] = ()
    preview_receipt: dict[str, Any] | None = None

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
        if self.action_policy is not None:
            payload["action_policy"] = dict(self.action_policy)
        if self.action_safety is not None:
            payload["action_safety"] = dict(self.action_safety)
        if self.precheck is not None:
            payload["precheck"] = dict(self.precheck)
        if self.hook_results:
            payload["hook_results"] = [dict(item) for item in self.hook_results]
        if self.preview_receipt is not None:
            payload["preview_receipt"] = dict(self.preview_receipt)
        return payload


@dataclass(frozen=True)
class ModelTurnResult:
    trace: dict[str, Any]
    error: AgentToolError | None = None
    event_plan: "EventNativePlanningResult | None" = None


@dataclass(frozen=True)
class EventNativePlanningResult:
    events: tuple[ModelToolCallEvent | ModelFinalAnswerEvent | AssistantEvent, ...]
    task_contract: dict[str, Any]
    required_capabilities: tuple[str, ...] = ()
    context_use: dict[str, Any] | None = None
    context_validation: dict[str, Any] | None = None
    provider: str = ""
    goal: str = ""
    schema_version: str = "om-event-native-planning-v1"

    def plan_like_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "goal": self.goal,
            "required_capabilities": list(self.required_capabilities),
            "context_use": _safe_context_use_payload(self.context_use),
            "steps": [
                _model_tool_call_plan_step_payload(event)
                for event in self.events
                if isinstance(event, ModelToolCallEvent)
            ],
        }
        if isinstance(self.task_contract, dict) and self.task_contract:
            payload["task_contract"] = _safe_task_contract_payload(self.task_contract)
        return payload

    def public_payload(self) -> dict[str, Any]:
        payload = self.plan_like_payload()
        payload["provider"] = self.provider
        payload["events"] = event_transcript_payload(self.events)
        if isinstance(self.context_validation, dict) and self.context_validation:
            payload["context_validation"] = dict(self.context_validation)
        return payload


@dataclass(frozen=True)
class _ModelEventProviderAttempt:
    response: dict[str, Any] | None
    planner_input: dict[str, Any]
    events: tuple[ModelToolCallEvent | ModelFinalAnswerEvent | AssistantEvent, ...] = ()
    reason: str = ""
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
class ToolExecutionOutcome:
    authorization_event: dict[str, Any]
    result_event: dict[str, Any] | None
    result_payload: dict[str, Any] | None
    tool_result: dict[str, Any] | None
    observation: dict[str, Any] | None
    fact_observation: dict[str, Any] | None
    synthesis_observation: dict[str, Any] | None
    precheck: dict[str, Any]
    postcheck: dict[str, Any] | None
    allowed: bool
    ok: bool
    error: AgentToolError | None
    error_payload: dict[str, Any] | None


@dataclass(frozen=True)
class GuardedModelToolCallExecution:
    model_event: ModelToolCallEvent
    guard_event: ToolGuardDecisionEvent
    result_adapter: ToolResultAdapterOutput
    allowed: bool
    ok: bool
    authorization_event: dict[str, Any]
    legacy_result_event: dict[str, Any] | None = None
    tool_result: dict[str, Any] | None = None
    observation: dict[str, Any] | None = None
    fact_observation: dict[str, Any] | None = None
    synthesis_observation: dict[str, Any] | None = None
    error_payload: dict[str, Any] | None = None
    preview_gate: dict[str, Any] | None = None
    schema_version: str = "om-guarded-model-tool-call-execution-v1"

    @property
    def result_event(self) -> Any:
        return self.result_adapter.event

    def public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "allowed": bool(self.allowed),
            "ok": bool(self.ok),
            "events": event_transcript_payload([self.model_event, self.guard_event, self.result_adapter.event]),
            "provider_tool_result": self.result_adapter.event.provider_tool_result_payload(),
            "authorization_event": dict(self.authorization_event),
        }
        if self.legacy_result_event is not None:
            payload["legacy_result_event"] = dict(self.legacy_result_event)
        if self.preview_gate is not None:
            payload["preview_gate"] = dict(self.preview_gate)
        if self.error_payload is not None:
            payload["error"] = dict(self.error_payload)
        return payload


@dataclass(frozen=True)
class AssistantToolLoopOutcome:
    status: str
    events: tuple[Any, ...]
    tool_results: tuple[ToolResultAdapterOutput, ...] = ()
    evidence: ModelEvidenceBundle | None = None
    final_answer: str | None = None
    final_answer_event: ModelFinalAnswerEvent | None = None
    answer_verification: ModelAnswerVerification | None = None
    clarification_request: dict[str, Any] | None = None
    preview_gate: dict[str, Any] | None = None
    stop_reason: str = ""
    trace: dict[str, Any] | None = None
    schema_version: str = "om-assistant-tool-loop-outcome-v1"

    @property
    def evidence_bundle(self) -> Any | None:
        return self.evidence.evidence_bundle if self.evidence is not None else None

    def public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "events": event_transcript_payload(self.events),
            "tool_result_count": len(self.tool_results),
            "trace": dict(self.trace or {}),
        }
        if self.evidence is not None:
            payload["evidence"] = self.evidence.public_payload()
        if self.final_answer is not None:
            payload["final_answer"] = self.final_answer
        if self.answer_verification is not None:
            payload["answer_verification"] = self.answer_verification.public_payload()
        if self.clarification_request is not None:
            payload["clarification_request"] = dict(self.clarification_request)
        if self.preview_gate is not None:
            payload["preview_gate"] = dict(self.preview_gate)
        return payload


class ToolExecutor:
    def __init__(
        self,
        *,
        execute_tool_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
        source: str = "agent_loop",
    ) -> None:
        self._execute_tool_fn = execute_tool_fn
        self._source = source

    def execute_read_tool(
        self,
        *,
        request: AssistantRequest,
        task_contract: dict[str, Any] | None,
        index: int,
        tool_name: str,
        payload: dict[str, Any],
        plan_arguments: dict[str, Any] | None = None,
    ) -> ToolExecutionOutcome:
        call = ToolCall(tool_name=tool_name, payload=dict(payload or {}))
        action_policy = decide_tool_action_policy(
            call=call,
            request=request,
            task_contract=task_contract,
            source=self._source,
            tool_policy=DEFAULT_TOOL_POLICY,
        )
        action_policy_payload = action_policy.public_payload()
        risk_class = _risk_class_from_action_policy(action_policy_payload)
        authorization_event = {
            "phase": "authorize_tool",
            "tool_name": tool_name,
            "allowed": bool(action_policy.allowed),
            "decision": action_policy_payload,
            "action_policy": action_policy_payload,
            "risk_class": risk_class,
        }
        action_safety = assess_action_safety(
            question=request.text,
            task_contract=task_contract,
            tool_name=tool_name,
            payload=payload,
            action_policy=action_policy_payload,
            source=self._source,
        )
        action_safety_payload = action_safety.public_payload()
        authorization_event["action_safety"] = action_safety_payload
        precheck = _pre_tool_check(
            tool_name=tool_name,
            payload=payload,
            plan_arguments=plan_arguments,
            task_contract=task_contract,
            action_policy=action_policy_payload,
            action_safety=action_safety_payload,
        )
        authorization_event["precheck"] = precheck
        authorization_event["hook_results"] = hook_results_from_tool_check(precheck)
        if not action_policy.allowed:
            error = action_policy.error or AgentToolError(
                code="PERMISSION_DENIED",
                message=action_policy.denied_reason or f"{tool_name} is not allowed through action policy",
            )
            authorization_event["allowed"] = False
            authorization_event["error_code"] = error.code
            return ToolExecutionOutcome(
                authorization_event=authorization_event,
                result_event=None,
                result_payload=None,
                tool_result=None,
                observation=None,
                fact_observation=None,
                synthesis_observation=None,
                precheck=precheck,
                postcheck=None,
                allowed=False,
                ok=False,
                error=error,
                error_payload=build_error_payload(error),
            )
        if action_policy_payload.get("allowed_effect") != "read":
            error = AgentToolError(
                code="PERMISSION_DENIED",
                message=f"{tool_name} is not allowed inside the automatic read tool loop",
                details={"tool_name": tool_name, "risk_class": risk_class, "action_policy": action_policy_payload},
            )
            authorization_event["allowed"] = False
            authorization_event["error_code"] = error.code
            return ToolExecutionOutcome(
                authorization_event=authorization_event,
                result_event=None,
                result_payload=None,
                tool_result=None,
                observation=None,
                fact_observation=None,
                synthesis_observation=None,
                precheck=precheck,
                postcheck=None,
                allowed=False,
                ok=False,
                error=error,
                error_payload=build_error_payload(error),
            )
        if precheck.get("status") == "fail":
            error = AgentToolError(
                code="PRE_TOOL_CHECK_FAILED",
                message="该请求未通过执行前安全检查。",
                details={"tool_name": tool_name, "precheck": precheck},
            )
            authorization_event["allowed"] = False
            authorization_event["error_code"] = error.code
            return ToolExecutionOutcome(
                authorization_event=authorization_event,
                result_event=None,
                result_payload=None,
                tool_result=None,
                observation=None,
                fact_observation=None,
                synthesis_observation=None,
                precheck=precheck,
                postcheck=None,
                allowed=False,
                ok=False,
                error=error,
                error_payload=build_error_payload(error),
            )

        result = self._execute_tool_fn(tool_name, dict(payload or {}))
        error_payload = result.get("error") if isinstance(result, dict) else None
        error_code = error_payload.get("code") if isinstance(error_payload, dict) else None
        step_ok = bool(result.get("ok", False)) if isinstance(result, dict) else False
        postcheck = _post_tool_check(tool_name=tool_name, payload=payload, result=result)
        evidence_summary = _tool_evidence_summary(tool_name=tool_name, payload=payload, result=result)
        postcheck_hooks = hook_results_from_tool_check(postcheck)
        tool_error = None
        if not step_ok:
            tool_error = AgentToolError(
                code=str(error_code or "TOOL_FAILED"),
                message=str(error_payload.get("message") if isinstance(error_payload, dict) else "tool call failed"),
                details=dict(error_payload.get("details") or {})
                if isinstance(error_payload, dict) and isinstance(error_payload.get("details"), dict)
                else {},
            )
        return ToolExecutionOutcome(
            authorization_event=authorization_event,
            result_event={
                "phase": "observe_tool_result",
                "tool_name": tool_name,
                "ok": step_ok,
                "error_code": str(error_code) if error_code else None,
                "postcheck": postcheck,
                "hook_results": postcheck_hooks,
                "evidence_summary": evidence_summary,
            },
            result_payload=result,
            tool_result={
                "index": index,
                "tool_name": tool_name,
                "ok": step_ok,
                "error": error_payload if isinstance(error_payload, dict) else None,
            },
            observation=build_tool_observation(index=index, tool_name=tool_name, payload=payload, result=result).public_payload(),
            fact_observation=build_fact_observation(index=index, tool_name=tool_name, payload=payload, result=result),
            synthesis_observation=build_synthesis_observation(index=index, tool_name=tool_name, payload=payload, result=result),
            precheck=precheck,
            postcheck=postcheck,
            allowed=True,
            ok=step_ok,
            error=tool_error,
            error_payload=(
                dict(error_payload)
                if isinstance(error_payload, dict)
                else {"code": "TOOL_FAILED", "message": "tool call failed"}
            )
            if not step_ok
            else None,
        )


def execute_model_tool_call_event(
    *,
    model_event: ModelToolCallEvent,
    request: AssistantRequest,
    task_contract: dict[str, Any],
    execute_tool_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
    context_validation: dict[str, Any] | None = None,
    attempted_signatures: set[str] | None = None,
    tool_call_count: int = 0,
    max_tool_calls: int = MAX_AGENT_LOOP_TOOL_CALLS,
    source: str = "agent_loop",
) -> GuardedModelToolCallExecution:
    signatures = attempted_signatures if attempted_signatures is not None else set()
    payload = _inject_system_fields(model_event.arguments, request=request, tool_name=model_event.tool_name)
    signature = _tool_loop_duplicate_signature(model_event.tool_name, payload)
    protocol_error = _model_tool_call_protocol_error(model_event)
    if protocol_error is not None:
        kind = _plan_step_kind(model_event.tool_name)
        guard = {
            "schema_version": TOOL_CHECK_SCHEMA_VERSION,
            "allowed": False,
            "decision": "provider_protocol_error",
            "reason": "model tool call arguments failed provider protocol parsing",
            "tool_name": model_event.tool_name,
            "risk_class": "READ_AUTO" if kind == "read" else ("SOFT_WRITE_PREVIEW" if kind == "preview" else "UNKNOWN"),
            "duplicate_signature": signature,
            "scope_source": _scope_source_for_guard(task_contract),
            "error_code": "INVALID_MODEL_EVENT",
        }
        return _guard_denied_model_tool_call_execution(
            model_event=model_event,
            payload=payload,
            guard=guard,
            error=AgentToolError(
                code=str(protocol_error.get("code") or "INVALID_MODEL_EVENT"),
                message="model tool call arguments were malformed; retry with valid structured arguments",
                details={
                    "tool_name": model_event.tool_name,
                    "provider_protocol_error": dict(protocol_error),
                },
            ),
        )
    if _plan_step_kind(model_event.tool_name) == "preview":
        return _preview_gate_model_tool_call_execution(
            model_event=model_event,
            request=request,
            task_contract=task_contract,
            context_validation=context_validation,
            payload=payload,
            duplicate_signature=signature,
        )
    if _plan_step_kind(model_event.tool_name) == "read" and _question_requests_preview_operation(request.text):
        guard = {
            "schema_version": TOOL_CHECK_SCHEMA_VERSION,
            "allowed": False,
            "decision": "read_for_preview_request",
            "reason": "write preview request cannot be handled by a read tool",
            "tool_name": model_event.tool_name,
            "risk_class": "READ_AUTO",
            "duplicate_signature": signature,
            "scope_source": _scope_source_for_guard(task_contract),
            "error_code": "PLAN_RISK_MISMATCH",
        }
        return _guard_denied_model_tool_call_execution(
            model_event=model_event,
            payload=payload,
            guard=guard,
            error=AgentToolError(
                code="PLAN_RISK_MISMATCH",
                message="这像写入预览请求，不能用只读查询替代。",
                hint="请进入对应的预览确认流程。",
                details={"planned_tool": model_event.tool_name},
            ),
        )
    if int(tool_call_count) >= int(max_tool_calls):
        guard = _tool_budget_guard_payload(
            model_event=model_event,
            payload=payload,
            task_contract=task_contract,
            max_tool_calls=max_tool_calls,
            tool_call_count=tool_call_count,
            duplicate_signature=signature,
        )
        return _guard_denied_model_tool_call_execution(
            model_event=model_event,
            payload=payload,
            guard=guard,
            error=AgentToolError(
                code="TOOL_BUDGET_EXHAUSTED",
                message=f"Agent loop 工具调用预算已用完（最多 {int(max_tool_calls)} 次），未执行该工具调用。",
                details={
                    "tool_name": model_event.tool_name,
                    "max_tool_calls": int(max_tool_calls),
                    "calls_used": int(tool_call_count),
                    "duplicate_signature": signature,
                },
            ),
        )

    guard = _read_tool_loop_guard(
        tool_name=model_event.tool_name,
        payload=payload,
        task_contract=task_contract,
        attempted_signatures=signatures,
    )
    if not guard["allowed"]:
        return _guard_denied_model_tool_call_execution(
            model_event=model_event,
            payload=payload,
            guard=guard,
            error=AgentToolError(
                code=str(guard.get("error_code") or "TOOL_LOOP_GUARD_REJECTED"),
                message=str(guard.get("reason") or "tool loop guard rejected the model tool call"),
                details={
                    "tool_name": model_event.tool_name,
                    "decision": guard.get("decision"),
                    "risk_class": guard.get("risk_class"),
                    "duplicate_signature": guard.get("duplicate_signature"),
                },
            ),
        )

    outcome = ToolExecutor(execute_tool_fn=execute_tool_fn, source=source).execute_read_tool(
        request=request,
        task_contract=task_contract,
        index=int(tool_call_count) + 1,
        tool_name=model_event.tool_name,
        payload=payload,
        plan_arguments=model_event.arguments,
    )
    outcome.authorization_event["tool_loop_guard"] = dict(guard)
    guard_event = _tool_guard_event_from_guard(
        model_event=model_event,
        guard=_guard_payload_after_authorization(guard=guard, outcome=outcome),
        normalized_payload=_safe_tool_payload(payload),
    )
    raw_result = (
        outcome.result_payload
        if isinstance(outcome.result_payload, dict)
        else _guard_error_tool_result(
            tool_name=model_event.tool_name,
            error_payload=outcome.error_payload
            or build_error_payload(
                outcome.error
                or AgentToolError(code="TOOL_NOT_EXECUTED", message="tool call was not executed")
            ),
            guard=guard,
        )
    )
    result_adapter = adapt_tool_result(
        event_id=f"result_{model_event.tool_call_id}",
        parent_event_id=guard_event.event_id,
        tool_call_id=model_event.tool_call_id,
        tool_name=model_event.tool_name,
        normalized_payload=_safe_tool_payload(payload),
        guard_decision=guard_event,
        output_contract=resolve_output_contract(model_event.tool_name, payload),
        raw_result=raw_result,
    )
    if outcome.ok and guard.get("duplicate_signature"):
        signatures.add(str(guard["duplicate_signature"]))
    return GuardedModelToolCallExecution(
        model_event=model_event,
        guard_event=guard_event,
        result_adapter=result_adapter,
        allowed=bool(outcome.allowed),
        ok=bool(outcome.ok),
        authorization_event=outcome.authorization_event,
        legacy_result_event=outcome.result_event,
        tool_result=outcome.tool_result,
        observation=outcome.observation,
        fact_observation=outcome.fact_observation,
        synthesis_observation=outcome.synthesis_observation,
        error_payload=outcome.error_payload,
    )


def _guard_denied_model_tool_call_execution(
    *,
    model_event: ModelToolCallEvent,
    payload: dict[str, Any],
    guard: dict[str, Any],
    error: AgentToolError,
) -> GuardedModelToolCallExecution:
    guard_event = _tool_guard_event_from_guard(
        model_event=model_event,
        guard=guard,
        normalized_payload=_safe_tool_payload(payload),
    )
    error_payload = build_error_payload(error)
    result_adapter = adapt_tool_result(
        event_id=f"result_{model_event.tool_call_id}",
        parent_event_id=guard_event.event_id,
        tool_call_id=model_event.tool_call_id,
        tool_name=model_event.tool_name,
        normalized_payload=_safe_tool_payload(payload),
        guard_decision=guard_event,
        output_contract=resolve_output_contract(model_event.tool_name, payload),
        raw_result=_guard_error_tool_result(tool_name=model_event.tool_name, error_payload=error_payload, guard=guard),
    )
    return GuardedModelToolCallExecution(
        model_event=model_event,
        guard_event=guard_event,
        result_adapter=result_adapter,
        allowed=False,
        ok=False,
        authorization_event={"phase": "tool_loop_guard", **dict(guard)},
        error_payload=error_payload,
    )


def _preview_gate_model_tool_call_execution(
    *,
    model_event: ModelToolCallEvent,
    request: AssistantRequest,
    task_contract: dict[str, Any],
    context_validation: dict[str, Any] | None,
    payload: dict[str, Any],
    duplicate_signature: str,
) -> GuardedModelToolCallExecution:
    error = _preview_gate_error(
        model_event=model_event,
        request=request,
        task_contract=task_contract,
        context_validation=context_validation,
    )
    guard = {
        "schema_version": TOOL_CHECK_SCHEMA_VERSION,
        "allowed": error is None,
        "decision": "preview_gate" if error is None else _preview_gate_denial_decision(error),
        "reason": "write tool intercepted before execution; host will create a pending preview"
        if error is None
        else str(error.message or error.code or "preview gate rejected tool call"),
        "tool_name": model_event.tool_name,
        "risk_class": "SOFT_WRITE_PREVIEW",
        "duplicate_signature": duplicate_signature,
        "scope_source": _scope_source_for_guard(task_contract),
        "error_code": None if error is None else str(error.code or "PREVIEW_GATE_REJECTED"),
    }
    if error is not None:
        return _guard_denied_model_tool_call_execution(
            model_event=model_event,
            payload=payload,
            guard=guard,
            error=error,
        )

    preview_gate = _preview_gate_payload(model_event=model_event, request=request, payload=payload)
    guard_event = _tool_guard_event_from_guard(
        model_event=model_event,
        guard=guard,
        normalized_payload=_safe_tool_payload(payload),
    )
    raw_result = build_response(
        tool_name=model_event.tool_name,
        ok=True,
        data={
            "status": "preview_requested",
            "preview_gate": preview_gate,
            "response_text": "这看起来是写入请求，已进入预览确认流程。",
            "writes_allowed": False,
            "apply_allowed": False,
        },
    )
    result_adapter = adapt_tool_result(
        event_id=f"result_{model_event.tool_call_id}",
        parent_event_id=guard_event.event_id,
        tool_call_id=model_event.tool_call_id,
        tool_name=model_event.tool_name,
        normalized_payload=_safe_tool_payload(payload),
        guard_decision=guard_event,
        output_contract=resolve_output_contract(model_event.tool_name, payload),
        raw_result=raw_result,
    )
    return GuardedModelToolCallExecution(
        model_event=model_event,
        guard_event=guard_event,
        result_adapter=result_adapter,
        allowed=True,
        ok=True,
        authorization_event={"phase": "preview_gate", **dict(guard)},
        legacy_result_event={
            "phase": "preview_gate",
            "tool_name": model_event.tool_name,
            "ok": True,
            "preview_gate": preview_gate,
        },
        tool_result={
            "index": 1,
            "tool_name": model_event.tool_name,
            "ok": True,
            "preview_gate": preview_gate,
        },
        observation=build_tool_observation(index=1, tool_name=model_event.tool_name, payload=payload, result=raw_result).public_payload(),
        fact_observation=build_fact_observation(index=1, tool_name=model_event.tool_name, payload=payload, result=raw_result),
        synthesis_observation=build_synthesis_observation(index=1, tool_name=model_event.tool_name, payload=payload, result=raw_result),
        error_payload=None,
        preview_gate=preview_gate,
    )


def _preview_gate_error(
    *,
    model_event: ModelToolCallEvent,
    request: AssistantRequest,
    task_contract: dict[str, Any],
    context_validation: dict[str, Any] | None,
) -> AgentToolError | None:
    if _model_tool_call_protocol_error(model_event) is not None:
        protocol_error = _model_tool_call_protocol_error(model_event) or {}
        return AgentToolError(
            code=str(protocol_error.get("code") or "INVALID_MODEL_EVENT"),
            message="model tool call arguments were malformed; retry with valid structured arguments",
            details={"tool_name": model_event.tool_name, "provider_protocol_error": dict(protocol_error)},
        )
    authority = _preview_authority_for_question(request.text)
    if not bool(authority.get("allowed", False)):
        return AgentToolError(
            code="PLAN_RISK_MISMATCH",
            message="这是只读问题，不能进入写入预览流程。",
            hint="Do not use preview-write tools for ordinary questions; write tools are only for explicit record, edit, upgrade, model-switch, or monitor-run requests.",
            details={"write_tool": model_event.tool_name, "preview_capabilities": [model_event.tool_name]},
        )
    allowed_preview_intents = _allowed_preview_intents_from_authority(authority)
    if allowed_preview_intents and model_event.tool_name not in allowed_preview_intents:
        return AgentToolError(
            code="PLAN_RISK_MISMATCH",
            message="当前消息只授权有限的写入预览能力，不能使用其它预览工具。",
            hint="Use the exposed preview capability or ask the user to clarify the operation.",
            details={
                "write_tool": model_event.tool_name,
                "preview_capabilities": [model_event.tool_name],
                "allowed_preview_intents": sorted(allowed_preview_intents),
            },
        )
    step = _agent_loop_step_from_model_event(
        index=1,
        event=model_event,
        question=request.text,
        task_contract=task_contract,
        context_validation=context_validation,
    )
    precheck_error = _planned_step_precheck_error((step,))
    if precheck_error is None:
        return None
    return _preview_precheck_clarification_error(step.precheck) or precheck_error


def _preview_gate_denial_decision(error: AgentToolError) -> str:
    if error.code == "NEEDS_CLARIFICATION":
        return "needs_clarification"
    if error.code == "PLAN_RISK_MISMATCH":
        return "risk_mismatch"
    return "preview_gate_denied"


def _preview_gate_payload(
    *,
    model_event: ModelToolCallEvent,
    request: AssistantRequest,
    payload: dict[str, Any],
) -> dict[str, Any]:
    arguments = dict(payload or {})
    if model_event.tool_name in {"manual_trade_open", "manual_trade_close", "manual_assignment", "manual_expiry"}:
        arguments["raw_text"] = request.text
    return {
        "schema_version": "om-assistant-preview-gate-v1",
        "status": "preview_requested",
        "source": "tool_pre_execution_gate",
        "intent_name": model_event.tool_name,
        "tool_call_id": model_event.tool_call_id,
        "arguments": arguments,
        "reason": model_event.purpose or "write tool intercepted before execution",
        "requires_confirmation": True,
        "apply_allowed": False,
    }


def _model_tool_call_protocol_error(model_event: ModelToolCallEvent) -> dict[str, Any] | None:
    protocol_error = getattr(model_event, "protocol_error", None)
    if isinstance(protocol_error, dict) and protocol_error:
        return dict(protocol_error)
    return None


def _tool_guard_event_from_guard(
    *,
    model_event: ModelToolCallEvent,
    guard: dict[str, Any],
    normalized_payload: dict[str, Any],
) -> ToolGuardDecisionEvent:
    return ToolGuardDecisionEvent(
        event_id=f"guard_{model_event.tool_call_id}",
        parent_event_id=model_event.event_id,
        tool_call_id=model_event.tool_call_id,
        tool_name=model_event.tool_name,
        allowed=bool(guard.get("allowed")),
        decision=str(guard.get("decision") or ""),
        reason=str(guard.get("reason") or ""),
        risk_class=str(guard.get("risk_class") or "UNKNOWN"),
        scope_source=str(guard.get("scope_source") or "unknown"),
        normalized_payload=normalized_payload,
        duplicate_signature=str(guard.get("duplicate_signature") or "") or None,
        error_code=str(guard.get("error_code") or "") or None,
    )


def _guard_payload_after_authorization(*, guard: dict[str, Any], outcome: ToolExecutionOutcome) -> dict[str, Any]:
    if outcome.allowed:
        return dict(guard)
    error_code = outcome.error.code if outcome.error is not None else "PERMISSION_DENIED"
    return {
        **dict(guard),
        "allowed": False,
        "decision": _authorization_denial_decision(outcome.authorization_event),
        "reason": outcome.error.message if outcome.error is not None else "tool authorization denied",
        "risk_class": str(outcome.authorization_event.get("risk_class") or guard.get("risk_class") or "UNKNOWN"),
        "error_code": error_code,
    }


def _authorization_denial_decision(authorization_event: dict[str, Any]) -> str:
    precheck = authorization_event.get("precheck") if isinstance(authorization_event.get("precheck"), dict) else {}
    if str(precheck.get("status") or "") in {"fail", "deny"}:
        return "pre_tool_check_failed"
    action_policy = authorization_event.get("action_policy") if isinstance(authorization_event.get("action_policy"), dict) else {}
    if action_policy.get("allowed") is False:
        return "action_policy_denied"
    return "authorization_denied"


def _tool_budget_guard_payload(
    *,
    model_event: ModelToolCallEvent,
    payload: dict[str, Any],
    task_contract: dict[str, Any],
    max_tool_calls: int,
    tool_call_count: int,
    duplicate_signature: str,
) -> dict[str, Any]:
    return {
        "schema_version": TOOL_CHECK_SCHEMA_VERSION,
        "allowed": False,
        "decision": "tool_budget_exhausted",
        "reason": "tool call budget exhausted",
        "tool_name": model_event.tool_name,
        "risk_class": "READ_AUTO" if _plan_step_kind(model_event.tool_name) == "read" else "UNKNOWN",
        "duplicate_signature": duplicate_signature,
        "scope_source": _scope_source_for_guard(task_contract),
        "max_tool_calls": int(max_tool_calls),
        "tool_call_count": int(tool_call_count),
        "payload_keys": sorted(str(key) for key in (payload or {}).keys()),
        "error_code": "TOOL_BUDGET_EXHAUSTED",
    }


def _guard_error_tool_result(*, tool_name: str, error_payload: dict[str, Any], guard: dict[str, Any]) -> dict[str, Any]:
    return build_response(
        tool_name=tool_name,
        ok=False,
        data={
            "guard_decision": {
                "decision": str(guard.get("decision") or ""),
                "reason": str(guard.get("reason") or ""),
                "risk_class": str(guard.get("risk_class") or ""),
                "scope_source": str(guard.get("scope_source") or ""),
            }
        },
        error=error_payload,
    )


def run_assistant_tool_event_loop(
    *,
    question: str,
    request: AssistantRequest,
    task_contract: dict[str, Any],
    initial_events: tuple[ModelToolCallEvent | ModelFinalAnswerEvent | AssistantEvent, ...],
    execute_tool_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
    context_validation: dict[str, Any] | None = None,
    provider: str = "openai",
    create_continuation_response_fn: CreateModelContinuationResponseFn | None = None,
    continuation_base_payload: dict[str, Any] | None = None,
    max_tool_calls: int = MAX_AGENT_LOOP_TOOL_CALLS,
) -> AssistantToolLoopOutcome:
    """Run a bounded event transcript without converting model events to legacy plans."""

    transcript: list[Any] = []
    tool_results: list[ToolResultAdapterOutput] = []
    attempted_signatures: set[str] = set()
    recoverable_error_counts: dict[str, int] = {}
    pending_events: tuple[ModelToolCallEvent | ModelFinalAnswerEvent | AssistantEvent, ...] = tuple(initial_events)
    tool_call_count = 0
    continuation_count = 0
    final_answer_retry_attempted = False
    final_answer_retry_reason = ""
    current_continuation_base_payload = dict(continuation_base_payload or {}) if continuation_base_payload is not None else None

    def _try_final_answer_retry(
        *,
        reason: str,
        results: tuple[tuple[ModelToolCallEvent, ToolResultEvent], ...],
        parent_event_id: str,
    ) -> AgentToolError | None:
        nonlocal pending_events
        nonlocal continuation_count
        nonlocal current_continuation_base_payload
        nonlocal final_answer_retry_attempted
        nonlocal final_answer_retry_reason

        final_answer_retry_attempted = True
        final_answer_retry_reason = reason
        try:
            retry_continuation = continue_model_after_tool_results(
                provider=provider,
                create_response_fn=create_continuation_response_fn,
                results=results,
                base_payload=_final_answer_only_continuation_base_payload(
                    provider=provider,
                    base_payload=current_continuation_base_payload,
                ),
                parent_event_id=parent_event_id,
            )
        except (OpenAIResponsesError, OpenAIChatCompletionsError) as err:
            return AgentToolError(
                code="LLM_PROVIDER_ERROR",
                message="LLM continuation provider failed.",
                details={"provider": provider, "http_status": err.http_status},
            )
        except AgentToolError as err:
            return err

        continuation_count += 1
        current_continuation_base_payload = dict(retry_continuation.request_payload)
        pending_events = tuple(retry_continuation.events)
        return None

    while True:
        if not pending_events:
            return _assistant_tool_loop_outcome(
                status="unsupported",
                stop_reason="invalid_model_event",
                question=question,
                task_contract=task_contract,
                transcript=transcript,
                tool_results=tool_results,
                trace_extra={
                    "planner_plan_used": False,
                    "continuation_count": continuation_count,
                    **_final_answer_retry_trace(
                        attempted=final_answer_retry_attempted,
                        reason=final_answer_retry_reason,
                    ),
                },
            )

        first = pending_events[0]
        if isinstance(first, ModelFinalAnswerEvent):
            transcript.append(first)
            evidence = _assistant_tool_loop_evidence(
                question=question,
                task_contract=task_contract,
                tool_results=tool_results,
                parent_event_id=first.event_id,
            )
            verification = verify_model_final_answer(
                answer_event=first,
                model_evidence=evidence,
                task_contract=task_contract,
                tool_results=tool_results,
            )
            final_answer = first.answer_text if verification.passed else (verification.fallback_text or None)
            return _assistant_tool_loop_outcome(
                status="done" if final_answer else "stopped",
                stop_reason="model_final_answer" if final_answer else "answer_verification_failed",
                question=question,
                task_contract=task_contract,
                transcript=transcript,
                tool_results=tool_results,
                evidence=evidence,
                final_answer=final_answer,
                final_answer_event=first,
                answer_verification=verification,
                trace_extra={
                    "planner_plan_used": False,
                    "continuation_count": continuation_count,
                    "answer_verification": verification.public_payload(),
                    **_final_answer_retry_trace(
                        attempted=final_answer_retry_attempted,
                        reason=final_answer_retry_reason,
                    ),
                },
            )

        if isinstance(first, AssistantEvent) and first.event_type == "clarification_request":
            transcript.append(first)
            return _assistant_tool_loop_outcome(
                status="needs_clarification",
                stop_reason="clarification_request",
                question=question,
                task_contract=task_contract,
                transcript=transcript,
                tool_results=tool_results,
                clarification_request=first.public_payload(),
                trace_extra={"planner_plan_used": False, "continuation_count": continuation_count},
            )

        tool_calls = tuple(event for event in pending_events if isinstance(event, ModelToolCallEvent))
        if not tool_calls:
            transcript.extend(pending_events)
            return _assistant_tool_loop_outcome(
                status="unsupported",
                stop_reason="invalid_model_event",
                question=question,
                task_contract=task_contract,
                transcript=transcript,
                tool_results=tool_results,
                trace_extra={
                    "planner_plan_used": False,
                    "continuation_count": continuation_count,
                    **_final_answer_retry_trace(
                        attempted=final_answer_retry_attempted,
                        reason=final_answer_retry_reason,
                    ),
                },
            )

        turn_executions: list[GuardedModelToolCallExecution] = []
        final_answer_only_continuation = False
        for model_event in tool_calls:
            execution = execute_model_tool_call_event(
                model_event=model_event,
                request=request,
                task_contract=task_contract,
                execute_tool_fn=execute_tool_fn,
                context_validation=context_validation,
                attempted_signatures=attempted_signatures,
                tool_call_count=tool_call_count,
                max_tool_calls=max_tool_calls,
            )
            transcript.extend((execution.model_event, execution.guard_event, execution.result_adapter.event))
            tool_results.append(execution.result_adapter)
            tool_call_count += 1
            turn_executions.append(execution)

            if execution.preview_gate is not None:
                return _assistant_tool_loop_outcome(
                    status="preview_requested",
                    stop_reason="preview_gate",
                    question=question,
                    task_contract=task_contract,
                    transcript=transcript,
                    tool_results=tool_results,
                    preview_gate=execution.preview_gate,
                    trace_extra={"planner_plan_used": False, "continuation_count": continuation_count},
                )

            if not execution.ok:
                if isinstance(execution.error_payload, dict) and execution.error_payload.get("code") == "NEEDS_CLARIFICATION":
                    return _assistant_tool_loop_outcome(
                        status="needs_clarification",
                        stop_reason="clarification_request",
                        question=question,
                        task_contract=task_contract,
                        transcript=transcript,
                        tool_results=tool_results,
                        clarification_request=_clarification_request_payload(str(execution.error_payload.get("message") or "")),
                        trace_extra={
                            "planner_plan_used": False,
                            "continuation_count": continuation_count,
                            "preview_error": dict(execution.error_payload),
                        },
                    )
                recoverable = _assistant_tool_loop_error_recoverable(execution)
                if recoverable:
                    error_signature = _assistant_tool_loop_recoverable_error_signature(execution)
                    recoverable_error_counts[error_signature] = recoverable_error_counts.get(error_signature, 0) + 1
                    duplicate_call = str(execution.guard_event.error_code or "") == "DUPLICATE_TOOL_CALL"
                    if recoverable_error_counts[error_signature] > 1 or (
                        duplicate_call and create_continuation_response_fn is None
                    ):
                        return _assistant_tool_loop_outcome(
                            status="stopped",
                            stop_reason="repeated_recoverable_error",
                            question=question,
                            task_contract=task_contract,
                            transcript=transcript,
                            tool_results=tool_results,
                            trace_extra={
                                "planner_plan_used": False,
                                "continuation_count": continuation_count,
                                "guard_denial_recoverable": True,
                                "repeated_error_signature": error_signature,
                            },
                        )
                    if duplicate_call:
                        final_answer_only_continuation = True
                else:
                    return _assistant_tool_loop_outcome(
                        status="stopped",
                        stop_reason=str(execution.guard_event.error_code or execution.guard_event.decision or "tool_guard_denied"),
                        question=question,
                        task_contract=task_contract,
                        transcript=transcript,
                        tool_results=tool_results,
                        trace_extra={
                            "planner_plan_used": False,
                            "continuation_count": continuation_count,
                            "guard_denial_recoverable": False,
                        },
                    )

        if create_continuation_response_fn is not None and turn_executions:
            try:
                continuation_base_payload_for_turn = current_continuation_base_payload
                if final_answer_only_continuation:
                    final_answer_retry_attempted = True
                    final_answer_retry_reason = final_answer_retry_reason or "duplicate_call"
                    continuation_base_payload_for_turn = _final_answer_only_continuation_base_payload(
                        provider=provider,
                        base_payload=current_continuation_base_payload,
                    )
                continuation = continue_model_after_tool_results(
                    provider=provider,
                    create_response_fn=create_continuation_response_fn,
                    results=tuple((execution.model_event, execution.result_adapter.event) for execution in turn_executions),
                    base_payload=continuation_base_payload_for_turn,
                    parent_event_id=turn_executions[-1].result_adapter.event.event_id,
                )
            except (OpenAIResponsesError, OpenAIChatCompletionsError) as err:
                continuation_error = AgentToolError(
                    code="LLM_PROVIDER_ERROR",
                    message="LLM continuation provider failed.",
                    details={"provider": provider, "http_status": err.http_status},
                )
                return _assistant_tool_loop_outcome(
                    status="stopped",
                    stop_reason="continuation_provider_error",
                    question=question,
                    task_contract=task_contract,
                    transcript=transcript,
                    tool_results=tool_results,
                    trace_extra={
                        "planner_plan_used": False,
                        "continuation_count": continuation_count,
                        "continuation_error": build_error_payload(continuation_error),
                        **_final_answer_retry_trace(
                            attempted=final_answer_retry_attempted,
                            reason=final_answer_retry_reason,
                        ),
                    },
                )
            except AgentToolError as err:
                return _assistant_tool_loop_outcome(
                    status="stopped",
                    stop_reason=str(err.code or "invalid_model_event").lower(),
                    question=question,
                    task_contract=task_contract,
                    transcript=transcript,
                    tool_results=tool_results,
                    trace_extra={
                        "planner_plan_used": False,
                        "continuation_count": continuation_count,
                        "continuation_error": build_error_payload(err),
                        **_final_answer_retry_trace(
                            attempted=final_answer_retry_attempted,
                            reason=final_answer_retry_reason,
                        ),
                    },
                )
            continuation_count += 1
            current_continuation_base_payload = dict(continuation.request_payload)
            pending_events = tuple(continuation.events)
            successful_results = _successful_execution_results(turn_executions)
            if successful_results and not pending_events and not final_answer_retry_attempted and _final_answer_retry_allowed(
                task_contract=task_contract,
                tool_results=tool_results,
            ):
                retry_error = _try_final_answer_retry(
                    reason="empty_continuation",
                    results=successful_results,
                    parent_event_id=turn_executions[-1].result_adapter.event.event_id,
                )
                if retry_error is not None:
                    return _assistant_tool_loop_outcome(
                        status="stopped",
                        stop_reason=_continuation_error_stop_reason(retry_error),
                        question=question,
                        task_contract=task_contract,
                        transcript=transcript,
                        tool_results=tool_results,
                        trace_extra={
                            "planner_plan_used": False,
                            "continuation_count": continuation_count,
                            "continuation_error": build_error_payload(retry_error),
                            **_final_answer_retry_trace(
                                attempted=final_answer_retry_attempted,
                                reason=final_answer_retry_reason,
                            ),
                        },
                    )
            if tool_call_count >= int(max_tool_calls) and any(
                isinstance(event, ModelToolCallEvent) for event in pending_events
            ):
                if successful_results and not final_answer_retry_attempted and _final_answer_retry_allowed(
                    task_contract=task_contract,
                    tool_results=tool_results,
                ):
                    retry_error = _try_final_answer_retry(
                        reason="tool_budget_exhausted",
                        results=successful_results,
                        parent_event_id=turn_executions[-1].result_adapter.event.event_id,
                    )
                    if retry_error is not None:
                        return _assistant_tool_loop_outcome(
                            status="stopped",
                            stop_reason=_continuation_error_stop_reason(retry_error),
                            question=question,
                            task_contract=task_contract,
                            transcript=transcript,
                            tool_results=tool_results,
                            trace_extra={
                                "planner_plan_used": False,
                                "continuation_count": continuation_count,
                                "continuation_error": build_error_payload(retry_error),
                                **_final_answer_retry_trace(
                                    attempted=final_answer_retry_attempted,
                                    reason=final_answer_retry_reason,
                                ),
                            },
                        )
                    continue
                return _assistant_tool_loop_outcome(
                    status="stopped",
                    stop_reason="tool_budget_exhausted",
                    question=question,
                    task_contract=task_contract,
                    transcript=transcript,
                    tool_results=tool_results,
                    trace_extra={"planner_plan_used": False, "continuation_count": continuation_count},
                )
            continue

        return _assistant_tool_loop_outcome(
            status="stopped",
            stop_reason="awaiting_model_continuation",
            question=question,
            task_contract=task_contract,
            transcript=transcript,
            tool_results=tool_results,
            evidence=_assistant_tool_loop_evidence(
                question=question,
                task_contract=task_contract,
                tool_results=tool_results,
                parent_event_id=transcript[-1].event_id if transcript and hasattr(transcript[-1], "event_id") else None,
            ),
            trace_extra={"planner_plan_used": False, "continuation_count": continuation_count},
        )


def _assistant_tool_loop_outcome(
    *,
    status: str,
    stop_reason: str,
    question: str,
    task_contract: dict[str, Any],
    transcript: list[Any],
    tool_results: list[ToolResultAdapterOutput],
    evidence: ModelEvidenceBundle | None = None,
    final_answer: str | None = None,
    final_answer_event: ModelFinalAnswerEvent | None = None,
    answer_verification: ModelAnswerVerification | None = None,
    clarification_request: dict[str, Any] | None = None,
    preview_gate: dict[str, Any] | None = None,
    trace_extra: dict[str, Any] | None = None,
) -> AssistantToolLoopOutcome:
    resolved_evidence = evidence
    if resolved_evidence is None and tool_results:
        resolved_evidence = _assistant_tool_loop_evidence(
            question=question,
            task_contract=task_contract,
            tool_results=tool_results,
            parent_event_id=transcript[-1].event_id if transcript and hasattr(transcript[-1], "event_id") else None,
        )
    events = _assistant_tool_loop_events_with_evidence(
        transcript=transcript,
        evidence=resolved_evidence,
        final_answer_event=final_answer_event,
    )
    trace_extra_payload = dict(trace_extra or {})
    evidence_summary = _assistant_tool_loop_trace_evidence_summary(resolved_evidence)
    trace = {
        "schema_version": "om-assistant-tool-loop-trace-v1",
        "status": status,
        "stop_reason": stop_reason,
        "loop_stop_reason": stop_reason,
        "event_count": len(events),
        "tool_result_count": len(tool_results),
        "tool_call_count": _assistant_tool_loop_tool_call_count(events),
        "scope_source": _assistant_tool_loop_scope_source(events, task_contract=task_contract),
        "answer_route": _assistant_tool_loop_answer_route(
            status=status,
            final_answer_event=final_answer_event,
            answer_verification=answer_verification,
        ),
        "repair_attempted": _assistant_tool_loop_repair_attempted(
            tool_results=tool_results,
            continuation_count=int(trace_extra_payload.get("continuation_count") or 0),
        ),
        "capability_selection": _assistant_tool_loop_capability_selection(events),
        "read_agent_mode": _assistant_tool_loop_read_agent_mode(task_contract),
        "evidence_summary": evidence_summary,
        "planner_plan_used": False,
    }
    trace.update(trace_extra_payload)
    trace["stop_category"] = _assistant_tool_loop_stop_category(
        status=status,
        stop_reason=stop_reason,
        trace=trace,
    )
    return AssistantToolLoopOutcome(
        status=status,
        stop_reason=stop_reason,
        events=events,
        tool_results=tuple(tool_results),
        evidence=resolved_evidence,
        final_answer=final_answer,
        final_answer_event=final_answer_event,
        answer_verification=answer_verification,
        clarification_request=clarification_request,
        preview_gate=preview_gate,
        trace=trace,
    )


def _assistant_tool_loop_events_with_evidence(
    *,
    transcript: list[Any],
    evidence: ModelEvidenceBundle | None,
    final_answer_event: ModelFinalAnswerEvent | None,
) -> tuple[Any, ...]:
    if evidence is None:
        return tuple(transcript)
    evidence_event = evidence.evidence_event
    evidence_event_id = str(evidence_event.event_id or "")
    if any(str(getattr(event, "event_id", "") or "") == evidence_event_id for event in transcript):
        return tuple(transcript)
    if final_answer_event is not None and transcript and transcript[-1] is final_answer_event:
        answer_event = final_answer_event
        if answer_event.parent_event_id != evidence_event_id:
            answer_event = replace(answer_event, parent_event_id=evidence_event_id)
        return (*tuple(transcript[:-1]), evidence_event, answer_event)
    return (*tuple(transcript), evidence_event)


def _assistant_tool_loop_tool_call_count(events: tuple[Any, ...]) -> int:
    return sum(1 for event in events if isinstance(event, ModelToolCallEvent))


def _assistant_tool_loop_scope_source(events: tuple[Any, ...], *, task_contract: dict[str, Any]) -> str:
    for event in events:
        if isinstance(event, ToolGuardDecisionEvent) and event.scope_source:
            return str(event.scope_source)
    return _scope_source_for_guard(task_contract)


def _assistant_tool_loop_answer_route(
    *,
    status: str,
    final_answer_event: ModelFinalAnswerEvent | None,
    answer_verification: ModelAnswerVerification | None,
) -> str:
    if answer_verification is not None and not answer_verification.passed and answer_verification.fallback_text:
        return "canonical_renderer"
    if answer_verification is not None and not answer_verification.passed:
        return "answer_verification_failed"
    if final_answer_event is not None:
        return str(final_answer_event.answer_route or "llm_from_evidence")
    if status == "preview_requested":
        return "preview_lifecycle"
    if status == "needs_clarification":
        return "clarification_request"
    return "loop_stopped"


def _assistant_tool_loop_repair_attempted(
    *,
    tool_results: list[ToolResultAdapterOutput],
    continuation_count: int,
) -> bool:
    return bool(continuation_count > 0 and any(not adapter.event.ok for adapter in tool_results))


def _assistant_tool_loop_capability_selection(events: tuple[Any, ...]) -> dict[str, Any]:
    selected: list[dict[str, Any]] = []
    guard_by_call_id = {
        str(event.tool_call_id): event
        for event in events
        if isinstance(event, ToolGuardDecisionEvent) and str(event.tool_call_id or "")
    }
    for event in events:
        if not isinstance(event, ModelToolCallEvent):
            continue
        guard = guard_by_call_id.get(str(event.tool_call_id or ""))
        selected.append(
            {
                "tool_name": event.tool_name,
                "effect": _plan_step_kind(event.tool_name) or "unknown",
                "allowed": bool(guard.allowed) if guard is not None else None,
                "decision": guard.decision if guard is not None else None,
                "risk_class": guard.risk_class if guard is not None else None,
            }
        )
    return {
        "selected": selected,
        "selected_count": len(selected),
    }


def _assistant_tool_loop_read_agent_mode(task_contract: dict[str, Any]) -> str:
    requested_effect = str(task_contract.get("requested_effect") or "read").strip()
    if requested_effect in {"", "read", "none"}:
        return "model_driven_read_loop"
    if requested_effect in {"preview", "preview_write"}:
        return "preview_boundary"
    return "write_boundary"


def _assistant_tool_loop_trace_evidence_summary(evidence: ModelEvidenceBundle | None) -> dict[str, Any]:
    if evidence is None:
        return {
            "fact_count": 0,
            "dataset_count": 0,
            "diagnostic_count": 0,
            "missing_data_count": 0,
            "conflict_count": 0,
        }
    payload = evidence.evidence_bundle.trace_payload()
    return {
        key: payload.get(key)
        for key in (
            "fact_count",
            "dataset_count",
            "diagnostic_count",
            "missing_data_count",
            "conflict_count",
            "sources",
            "tools",
            "diagnostic_domains",
        )
        if payload.get(key) not in (None, "", [], {})
    }


def _assistant_tool_loop_stop_category(*, status: str, stop_reason: str, trace: dict[str, Any]) -> str:
    if stop_reason == "model_final_answer":
        return "model_final_answer"
    if status == "needs_clarification" or stop_reason == "clarification_request":
        return "clarification_request"
    if status == "preview_requested" or stop_reason == "preview_gate":
        return "preview_gate"
    if stop_reason == "answer_verification_failed":
        return "answer_verification_failed"
    if stop_reason == "tool_budget_exhausted":
        evidence_summary = trace.get("evidence_summary") if isinstance(trace.get("evidence_summary"), dict) else {}
        return "tool_budget_exhausted_with_evidence" if evidence_summary.get("dataset_count") else "tool_budget_exhausted"
    if trace.get("guard_denial_recoverable") is False:
        return "unrecoverable_guard_denial"
    if trace.get("guard_denial_recoverable") is True:
        return "recoverable_guard_denial"
    if stop_reason in {"continuation_provider_error", "repeated_recoverable_error", "invalid_model_event"}:
        return stop_reason
    if status == "stopped":
        return "loop_stopped"
    return status or stop_reason or "unknown"


def _assistant_tool_loop_evidence(
    *,
    question: str,
    task_contract: dict[str, Any],
    tool_results: list[ToolResultAdapterOutput],
    parent_event_id: str | None,
) -> ModelEvidenceBundle:
    return build_model_evidence_bundle(
        question=question,
        task_contract=task_contract,
        tool_results=tool_results,
        parent_event_id=parent_event_id,
    )


def _assistant_tool_loop_error_recoverable(execution: GuardedModelToolCallExecution) -> bool:
    code = str(execution.guard_event.error_code or "")
    decision = str(execution.guard_event.decision or "")
    if code == "PLAN_RISK_MISMATCH":
        return True
    if code == "PERMISSION_DENIED" and decision == "write_boundary":
        return True
    if code in {"PRE_TOOL_CHECK_FAILED", "DUPLICATE_TOOL_CALL", "UNKNOWN_TOOL", "INVALID_MODEL_EVENT"}:
        return True
    if decision in {"pre_tool_check_failed", "duplicate_call", "unknown_tool", "provider_protocol_error"}:
        return True
    if decision == "write_boundary" and str(execution.guard_event.risk_class or "") == "READ_AUTO":
        return True
    error_payload = execution.error_payload if isinstance(execution.error_payload, dict) else {}
    error_code = str(error_payload.get("code") or "")
    return error_code in {"INPUT_ERROR", "SCHEMA_INVALID", "TOOL_RUNTIME_ERROR", "UNKNOWN_TOOL", "INVALID_MODEL_EVENT"}


def _assistant_tool_loop_recoverable_error_signature(execution: GuardedModelToolCallExecution) -> str:
    if str(execution.guard_event.error_code or "") == "DUPLICATE_TOOL_CALL":
        payload = {
            "tool_name": execution.model_event.tool_name,
            "tool_call_error_code": str(execution.guard_event.error_code or ""),
            "tool_call_decision": str(execution.guard_event.decision or ""),
            "duplicate_signature": str(execution.guard_event.duplicate_signature or ""),
            "result_error_code": str(execution.result_adapter.event.error_code or ""),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    payload = {
        "tool_name": execution.model_event.tool_name,
        "tool_call_error_code": str(execution.guard_event.error_code or ""),
        "tool_call_decision": str(execution.guard_event.decision or ""),
        "normalized_payload": execution.guard_event.normalized_payload,
        "result_error_code": str(execution.result_adapter.event.error_code or ""),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _final_answer_retry_allowed(
    *,
    task_contract: dict[str, Any],
    tool_results: list[ToolResultAdapterOutput],
) -> bool:
    requested_effect = str(task_contract.get("requested_effect") or "read").strip()
    if requested_effect not in {"", "read", "none"}:
        return False
    return bool(canonical_fallback_from_tool_results(tool_results))


def _successful_execution_results(
    executions: list[GuardedModelToolCallExecution],
) -> tuple[tuple[ModelToolCallEvent, ToolResultEvent], ...]:
    return tuple(
        (execution.model_event, execution.result_adapter.event)
        for execution in executions
        if execution.ok
    )


def _final_answer_retry_trace(*, attempted: bool, reason: str) -> dict[str, Any]:
    if not attempted:
        return {}
    trace: dict[str, Any] = {"final_answer_retry_attempted": True}
    if reason:
        trace["final_answer_retry_reason"] = reason
    return trace


def _continuation_error_stop_reason(error: AgentToolError) -> str:
    if str(error.code or "") == "LLM_PROVIDER_ERROR":
        return "continuation_provider_error"
    return str(error.code or "invalid_model_event").lower()


def execute_tool_loop_payload(
    *,
    question: str,
    request: AssistantRequest,
    loop_payload: dict[str, Any],
    command_id: str | None = None,
    settings: AssistantSettings,
    conversation_context: dict[str, Any] | None,
    execute_tool_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    precomputed = loop_payload.get("_precomputed_tool_loop_result")
    if isinstance(precomputed, dict):
        return _precomputed_tool_loop_response(precomputed, command_id=command_id)
    events = _assistant_tool_loop_events_from_payload(loop_payload.get("events"))
    task_contract = _assistant_tool_loop_task_contract(
        question=question,
        request=request,
        loop_payload=loop_payload,
        conversation_context=conversation_context,
    )
    provider = normalize_llm_provider(str(loop_payload.get("provider") or settings.llm.provider or "openai"))
    continuation_response_fn = _assistant_tool_loop_continuation_response_fn(
        provider=provider,
        llm_settings=settings.llm,
    )
    continuation_base_payload = (
        _assistant_tool_loop_continuation_base_payload(
            provider=provider,
            question=question,
            conversation_context=conversation_context,
            llm_settings=settings.llm,
        )
        if continuation_response_fn is not None
        else None
    )
    outcome = run_assistant_tool_event_loop(
        question=question,
        request=request,
        task_contract=task_contract,
        initial_events=events,
        execute_tool_fn=execute_tool_fn,
        provider=provider,
        create_continuation_response_fn=continuation_response_fn,
        continuation_base_payload=continuation_base_payload,
    )
    return _build_tool_loop_response_from_outcome(
        outcome=outcome,
        question=question,
        command_id=command_id,
        task_contract=task_contract,
    )


def _precomputed_tool_loop_response(precomputed: dict[str, Any], *, command_id: str | None) -> dict[str, Any]:
    response = dict(precomputed)
    data = response.get("data")
    if isinstance(data, dict):
        patched_data = dict(data)
        if command_id:
            patched_data["command_id"] = command_id
        response["data"] = patched_data
    return response


def _build_tool_loop_response_from_outcome(
    *,
    outcome: AssistantToolLoopOutcome,
    question: str,
    command_id: str | None,
    task_contract: dict[str, Any],
) -> dict[str, Any]:
    response_text = _assistant_tool_loop_response_text(outcome)
    ok = outcome.status == "done" and (bool(outcome.final_answer) or any(result.event.ok for result in outcome.tool_results))
    if outcome.status == "stopped" and outcome.stop_reason != "answer_verification_failed":
        ok = any(result.event.ok for result in outcome.tool_results)
    final_response = {
        "status": "synthesized" if outcome.final_answer else "rendered",
        "reason": outcome.stop_reason or outcome.status,
        "canonical_renderer_required": not bool(outcome.final_answer),
        "llm_may_summarize": bool(outcome.final_answer),
    }
    if outcome.trace.get("final_answer_retry_attempted"):
        final_response["final_answer_retry_attempted"] = True
    retry_reason = str(outcome.trace.get("final_answer_retry_reason") or "").strip()
    if retry_reason:
        final_response["final_answer_retry_reason"] = retry_reason
    observations = (
        [dict(item) for item in outcome.evidence.observations]
        if outcome.evidence is not None
        else []
    )
    evidence_payload = outcome.evidence.evidence_bundle.public_payload() if outcome.evidence is not None else {}
    data = {
        "response_text": response_text,
        "schema_version": "om-assistant-tool-loop-result-v1",
        "question": question,
        "command_id": command_id,
        "task_contract": task_contract,
        "event_loop": outcome.public_payload(),
        "event_transcript": event_transcript_payload(outcome.events),
        "observations": observations,
        "synthesis_observations": observations,
        "evidence_bundle": evidence_payload,
        "tool_events": _assistant_tool_loop_tool_events(outcome),
        "tool_calls_used": len(outcome.tool_results),
        "writes_allowed": False,
        "final_response": final_response,
    }
    error = None
    if not ok:
        error_details: dict[str, Any] = {"status": outcome.status, "stop_reason": outcome.stop_reason}
        trace = outcome.trace if isinstance(outcome.trace, dict) else {}
        preview_error = trace.get("preview_error") if isinstance(trace.get("preview_error"), dict) else {}
        if not preview_error:
            preview_error = _last_tool_result_error_payload(outcome.tool_results)
        preview_error_details = (
            preview_error.get("details")
            if isinstance(preview_error.get("details"), dict)
            else {}
        )
        error_hint = str(preview_error.get("hint") or "").strip() if isinstance(preview_error, dict) else ""
        error_details.update(dict(preview_error_details))
        if outcome.clarification_request is not None:
            error_details.setdefault("clarification_request", dict(outcome.clarification_request))
        error = build_error_payload(
            AgentToolError(
                code=_assistant_tool_loop_error_code(outcome),
                message=_assistant_tool_loop_error_message(outcome),
                hint=error_hint or None,
                details=error_details,
            )
        )
    return build_response(
        tool_name=INTERNAL_TOOL_LOOP_NAME,
        ok=ok,
        data=data,
        error=error,
    )


def _last_tool_result_error_payload(tool_results: tuple[ToolResultAdapterOutput, ...]) -> dict[str, Any]:
    for adapter in reversed(tuple(tool_results)):
        raw_result = adapter.raw_result if isinstance(adapter.raw_result, dict) else {}
        error = raw_result.get("error") if isinstance(raw_result.get("error"), dict) else {}
        if error:
            return dict(error)
    return {}


def _assistant_tool_loop_continuation_response_fn(
    *,
    provider: str,
    llm_settings: AssistantLlmSettings,
) -> CreateModelContinuationResponseFn | None:
    if not bool(llm_settings.enabled):
        return None
    if missing_llm_config(llm_settings):
        return None
    normalized_provider = normalize_llm_provider(provider or llm_settings.provider)
    if not is_supported_llm_provider(normalized_provider):
        return None
    api_key = llm_api_key_value(llm_settings, environ=None)
    if not api_key:
        return None
    response_fn: CreateToolCallPayloadResponseFn = provider_create_tool_call_payload_response_fn(normalized_provider)

    def _create_response(payload: dict[str, Any]) -> dict[str, Any]:
        return response_fn(
            api_key=api_key,
            base_url=llm_settings.base_url,
            payload=dict(payload or {}),
            timeout=int(llm_settings.timeout_seconds),
        )

    return _create_response


def _final_answer_only_continuation_base_payload(*, provider: str, base_payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(base_payload or {})
    payload.pop("tools", None)
    payload.pop("tool_choice", None)
    instruction = (
        "The existing tool observations are the only allowed evidence for this answer. Do not call any more tools. "
        "Produce one natural user-facing final answer using only those observations. "
        "If useful data is missing, say that explicitly instead of inventing it."
    )
    if provider_api_kind(provider) == "chat_completions":
        messages = payload.get("messages")
        existing = [dict(item) for item in messages] if isinstance(messages, list) else []
        payload["messages"] = [*existing, {"role": "system", "content": instruction}]
        return payload
    existing_instructions = str(payload.get("instructions") or "").strip()
    payload["instructions"] = f"{existing_instructions}\n\n{instruction}".strip()
    return payload


def _assistant_tool_loop_continuation_base_payload(
    *,
    provider: str,
    question: str,
    conversation_context: dict[str, Any] | None,
    llm_settings: AssistantLlmSettings,
) -> dict[str, Any]:
    planner_payload = _planner_input_payload(question, conversation_context=conversation_context)
    planner_input_text = json.dumps(planner_payload, ensure_ascii=False, sort_keys=True)
    tools = _provider_tool_call_tools(provider, planner_payload)
    if provider_api_kind(provider) == "chat_completions":
        payload = {
            "model": str(llm_settings.model or "").strip(),
            "messages": [
                {"role": "system", "content": _model_event_planner_instructions()},
                {"role": "user", "content": planner_input_text},
            ],
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": int(llm_settings.max_output_tokens),
            "stream": False,
        }
        for key, value in chat_completions_payload_options(provider).items():
            if value is not None:
                payload[key] = value
        return payload
    return {
        "model": str(llm_settings.model or "").strip(),
        "instructions": _model_event_planner_instructions(),
        "input": [{"role": "user", "content": planner_input_text}],
        "tools": tools,
        "tool_choice": "auto",
        "max_output_tokens": int(llm_settings.max_output_tokens),
        "store": False,
        "temperature": 0.0,
    }


def _assistant_tool_loop_events_from_payload(value: Any) -> tuple[ModelToolCallEvent | ModelFinalAnswerEvent | AssistantEvent, ...]:
    if not isinstance(value, list) or not value:
        raise AgentToolError(code="INVALID_MODEL_EVENT", message="tool_loop payload requires non-empty model events")
    return tuple(_assistant_tool_loop_event_from_payload(item) for item in value if isinstance(item, dict))


def _assistant_tool_loop_event_from_payload(payload: dict[str, Any]) -> ModelToolCallEvent | ModelFinalAnswerEvent | AssistantEvent:
    event_type = str(payload.get("event_type") or "").strip()
    if event_type == "model_tool_call":
        return ModelToolCallEvent(
            event_id=str(payload.get("event_id") or payload.get("tool_call_id") or "model_tool_call_1"),
            tool_call_id=str(payload.get("tool_call_id") or payload.get("event_id") or "call_1"),
            tool_name=str(payload.get("tool_name") or "").strip(),
            arguments=dict(payload.get("arguments") or {}) if isinstance(payload.get("arguments"), dict) else {},
            purpose=str(payload.get("purpose") or ""),
            provider=str(payload.get("provider") or "") or None,
            parent_event_id=str(payload.get("parent_event_id") or "") or None,
            protocol_error=(
                dict(payload.get("protocol_error"))
                if isinstance(payload.get("protocol_error"), dict)
                else None
            ),
            schema_version=str(payload.get("schema_version") or MODEL_EVENT_SCHEMA_VERSION),
        )
    if event_type == "model_final_answer":
        return ModelFinalAnswerEvent(
            event_id=str(payload.get("event_id") or "model_final_answer_1"),
            answer_text=str(payload.get("answer_text") or ""),
            answer_route=str(payload.get("answer_route") or "llm_from_evidence"),
            parent_event_id=str(payload.get("parent_event_id") or "") or None,
            schema_version=str(payload.get("schema_version") or MODEL_EVENT_SCHEMA_VERSION),
        )
    if event_type in {"clarification_request", "loop_stopped", "context_projected", "user_message"}:
        return AssistantEvent(
            event_id=str(payload.get("event_id") or event_type),
            event_type=event_type,  # type: ignore[arg-type]
            payload=dict(payload.get("payload") or {}) if isinstance(payload.get("payload"), dict) else {},
            parent_event_id=str(payload.get("parent_event_id") or "") or None,
            schema_version=str(payload.get("schema_version") or MODEL_EVENT_SCHEMA_VERSION),
        )
    raise AgentToolError(
        code="INVALID_MODEL_EVENT",
        message=f"unsupported tool_loop event type: {event_type or 'missing'}",
        details={"event_type": event_type},
    )


def _assistant_tool_loop_task_contract(
    *,
    question: str,
    request: AssistantRequest,
    loop_payload: dict[str, Any],
    conversation_context: dict[str, Any] | None,
) -> dict[str, Any]:
    contract = loop_payload.get("task_contract") if isinstance(loop_payload.get("task_contract"), dict) else {}
    if contract:
        return dict(contract)
    built = build_task_contract(
        question=question,
        plan={"goal": question, "steps": []},
        request_context=request.public_payload(),
        today=_planner_today_from_context(conversation_context),
    )
    return built.public_payload()


def _assistant_tool_loop_response_text(outcome: AssistantToolLoopOutcome) -> str:
    if outcome.final_answer:
        return outcome.final_answer
    if outcome.stop_reason == "answer_verification_failed":
        return "需要先读取相关 OM 证据后才能回答；本次没有执行工具。"
    fallback = canonical_fallback_from_tool_results(outcome.tool_results)
    if fallback:
        return fallback
    if outcome.status == "needs_clarification" and outcome.clarification_request:
        payload = outcome.clarification_request.get("payload")
        if isinstance(payload, dict):
            return str(payload.get("question") or payload.get("message") or "需要补充查询范围。")
    if outcome.status == "preview_requested":
        return "这看起来是写入预览请求，需要进入预览确认流程。"
    if outcome.tool_results:
        return "已完成工具调用，但当前结果没有可渲染的文本。"
    return "模型没有生成可执行工具调用，未执行工具。"


def _assistant_tool_loop_tool_events(outcome: AssistantToolLoopOutcome) -> list[dict[str, Any]]:
    events = event_transcript_payload(outcome.events)
    out: list[dict[str, Any]] = [
        {
            "phase": "event_loop",
            "status": outcome.status,
            "stop_reason": outcome.stop_reason,
            "event_count": len(events),
            "planner_plan_used": False,
        }
    ]
    for event in events:
        out.append({"phase": str(event.get("event_type") or "event"), **event})
    return out


def _assistant_tool_loop_error_code(outcome: AssistantToolLoopOutcome) -> str:
    if outcome.status == "needs_clarification":
        return "NEEDS_CLARIFICATION"
    for adapter in reversed(tuple(outcome.tool_results)):
        error_code = str(adapter.event.error_code or "").strip()
        if error_code:
            return error_code
    if outcome.stop_reason:
        return str(outcome.stop_reason).upper()
    return "TOOL_LOOP_STOPPED"


def _assistant_tool_loop_error_message(outcome: AssistantToolLoopOutcome) -> str:
    if outcome.status == "needs_clarification":
        return _assistant_tool_loop_response_text(outcome)
    if outcome.status == "preview_requested":
        return "model requested preview; automatic read loop stopped"
    return "assistant tool loop stopped before producing a successful answer"


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


ModelTurnFn = Callable[[str, AssistantSettings, dict[str, Any] | None], ModelTurnResult]


def run_read_only_agent_loop(
    text: str,
    *,
    settings: AssistantSettings,
    conversation_context: dict[str, Any] | None,
    model_turn_fn: ModelTurnFn | None = None,
    request: AssistantRequest | None = None,
    execute_tool_fn: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    now_fn: Callable[[], date] | None = None,
    max_steps: int = MAX_TOOL_PLAN_STEPS,
) -> AgentLoopResult:
    """Run or adapt the bounded assistant model-turn loop."""
    steps = max(1, min(int(max_steps), MAX_TOOL_PLAN_STEPS))
    today = _planner_today(now_fn)
    loop_context = _with_temporal_context(conversation_context, today=today)
    model_turn_entry_fn = model_turn_fn or create_model_turn_events
    model_turn_result = model_turn_entry_fn(text, settings, loop_context)
    model_turn_result = _normalize_model_turn_result(model_turn_result, question=text, today=today)
    if model_turn_result.event_plan is None:
        error = model_turn_result.error or _no_tool_plan_error()
        trace = dict(model_turn_result.trace)
        trace["agent_loop"] = _direct_model_turn_rejection_trace(
            error=error,
            max_steps=steps,
        )
        return AgentLoopResult(
            planning=AgentLoopPlanningOutcome(
                perception=None,
                trace=dict(trace),
                error=error,
            ),
            trace=trace,
            steps=(),
        )
    if request is None or execute_tool_fn is None:
        error = AgentToolError(
            code="TOOL_LOOP_CONTEXT_MISSING",
            message="assistant tool_call event loop requires request and execute_tool_fn.",
        )
        trace = dict(model_turn_result.trace)
        trace["agent_loop"] = _direct_model_turn_rejection_trace(
            error=error,
            max_steps=steps,
        )
        return AgentLoopResult(
            planning=AgentLoopPlanningOutcome(
                perception=None,
                trace=dict(trace),
                error=error,
            ),
            trace=trace,
            steps=(),
        )
    return _direct_model_turn_loop_result(
        text=text,
        request=request,
        execute_tool_fn=execute_tool_fn,
        settings=settings,
        conversation_context=loop_context,
        model_turn_result=model_turn_result,
        max_steps=steps,
    )


def _direct_model_turn_loop_result(
    *,
    text: str,
    request: AssistantRequest,
    execute_tool_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
    settings: AssistantSettings,
    conversation_context: dict[str, Any] | None,
    model_turn_result: ModelTurnResult,
    max_steps: int,
) -> AgentLoopResult:
    event_plan = model_turn_result.event_plan
    if event_plan is None:
        raise AssertionError("event_plan is required for assistant tool_call event loop")
    provider = normalize_llm_provider(str(event_plan.provider or settings.llm.provider or "openai"))
    if model_turn_result.error is not None:
        trace = dict(model_turn_result.trace)
        trace["agent_loop"] = _direct_model_turn_rejection_trace(
            error=model_turn_result.error,
            max_steps=max_steps,
        )
        return AgentLoopResult(
            planning=AgentLoopPlanningOutcome(
                perception=None,
                trace=dict(trace),
                error=model_turn_result.error,
            ),
            trace=trace,
            steps=(),
        )
    continuation_response_fn = _assistant_tool_loop_continuation_response_fn(
        provider=provider,
        llm_settings=settings.llm,
    )
    task_contract = _assistant_tool_loop_task_contract(
        question=text,
        request=request,
        loop_payload={"task_contract": dict(event_plan.task_contract or {})},
        conversation_context=conversation_context,
    )
    validation_error = _direct_model_turn_validation_error(
        text=text,
        event_plan=event_plan,
        recoverable_observation_enabled=continuation_response_fn is not None,
    )
    if validation_error is not None:
        trace = dict(model_turn_result.trace)
        trace["agent_loop"] = _direct_model_turn_rejection_trace(
            error=validation_error,
            max_steps=max_steps,
        )
        return AgentLoopResult(
            planning=AgentLoopPlanningOutcome(
                perception=None,
                trace=dict(trace),
                error=validation_error,
            ),
            trace=trace,
            steps=(),
        )
    continuation_base_payload = (
        _assistant_tool_loop_continuation_base_payload(
            provider=provider,
            question=text,
            conversation_context=conversation_context,
            llm_settings=settings.llm,
        )
        if continuation_response_fn is not None
        else None
    )
    outcome = run_assistant_tool_event_loop(
        question=text,
        request=request,
        task_contract=task_contract,
        initial_events=event_plan.events,
        execute_tool_fn=execute_tool_fn,
        context_validation=event_plan.context_validation,
        provider=provider,
        create_continuation_response_fn=continuation_response_fn,
        continuation_base_payload=continuation_base_payload,
    )
    tool_loop_result = _build_tool_loop_response_from_outcome(
        outcome=outcome,
        question=text,
        command_id=None,
        task_contract=task_contract,
    )
    steps = _agent_loop_steps_from_model_events(
        events=outcome.events,
        question=text,
        task_contract=task_contract,
        provider=provider,
        context_validation=event_plan.context_validation,
    )
    trace = dict(model_turn_result.trace)
    trace["agent_loop"] = _direct_model_turn_agent_loop_trace(
        outcome=outcome,
        max_steps=max_steps,
        steps=steps,
        final_response=dict(tool_loop_result.get("data", {}).get("final_response") or {})
        if isinstance(tool_loop_result.get("data"), dict)
        else {},
    )
    planning = AgentLoopPlanningOutcome(
        perception=PerceptionResult(
            intent_name="tool_loop",
            arguments={
                "events": event_transcript_payload(event_plan.events),
                "task_contract": task_contract,
                "provider": provider,
            },
            source="agent_loop_events",
            confidence=1.0,
        ),
        trace=dict(trace),
    )
    return AgentLoopResult(
        planning=planning,
        trace=trace,
        steps=steps,
        tool_loop_result=tool_loop_result,
    )


def _direct_model_turn_validation_error(
    *,
    text: str,
    event_plan: EventNativePlanningResult,
    recoverable_observation_enabled: bool,
) -> AgentToolError | None:
    model_events = tuple(event for event in event_plan.events if isinstance(event, ModelToolCallEvent))
    if not model_events:
        return None
    validation_error = _validate_model_tool_call_events(model_events, question=text)
    if validation_error is None:
        return None
    if validation_error.code == "PLAN_RISK_MISMATCH" and _defer_preview_boundary_mismatch_to_tool_guard(
        validation_error,
        text=text,
        event_plan=event_plan,
    ):
        return None
    if recoverable_observation_enabled and validation_error.code == "INPUT_ERROR":
        return None
    if recoverable_observation_enabled and _allow_model_turn_guard_observation(validation_error, question=text, events=model_events):
        return None
    return validation_error


def _defer_preview_boundary_mismatch_to_tool_guard(
    error: AgentToolError,
    *,
    text: str,
    event_plan: EventNativePlanningResult,
) -> bool:
    details = error.details if isinstance(error.details, dict) else {}
    if details.get("preview_capabilities"):
        return True
    if _question_requests_preview_operation(text):
        return True
    task_contract = event_plan.task_contract if isinstance(event_plan.task_contract, dict) else {}
    return str(task_contract.get("requested_effect") or "").strip() in {"preview", "preview_write"}


def _agent_loop_steps_from_model_events(
    *,
    events: tuple[Any, ...],
    question: str,
    task_contract: dict[str, Any],
    provider: str,
    context_validation: dict[str, Any] | None = None,
) -> tuple[AgentLoopStep, ...]:
    steps: list[AgentLoopStep] = []
    for event in events:
        model_event: ModelToolCallEvent | None = None
        if isinstance(event, ModelToolCallEvent):
            model_event = event
        if model_event is None:
            continue
        steps.append(
            _agent_loop_step_from_model_event(
                index=len(steps) + 1,
                event=model_event,
                question=question,
                task_contract=task_contract,
                context_validation=context_validation,
            )
        )
    return tuple(steps)


def _direct_model_turn_agent_loop_trace(
    *,
    outcome: AssistantToolLoopOutcome,
    max_steps: int,
    steps: tuple[AgentLoopStep, ...],
    final_response: dict[str, Any],
) -> dict[str, Any]:
    loop_trace = dict(outcome.trace)
    continuation_count = int(loop_trace.get("continuation_count") or 0)
    return {
        **loop_trace,
        "schema_version": AGENT_LOOP_SCHEMA_VERSION,
        "enabled": True,
        "runtime": "model_turn_loop",
        "model_turns": 1 + continuation_count,
        "max_steps": int(max_steps),
        "steps_used": len(steps),
        "writes_allowed": False,
        "preview_operations_allowed": True,
        "steps": [step.public_payload() for step in steps],
        "final_response": final_response
        or FinalResponsePlan(
            status="rendered",
            reason=outcome.stop_reason or outcome.status,
            canonical_renderer_required=not bool(outcome.final_answer),
            llm_may_summarize=bool(outcome.final_answer),
        ).public_payload(),
    }


def _direct_model_turn_rejection_trace(
    *,
    error: AgentToolError,
    max_steps: int,
) -> dict[str, Any]:
    status = "needs_clarification" if error.code == "NEEDS_CLARIFICATION" else "rejected"
    return {
        "schema_version": AGENT_LOOP_SCHEMA_VERSION,
        "enabled": True,
        "runtime": "model_turn_loop",
        "model_turns": 1,
        "max_steps": int(max_steps),
        "steps_used": 0,
        "writes_allowed": False,
        "preview_operations_allowed": True,
        "steps": [],
        "loop_stop_reason": str(error.code or "validation_rejected").lower(),
        "status": status,
        "error_code": error.code,
        "final_response": FinalResponsePlan(
            status=status,
            reason=str(error.message or error.code or "model turn rejected by host validation"),
        ).public_payload(),
    }


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


def _pre_tool_check(
    *,
    tool_name: str,
    payload: dict[str, Any],
    plan_arguments: dict[str, Any] | None = None,
    task_contract: dict[str, Any] | None = None,
    action_policy: dict[str, Any],
    action_safety: dict[str, Any] | None = None,
) -> dict[str, Any]:
    planner_banned = sorted(_banned_plan_argument_paths(plan_arguments)) if plan_arguments is not None else []
    allowed_arguments = _allowed_plan_arguments(tool_name)
    planner_extra = (
        sorted(str(key) for key in (plan_arguments or {}) if allowed_arguments and str(key) not in allowed_arguments)
        if plan_arguments is not None
        else []
    )
    schema_payload = _plan_schema_payload(tool_name, plan_arguments if plan_arguments is not None else payload or {})
    schema_error = _plan_tool_input_schema_error(
        tool_name=tool_name,
        payload=schema_payload,
        enforce_required=_plan_step_kind(tool_name) == "preview",
    )
    schema_status = "fail" if schema_error is not None else ("pass" if schema_payload else "not_applicable")
    action_status = "pass" if action_policy.get("allowed") else "deny"
    safety_payload = action_safety if isinstance(action_safety, dict) else {}
    safety_status = str(safety_payload.get("status") or "not_applicable")
    safety_check_status = "pass" if safety_status in {"allow", "allow_followup", "allow_preview"} else safety_status
    planner_status = "fail" if planner_banned or planner_extra else ("pass" if plan_arguments is not None else "not_applicable")
    write_status = "pass" if str(action_policy.get("allowed_effect") or "") in {"read", "none", "preview"} else "fail"
    scope_status, scope_reason = _scope_guard_status(payload=payload, task_contract=task_contract)
    status = "pass"
    if action_status == "deny":
        status = "deny"
    if (
        planner_status == "fail"
        or schema_status == "fail"
        or write_status == "fail"
        or scope_status == "fail"
        or safety_check_status in {"deny", "ask", "suspicious", "fail"}
    ):
        status = "fail"
    return {
        "schema_version": TOOL_CHECK_SCHEMA_VERSION,
        "stage": "pre_tool",
        "status": status,
        "checks": [
            {"name": "action_policy", "status": action_status},
            {
                "name": "action_safety",
                "status": safety_check_status,
                "code": safety_payload.get("code"),
                "route": safety_payload.get("route"),
            },
            {"name": "planner_argument_guard", "status": planner_status},
            {
                "name": "input_schema",
                "status": schema_status,
                "error": build_error_payload(schema_error) if schema_error is not None else None,
            },
            {"name": "scope_guard", "status": scope_status, "reason": scope_reason},
            {"name": "write_guard", "status": write_status},
        ],
        "tool_name": str(tool_name or ""),
        "payload_keys": sorted(str(key) for key in (payload or {}).keys()),
        "banned_arguments": planner_banned,
        "extra_arguments": planner_extra,
        "schema_error": build_error_payload(schema_error) if schema_error is not None else None,
        "action_safety": dict(safety_payload) if safety_payload else {},
    }


def _risk_class_from_action_policy(action_policy: dict[str, Any]) -> str:
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


def _scope_guard_status(*, payload: dict[str, Any], task_contract: dict[str, Any] | None) -> tuple[str, str | None]:
    scope = task_contract.get("scope") if isinstance(task_contract, dict) else None
    requested = [str(item).strip().lower() for item in (scope or {}).get("requested_accounts") or [] if str(item).strip()]
    if not requested:
        return "not_applicable", None
    provided = _payload_accounts(payload)
    if not provided:
        return "not_applicable", None
    out_of_scope = sorted(account for account in provided if account not in requested)
    if out_of_scope:
        return "fail", "account_out_of_task_scope:" + ",".join(out_of_scope)
    return "pass", None


def _payload_accounts(payload: dict[str, Any]) -> list[str]:
    values: list[str] = []
    account = payload.get("account") if isinstance(payload, dict) else None
    accounts = payload.get("accounts") if isinstance(payload, dict) else None
    if isinstance(account, str):
        values.append(account)
    elif isinstance(account, list):
        values.extend(str(item) for item in account)
    if isinstance(accounts, str):
        values.append(accounts)
    elif isinstance(accounts, list):
        values.extend(str(item) for item in accounts)
    return _unique_strings([str(value).strip().lower() for value in values if str(value).strip()])


def _post_tool_check(*, tool_name: str, payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    output_contract = resolve_output_contract(tool_name, payload)
    ok = bool(result.get("ok", False)) if isinstance(result, dict) else False
    evidence_summary = _tool_evidence_summary(tool_name=tool_name, payload=payload, result=result)
    data = result.get("data") if isinstance(result, dict) else None
    contract_status, missing_contract_fields = _evidence_contract_status(output_contract)
    freshness_status = _freshness_check_status(output_contract=output_contract, data=data)
    missing_data_count = _tool_missing_data_count(data)
    missing_data_status = "warning" if missing_data_count else "pass"
    checks = [
        {"name": "result_status", "status": "pass" if ok else "fail"},
        {"name": "output_contract", "status": "pass" if output_contract else "not_declared"},
        {
            "name": "evidence_contract",
            "status": contract_status,
            "missing_fields": missing_contract_fields,
        },
    ]
    if output_contract.get("freshness_fields"):
        checks.append({"name": "freshness", "status": freshness_status})
    if output_contract.get("missing_data_fields") or missing_data_count:
        checks.append({"name": "missing_data", "status": missing_data_status, "count": missing_data_count})
    status = _post_tool_check_status(ok=ok, checks=checks)
    return {
        "schema_version": TOOL_CHECK_SCHEMA_VERSION,
        "stage": "post_tool",
        "status": status,
        "checks": checks,
        "tool_name": str(tool_name or ""),
        "output_contract_present": bool(output_contract),
        "evidence_summary": evidence_summary,
    }


def _post_tool_check_status(*, ok: bool, checks: list[dict[str, Any]]) -> str:
    if not ok:
        return "fail"
    statuses = {str(check.get("status") or "") for check in checks}
    if "fail" in statuses:
        return "fail"
    if "warning" in statuses or "not_declared" in statuses:
        return "warning"
    return "pass"


def _evidence_contract_status(output_contract: dict[str, Any]) -> tuple[str, list[str]]:
    if not output_contract:
        return "not_declared", []
    required = ["source_label", "canonical_renderer", "fact_fields"]
    if str(output_contract.get("result_shape") or "").strip().lower() != "scalar":
        required.append("primary_rows")
    missing = [field for field in required if not output_contract.get(field)]
    return ("warning" if missing else "pass"), missing


def _freshness_check_status(*, output_contract: dict[str, Any], data: Any) -> str:
    if not output_contract.get("freshness_fields"):
        return "not_applicable"
    if _tool_missing_data_count(data):
        return "warning"
    if not isinstance(data, dict):
        return "warning"
    quote_refresh = data.get("quote_refresh")
    if isinstance(quote_refresh, dict):
        status = str(quote_refresh.get("status") or "").strip().lower()
        if status in {"missing_quote", "stale", "expired", "failed", "error"}:
            return "warning"
    rows = data.get("rows")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            quote_status = str(row.get("quote_status") or "").strip().lower()
            if quote_status in {"missing_quote", "stale", "expired", "failed", "error"}:
                return "warning"
    return "pass"


def _tool_evidence_summary(*, tool_name: str, payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    output_contract = resolve_output_contract(tool_name, payload)
    data = result.get("data") if isinstance(result, dict) else None
    primary_rows = str(output_contract.get("primary_rows") or "").strip()
    row_count_field = str(output_contract.get("row_count_field") or "").strip()
    row_count = None
    if isinstance(data, dict):
        if row_count_field and row_count_field in data:
            row_count = data.get(row_count_field)
        elif primary_rows and isinstance(data.get(primary_rows), list):
            row_count = len(data[primary_rows])
    summary = {
        "tool_name": str(tool_name or ""),
        "source_label": output_contract.get("source_label"),
        "canonical_renderer": output_contract.get("canonical_renderer"),
        "guard_profile": output_contract.get("guard_profile"),
        "result_shape": output_contract.get("result_shape"),
        "primary_rows": primary_rows or None,
        "row_count": row_count,
        "fact_field_count": len(output_contract.get("fact_fields") or []) if isinstance(output_contract.get("fact_fields"), list) else 0,
        "missing_data_count": _tool_missing_data_count(data),
    }
    return {key: value for key, value in summary.items() if value is not None}


def _tool_missing_data_count(data: Any) -> int:
    if not isinstance(data, dict):
        return 0
    count = 0
    missing = data.get("missing_data")
    if isinstance(missing, list):
        count += len(missing)
    elif isinstance(missing, dict):
        count += len(missing)
    quote_refresh = data.get("quote_refresh")
    if isinstance(quote_refresh, dict) and isinstance(quote_refresh.get("missing_symbols"), list):
        count += len(quote_refresh["missing_symbols"])
    return count


def _read_tool_loop_guard(
    *,
    tool_name: str,
    payload: dict[str, Any],
    task_contract: dict[str, Any],
    attempted_signatures: set[str],
) -> dict[str, Any]:
    tool_name = str(tool_name or "")
    signature = _tool_loop_duplicate_signature(tool_name, payload)
    kind = _plan_step_kind(tool_name)
    definition = get_tool_definition(tool_name)
    if kind is None and definition is None:
        return {
            "schema_version": TOOL_CHECK_SCHEMA_VERSION,
            "allowed": False,
            "decision": "unknown_tool",
            "reason": "requested tool is not available in the assistant tool manifest",
            "tool_name": tool_name,
            "risk_class": "UNKNOWN",
            "duplicate_signature": signature,
            "scope_source": _scope_source_for_guard(task_contract),
            "error_code": "UNKNOWN_TOOL",
        }
    if kind != "read":
        return {
            "schema_version": TOOL_CHECK_SCHEMA_VERSION,
            "allowed": False,
            "decision": "not_read_auto",
            "reason": "automatic tool loop only executes READ_AUTO tools",
            "tool_name": tool_name,
            "risk_class": "SOFT_WRITE_PREVIEW" if kind == "preview" else "UNKNOWN",
            "duplicate_signature": signature,
            "scope_source": _scope_source_for_guard(task_contract),
            "error_code": "PERMISSION_DENIED",
        }
    requested_effect = str(task_contract.get("requested_effect") or "read").strip()
    if requested_effect != "read":
        return {
            "schema_version": TOOL_CHECK_SCHEMA_VERSION,
            "allowed": False,
            "decision": "write_boundary",
            "reason": "task contract requested effect is not read",
            "tool_name": tool_name,
            "risk_class": "READ_AUTO",
            "duplicate_signature": signature,
            "scope_source": _scope_source_for_guard(task_contract),
            "requested_effect": requested_effect,
            "error_code": "PERMISSION_DENIED",
        }
    if signature and signature in attempted_signatures:
        return {
            "schema_version": TOOL_CHECK_SCHEMA_VERSION,
            "allowed": False,
            "decision": "duplicate_call",
            "reason": "read tool call repeats an earlier normalized payload",
            "tool_name": tool_name,
            "risk_class": "READ_AUTO",
            "duplicate_signature": signature,
            "scope_source": _scope_source_for_guard(task_contract),
            "error_code": "DUPLICATE_TOOL_CALL",
        }
    return {
        "schema_version": TOOL_CHECK_SCHEMA_VERSION,
        "allowed": True,
        "decision": "allow",
        "reason": "read_auto_in_scope",
        "tool_name": tool_name,
        "risk_class": "READ_AUTO",
        "duplicate_signature": signature,
        "scope_source": _scope_source_for_guard(task_contract),
    }


def _tool_loop_duplicate_signature(tool_name: str, payload: Any) -> str:
    if not isinstance(payload, dict):
        return str(tool_name or "")
    return _followup_step_signature(str(tool_name or ""), payload)


def _scope_source_for_guard(task_contract: dict[str, Any]) -> str:
    if not isinstance(task_contract, dict):
        return "unknown"
    if bool(task_contract.get("planner_declared")):
        return "planner_declared"
    scope = task_contract.get("scope")
    if isinstance(scope, dict) and any(scope.values()):
        return "task_contract"
    return "system_injected"


def _evidence_gap_allows_followup(gap: dict[str, Any]) -> bool:
    if gap.get("recoverable") is False:
        return False
    recoverable_by = str(gap.get("recoverable_by") or "").strip().lower()
    if not recoverable_by:
        return False
    if recoverable_by in _BLOCKED_FOLLOWUP_RECOVERABLE_BY:
        return False
    suggested_tool = str(gap.get("suggested_tool") or "").strip()
    if not suggested_tool:
        return False
    if suggested_tool not in pure_read_tool_names():
        return False
    return True


def _followup_decision_contract(*, evidence_gaps: list[dict[str, Any]]) -> dict[str, Any]:
    allowed_tools = _followup_allowed_tools(evidence_gaps)
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


def _followup_allowed_tools(evidence_gaps: list[dict[str, Any]]) -> list[str]:
    allowed: set[str] = set()
    for gap in evidence_gaps:
        if not isinstance(gap, dict):
            continue
        suggested_tool = str(gap.get("suggested_tool") or "").strip()
        if not suggested_tool:
            continue
        allowed.add(suggested_tool)
        if suggested_tool == "analysis_query" or str(gap.get("recoverable_by") or "") == "analysis_query":
            allowed.add("analysis_catalog")
    pure_read = pure_read_tool_names()
    return sorted(tool for tool in allowed if tool in pure_read)


def _followup_plan_step_tool_names(plan: Any) -> list[str]:
    if isinstance(plan, dict):
        raw_steps = plan.get("steps")
        if not isinstance(raw_steps, list):
            return []
        return [str(step.get("tool_name") or "") for step in raw_steps if isinstance(step, dict)]
    steps = getattr(plan, "steps", ())
    return [str(getattr(step, "tool_name", "") or "") for step in steps]


def _followup_tool_allowlist_rejection(plan: Any, *, evidence_gaps: list[dict[str, Any]]) -> str:
    allowed_tools = set(_followup_allowed_tools(evidence_gaps))
    for tool_name in _followup_plan_step_tool_names(plan):
        if tool_name not in allowed_tools:
            return f"follow-up plan used {tool_name}, which is not allowed for the recoverable evidence gap"
    return ""


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
        if not _is_system_injected_signature_argument(str(key))
    }
    if not comparable:
        return str(tool_name or "")
    return f"{tool_name}:{json.dumps(comparable, ensure_ascii=False, sort_keys=True, default=str)}"


def _is_system_injected_signature_argument(key: str) -> bool:
    return _is_banned_plan_argument(key) or str(key or "") in {
        "assistant_config_path",
        "audit_db",
        "command_id",
        "config_key",
        "config_path",
        "data_config",
        "message_id",
    }


def _normalized_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", str(sql or "").strip().lower())


def create_model_turn_events(
    text: str,
    settings: AssistantSettings,
    conversation_context: dict[str, Any] | None,
    *,
    create_tool_call_response_fn: CreateToolCallResponseFn | None = None,
    environ: dict[str, str] | None = None,
) -> ModelTurnResult:
    return _create_model_turn_events(
        text,
        settings,
        conversation_context,
        create_tool_call_response_fn=create_tool_call_response_fn,
        environ=environ,
    )


def _create_model_turn_events(
    text: str,
    settings: AssistantSettings,
    conversation_context: dict[str, Any] | None,
    *,
    create_tool_call_response_fn: CreateToolCallResponseFn | None = None,
    environ: dict[str, str] | None = None,
) -> ModelTurnResult:
    llm_settings = settings.llm
    if not llm_settings.enabled:
        return ModelTurnResult(trace=_llm_trace(llm_settings, attempted=False, reason="disabled"))

    missing = missing_llm_config(llm_settings)
    if missing:
        return ModelTurnResult(
            trace=_llm_trace(llm_settings, attempted=False, reason="missing_config", missing=missing),
            error=AgentToolError(
                code="LLM_UNAVAILABLE",
                message="LLM AgentLoop is enabled but not fully configured.",
                hint="Set assistant.llm.provider, assistant.llm.model, and assistant.llm.api_key_env, or disable assistant.agent_loop.enabled.",
                details={"missing": missing},
            ),
        )

    provider = normalize_llm_provider(llm_settings.provider)
    if not is_supported_llm_provider(provider):
        return ModelTurnResult(
            trace=_llm_trace(llm_settings, attempted=False, reason="unsupported_provider"),
            error=unsupported_llm_provider_error(llm_settings, component="planner"),
        )

    api_key = llm_api_key_value(llm_settings, environ=environ)
    if not api_key:
        return ModelTurnResult(
            trace=_llm_trace(llm_settings, attempted=False, reason="missing_api_key", missing=["api_key"]),
            error=AgentToolError(
                code="LLM_UNAVAILABLE",
                message="LLM planner API key is not configured.",
                hint=f"Set {llm_settings.api_key_env} in the local env file or process environment.",
                details={"api_key_env": llm_settings.api_key_env},
            ),
        )

    planner_payload = _planner_input_payload(text, conversation_context=conversation_context)
    frame_delta_result = _model_turn_result_from_frame_delta(
        text=text,
        settings=settings,
        conversation_context=conversation_context,
        planner_payload=planner_payload,
        provider=provider,
    )
    if frame_delta_result is not None:
        return frame_delta_result
    response_fn = create_tool_call_response_fn or provider_create_tool_call_response_fn(provider)
    attempt = _model_event_provider_attempt(
        planner_payload=planner_payload,
        response_fn=response_fn,
        api_key=api_key,
        provider=provider,
        llm_settings=llm_settings,
    )
    base_trace = _llm_trace(
        llm_settings,
        attempted=True,
        reason=attempt.reason,
        error_code=attempt.error.code if attempt.error is not None else None,
        schema_version=TOOL_PLAN_SCHEMA_VERSION if attempt.error is None and attempt.events else None,
        conversation_context=conversation_context,
        planner_input=attempt.planner_input,
    )
    base_trace["event_model"] = {
        "schema_version": "om-assistant-event-planner-v1",
        "provider": provider,
        "api_kind": provider_api_kind(provider),
        "event_count": len(attempt.events),
        "events": event_transcript_payload(attempt.events) if attempt.events else [],
        "legacy_json_plan_used": False,
    }
    if attempt.error is not None:
        return ModelTurnResult(trace=base_trace, error=attempt.error)

    event_plan, event_error = _event_native_planning_from_model_events(
        text=text,
        events=attempt.events,
        conversation_context=conversation_context,
        planner_manifest=planner_payload.get("tools") if isinstance(planner_payload.get("tools"), list) else [],
        provider=provider,
    )
    if event_error is not None:
        base_trace["reason"] = "invalid_model_event" if event_error.code == "INVALID_MODEL_EVENT" else _planner_error_trace_reason(event_error)
        base_trace["error_code"] = event_error.code
        if event_plan is not None:
            base_trace["planner_context_use"] = _safe_context_use_payload(event_plan.context_use)
            base_trace["context_validation"] = event_plan.context_validation
            base_trace["event_plan"] = event_plan.public_payload()
        return ModelTurnResult(trace=base_trace, error=event_error, event_plan=event_plan)
    if event_plan is None:
        error = _invalid_model_event_error("模型没有生成可执行工具调用，未执行工具。")
        base_trace["reason"] = "invalid_model_event"
        base_trace["error_code"] = error.code
        return ModelTurnResult(trace=base_trace, error=error)

    base_trace["planner_context_use"] = _safe_context_use_payload(event_plan.context_use)
    base_trace["context_validation"] = event_plan.context_validation
    base_trace["event_plan"] = event_plan.public_payload()
    return ModelTurnResult(trace={**base_trace, "reason": "accepted"}, event_plan=event_plan)


def _model_turn_result_from_frame_delta(
    *,
    text: str,
    settings: AssistantSettings,
    conversation_context: dict[str, Any] | None,
    planner_payload: dict[str, Any],
    provider: str,
) -> ModelTurnResult | None:
    value = _short_scalar_set_value(text)
    if value is _NO_FRAME_DELTA_VALUE:
        return None
    projection = _planner_payload_context_projection(planner_payload)
    frames = [
        frame
        for frame in projection.get("active_frames") or []
        if isinstance(frame, dict)
        and frame.get("type") == "symbol_setting"
        and "set_value" in {str(item) for item in frame.get("allowed_deltas") or []}
    ]
    if not frames:
        return None
    planner_input = _planner_input_trace(planner_payload, json.dumps(planner_payload, ensure_ascii=False, sort_keys=True))
    if len(frames) != 1:
        context_use = _ambiguous_context_use(
            current_slots={"setting_new_value": [value]},
            reason="multiple_active_setting_frames",
            question="当前追问可能引用多个配置项，请明确要修改哪个标的和字段。",
        )
        validation = validate_context_use(
            current_user_message=text,
            context_projection=projection,
            plan_payload={"context_use": context_use, "steps": []},
            planner_manifest=planner_payload.get("tools") if isinstance(planner_payload.get("tools"), list) else [],
        )
        error = _context_validation_error(validation) or AgentToolError(
            code="PLAN_CONTEXT_AMBIGUOUS",
            message="当前追问可能引用多个配置项，请明确要修改哪个标的和字段。",
        )
        trace = _llm_trace(
            settings.llm,
            attempted=False,
            reason=_planner_error_trace_reason(error),
            error_code=error.code,
            conversation_context=conversation_context,
            planner_input=planner_input,
        )
        trace["planner_context_use"] = _safe_context_use_payload(context_use)
        trace["context_validation"] = validation
        return ModelTurnResult(trace=trace, error=error)

    frame = frames[0]
    symbol = str(frame.get("symbol") or "").strip()
    setting_path = str(frame.get("setting_path") or "").strip()
    if not symbol or not setting_path:
        return None
    arguments = {"symbol": symbol, "set": {setting_path: value}}
    event = ModelToolCallEvent(
        event_id="host_frame_delta_symbol_edit",
        tool_call_id="host_frame_delta_symbol_edit",
        tool_name="symbol_edit",
        arguments=arguments,
        purpose="apply scalar follow-up to active symbol setting frame",
        provider="host",
        parent_event_id="user_message_1",
    )
    validation_error = _validate_model_tool_call_events((event,), question=text)
    if validation_error is not None:
        return None

    context_use = _context_use_for_frame_delta(frame=frame, value=value)
    plan_payload = _model_tool_calls_plan_like_payload(
        goal=text,
        events=(event,),
        required_capabilities=_required_capabilities_for_model_tool_calls((event,)),
        task_contract=None,
        context_use=context_use,
    )
    today = _planner_today_from_context(conversation_context)
    host_contract = build_task_contract(question=text, plan=plan_payload, request_context=None, today=today)
    task_contract = _planner_task_contract_from_host_contract(host_contract.public_payload())
    event_plan = EventNativePlanningResult(
        events=(event,),
        task_contract=task_contract,
        required_capabilities=_required_capabilities_for_model_tool_calls((event,)),
        context_use=context_use,
        provider=provider,
        goal=str(text or "").strip(),
    )
    validation = validate_context_use(
        current_user_message=text,
        context_projection=projection,
        plan_payload=event_plan.plan_like_payload(),
        planner_manifest=planner_payload.get("tools") if isinstance(planner_payload.get("tools"), list) else [],
    )
    event_plan = replace(event_plan, context_validation=validation)
    context_error = _context_validation_error(validation)
    trace = _llm_trace(
        settings.llm,
        attempted=False,
        reason="frame_delta",
        schema_version=TOOL_PLAN_SCHEMA_VERSION,
        conversation_context=conversation_context,
        planner_input=planner_input,
    )
    trace["event_model"] = {
        "schema_version": "om-assistant-event-planner-v1",
        "provider": "host",
        "api_kind": "deterministic",
        "event_count": 1,
        "events": event_transcript_payload((event,)),
        "legacy_json_plan_used": False,
    }
    trace["planner_context_use"] = _safe_context_use_payload(event_plan.context_use)
    trace["context_validation"] = event_plan.context_validation
    trace["event_plan"] = event_plan.public_payload()
    if context_error is not None:
        trace["reason"] = _planner_error_trace_reason(context_error)
        trace["error_code"] = context_error.code
        return ModelTurnResult(trace=trace, error=context_error, event_plan=event_plan)
    return ModelTurnResult(trace={**trace, "reason": "accepted"}, event_plan=event_plan)


_NO_FRAME_DELTA_VALUE = object()


def _short_scalar_set_value(text: str) -> object:
    compact = re.sub(r"\s+", "", str(text or "").lower())
    match = re.fullmatch(r"(改为|改成|设为|设置成|调到|改到|降到|升到)(true|false|on|off|[0-9]+(?:\.[0-9]+)?)", compact)
    if not match:
        return _NO_FRAME_DELTA_VALUE
    raw = match.group(2)
    if raw in {"true", "on"}:
        return True
    if raw in {"false", "off"}:
        return False
    return float(raw) if "." in raw else int(raw)


def _planner_payload_context_projection(planner_payload: dict[str, Any]) -> dict[str, Any]:
    context = planner_payload.get("context") if isinstance(planner_payload.get("context"), dict) else {}
    projection = context.get("context_projection") if isinstance(context.get("context_projection"), dict) else {}
    return projection


def _context_use_for_frame_delta(*, frame: dict[str, Any], value: object) -> dict[str, Any]:
    inherited_slots: dict[str, list[Any]] = {}
    for key in ("symbol", "market", "strategy", "setting_path", "setting_field"):
        item = frame.get(key)
        if item not in (None, ""):
            inherited_slots[key] = [item]
    return {
        "schema_version": PLANNER_CONTEXT_USE_SCHEMA_VERSION,
        "mode": "frame_delta",
        "referenced_turn_ids": [str(frame.get("turn_id"))] if frame.get("turn_id") else [],
        "referenced_evidence_refs": [str(frame.get("source_ref_id"))] if frame.get("source_ref_id") else [],
        "referenced_frame_ids": [str(frame.get("frame_id"))] if frame.get("frame_id") else [],
        "inherited_slots": inherited_slots,
        "current_message_slots": {"setting_new_value": [value]},
        "override_slots": {},
        "delta": {"type": "set_value", "value": value},
        "requires_clarification": False,
        "clarification_question": None,
    }


def _model_event_provider_attempt(
    *,
    planner_payload: dict[str, Any],
    response_fn: CreateToolCallResponseFn,
    api_key: str,
    provider: str,
    llm_settings: AssistantLlmSettings,
) -> _ModelEventProviderAttempt:
    planner_input_text = json.dumps(planner_payload, ensure_ascii=False, sort_keys=True)
    planner_input_trace = _planner_input_trace(planner_payload, planner_input_text)
    tools = _provider_tool_call_tools(provider, planner_payload)
    try:
        response = response_fn(
            api_key=api_key,
            base_url=llm_settings.base_url,
            model=llm_settings.model,
            input_text=planner_input_text,
            instructions=_model_event_planner_instructions(),
            tools=tools,
            timeout=int(llm_settings.timeout_seconds),
            max_output_tokens=int(llm_settings.max_output_tokens),
        )
    except (OpenAIResponsesError, OpenAIChatCompletionsError) as err:
        return _ModelEventProviderAttempt(
            response=None,
            planner_input=planner_input_trace,
            reason="provider_error",
            error=AgentToolError(
                code="LLM_PROVIDER_ERROR",
                message=str(err),
                details={"provider": provider, "http_status": err.http_status},
            ),
        )
    except Exception as err:
        return _ModelEventProviderAttempt(
            response=None,
            planner_input=planner_input_trace,
            reason="provider_error",
            error=AgentToolError(
                code="LLM_PROVIDER_ERROR",
                message=f"LLM planner provider failed: {type(err).__name__}: {err}",
                details={"provider": provider},
            ),
        )
    try:
        events = model_events_from_provider_response(response, provider=provider, parent_event_id="user_message_1")
    except AgentToolError as err:
        return _ModelEventProviderAttempt(
            response=response,
            planner_input=planner_input_trace,
            reason="invalid_model_event",
            error=err,
        )
    if not events:
        return _ModelEventProviderAttempt(
            response=response,
            planner_input=planner_input_trace,
            events=(),
            reason="invalid_model_event",
            error=_invalid_model_event_error("模型没有生成结构化 tool call，未执行工具。"),
        )
    return _ModelEventProviderAttempt(
        response=response,
        planner_input=planner_input_trace,
        events=events,
        reason="accepted",
    )


def _event_native_planning_from_model_events(
    *,
    text: str,
    events: tuple[ModelToolCallEvent | ModelFinalAnswerEvent | AssistantEvent, ...],
    conversation_context: dict[str, Any] | None,
    planner_manifest: list[dict[str, Any]],
    provider: str,
) -> tuple[EventNativePlanningResult | None, AgentToolError | None]:
    first = events[0] if events else None
    if isinstance(first, ModelToolCallEvent):
        tool_calls = tuple(event for event in events if isinstance(event, ModelToolCallEvent))
        if len(tool_calls) > MAX_TOOL_PLAN_STEPS:
            return None, AgentToolError(
                code="PLAN_TOO_MANY_STEPS",
                message=f"这个问题需要超过 {MAX_TOOL_PLAN_STEPS} 次工具调用，请拆分问题。",
                details={"max_steps": MAX_TOOL_PLAN_STEPS, "steps": len(tool_calls)},
            )
        return _event_native_planning_from_model_tool_calls(
            text=text,
            events=tool_calls,
            conversation_context=conversation_context,
            planner_manifest=planner_manifest,
            provider=provider,
        )
    if isinstance(first, AssistantEvent) and first.event_type == "clarification_request":
        return None, AgentToolError(
            code="NEEDS_CLARIFICATION",
            message=str(first.payload.get("question") or first.payload.get("message") or "需要补充查询范围。"),
            details={"clarification_request": first.public_payload()},
        )
    if isinstance(first, ModelFinalAnswerEvent):
        return _event_native_planning_from_model_final_answer(
            text=text,
            event=first,
            conversation_context=conversation_context,
            provider=provider,
        ), None
    return None, _invalid_model_event_error("模型没有生成可执行工具调用，未执行工具。")


def _event_native_planning_from_model_final_answer(
    *,
    text: str,
    event: ModelFinalAnswerEvent,
    conversation_context: dict[str, Any] | None,
    provider: str,
) -> EventNativePlanningResult:
    today = _planner_today_from_context(conversation_context)
    plan_payload = {
        "goal": text,
        "steps": [],
        "task_contract": {
            "goal": text,
            "requested_effect": "read",
        },
    }
    host_contract = build_task_contract(question=text, plan=plan_payload, request_context=None, today=today)
    task_contract = _planner_task_contract_from_host_contract(host_contract.public_payload())
    return EventNativePlanningResult(
        events=(event,),
        task_contract=task_contract,
        required_capabilities=(),
        context_use=_default_context_use(),
        provider=provider,
        goal=str(text or "").strip(),
    )


def _event_native_planning_from_model_tool_calls(
    *,
    text: str,
    events: tuple[ModelToolCallEvent, ...],
    conversation_context: dict[str, Any] | None,
    planner_manifest: list[dict[str, Any]],
    provider: str,
) -> tuple[EventNativePlanningResult | None, AgentToolError | None]:
    today = _planner_today_from_context(conversation_context)
    normalized_events = _normalize_model_tool_call_events(events, question=text, today=today)
    validation_error = _validate_model_tool_call_events(normalized_events, question=text)
    if validation_error is not None and not _allow_model_turn_guard_observation(
        validation_error,
        question=text,
        events=normalized_events,
    ):
        return None, validation_error

    initial_payload = _model_tool_calls_plan_like_payload(
        goal=text,
        events=normalized_events,
        required_capabilities=_required_capabilities_for_model_tool_calls(normalized_events),
        task_contract=None,
        context_use=_default_context_use(),
    )
    host_contract = build_task_contract(
        question=text,
        plan=initial_payload,
        request_context=None,
        today=today,
    )
    task_contract = _planner_task_contract_from_host_contract(host_contract.public_payload())
    context_use = _context_use_for_model_tool_calls(
        question=text,
        events=normalized_events,
        conversation_context=conversation_context,
        task_contract=task_contract,
    )
    event_plan = EventNativePlanningResult(
        events=normalized_events,
        task_contract=task_contract,
        required_capabilities=_required_capabilities_for_model_tool_calls(normalized_events),
        context_use=context_use,
        context_validation=None,
        provider=provider,
        goal=str(text or "").strip(),
    )
    validation_payload = event_plan.plan_like_payload()
    if _safe_context_use_payload(context_use).get("mode") == "ambiguous":
        validation_payload["steps"] = []
    context_validation = validate_context_use(
        current_user_message=text,
        context_projection=conversation_context.get("context_projection")
        if isinstance(conversation_context, dict) and isinstance(conversation_context.get("context_projection"), dict)
        else None,
        plan_payload=validation_payload,
        planner_manifest=planner_manifest,
    )
    event_plan = replace(event_plan, context_validation=context_validation)
    context_error = _context_validation_error(context_validation)
    if context_error is not None:
        return event_plan, context_error
    return event_plan, None


def _allow_model_turn_guard_observation(
    error: AgentToolError,
    *,
    question: str,
    events: tuple[ModelToolCallEvent, ...],
) -> bool:
    if not events:
        return False
    if error.code == "INPUT_ERROR":
        return True
    if error.code == "PERMISSION_DENIED":
        details = error.details if isinstance(error.details, dict) else {}
        if details.get("banned_arguments"):
            return True
        tool_name = str(details.get("tool_name") or "").strip()
        if tool_name and _plan_step_kind(tool_name) is None:
            return True
    if error.code != "PLAN_RISK_MISMATCH":
        return False
    return True


def _model_tool_calls_plan_like_payload(
    *,
    goal: str,
    events: tuple[ModelToolCallEvent, ...],
    required_capabilities: tuple[str, ...],
    task_contract: dict[str, Any] | None,
    context_use: dict[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "om-event-native-planning-v1",
        "goal": str(goal or "").strip(),
        "required_capabilities": list(required_capabilities),
        "context_use": _safe_context_use_payload(context_use),
        "steps": [_model_tool_call_plan_step_payload(event) for event in events],
    }
    if isinstance(task_contract, dict) and task_contract:
        payload["task_contract"] = _safe_task_contract_payload(task_contract)
    return payload


def _model_tool_call_plan_step_payload(event: ModelToolCallEvent) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": event.tool_call_id or event.event_id,
        "tool_name": event.tool_name,
        "arguments": _safe_plan_arguments_for_context(event),
    }
    if event.purpose:
        payload["purpose"] = event.purpose
    return payload


def _safe_plan_arguments_for_context(event: ModelToolCallEvent) -> dict[str, Any]:
    arguments = dict(event.arguments)
    payload = _safe_tool_payload(arguments)
    if event.tool_name == "symbol_edit":
        sets = arguments.get("set")
        if isinstance(sets, dict):
            safe_sets = {
                str(key): value
                for key, value in sets.items()
                if str(key).strip() and (isinstance(value, (str, int, float, bool)) or value is None)
            }
            if safe_sets:
                payload["set"] = safe_sets
        ensure_use = arguments.get("ensure_use")
        if isinstance(ensure_use, list):
            safe_use = [str(item) for item in ensure_use if str(item).strip()][:8]
            if safe_use:
                payload["ensure_use"] = safe_use
    return payload


def _normalize_model_tool_call_events(
    events: tuple[ModelToolCallEvent, ...],
    *,
    question: str,
    today: date,
) -> tuple[ModelToolCallEvent, ...]:
    months = _extract_month_filters(question, today=today)
    month = months[0] if len(months) == 1 else None
    detail_requested = _question_requests_income_detail(question)
    all_history_requested = _question_requests_all_income_history(question)
    all_accounts_requested = _question_requests_all_accounts(question)
    normalized: list[ModelToolCallEvent] = []
    monthly_step_index = 0
    monthly_step_count = sum(1 for event in events if event.tool_name == "monthly_income_report")
    for event in events:
        arguments = dict(event.arguments)
        changed = False
        if event.tool_name == "monthly_income_report":
            monthly_step_index += 1
            purpose_months = _extract_month_filters(event.purpose, today=today)
            if all_accounts_requested and "account" in arguments:
                arguments.pop("account", None)
                changed = True
            if all_history_requested and not months and "month" in arguments:
                arguments.pop("month", None)
                changed = True
            elif len(months) > 1 and monthly_step_count == 1 and "month" in arguments:
                arguments.pop("month", None)
                changed = True
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
        normalized.append(replace(event, arguments=arguments) if changed else event)
    return tuple(normalized)


def _validate_model_tool_call_events(
    events: tuple[ModelToolCallEvent, ...],
    *,
    question: str | None = None,
    allow_preview: bool = True,
    preview_authority: dict[str, Any] | None = None,
) -> AgentToolError | None:
    if not events:
        return _no_tool_plan_error()
    kinds: list[str] = []
    for event in events:
        step_kind = _plan_step_kind(event.tool_name)
        if step_kind is None:
            return AgentToolError(
                code="PERMISSION_DENIED",
                message=f"{event.tool_name} is not allowed in assistant event planner",
                hint="AgentLoop planning is restricted to read-only capabilities plus explicitly allowed preview capabilities.",
                details={"allowed_tools": sorted(AGENT_LOOP_ALLOWED_TOOLS), "tool_name": event.tool_name},
            )
        kinds.append(step_kind)
        banned = sorted(_banned_plan_argument_paths(event.arguments))
        if banned:
            return AgentToolError(
                code="PERMISSION_DENIED",
                message="model tool call contains system-scoped or path arguments",
                hint="config/path/runtime fields are injected by the system, not by LLM tool calls.",
                details={"tool_name": event.tool_name, "banned_arguments": banned},
            )
        allowed_args = _allowed_plan_arguments(event.tool_name)
        extra = sorted(str(key) for key in event.arguments if allowed_args and str(key) not in allowed_args)
        if extra:
            return AgentToolError(
                code="INPUT_ERROR",
                message=f"model tool call has unsupported arguments for {event.tool_name}: {', '.join(extra)}",
                details={"tool_name": event.tool_name, "allowed_arguments": sorted(allowed_args), "extra_arguments": extra},
            )
        schema_error = _plan_tool_input_schema_error(
            tool_name=event.tool_name,
            payload=_plan_schema_payload(event.tool_name, event.arguments),
            enforce_required=step_kind == "preview",
        )
        if schema_error is not None:
            return schema_error
    if question is not None and _question_requests_symbol_config_read(question):
        planned_tools = [event.tool_name for event in events]
        if "analysis_query" in planned_tools and "symbol_config_read" not in planned_tools:
            return AgentToolError(
                code="PLAN_RISK_MISMATCH",
                message="当前问题需要查询监控标的配置字段，不能用通用分析查询替代。",
                hint="请改用 symbol_config_read，并传入 symbol、strategy 和 field。",
                details={
                    "planned_tools": planned_tools,
                    "required_tool": "symbol_config_read",
                    "misused_tool": "analysis_query",
                },
            )
    if question is not None and _question_requests_cash_headroom(question):
        planned_tools = [event.tool_name for event in events]
        if "query_cash_headroom" not in planned_tools:
            return AgentToolError(
                code="PLAN_RISK_MISMATCH",
                message="当前问题需要现金余量证据，不能用其他查询替代。",
                hint="请改用 query_cash_headroom，并传入 account。",
                details={
                    "planned_tools": planned_tools,
                    "required_tool": "query_cash_headroom",
                    "misused_tool": planned_tools[0] if planned_tools else None,
                },
            )
    preview_count = sum(1 for kind in kinds if kind == "preview")
    read_count = sum(1 for kind in kinds if kind == "read")
    authority = _preview_authority_for_question(question, preview_authority=preview_authority)
    allowed_preview_intents = _allowed_preview_intents_from_authority(authority)
    if preview_count:
        if not allow_preview:
            return AgentToolError(
                code="PERMISSION_DENIED",
                message="preview-write capabilities cannot be executed through assistant.tool_loop",
                details={"preview_capabilities": sorted(event.tool_name for event in events if _plan_step_kind(event.tool_name) == "preview")},
            )
        preview_tools = sorted(event.tool_name for event in events if _plan_step_kind(event.tool_name) == "preview")
        if question is not None and not bool(authority.get("allowed", False)):
            return AgentToolError(
                code="PLAN_RISK_MISMATCH",
                message="这不是明确的写入预览请求，不能进入预览确认流程。",
                hint="preview-write 只能用于明确的记录、修改、升级或模型切换请求；普通查询必须规划为只读工具。",
                details={"preview_capabilities": preview_tools},
            )
        disallowed_preview_tools = [tool_name for tool_name in preview_tools if allowed_preview_intents and tool_name not in allowed_preview_intents]
        if disallowed_preview_tools:
            return AgentToolError(
                code="PLAN_RISK_MISMATCH",
                message="当前消息只授权有限的写入预览能力，不能使用其它预览工具。",
                hint="请改用授权的 preview capability，或先向用户澄清。",
                details={
                    "preview_capabilities": preview_tools,
                    "allowed_preview_intents": sorted(allowed_preview_intents),
                    "disallowed_preview_capabilities": disallowed_preview_tools,
                },
            )
        if preview_count > 1 or read_count:
            return AgentToolError(
                code="PLAN_UNSUPPORTED_COMPOSITION",
                message="一次聊天计划只能包含纯只读分析，或一个写入预览操作。",
                hint="请把查询分析和写入预览拆成两条消息。",
                details={"read_steps": read_count, "preview_steps": preview_count},
            )
    elif question is not None and _question_requests_preview_operation(question):
        return AgentToolError(
            code="PLAN_RISK_MISMATCH",
            message="这像写入预览请求，需要进入对应的预览确认流程。",
            hint="请重新规划为对应的 preview capability；不要把记录交易、成交提醒或配置修改降级成持仓/收益查询。",
            details={"planned_tools": [event.tool_name for event in events]},
        )
    return None


def _context_use_for_model_tool_calls(
    *,
    question: str,
    events: tuple[ModelToolCallEvent, ...],
    conversation_context: dict[str, Any] | None,
    task_contract: dict[str, Any],
) -> dict[str, Any]:
    context_use = _default_context_use()
    plan_slots = _model_tool_call_safe_slots_for_context(events)
    current_slots = _current_message_slots_for_model_plan(
        question=question,
        plan_slots=plan_slots,
        task_contract=task_contract,
    )
    context_use["current_message_slots"] = current_slots
    inherited_slots = _slot_delta(plan_slots, current_slots)
    if _model_tool_calls_have_current_required_scope(events=events, current_slots=current_slots):
        inherited_slots = _slots_requiring_context_source(inherited_slots)
    if not inherited_slots:
        return context_use

    projection = _context_projection_from_conversation(conversation_context)
    if not projection or not _question_is_contextual_followup(question):
        return context_use
    sources = _context_sources_for_model_plan(projection)
    if not sources:
        return _ambiguous_context_use(
            current_slots=current_slots,
            reason="followup_context_missing",
            question="当前追问需要沿用上下文，但没有可见的上一轮证据范围。请明确账户、标的、月份或上一轮结果。",
        )
    if len(sources) > 1 and not current_slots:
        return _ambiguous_context_use(
            current_slots=current_slots,
            reason="multiple_context_sources_without_current_slot",
            question="当前追问可能引用多轮上下文，请明确要沿用哪一轮、哪个账户/标的/月度。",
        )

    matches = [source for source in sources if _slot_values_subset(inherited_slots, source.get("safe_slots", {}))]
    if len(matches) != 1:
        return _ambiguous_context_use(
            current_slots=current_slots,
            reason="context_source_not_unique",
            question="当前追问缺少足够上下文，请明确要沿用的账户、标的、月份或上一轮结果。",
        )

    source = matches[0]
    context_use["mode"] = "carry"
    context_use["referenced_turn_ids"] = _unique_strings(source.get("turn_ids") or [])
    context_use["referenced_evidence_refs"] = _unique_strings(source.get("evidence_refs") or [])
    context_use["inherited_slots"] = inherited_slots
    return context_use


def _model_tool_call_safe_slots_for_context(events: tuple[ModelToolCallEvent, ...]) -> dict[str, list[Any]]:
    slots: dict[str, list[Any]] = {}
    for event in events:
        for key, value in dict(event.arguments or {}).items():
            slot_key = str(key or "").strip()
            if slot_key not in SAFE_SLOT_KEYS:
                continue
            for item in _context_slot_values(value):
                _add_context_slot(slots, slot_key, item)
        if event.tool_name == "symbol_edit":
            slots = _merge_context_slots(slots, _symbol_edit_setting_slots_for_context(dict(event.arguments or {})))
    return slots


def _symbol_edit_setting_slots_for_context(arguments: dict[str, Any]) -> dict[str, list[Any]]:
    sets = arguments.get("set") if isinstance(arguments.get("set"), dict) else {}
    slots: dict[str, list[Any]] = {}
    for raw_path, value in sets.items():
        path = str(raw_path or "").strip()
        if not path:
            continue
        _add_context_slot(slots, "setting_path", path)
        parts = [part for part in path.split(".") if part]
        if parts:
            _add_context_slot(slots, "setting_field", parts[-1])
        if len(parts) >= 2:
            _add_context_slot(slots, "strategy", parts[0])
        if isinstance(value, (str, int, float, bool)) or value is None:
            _add_context_slot(slots, "setting_new_value", value)
    return slots


def _merge_context_slots(left: dict[str, list[Any]], right: dict[str, list[Any]]) -> dict[str, list[Any]]:
    out = {key: list(values) for key, values in left.items()}
    for key, values in right.items():
        for value in values:
            _add_context_slot(out, key, value)
    return out


def _model_tool_calls_have_current_required_scope(
    *,
    events: tuple[ModelToolCallEvent, ...],
    current_slots: dict[str, list[Any]],
) -> bool:
    saw_required_scope = False
    for event in events:
        required_slots = _required_safe_slots_for_model_tool_call(event)
        if not required_slots:
            continue
        saw_required_scope = True
        if not all(current_slots.get(slot_key) for slot_key in required_slots):
            return False
    return saw_required_scope


def _slots_requiring_context_source(slots: dict[str, list[Any]]) -> dict[str, list[Any]]:
    return {
        key: list(values)
        for key, values in slots.items()
        if key not in _CURRENT_SCOPE_OPTIONAL_FILTER_SLOTS and values
    }


def _required_safe_slots_for_model_tool_call(event: ModelToolCallEvent) -> tuple[str, ...]:
    binding = planner_binding_for_tool(str(event.tool_name or "").strip())
    if binding is None:
        return ()
    return tuple(str(key) for key in binding.required_arguments if str(key) in SAFE_SLOT_KEYS)


def _provider_tool_call_tools(provider: str, planner_payload: dict[str, Any]) -> list[dict[str, Any]]:
    manifest = planner_payload.get("tools") if isinstance(planner_payload.get("tools"), list) else []
    if provider_api_kind(provider) == "chat_completions":
        return chat_completions_tools_payload(manifest, read_only_only=False)
    return openai_responses_tools_payload(manifest, read_only_only=False)


def _required_capabilities_for_model_tool_calls(events: tuple[ModelToolCallEvent, ...]) -> tuple[str, ...]:
    capabilities: list[str] = []
    for event in events:
        capabilities.extend(_required_capabilities_for_model_tool_call(event))
    return tuple(_unique_strings(capabilities))


def _required_capabilities_for_model_tool_call(event: ModelToolCallEvent) -> tuple[str, ...]:
    tool_name = str(event.tool_name or "").strip()
    if tool_name == "monthly_income_report":
        return ("income_report",)
    if tool_name == "analysis_query":
        return ("analysis_query",)
    if tool_name == "candidate_filter_explain":
        return ("filter_explain",)
    binding = planner_binding_for_tool(tool_name)
    if binding is not None and binding.intent_name:
        return (str(binding.intent_name),)
    return (tool_name,) if tool_name else ()


def _current_message_slots_for_model_plan(
    *,
    question: str,
    plan_slots: dict[str, list[Any]],
    task_contract: dict[str, Any],
) -> dict[str, list[Any]]:
    slots: dict[str, list[Any]] = {}
    scope = task_contract.get("scope") if isinstance(task_contract.get("scope"), dict) else {}
    for scope_key, slot_key in (
        ("requested_accounts", "account"),
        ("requested_symbols", "symbol"),
        ("requested_months", "month"),
    ):
        for item in _context_slot_values(scope.get(scope_key)):
            if _slot_value_in(item, plan_slots.get(slot_key, [])):
                _add_context_slot(slots, slot_key, item)
    for key, values in plan_slots.items():
        for value in values:
            if _context_slot_value_in_question(question, value, slot_key=key):
                _add_context_slot(slots, key, value)
    return slots


def _slot_delta(left: dict[str, list[Any]], right: dict[str, list[Any]]) -> dict[str, list[Any]]:
    delta: dict[str, list[Any]] = {}
    for key, values in left.items():
        for value in values:
            if not _slot_value_in(value, right.get(key, [])):
                _add_context_slot(delta, key, value)
    return delta


def _context_projection_from_conversation(conversation_context: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(conversation_context, dict):
        return {}
    projection = conversation_context.get("context_projection")
    return projection if isinstance(projection, dict) else {}


def _context_sources_for_model_plan(projection: dict[str, Any]) -> list[dict[str, Any]]:
    refs = [item for item in projection.get("available_evidence_refs") or [] if isinstance(item, dict)]
    if refs:
        sources: list[dict[str, Any]] = []
        for ref in refs:
            ref_id = str(ref.get("ref_id") or "").strip()
            turn_id = str(ref.get("turn_id") or "").strip()
            source = {
                "turn_ids": [turn_id] if turn_id else [],
                "evidence_refs": [ref_id] if ref_id else [],
                "safe_slots": _context_slot_mapping(ref.get("safe_slots")),
            }
            if source["safe_slots"]:
                sources.append(source)
        return sources

    sources = []
    for turn in projection.get("recent_turns") or []:
        if not isinstance(turn, dict):
            continue
        turn_id = str(turn.get("turn_id") or "").strip()
        source = {
            "turn_ids": [turn_id] if turn_id else [],
            "evidence_refs": _unique_strings(turn.get("evidence_refs") or []),
            "safe_slots": _context_slot_mapping(turn.get("safe_slots")),
        }
        if source["safe_slots"]:
            sources.append(source)
    return sources


def _ambiguous_context_use(*, current_slots: dict[str, list[Any]], reason: str, question: str) -> dict[str, Any]:
    context_use = _default_context_use()
    context_use["mode"] = "ambiguous"
    context_use["current_message_slots"] = current_slots
    context_use["requires_clarification"] = True
    context_use["clarification_question"] = question
    context_use["reason"] = reason
    return context_use


def _question_is_contextual_followup(question: str) -> bool:
    compact = re.sub(r"\s+", "", str(question or "").lower())
    if not compact:
        return False
    tokens = (
        "继续",
        "再看",
        "再拆",
        "进一步",
        "这个",
        "上个",
        "上面",
        "刚才",
        "前面",
        "接着",
        "补证据",
        "怎么算",
        "为什么",
        "原因",
        "改为",
        "改成",
        "设置成",
        "设为",
        "调到",
        "改到",
        "降到",
        "升到",
    )
    return any(token in compact for token in tokens) or compact in {"继续", "继续分析", "继续解释"}


def _context_slot_mapping(value: Any) -> dict[str, list[Any]]:
    if not isinstance(value, dict):
        return {}
    slots: dict[str, list[Any]] = {}
    for key, raw_values in value.items():
        slot_key = str(key or "").strip()
        if not slot_key:
            continue
        for item in _context_slot_values(raw_values):
            _add_context_slot(slots, slot_key, item)
    return slots


def _context_slot_values(value: Any) -> list[Any]:
    if isinstance(value, list):
        values = value
    elif isinstance(value, tuple):
        values = list(value)
    elif isinstance(value, set):
        values = sorted(value)
    else:
        values = [value]
    return [item for item in values if item not in ("", None, [], {})]


def _add_context_slot(slots: dict[str, list[Any]], key: str, value: Any) -> None:
    if value in ("", None):
        return
    bucket = slots.setdefault(str(key), [])
    if not _slot_value_in(value, bucket):
        bucket.append(value)


def _slot_values_subset(left: dict[str, list[Any]], right: dict[str, list[Any]]) -> bool:
    for key, values in left.items():
        for value in values:
            if not _slot_value_in(value, right.get(key, [])):
                return False
    return True


def _slot_value_in(value: Any, values: list[Any]) -> bool:
    normalized = _normalized_context_slot_value(value)
    return any(_normalized_context_slot_value(item) == normalized for item in values)


def _normalized_context_slot_value(value: Any) -> str:
    return str(value).strip().lower()


def _context_slot_value_in_question(question: str, value: Any, *, slot_key: str | None = None) -> bool:
    text = str(question or "")
    value_text = str(value or "").strip()
    if not value_text:
        return False
    if re.fullmatch(r"[A-Za-z0-9_.-]+", value_text):
        return (
            re.search(rf"(?<![A-Za-z0-9_.-]){re.escape(value_text)}(?![A-Za-z0-9_.-])", text, re.IGNORECASE)
            is not None
            or (
                slot_key in {"strategy", "setting_path", "setting_field"}
                and _setting_slot_value_in_question(text, value_text)
            )
        )
    return value_text in text


_SETTING_SLOT_ALIASES = {
    "sellput": ("sellput", "卖put"),
    "sellcall": ("sellcall", "卖call"),
    "coveredcall": ("coveredcall", "备兑"),
    "maxstrike": ("maxstrike", "最大行权价", "最高行权价", "行权价上限"),
    "minstrike": ("minstrike", "最小行权价", "最低行权价", "行权价下限"),
    "enabled": ("enabled", "启用", "开启", "打开", "禁用", "关闭"),
}
_SETTING_PATH_TOKENS = {
    "sellputmaxstrike": ("sellput", "maxstrike"),
    "sellputenabled": ("sellput", "enabled"),
    "sellcallminstrike": ("sellcall", "minstrike"),
    "sellcallenabled": ("sellcall", "enabled"),
    "coveredcallminstrike": ("coveredcall", "minstrike"),
    "coveredcallenabled": ("coveredcall", "enabled"),
    "sellput": ("sellput",),
    "sellcall": ("sellcall",),
    "coveredcall": ("coveredcall",),
    "maxstrike": ("maxstrike",),
    "minstrike": ("minstrike",),
    "enabled": ("enabled",),
}


def _setting_slot_value_in_question(question: str, value_text: str) -> bool:
    tokens = _SETTING_PATH_TOKENS.get(_compact_setting_text(value_text))
    if not tokens:
        return False
    compact_question = _compact_setting_text(question)
    return all(any(alias in compact_question for alias in _SETTING_SLOT_ALIASES[token]) for token in tokens)


def _compact_setting_text(value: Any) -> str:
    return re.sub(r"[\s_.-]+", "", str(value or "").strip().lower())


def _planner_task_contract_from_host_contract(contract: dict[str, Any]) -> dict[str, Any]:
    return {
        key: contract[key]
        for key in (
            "schema_version",
            "goal",
            "domain",
            "task_mode",
            "requested_effect",
            "intent_families",
            "scope",
            "required_answer",
            "optional_answer",
            "required_evidence",
            "answer_shape",
            "constraints",
        )
        if key in contract
    }


def _invalid_model_event_error(message: str) -> AgentToolError:
    return AgentToolError(
        code="INVALID_MODEL_EVENT",
        message=message,
        hint="请换一种更明确的问法，或稍后重试；本次没有执行任何工具。",
    )


def _context_validation_error(validation: dict[str, Any] | None) -> AgentToolError | None:
    if not isinstance(validation, dict):
        return None
    status = str(validation.get("status") or "")
    if status == "passed":
        return None
    if status == "ask_clarification":
        return AgentToolError(
            code="PLAN_CONTEXT_AMBIGUOUS",
            message=_context_validation_clarification_text(validation),
            hint="请明确要沿用哪一轮、哪个账户/标的/月度，或把当前问题改成自包含查询。",
            details={
                "context_validation": dict(validation),
                "requires_user_clarification": True,
                "model_turn_recoverable": False,
            },
        )
    if status == "blocked":
        return AgentToolError(
            code="PLAN_CONTEXT_INVALID",
            message="Planner 对上一轮上下文的引用不安全，已阻止执行工具。",
            hint="需要重新规划为带显式引用的上下文使用，或向用户澄清要沿用哪一轮范围。",
            details={
                "context_validation": dict(validation),
                "requires_user_clarification": False,
                "model_turn_recoverable": True,
            },
        )
    return AgentToolError(
        code="PLAN_CONTEXT_INVALID",
        message="Planner 上下文校验未通过，已阻止执行工具。",
        details={"context_validation": dict(validation), "model_turn_recoverable": False},
    )


def _context_validation_clarification_text(validation: dict[str, Any]) -> str:
    violation = validation.get("violation") if isinstance(validation.get("violation"), dict) else {}
    reason = str(violation.get("reason") or "").strip()
    if reason == "planner_declared_ambiguity":
        return "上一轮上下文不明确，请确认要沿用哪一轮范围。"
    if reason == "context_projection_truncated_without_reference":
        return "可见上下文已被裁剪，请明确要沿用的账户、标的、月份或上一轮结果。"
    if reason == "carry_context_without_reference_and_overlapping_slots":
        return "有多轮上下文都可能匹配当前追问，请确认要沿用哪一个范围。"
    return "当前追问缺少足够上下文，请明确要沿用哪一轮范围。"


def _planner_error_trace_reason(err: AgentToolError) -> str:
    if err.code == "PLAN_CONTEXT_AMBIGUOUS":
        return "context_validation_ask_clarification"
    if err.code == "PLAN_CONTEXT_INVALID":
        return "context_validation_blocked"
    if err.code == "NEEDS_CLARIFICATION":
        return "no_plan"
    return "invalid_plan"


def _default_context_use() -> dict[str, Any]:
    return {
        "schema_version": PLANNER_CONTEXT_USE_SCHEMA_VERSION,
        "mode": "none",
        "referenced_turn_ids": [],
        "referenced_evidence_refs": [],
        "referenced_frame_ids": [],
        "inherited_slots": {},
        "current_message_slots": {},
        "override_slots": {},
        "delta": {},
        "requires_clarification": False,
        "clarification_question": None,
    }


def _normalized_context_use(value: Any) -> dict[str, Any]:
    out = _default_context_use()
    if not isinstance(value, dict):
        return out
    mode = str(value.get("mode") or "").strip()
    if mode in PLANNER_CONTEXT_USE_MODES:
        out["mode"] = mode
    out["referenced_turn_ids"] = _context_use_string_list(value.get("referenced_turn_ids"))
    out["referenced_evidence_refs"] = _context_use_string_list(value.get("referenced_evidence_refs"))
    out["referenced_frame_ids"] = _context_use_string_list(value.get("referenced_frame_ids"))
    out["inherited_slots"] = _context_use_slots(value.get("inherited_slots"))
    out["current_message_slots"] = _context_use_slots(value.get("current_message_slots"))
    out["override_slots"] = _context_use_slots(value.get("override_slots"))
    delta = value.get("delta") if isinstance(value.get("delta"), dict) else {}
    delta_type = str(delta.get("type") or "").strip()
    if delta_type:
        out["delta"] = {
            "type": _clip_context_use_text(delta_type, 80),
            "value": delta.get("value") if isinstance(delta.get("value"), (str, int, float, bool)) or delta.get("value") is None else None,
        }
    out["requires_clarification"] = bool(value.get("requires_clarification"))
    clarification = str(value.get("clarification_question") or "").strip()
    out["clarification_question"] = _clip_context_use_text(clarification, 240) if clarification else None
    reason = str(value.get("reason") or "").strip()
    if reason:
        out["reason"] = _clip_context_use_text(reason, 240)
    return out


def _safe_context_use_payload(value: Any) -> dict[str, Any]:
    return _normalized_context_use(value)


def _clip_context_use_text(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: max(0, limit - 3)] + "..."


def _context_use_string_list(value: Any, *, limit: int = 20) -> list[str]:
    values = value if isinstance(value, list) else []
    out: list[str] = []
    for item in values[:limit]:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(_clip_context_use_text(text, 120))
    return out


def _context_use_slots(value: Any) -> dict[str, list[Any]]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[Any]] = {}
    for key, raw_values in value.items():
        slot_key = str(key or "").strip()
        if not slot_key:
            continue
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        bucket: list[Any] = []
        for item in values[:20]:
            if isinstance(item, dict):
                continue
            normalized: Any = item if isinstance(item, (int, float, bool)) else _clip_context_use_text(str(item or "").strip(), 120)
            if normalized not in ("", None) and normalized not in bucket:
                bucket.append(normalized)
        if bucket:
            out[slot_key] = bucket
    return out


def _plan_step_kind(tool_name: str) -> str | None:
    name = str(tool_name or "")
    if name in AGENT_LOOP_READ_TOOLS:
        return "read"
    if name in AGENT_LOOP_PREVIEW_CAPABILITIES:
        spec = _COMMAND_SPECS_BY_INTENT.get(name)
        if spec is not None and is_llm_planner_preview_spec(spec):
            return "preview"
    return None


def _allowed_plan_arguments(tool_name: str) -> set[str]:
    if tool_name in AGENT_LOOP_READ_TOOLS:
        definition = get_tool_definition(tool_name)
        return _filter_plan_arguments(definition.input_schema) if definition is not None else set()
    spec = _COMMAND_SPECS_BY_INTENT.get(tool_name)
    if spec is None or not is_llm_planner_preview_spec(spec):
        return set()
    allowed = _filter_plan_arguments(spec.arguments)
    if tool_name in {"manual_trade_open", "manual_trade_close", "manual_assignment", "manual_expiry"}:
        allowed.add("account")
        allowed.add("raw_text")
    if tool_name == "manual_assignment":
        allowed.update(_LIFECYCLE_ASSIGNMENT_MODEL_SCHEMA)
    if tool_name == "manual_expiry":
        allowed.update(_LIFECYCLE_EXPIRY_MODEL_SCHEMA)
    return allowed


def _plan_tool_input_schema(tool_name: str) -> dict[str, Any]:
    if tool_name in AGENT_LOOP_READ_TOOLS:
        definition = get_tool_definition(tool_name)
        return (
            build_tool_input_json_schema(
                definition.input_schema,
                additional_properties=True,
            )
            if definition is not None
            else {}
        )
    spec = _COMMAND_SPECS_BY_INTENT.get(tool_name)
    if spec is None or not is_llm_planner_preview_spec(spec):
        return {}
    return build_tool_input_json_schema(
        _planner_preview_input_schema(tool_name),
        additional_properties=True,
    )


def _plan_schema_payload(tool_name: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(payload or {})
    if tool_name in {"manual_trade_open", "manual_trade_close", "manual_assignment", "manual_expiry"}:
        out.pop("raw_text", None)
    return out


def _plan_tool_input_schema_error(
    *,
    tool_name: str,
    payload: dict[str, Any],
    enforce_required: bool = False,
) -> AgentToolError | None:
    schema = _plan_tool_input_schema(tool_name)
    if not schema:
        return None
    try:
        validate_tool_input_payload(
            tool_name=tool_name,
            payload=_plan_schema_payload(tool_name, payload),
            schema=schema,
            enforce_required=enforce_required,
        )
    except AgentToolError as err:
        return err
    return None


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
    return _preview_authority_requires_preview(_preview_authority_for_question(question))


def _preview_authority_for_question(
    question: str | None,
    *,
    preview_authority: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(preview_authority, dict) and preview_authority:
        return dict(preview_authority)
    if question is None:
        return {"allowed": False, "mode": "read", "allowed_preview_intents": []}
    return preview_authority_from_text(question)


def _preview_authority_requires_preview(authority: dict[str, Any]) -> bool:
    return bool(authority.get("allowed", False)) and str(authority.get("mode") or "") == "explicit"


def _allowed_preview_intents_from_authority(authority: dict[str, Any]) -> set[str]:
    return {str(item) for item in authority.get("allowed_preview_intents") or () if str(item).strip()}


def _question_requests_symbol_config_read(question: str) -> bool:
    if _question_requests_preview_operation(question):
        return False
    compact = re.sub(r"\s+", "", str(question or "").lower())
    if any(token in compact for token in ("maxstrike", "max_strike", "minstrike", "min_strike")):
        return True
    option_strategy = any(
        token in compact
        for token in ("sellput", "sellcall", "coveredcall", "sell_put", "sell_call", "covered_call", "卖put", "卖call")
    )
    if option_strategy and any(token in compact for token in ("最大行权价", "最高行权价", "最低行权价", "最小行权价", "行权价上限", "行权价下限")):
        return True
    return bool(
        any(token in compact for token in ("当前配置", "现在配置", "配置是多少", "配置的是多少"))
        and option_strategy
    )


def _question_requests_cash_headroom(question: str) -> bool:
    compact = re.sub(r"\s+", "", str(question or "").lower())
    if not any(token in compact for token in ("sellput", "sell_put", "卖put", "卖出put", "shortput")):
        return False
    return any(token in compact for token in ("现金加货基", "现金类", "担保金", "现金担保", "资金", "cash", "超过", "余量", "缺口"))


def _planner_omitted_read_tools_for_question(question: str) -> frozenset[str]:
    if _question_requests_cash_headroom(question):
        return frozenset()
    return frozenset({"query_cash_headroom"})


def _preview_precheck_clarification_error(precheck: dict[str, Any] | None) -> AgentToolError | None:
    if not isinstance(precheck, dict) or precheck.get("status") != "fail":
        return None
    safety = precheck.get("action_safety") if isinstance(precheck.get("action_safety"), dict) else {}
    safety_code = str(safety.get("code") or "")
    if safety_code == "missing_account_scope":
        message = "需要明确账户后才能创建写入预览。"
    elif safety_code == "missing_symbol_scope":
        message = "需要明确标的后才能创建写入预览。"
    else:
        return None
    return AgentToolError(
        code="NEEDS_CLARIFICATION",
        message=message,
        details={
            "clarification_request": _clarification_request_payload(message),
            "precheck": precheck,
        },
    )


def _planned_step_precheck_error(steps: tuple[AgentLoopStep, ...]) -> AgentToolError | None:
    for step in steps:
        precheck = step.precheck if isinstance(step.precheck, dict) else {}
        if precheck.get("status") != "fail":
            continue
        return AgentToolError(
            code="PRE_TOOL_CHECK_FAILED",
            message="该请求未通过执行前安全检查。",
            details={"tool_name": step.tool_name, "precheck": precheck},
        )
    return None


def _agent_loop_step_from_model_event(
    *,
    index: int,
    event: ModelToolCallEvent,
    question: str = "",
    task_contract: dict[str, Any] | None = None,
    context_validation: dict[str, Any] | None = None,
) -> AgentLoopStep:
    kind = _plan_step_kind(event.tool_name)
    action_policy_payload: dict[str, Any] | None = None
    action_safety_payload: dict[str, Any] | None = None
    precheck: dict[str, Any] | None = None
    hook_results: tuple[dict[str, Any], ...] = ()
    if kind == "preview":
        call = ToolCall(tool_name=event.tool_name, payload=dict(event.arguments))
        action_policy = decide_tool_action_policy(
            call=call,
            request=None,
            task_contract=None,
            source="agent_loop_events",
            tool_policy=DEFAULT_TOOL_POLICY,
        )
        action_policy_payload = action_policy.public_payload()
        action_safety = assess_action_safety(
            question=question,
            task_contract=task_contract,
            tool_name=event.tool_name,
            payload=dict(event.arguments),
            action_policy=action_policy_payload,
            context_validation=context_validation,
            source="agent_loop_events",
        )
        action_safety_payload = action_safety.public_payload()
        precheck = _pre_tool_check(
            tool_name=event.tool_name,
            payload=dict(event.arguments),
            plan_arguments=dict(event.arguments),
            task_contract=task_contract,
            action_policy=action_policy_payload,
            action_safety=action_safety_payload,
        )
        hook_results = tuple(hook_results_from_tool_check(precheck))
    return AgentLoopStep(
        index=index,
        phase="model_event",
        status="planned",
        intent_name=event.tool_name if kind == "preview" else None,
        tool_name=event.tool_name,
        arguments=dict(event.arguments),
        purpose=event.purpose,
        action_policy=action_policy_payload,
        action_safety=action_safety_payload,
        precheck=precheck,
        hook_results=hook_results,
    )


def _safe_tool_payload(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "action",
        "broker",
        "config_key",
        "account",
        "accounts",
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
        "market_scope",
        "sql",
        "query",
        "view",
        "views",
        "symbol",
        "set",
        "strategy",
        "field",
        "function",
        "market",
        "model_profile",
        "target_version",
        "operation_id",
        "operation_resolution",
        "updates",
        "option_type",
        "side",
        "position_side",
        "contracts_to_close",
        "strike",
        "expiration",
        "expiration_ymd",
        "stock_side",
        "stock_qty",
        "stock_price",
        "event_time_ms",
        "as_of_ms",
        "record_id",
    }
    return {key: payload[key] for key in sorted(allowed) if key in payload}


def _safe_task_contract_payload(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "schema_version",
        "goal",
        "domain",
        "task_mode",
        "requested_effect",
        "intent_families",
        "scope",
        "required_answer",
        "optional_answer",
        "required_evidence",
        "answer_shape",
        "constraints",
        "ambiguities",
    }
    return {key: payload[key] for key in sorted(allowed) if key in payload}


def _safe_selected_recipe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "name",
        "domains",
        "task_modes",
        "evidence_needs",
        "primary_views",
        "source_tools",
        "external_evidence",
        "followup_tool",
        "answer_shape",
        "match_source",
        "reason",
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


def _normalize_model_turn_result(result: ModelTurnResult, *, question: str, today: date) -> ModelTurnResult:
    if result.event_plan is not None:
        event_plan = result.event_plan
        model_events = tuple(event for event in event_plan.events if isinstance(event, ModelToolCallEvent))
        if model_events:
            normalized_model_events = _normalize_model_tool_call_events(model_events, question=question, today=today)
            model_iter = iter(normalized_model_events)
            normalized_events = tuple(
                next(model_iter) if isinstance(event, ModelToolCallEvent) else event
                for event in event_plan.events
            )
            if normalized_events != event_plan.events:
                return ModelTurnResult(
                    trace=result.trace,
                    error=result.error,
                    event_plan=replace(event_plan, events=normalized_events),
                )
        return result
    return result


def _question_requests_income_detail(question: str) -> bool:
    text = str(question or "")
    compact = re.sub(r"\s+", "", text)
    detail_tokens = ("明细", "组成", "构成", "来源", "由什么组成")
    analysis_tokens = ("分析", "复盘", "表现")
    income_tokens = ("收益", "收入", "现金流", "权利金", "已实现", "PnL", "pnl")
    return any(token in text for token in detail_tokens) or (
        any(token in compact for token in analysis_tokens)
        and any(token in compact for token in income_tokens)
    )


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
    output_contract = resolve_output_contract(tool_name, payload)
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
    output_contract = resolve_output_contract(tool_name, payload)
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
        if str(result.get("tool_name") or "") == "symbol_config_read":
            for key in ("canonical_symbol", "market", "strategy", "field", "path", "value", "found"):
                value = data.get(key)
                if isinstance(value, (str, int, float, bool)) or value is None:
                    summary[key] = value
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


def _followup_clarification_decision(followup_decisions: list[dict[str, Any]]) -> dict[str, Any]:
    for decision in reversed(followup_decisions):
        if not isinstance(decision, dict):
            continue
        if str(decision.get("decision") or "") != "ask_clarification":
            continue
        return dict(decision)
    return {}


def _clarification_text_from_decision(decision: dict[str, Any]) -> str:
    if not decision:
        return ""
    text = str(decision.get("clarification") or decision.get("reason") or "").strip()
    if text:
        return text
    if str(decision.get("decision") or "") == "ask_clarification":
        return "需要补充范围后才能继续分析。"
    return ""


def _followup_clarification_should_ask(
    *,
    clarification: str,
    clarification_reason: str,
    evidence_gaps: list[dict[str, Any]],
) -> bool:
    if clarification_reason == "missing_operation_scope" and _has_high_risk_followup_gap(evidence_gaps):
        return True
    compact = re.sub(r"\s+", "", str(clarification or "").lower())
    if any(token in compact for token in ("确认", "取消", "confirm", "cancel")) and _has_high_risk_followup_gap(evidence_gaps):
        return True
    return False


def _has_high_risk_followup_gap(evidence_gaps: list[dict[str, Any]]) -> bool:
    for gap in evidence_gaps:
        if not isinstance(gap, dict):
            continue
        text = " ".join(
            str(gap.get(key) or "").lower()
            for key in ("kind", "recoverable_by", "suggested_tool", "risk_level", "reason")
        )
        if any(token in text for token in ("confirm", "cancel", "permission", "preview_write", "confirm_write")):
            return True
    return False


def _clarification_reason_code(text: str) -> str:
    compact = re.sub(r"\s+", "", str(text or "").lower())
    if any(token in compact for token in ("operation_id", "operationid", "操作id", "操作编号", "确认", "取消", "confirm", "cancel")):
        return "missing_operation_scope"
    has_account = "账户" in compact or "account" in compact
    has_symbol = "标的" in compact or "symbol" in compact
    has_period = "月份" in compact or "month" in compact or "日期" in compact or "period" in compact
    if sum(bool(item) for item in (has_account, has_symbol, has_period)) > 1:
        return "missing_scope"
    if has_account:
        return "missing_account_scope"
    if has_symbol:
        return "missing_symbol_scope"
    if has_period:
        return "missing_period_scope"
    if "范围" in compact or "scope" in compact:
        return "missing_scope"
    return "needs_clarification"


def _clarification_request_payload(text: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
    question = str(text or "").strip() or "需要补充范围后才能继续分析。"
    slot = _clarification_slot(question)
    return {
        "schema_version": "om-agent-clarification-request-v1",
        "status": "needs_user_input",
        "questions": [
            {
                "slot": slot,
                "question": question,
                "options": _clarification_options(slot=slot, context=context),
            }
        ],
    }


def _clarification_slot(question: str) -> str:
    compact = re.sub(r"\s+", "", str(question or "").lower())
    has_account = "账户" in compact or "account" in compact
    has_period = "月份" in compact or "month" in compact or "日期" in compact
    has_symbol = "标的" in compact or "symbol" in compact
    if sum(bool(item) for item in (has_account, has_period, has_symbol)) > 1:
        return "scope"
    if has_account:
        return "account"
    if has_period:
        return "period"
    if has_symbol:
        return "symbol"
    return "scope"


def _clarification_options(*, slot: str, context: dict[str, Any] | None) -> list[dict[str, str]]:
    if slot == "account":
        return [
            {"label": account, "description": f"只查询 {account} 账户"}
            for account in _clarification_account_candidates(context)
        ]
    return []


def _clarification_account_candidates(context: dict[str, Any] | None) -> list[str]:
    if not isinstance(context, dict):
        return []
    candidates: list[str] = []
    for key in ("account_options", "available_accounts", "configured_accounts"):
        candidates.extend(_string_values(context.get(key)))
    options = context.get("clarification_options")
    if isinstance(options, dict):
        candidates.extend(_string_values(options.get("accounts")))
    followup = context.get("agent_loop_followup")
    if isinstance(followup, dict):
        for gap in followup.get("evidence_gaps") or []:
            if isinstance(gap, dict):
                candidates.extend(_string_values(gap.get("missing_accounts")))
        prior_plan = followup.get("prior_plan")
        if isinstance(prior_plan, dict):
            contract = prior_plan.get("task_contract") if isinstance(prior_plan.get("task_contract"), dict) else {}
            scope = contract.get("scope") if isinstance(contract.get("scope"), dict) else {}
            candidates.extend(_string_values(scope.get("requested_accounts")))
    return _unique_strings(candidates)


def _string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _unique_strings(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


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
    if tool_name == "close_advice_read":
        payload.setdefault("market_scope", "all")
    if tool_name == "operation_timeline" and request.audit_db:
        payload.setdefault("audit_db", request.audit_db)
    if tool_name == "runtime_runs":
        payload.setdefault("limit", 10)
    if tool_name == "runtime_logs":
        payload.setdefault("kind", "all")
        payload.setdefault("lines", 50)
    if tool_name == "notification_perception_read" and request.conversation_id:
        payload["conversation_id"] = request.conversation_id
    return payload


def _config_path_for_tool_payload(*, tool_name: str, payload: dict[str, Any], default: str | None) -> str | None:
    if not default:
        return None
    if tool_name not in _SYMBOL_MARKET_CONFIG_PLAN_TOOLS:
        return default
    market_key = _market_config_key(payload.get("symbol"))
    if market_key is None:
        return default
    path = Path(str(default))
    if path.name not in {"config.us.json", "config.hk.json"}:
        return default
    return str(path.with_name(f"config.{market_key}.json"))


def _config_key_for_tool_payload(*, tool_name: str, payload: dict[str, Any], default: str) -> str:
    if tool_name not in _SYMBOL_MARKET_CONFIG_PLAN_TOOLS:
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


def _planner_context_projection(
    text: str,
    *,
    conversation_context: dict[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(conversation_context, dict):
        projection = conversation_context.get("context_projection")
        if isinstance(projection, dict) and projection:
            return dict(projection)
    return build_context_projection(
        current_user_message=str(text or ""),
        conversation_context=conversation_context if isinstance(conversation_context, dict) else None,
    )


def _planner_projection_payload(projection: dict[str, Any], *, current_user_message: str) -> dict[str, Any]:
    policy = projection.get("policy") if isinstance(projection.get("policy"), dict) else {}
    budget = projection.get("budget") if isinstance(projection.get("budget"), dict) else {}
    out: dict[str, Any] = {
        "schema_version": str(projection.get("schema_version") or "om-context-projection-v1"),
        "current_user_message": {"text": str(current_user_message or "")},
        "recent_turns": _planner_projection_items(
            projection.get("recent_turns"),
            allowed_keys={
                "turn_id",
                "created_at",
                "updated_at",
                "user_summary",
                "assistant_summary",
                "tools",
                "safe_slots",
                "evidence_refs",
                "result_status",
            },
            limit=6,
        ),
        "recent_successful_tools": _planner_projection_items(
            projection.get("recent_successful_tools"),
            allowed_keys={
                "turn_id",
                "created_at",
                "tool_name",
                "purpose",
                "safe_payload",
                "safe_slots",
                "evidence_refs",
                "data_shape",
                "result_status",
            },
            limit=5,
        ),
        "available_evidence_refs": _planner_projection_items(
            projection.get("available_evidence_refs"),
            allowed_keys={"ref_id", "turn_id", "source_type", "source_tool", "label", "safe_slots", "data_shape"},
            limit=12,
        ),
        "active_frames": _planner_projection_items(
            projection.get("active_frames"),
            allowed_keys={
                "frame_id",
                "type",
                "source_tool",
                "source_ref_id",
                "turn_id",
                "symbol",
                "market",
                "strategy",
                "setting_path",
                "setting_field",
                "current_value",
                "allowed_deltas",
            },
            limit=5,
        ),
        "open_evidence_gaps": _planner_projection_items(
            projection.get("open_evidence_gaps"),
            allowed_keys={
                "gap_id",
                "turn_id",
                "kind",
                "summary",
                "suggested_tools",
                "suggested_views",
                "safe_slots",
                "created_at",
            },
            limit=5,
        ),
        "pending_operations": _planner_projection_items(
            projection.get("pending_operations"),
            allowed_keys={"operation_id", "operation_type", "status", "summary", "created_at", "expires_at", "safe_slots"},
            limit=5,
        ),
        "user_profile": _planner_projection_sanitize(projection.get("user_profile")),
        "relevant_memories": _planner_projection_items(
            projection.get("relevant_memories"),
            allowed_keys={"memory_id", "type", "title", "summary", "content", "tags", "relevance"},
            limit=5,
        ),
        "policy": {
            "current_message_wins": bool(policy.get("current_message_wins", True)),
            "context_is_hint": bool(policy.get("context_is_hint", True)),
            "ask_when_ambiguous": bool(policy.get("ask_when_ambiguous", True)),
            "declare_context_use": bool(policy.get("declare_context_use", True)),
            "memory_is_hint": bool(policy.get("memory_is_hint", True)),
            "tool_evidence_wins_memory": bool(policy.get("tool_evidence_wins_memory", True)),
            "memory_cannot_authorize_writes": bool(policy.get("memory_cannot_authorize_writes", True)),
        },
        "budget": _planner_projection_sanitize(budget) if budget else {"truncated": False},
    }
    system_events = _planner_projection_items(
        projection.get("system_events"),
        allowed_keys={"schema_version", "event_id", "event_type", "summary", "reason"},
        limit=3,
    )
    if system_events:
        out["system_events"] = system_events
    _trim_planner_projection_payload(out)
    return out


def _planner_projection_items(
    value: Any,
    *,
    allowed_keys: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if len(out) >= max(0, int(limit)):
            break
        if not isinstance(item, dict):
            continue
        compact = {
            str(key): _planner_projection_sanitize(item.get(key))
            for key in allowed_keys
            if item.get(key) not in (None, "", [], {})
        }
        if compact:
            out.append(compact)
    return out


def _planner_projection_sanitize(value: Any, *, string_limit: int = 600) -> Any:
    if value in (None, "", [], {}):
        return value
    if isinstance(value, str):
        return value[:string_limit]
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, list):
        return [
            _planner_projection_sanitize(item, string_limit=string_limit)
            for item in value[:20]
            if not isinstance(item, dict) or item
        ]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key or "").strip()
            if not key_text:
                continue
            sanitized = _planner_projection_sanitize(item, string_limit=string_limit)
            if sanitized not in (None, "", [], {}):
                out[key_text] = sanitized
        return out
    return str(value)[:string_limit]


def _trim_planner_projection_payload(payload: dict[str, Any]) -> None:
    budget = payload.get("budget") if isinstance(payload.get("budget"), dict) else {}
    raw_max_chars = budget.get("max_chars") if budget else None
    try:
        max_chars = int(raw_max_chars or 12000)
    except (TypeError, ValueError):
        max_chars = 12000
    max_chars = max(1000, min(max_chars, 12000))
    while len(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)) > max_chars:
        for key in (
            "relevant_memories",
            "recent_turns",
            "recent_successful_tools",
            "available_evidence_refs",
            "open_evidence_gaps",
        ):
            items = payload.get(key)
            if isinstance(items, list) and items:
                items.pop()
                budget = payload.setdefault("budget", {})
                if isinstance(budget, dict):
                    budget["truncated"] = True
                    budget["truncation_reason"] = "planner_projection_budget"
                break
        else:
            break


def _planner_input_payload(text: str, *, conversation_context: dict[str, Any] | None) -> dict[str, Any]:
    selection = _planner_analysis_view_selection(text, conversation_context=conversation_context)
    preview_authority = _planner_preview_authority(text)
    tools = _planner_tool_manifest(
        analysis_view_names=selection["selected_analysis_views"],
        include_read_tools=True,
        include_preview_capabilities=True,
        omit_read_tools=_planner_omitted_read_tools_for_question(text),
        allowed_preview_intents=preview_authority.get("allowed_preview_intents"),
    )
    payload: dict[str, Any] = {
        "message": str(text or ""),
        "current_user_message": {"text": str(text or "")},
        "tools": tools,
        "preview_authority": preview_authority,
        "manifest_budget": _planner_manifest_budget(
            tools=tools,
            analysis_view_selection=selection,
            preview_authority=preview_authority,
        ),
    }
    if isinstance(conversation_context, dict):
        projection = _planner_context_projection(text, conversation_context=conversation_context)
        projection_payload = _planner_projection_payload(projection, current_user_message=text)
        context_payload = {
            "current_user_message": {"text": str(text or "")},
            "context_projection": projection_payload,
            "context_policy": dict(projection_payload.get("policy") or {}),
            "temporal_context": conversation_context.get("temporal_context")
            if isinstance(conversation_context.get("temporal_context"), dict)
            else {},
        }
        repair = conversation_context.get("context_validation_repair")
        if isinstance(repair, dict) and repair:
            context_payload["context_validation_repair"] = dict(repair)
        payload["context"] = context_payload
    return payload


def _planner_preview_authority(text: str) -> dict[str, Any]:
    authority = preview_authority_from_text(text)
    return {
        **authority,
        "schema_version": "om-planner-preview-authority-v1",
        "policy": (
            "The model may select exactly one preview capability only when the current user message explicitly "
            "asks to record/change/administer something or contains a broker lifecycle/fill notice. Ambiguous "
            "admin update wording may select only the exposed preview capability or ask for clarification. The "
            "host injects raw_text and never lets the model confirm, apply, notify, or mutate state directly."
        ),
    }


def _planner_input_text(text: str, *, conversation_context: dict[str, Any] | None) -> str:
    return json.dumps(
        _planner_input_payload(text, conversation_context=conversation_context),
        ensure_ascii=False,
        sort_keys=True,
    )


def _planner_input_trace(payload: dict[str, Any], input_text: str) -> dict[str, Any]:
    tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
    budget = payload.get("manifest_budget") if isinstance(payload.get("manifest_budget"), dict) else {}
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    projection = context.get("context_projection") if isinstance(context.get("context_projection"), dict) else {}
    projection_trace = context_projection_trace(projection)
    return {
        "chars": len(input_text),
        "tool_count": len(tools),
        "context_projection": projection_trace,
        "manifest_budget": dict(budget),
    }


def _answer_context_trace(question: str, conversation_context: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(conversation_context, dict):
        return {"provided": False}
    payload: dict[str, Any] = {
        "provided": True,
        "window_messages": int(conversation_context.get("window_messages") or 0),
    }
    projection_trace = context_projection_trace(
        conversation_context.get("context_projection")
        if isinstance(conversation_context.get("context_projection"), dict)
        else None
    )
    if projection_trace.get("provided"):
        payload["context_projection"] = projection_trace
    return payload


def _planner_manifest_budget(
    *,
    tools: list[dict[str, Any]],
    analysis_view_selection: dict[str, Any],
    preview_authority: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected = [
        str(item)
        for item in analysis_view_selection.get("selected_analysis_views") or []
        if str(item).strip()
    ]
    preview = preview_authority if isinstance(preview_authority, dict) else {}
    return {
        "schema_version": "om-planner-manifest-budget-v1",
        "mode": analysis_view_selection.get("mode"),
        "manifest_chars": len(json.dumps(tools, ensure_ascii=False, sort_keys=True)),
        "tool_count": len(tools),
        "analysis_views_total": len(ANALYSIS_VIEW_SPECS),
        "analysis_views_included": len(selected),
        "analysis_views_omitted": max(0, len(ANALYSIS_VIEW_SPECS) - len(selected)),
        "selected_analysis_views": selected,
        "matched_view_groups": list(analysis_view_selection.get("matched_view_groups") or []),
        "selection_sources": list(analysis_view_selection.get("selection_sources") or []),
        "preview_authority_allowed": bool(preview.get("allowed", False)),
        "preview_authority_mode": str(preview.get("mode") or ""),
        "allowed_preview_intents": list(preview.get("allowed_preview_intents") or []),
        "fallback": "use analysis_catalog first when a needed analysis view or field is not included",
    }


def _model_event_planner_instructions() -> str:
    return """\
You are the options-monitor assistant tool caller.

Use the provided tool/function calling interface for read-only tool selection and host-approved preview capability selection.
Do not write JSON plans in normal text. Do not wrap tool choices in Markdown.

Rules:
- For business, runtime, position, candidate, config, or income questions, call 1 to 3 suitable read-only tools when needed; prefer one tool when it is enough.
- Use only tools exposed in the current tool list. Do not invent tool names.
- Do not include system-scoped arguments such as config_key, config_path, paths, host, port, timeout, audit_db, report_path, run_dir, or output_dir. The host injects those.
- For income analysis/review/performance/source/composition questions, call monthly_income_report and set include_rows=true.
- For a month without a year such as "6月", resolve it from the current date in the input context.
- If the user asks a single-symbol candidate filtering/root-cause question, call candidate_filter_explain with the user-visible symbol.
- If the request is write/admin/confirm/apply, do not call a read tool. If a preview capability is exposed in the current tool list, select that preview capability through the tool/function calling interface; otherwise ask for clarification.
- For manual trade preview capabilities, do not copy the original user message into arguments. Provide account only when explicit; the host injects raw_text.
- For requests to run/trigger one US/HK monitor cycle such as "跑一次港股监控", use monitor_run_now with market=hk/us. For symbol-scoped requests such as "单独跑一次 PDD 的监控", use monitor_run_now with symbols=["PDD"]; the host infers market when possible and forces no-send. This creates only a pending admin preview; never call runtime read tools as a substitute and never execute tick directly.
- If no safe read tool applies, do not guess.
"""


def _planner_instructions() -> str:
    return """\
You are the options-monitor assistant capability planner.
Return only JSON that matches the requested schema.

Rules:
- Produce 1 to 3 read-only tool calls, or exactly 1 preview-write capability call.
- Always fill task_contract. The task_contract is your structured understanding of the user goal; the steps are your executable plan. If unsure, use domain=general, requested_effect=read, conservative answer/evidence requirements, empty scope, and record uncertainty in ambiguities instead of omitting task_contract.
- Also fill selected_recipe.name when one investigation recipe matches the task_contract and planned evidence path. Use income_analysis_breakdown for income analysis/breakdown, position_or_quote_diagnosis for position/runtime diagnosis, operation_status_readback for operation status/readback questions, strategy_replay_review for strategy/candidate replay or recommendation, and action_lifecycle_audit for preview-write lifecycle/audit questions.
- task_contract.domain is one of income, position, candidate, config, operation, runtime, strategy, general. task_contract.task_mode is one of summarize, analyze, compare, diagnose, explain, recommend, preview_write.
- Use task_mode=analyze for analysis/复盘/表现/来源/结构 questions, compare for same-scope comparisons, diagnose for why/missing/failure/abnormal status questions, explain for rules or accounting policies, recommend for advisory options, and preview_write only for approved preview operations.
- task_contract.required_evidence should name evidence categories needed to complete the answer, not tool names. Examples: summary, driver_or_breakdown, same_scope_comparable_data, observed_status, diagnostic_evidence, rule_or_config_source, current_state, constraints, risk_premise, source_policy.
- task_contract.answer_shape should name what the final answer must cover, such as conclusion, drivers, same_scope_comparison, cause_chain, evidence_boundary, risk, premise, options, source_policy.
- Always fill context_use when the schema includes it. Use mode=none when the current message is self-contained; use carry/refine/override/frame_delta only when the plan intentionally depends on prior conversation state.
- When context.context_projection.active_frames has one relevant frame and the current message is a short delta such as a new scalar value, use mode=frame_delta with referenced_frame_ids and delta. If multiple frames fit, ask clarification.
- Current user message wins over context.context_projection. If the user explicitly changes account, symbol, month, domain, strategy, operation, or requested effect, declare mode=override and put the replacement values in current_message_slots or override_slots.
- Use context.context_projection as the only conversation-state authority for planning. recent_turns, recent_successful_tools, available_evidence_refs, open_evidence_gaps, pending_operations, and safe_slots are planner-visible hints, not hidden truth.
- Use context.context_projection.relevant_memories only as hint-only collaboration, OM usage, and parameter-tuning preferences. Do not treat memory as market data, ledger state, runtime config, or authorization for writes.
- Only inherit slots that are needed by the plan, and declare them in context_use.inherited_slots with referenced_turn_ids or referenced_evidence_refs when available. Prefer evidence refs over raw prior summaries whenever a prior tool result is being reused.
- If open_evidence_gaps suggests relevant views/tools, treat them as recoverable evidence hints, not as facts.
- If prior context is required but cannot be chosen safely, set context_use.mode=ambiguous, requires_clarification=true, provide a clarification_question, and return steps=[] rather than guessing.
- Use only tools/capabilities in the provided manifest.
- Fill required_capabilities with the user's required answer capabilities from the tool manifest. Use [] only when the request needs no special capability beyond the planned tool call.
- Preview-write capabilities only create a pending preview. They never apply writes, confirm pending operations, notify users externally, or mutate config/ledger directly.
- Never plan confirm/cancel/apply actions. Confirm/cancel must be handled by deterministic user commands bound to a pending operation.
- Do not include system-scoped, path, config, audit, host, port, timeout, service, delivery, or trigger arguments such as config_key, config_path, data_config, output_dir, report_path, run_dir, logs_root, state_dir, opend_telnet_host, timeout_sec, or audit_db. The system injects those.
- Resolve relative dates using context.temporal_context.current_date in Asia/Shanghai. For a month without a year such as "6月", use the current_date year.
- For short follow-up questions about a term, metric, calculation, or previous result, inspect context.context_projection.recent_turns and available_evidence_refs. Carry or refine only when one prior turn/evidence ref is clearly relevant; otherwise ask clarification.
- For monthly income summary questions, use monthly_income_report with account/month when available.
- For combined/all-account return questions, include required_capabilities=["combined_account_return"] and use monthly_income_report without account.
- For income analysis/review/performance, cashflow detail, net cashflow composition, net inflow source, "分析", "复盘", "表现", "明细", "组成", "构成", "来源", or "由什么组成", use monthly_income_report with include_rows=true; the Agent composer will write the final user response from tool evidence.
- For assigned stock / 被指派正股 / 指派正股 holding PnL, floating PnL, spot, cost basis, or lifecycle PnL questions, use option_positions_read with action="assigned-stock", status="open" unless the user asks all/closed, refresh_quotes=true for current holding PnL. AgentLoop decides the final answer path from tool evidence.
- For all-history, cumulative, or total net cashflow questions, omit month so monthly_income_report reads all OM local ledger months.
- For multiple explicit months, either call monthly_income_report once per month with matching arguments, or omit month and synthesize from all available rows; never duplicate one month while claiming another.
- For "记录开仓", "记录平仓", Futu 成交提醒, 成功卖出/买入 option fills, use manual_trade_open or manual_trade_close. Do not copy the original user message into arguments; the host injects raw_text.
- For Futu 期权被指派通知 / 已被指派, use manual_assignment. For Futu 期权到期失效通知 / 已到期失效, use manual_expiry. Do not copy the original user message into arguments; the host injects raw_text.
- For current monitored-symbol config questions such as "max strike 是多少", "当前配置", or "查询 sell_put.max_strike", use symbol_config_read with symbol plus optional strategy/field.
- For user-provided company names, Chinese names, aliases, Futu codes, or uncertain market suffixes, use symbol_resolve when the user asks for identity resolution or when a later SQL-style analysis needs a canonical symbol. Symbol-aware tools may also receive the original alias/name and resolve it internally.
- For single-symbol candidate filter/rejection questions such as "为什么 X 没出现在候选里", "X 被哪个参数过滤了", "why was X filtered", or "why missing candidate", use candidate_filter_explain with symbol plus optional account/function/run_id. Do not use analysis_query for that single-symbol root-cause shape unless the user asks to compare/group/trend across symbols, accounts, rules, or runs.
- For non-monthly-income open-ended analytical questions such as 分析, 复盘, 表现, 对比, 有什么不同, 排名, 趋势, 组成, 来源, 按账户/月份/标的汇总, or cross-domain questions across income/positions/trades/assigned stock/config, prefer analysis_query over narrow business renderers. Use analysis_catalog first only when fields/views are unknown.
- For analysis_query, use only columns listed in the tool manifest analysis_views. Never invent SQL columns. If the needed fields are not clear from the manifest, plan analysis_catalog before analysis_query.
- For monitored-symbol setting changes such as covered call min strike 85, use symbol_edit. Do not use symbol_edit for questions about the current value.
- For model switch requests, use model_use. For immediate software upgrade requests, use upgrade_now.
- For "跑一次/执行一次/运行一次/触发一次" plus "港股/美股" plus "监控/tick/扫描", use monitor_run_now with market=hk/us. For a single-symbol request such as "单独跑一次 PDD 的监控", use symbols=["PDD"] and omit market if the symbol uniquely implies it. This only creates a pending admin preview; symbol-scoped runs are forced to no-send by the host. Do not use runtime_status, runtime_runs, or runtime_logs for this execution request, and never plan the live tick itself.
- Do not include answer-rendering fields such as response_mode, canonical, synthesis, or renderer choices. AgentLoop decides the final answer path, verifies evidence, and owns deterministic fallback.
- If there is no safe plan, or required slots are missing and the capability cannot safely handle them, return steps=[] instead of guessing.
"""


def _planner_analysis_view_selection(
    text: str,
    *,
    conversation_context: dict[str, Any] | None,
) -> dict[str, Any]:
    selected: list[str] = []
    matched_groups: list[str] = []
    selection_sources: list[str] = []
    message_groups, message_views = _planner_matched_analysis_groups(_planner_selection_haystack([text]))
    if message_views:
        matched_groups.extend(message_groups)
        selected.extend(message_views)
        selection_sources.append("message")
    projection = _planner_context_projection(text, conversation_context=conversation_context)
    gap_views = _planner_context_suggested_analysis_views(text, conversation_context)
    if gap_views:
        selected.extend(gap_views)
        matched_groups.extend(_planner_groups_for_analysis_views(gap_views))
        selection_sources.append("context_projection.open_evidence_gaps")

    projection_has_recent_evidence = bool(
        projection.get("recent_turns") or projection.get("recent_successful_tools") or projection.get("available_evidence_refs")
    )
    if projection_has_recent_evidence and (
        not message_views or _planner_should_blend_context_for_term_followup(text, conversation_context)
    ):
        context_hints = _planner_selection_context_hints(conversation_context, text=text)
        context_groups, context_group_views = _planner_matched_analysis_groups(
            _planner_selection_haystack(context_hints)
        )
        if context_group_views:
            matched_groups.extend(context_groups)
            selected.extend(context_group_views)
            selection_sources.append("context_projection.recent_evidence")

    if not selected:
        ref_views = _planner_projection_views_from_refs(projection)
        if ref_views:
            selected.extend(ref_views)
            matched_groups.extend(_planner_groups_for_analysis_views(ref_views))
            selection_sources.append("context_projection.available_evidence_refs")

    if not selected:
        selected.extend(_DEFAULT_PLANNER_ANALYSIS_VIEWS)
        selection_sources.append("default")
    selected = [
        view
        for view in _unique_strings([str(item) for item in selected])
        if view in ANALYSIS_VIEW_SPECS
    ][:MAX_PLANNER_ANALYSIS_VIEWS]
    if not selected:
        selected = sorted(ANALYSIS_VIEW_SPECS)[:MAX_PLANNER_ANALYSIS_VIEWS]
        selection_sources.append("catalog_fallback")
    return {
        "mode": "scoped_analysis_views",
        "selected_analysis_views": selected,
        "matched_view_groups": matched_groups or ["default"],
        "selection_sources": _unique_strings(selection_sources),
    }


def _planner_matched_analysis_groups(haystack: str) -> tuple[list[str], list[str]]:
    matched_groups: list[str] = []
    selected: list[str] = []
    if not haystack.strip():
        return matched_groups, selected
    for group_name, keywords, views in _ANALYSIS_VIEW_GROUPS:
        if not any(_planner_keyword_matches(haystack, keyword) for keyword in keywords):
            continue
        matched_groups.append(group_name)
        selected.extend(views)
    return matched_groups, selected


def _planner_should_blend_context_for_term_followup(
    text: str,
    conversation_context: dict[str, Any] | None,
) -> bool:
    projection = _planner_context_projection(text, conversation_context=conversation_context)
    if not _planner_selection_context_hints(conversation_context, text=text):
        return False
    if not (projection.get("recent_turns") or projection.get("recent_successful_tools") or projection.get("available_evidence_refs")):
        return False
    compact = re.sub(r"\s+", "", str(text or "").lower())
    if not compact:
        return False
    if _planner_message_has_explicit_context_switch(compact):
        return False
    term_tokens = (
        "怎么算",
        "怎么计算",
        "如何计算",
        "怎么来的",
        "是什么",
        "什么意思",
        "指什么",
        "这个",
        "那个",
        "刚才",
        "上面",
        "前面",
        "为什么",
        "为何",
        "why",
        "how",
        "calculate",
        "computed",
        "meaning",
    )
    return any(token in compact for token in term_tokens)


def _planner_message_has_explicit_context_switch(compact_text: str) -> bool:
    explicit_tokens = (
        "账户",
        "account",
        "标的",
        "symbol",
        "候选",
        "过滤",
        "candidate",
        "filter",
        "持仓",
        "仓位",
        "position",
        "运行",
        "通知",
        "调度",
        "runtime",
        "scheduler",
        "配置",
        "config",
        "策略",
        "strategy",
        "平仓",
        "close",
        "开仓",
        "成交",
        "trade",
    )
    if any(token in compact_text for token in explicit_tokens):
        return True
    if re.search(r"\b[A-Z]{2,6}(?:\.[A-Z]{1,3})?\b", compact_text.upper()):
        return True
    if re.search(r"20\d{2}[-年/]?\d{1,2}|[一二三四五六七八九十\d]{1,2}月", compact_text):
        return True
    return False


def _planner_selection_haystack(parts: list[Any] | tuple[Any, ...]) -> str:
    lower = " ".join(str(part or "") for part in parts if str(part or "").strip()).lower()
    compact = re.sub(r"\s+", "", lower)
    return f"{lower} {compact}"


def _planner_keyword_matches(haystack: str, keyword: str) -> bool:
    raw = str(keyword or "").lower().strip()
    if not raw:
        return False
    return raw in haystack or re.sub(r"\s+", "", raw) in haystack


def _planner_selection_context_hints(conversation_context: dict[str, Any] | None, *, text: str | None = None) -> list[str]:
    projection = _planner_context_projection(str(text or ""), conversation_context=conversation_context)
    hints: list[str] = []
    for gap in projection.get("open_evidence_gaps") or []:
        if not isinstance(gap, dict):
            continue
        hints.append(str(gap.get("kind") or ""))
        hints.append(str(gap.get("summary") or ""))
        hints.append(_compact_json_for_planner_selection(gap.get("suggested_tools")))
        hints.append(_compact_json_for_planner_selection(gap.get("suggested_views")))
        hints.append(_compact_json_for_planner_selection(gap.get("safe_slots")))
    for item in projection.get("recent_turns") or []:
        if not isinstance(item, dict):
            continue
        hints.append(str(item.get("user_summary") or ""))
        hints.append(str(item.get("assistant_summary") or ""))
        hints.append(_compact_json_for_planner_selection(item.get("tools")))
        hints.append(_compact_json_for_planner_selection(item.get("safe_slots")))
    for item in projection.get("recent_successful_tools") or []:
        if not isinstance(item, dict):
            continue
        tool_name = str(item.get("tool_name") or "").strip()
        if tool_name:
            hints.append(tool_name)
            hints.extend(_PLANNER_CONTEXT_TOOL_HINTS.get(tool_name, ()))
        hints.append(str(item.get("purpose") or ""))
        hints.append(_compact_json_for_planner_selection(item.get("safe_payload")))
        hints.append(_compact_json_for_planner_selection(item.get("safe_slots")))
        hints.append(_compact_json_for_planner_selection(item.get("data_shape")))
    for ref in projection.get("available_evidence_refs") or []:
        if not isinstance(ref, dict):
            continue
        tool_name = str(ref.get("source_tool") or "").strip()
        if tool_name:
            hints.append(tool_name)
            hints.extend(_PLANNER_CONTEXT_TOOL_HINTS.get(tool_name, ()))
        hints.append(str(ref.get("label") or ""))
        hints.append(_compact_json_for_planner_selection(ref.get("safe_slots")))
        hints.append(_compact_json_for_planner_selection(ref.get("data_shape")))

    return [hint for hint in hints if str(hint or "").strip()]


def _planner_context_suggested_analysis_views(text: str, conversation_context: dict[str, Any] | None) -> list[str]:
    projection = _planner_context_projection(text, conversation_context=conversation_context)
    views: list[str] = []
    for gap in projection.get("open_evidence_gaps") or []:
        if not isinstance(gap, dict):
            continue
        for view in gap.get("suggested_views") or []:
            name = str(view or "").strip()
            if name in ANALYSIS_VIEW_SPECS:
                views.append(name)
    return _unique_strings(views)


def _planner_projection_views_from_refs(projection: dict[str, Any]) -> list[str]:
    views: list[str] = []
    for ref in projection.get("available_evidence_refs") or []:
        if not isinstance(ref, dict):
            continue
        data_shape = ref.get("data_shape") if isinstance(ref.get("data_shape"), dict) else {}
        for view in data_shape.get("views_used") or data_shape.get("views") or []:
            name = str(view or "").strip()
            if name in ANALYSIS_VIEW_SPECS:
                views.append(name)
    return _unique_strings(views)


def _planner_groups_for_analysis_views(views: list[str]) -> list[str]:
    view_set = {str(item) for item in views if str(item).strip()}
    groups: list[str] = []
    for group_name, _keywords, group_views in _ANALYSIS_VIEW_GROUPS:
        if view_set.intersection(str(view) for view in group_views):
            groups.append(group_name)
    return _unique_strings(groups)


def _compact_json_for_planner_selection(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(value)
    return text[:1000]


def _planner_tool_manifest(
    analysis_view_names: list[str] | tuple[str, ...] | set[str] | None = None,
    *,
    include_read_tools: bool = True,
    include_preview_capabilities: bool = True,
    omit_read_tools: tuple[str, ...] | set[str] | list[str] | frozenset[str] | None = None,
    allowed_preview_intents: tuple[str, ...] | set[str] | list[str] | None = None,
) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    omitted_read_tools = {str(item) for item in omit_read_tools or () if str(item).strip()}
    for name in sorted(AGENT_LOOP_READ_TOOLS) if include_read_tools else ():
        if name in omitted_read_tools:
            continue
        definition = get_tool_definition(name)
        if definition is None:
            continue
        input_schema = {
            key: value
            for key, value in definition.input_schema.items()
            if not _is_banned_plan_argument(str(key))
        }
        notes = list(definition.planner_notes)
        semantics = definition.resolve_planner_semantics({"analysis_view_names": analysis_view_names})
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
    allowed_preview = {str(item) for item in allowed_preview_intents or () if str(item).strip()}
    for spec in planner_preview_specs() if include_preview_capabilities else ():
        if allowed_preview and spec.intent_name not in allowed_preview:
            continue
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


_LIFECYCLE_ASSIGNMENT_MODEL_SCHEMA: dict[str, Any] = {
    "symbol": {"type": ["string", "null"], "description": "Canonical symbol if known, e.g. 0700.HK for 腾讯."},
    "option_type": {"type": ["string", "null"], "enum": ["put", "call", "p", "c", "沽", "购", None]},
    "position_side": {"type": ["string", "null"], "enum": ["short", "long", None]},
    "contracts_to_close": {"type": ["integer", "null"]},
    "strike": {"type": ["number", "null"]},
    "expiration_ymd": {"type": ["string", "null"], "description": "YYYY-MM-DD."},
    "stock_side": {"type": ["string", "null"], "enum": ["buy", "sell", None]},
    "stock_qty": {"type": ["integer", "null"], "description": "Settled shares. Host validates this against matched open lots."},
    "stock_price": {"type": ["number", "null"], "description": "Usually the strike for assignment settlement."},
    "record_id": {"type": ["string", "null"]},
    "as_of_ms": {"type": ["integer", "null"]},
}
_LIFECYCLE_EXPIRY_MODEL_SCHEMA: dict[str, Any] = {
    "symbol": {"type": ["string", "null"]},
    "option_type": {"type": ["string", "null"], "enum": ["put", "call", "p", "c", "沽", "购", None]},
    "position_side": {"type": ["string", "null"], "enum": ["short", "long", None]},
    "contracts_to_close": {"type": ["integer", "null"]},
    "strike": {"type": ["number", "null"]},
    "expiration_ymd": {"type": ["string", "null"], "description": "YYYY-MM-DD."},
    "event_time_ms": {"type": ["integer", "null"]},
    "close_reason": {"type": ["string", "null"]},
}


def _planner_preview_input_schema(intent_name: str) -> dict[str, Any]:
    if intent_name in {"manual_trade_open", "manual_trade_close", "manual_assignment", "manual_expiry"}:
        extra = (
            _LIFECYCLE_ASSIGNMENT_MODEL_SCHEMA
            if intent_name == "manual_assignment"
            else _LIFECYCLE_EXPIRY_MODEL_SCHEMA
            if intent_name == "manual_expiry"
            else {}
        )
        return {
            "account": {
                "type": ["string", "null"],
                "enum": [*ACCOUNT_VALUES, None],
                "description": "Optional account label if explicitly present. The host injects the original user message as raw_text.",
            },
            **extra,
        }
    if intent_name == "manual_trade_update":
        return {
            "operation_id": {"type": ["string", "null"]},
            "operation_resolution": {"type": ["string", "null"]},
            "updates": {"type": "object"},
        }
    if intent_name == "symbol_edit":
        return {
            "symbol": {"type": "string", "required": True},
            "set": {**_SYMBOL_EDIT_SET_SCHEMA, "required": True},
            "ensure_use": {"type": ["array", "null"], "items": {"type": "string"}},
        }
    if intent_name == "model_use":
        return {"model_profile": {"type": "string"}}
    if intent_name == "upgrade_now":
        return {"target_version": {"type": ["string", "null"]}}
    if intent_name == "monitor_run_now":
        return {
            "market": {
                "type": ["string", "null"],
                "enum": ["hk", "us", None],
                "description": "Explicit market from the user message. Use hk for 港股/HK, us for 美股/US. Omit when symbols uniquely imply the market.",
            },
            "accounts": {
                "type": ["array", "null"],
                "items": {"type": "string", "enum": list(ACCOUNT_VALUES)},
                "description": "Optional accounts only when explicitly present; otherwise omit and the host reads runtime config accounts.",
            },
            "symbols": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": "Optional explicit symbol list for a symbol-scoped no-send monitor run. Use when the user asks to run one specific symbol such as PDD.",
            },
        }
    return {}


def _planner_preview_notes(intent_name: str) -> list[str]:
    if intent_name == "manual_trade_open":
        return [
            "Use for 记录开仓, Futu 成交提醒, 成功卖出/买入 option opening fills.",
            "Do not provide raw_text; the host injects the original user message so the deterministic trade parser can extract symbol, expiration, strike, contracts, premium, and account.",
            "Creates only a pending preview; it never writes the ledger until a deterministic confirm command is received.",
        ]
    if intent_name == "manual_trade_close":
        return [
            "Use for 记录平仓 and closing fill reminders.",
            "Do not provide raw_text; the host injects the original user message.",
            "Creates only a pending preview; it never writes the ledger until a deterministic confirm command is received.",
        ]
    if intent_name == "manual_assignment":
        return [
            "Use for Futu 期权被指派通知 or 已被指派 lifecycle notices.",
            "Extract visible lifecycle fields such as symbol, option_type, position_side, contracts_to_close, strike, expiration_ymd, stock_side, stock_qty, and stock_price; omit uncertain fields.",
            "Do not provide raw_text; the host injects the original user message and validates your structured fields against open lots before creating a preview.",
            "Creates only a pending preview; it never writes the ledger until a deterministic confirm command is received.",
        ]
    if intent_name == "manual_expiry":
        return [
            "Use for Futu 期权到期失效通知 or 已到期失效 lifecycle notices.",
            "Extract visible lifecycle fields such as symbol, option_type, position_side, contracts_to_close, strike, and expiration_ymd; omit uncertain fields.",
            "Do not provide raw_text; the host injects the original user message and validates your structured fields against open lots before creating a preview.",
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
    if intent_name == "monitor_run_now":
        return [
            "Use for explicit run/trigger-one-cycle monitor requests such as 跑一次港股监控, run HK tick once, or 单独跑一次 PDD 的监控.",
            "Use market=hk/us for whole-market runs. Use symbols=[...] for symbol-scoped runs; the host infers market when possible and forces no-send.",
            "Creates only a pending admin preview. The host will require deterministic confirmation before running tick-cron; whole-market runs may send real notifications.",
        ]
    return []


def _plan_like_public_payload(plan: Any) -> dict[str, Any]:
    if isinstance(plan, dict):
        return dict(plan)
    if hasattr(plan, "plan_like_payload"):
        payload = plan.plan_like_payload()
        return dict(payload) if isinstance(payload, dict) else {}
    if hasattr(plan, "public_payload"):
        payload = plan.public_payload()
        return dict(payload) if isinstance(payload, dict) else {}
    return {}


def _synthesis_input_text(
    question: str,
    *,
    plan: Any,
    observations: list[dict[str, Any]],
    conversation_context: dict[str, Any] | None,
) -> str:
    payload: dict[str, Any] = {
        "question": str(question or ""),
        "plan": _plan_like_public_payload(plan),
        "observations": observations,
    }
    if isinstance(conversation_context, dict):
        context_payload: dict[str, Any] = {
            "window_messages": int(conversation_context.get("window_messages") or 0),
            "user_profile": conversation_context.get("user_profile")
            if isinstance(conversation_context.get("user_profile"), dict)
            else {"provided": False},
        }
        payload["context"] = context_payload
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
- Do not mention internal answer modes, renderer names, fact mode, or analysis mode.
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
    planner_input: dict[str, Any] | None = None,
    planner_context_use: dict[str, Any] | None = None,
    context_validation: dict[str, Any] | None = None,
    context_validation_repair: dict[str, Any] | None = None,
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
        projection = (
            conversation_context.get("context_projection")
            if isinstance(conversation_context, dict) and isinstance(conversation_context.get("context_projection"), dict)
            else None
        )
        payload["context"] = {
            "provided": True,
            "window_messages": int(conversation_context.get("window_messages") or 0) if isinstance(conversation_context, dict) else 0,
            "recent_count": len(recent) if isinstance(recent, list) else 0,
            "pending_count": len(pending) if isinstance(pending, list) else 0,
            "context_projection": context_projection_trace(projection),
            "user_profile": user_profile_trace(
                conversation_context.get("user_profile") if isinstance(conversation_context, dict) else None
            ),
        }
    if planner_input:
        payload["planner_input"] = dict(planner_input)
    if planner_context_use is not None:
        payload["planner_context_use"] = _safe_context_use_payload(planner_context_use)
    if context_validation is not None:
        payload["context_validation"] = dict(context_validation)
    if context_validation_repair is not None:
        payload["context_validation_repair"] = dict(context_validation_repair)
    return payload


def skipped_llm_trace(settings: Any, *, reason: str) -> dict[str, Any]:
    return _llm_trace(settings, attempted=False, reason=reason)


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
        row_count = _optional_int(data.get("row_count"))
        rows, rows_complete, row_preview_limit = _synthesis_rows_with_metadata(
            data.get("rows"),
            row_count=row_count,
            default_limit=30,
            truncated=bool(data.get("truncated", False)),
        )
        return {
            "source_label": data.get("source_label") or "OM read-only analysis workspace",
            "query": dict(data.get("query") or {}) if isinstance(data.get("query"), dict) else {},
            "columns": list(data.get("columns") or []),
            "rows": rows,
            "row_count": row_count if row_count is not None else data.get("row_count"),
            "rows_complete": rows_complete,
            "row_preview_limit": row_preview_limit,
            "truncated": bool(data.get("truncated", False)),
            "views_used": list(data.get("views_used") or []),
            "cell_refs": _clip_mapping(data.get("cell_refs"), limit=120) if isinstance(data.get("cell_refs"), dict) else {},
            "fallback_text": data.get("fallback_text"),
        }
    if tool_name == "candidate_filter_explain":
        return _candidate_filter_synthesis_data(data)
    if tool_name == "monthly_income_report":
        coverage = _monthly_income_coverage(data)
        summary_rows, summary_complete, summary_limit = _synthesis_rows_with_metadata(
            data.get("summary"),
            row_count=_optional_int(data.get("row_count")),
            default_limit=8,
        )
        return_summary_rows, return_summary_complete, return_summary_limit = _synthesis_rows_with_metadata(
            data.get("return_summary"),
            row_count=None,
            default_limit=8,
        )
        combined_return_summary_rows, combined_return_summary_complete, combined_return_summary_limit = (
            _synthesis_rows_with_metadata(
                data.get("combined_return_summary"),
                row_count=None,
                default_limit=8,
            )
        )
        out = {
            "summary": summary_rows,
            "summary_complete": summary_complete,
            "summary_preview_limit": summary_limit,
            "return_summary": return_summary_rows,
            "return_summary_complete": return_summary_complete,
            "return_summary_preview_limit": return_summary_limit,
            "combined_return_summary": combined_return_summary_rows,
            "combined_return_summary_complete": combined_return_summary_complete,
            "combined_return_summary_preview_limit": combined_return_summary_limit,
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
                count_key = f"{key[:-1]}_count" if key.endswith("s") else f"{key}_count"
                row_count = _optional_int(data.get(count_key))
                if row_count is None:
                    row_count = len(rows)
                clipped_rows, rows_complete, row_preview_limit = _synthesis_rows_with_metadata(
                    rows,
                    row_count=row_count,
                    default_limit=20,
                )
                out[key] = clipped_rows
                out[count_key] = row_count
                out[f"{key}_complete"] = rows_complete
                out[f"{key}_preview_limit"] = row_preview_limit
                if not rows_complete:
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


def _candidate_filter_synthesis_data(data: dict[str, Any]) -> dict[str, Any]:
    functions: list[dict[str, Any]] = []
    for item in data.get("functions") or []:
        if not isinstance(item, dict):
            continue
        events: list[dict[str, Any]] = []
        for event in item.get("events") or []:
            if not isinstance(event, dict):
                continue
            events.append(
                {
                    "status": event.get("status"),
                    "rule": event.get("rule"),
                    "rule_label": event.get("rule_label"),
                    "is_rejection": event.get("is_rejection"),
                    "metric_value": event.get("metric_value"),
                    "threshold": event.get("threshold"),
                    "message": event.get("message"),
                    "contract_symbol": event.get("contract_symbol"),
                    "expiration": event.get("expiration"),
                    "strike": event.get("strike"),
                }
            )
            if len(events) >= 6:
                break
        functions.append(
            {
                "function": item.get("function"),
                "status": item.get("status"),
                "rejection_reasons": _clip_list(item.get("rejection_reasons"), limit=8),
                "rejection_reason_counts": dict(item.get("rejection_reason_counts") or {})
                if isinstance(item.get("rejection_reason_counts"), dict)
                else {},
                "reason_labels": dict(item.get("reason_labels") or {}) if isinstance(item.get("reason_labels"), dict) else {},
                "events": events,
            }
        )
        if len(functions) >= 8:
            break
    return {
        "symbol": data.get("symbol"),
        "raw_symbol": data.get("raw_symbol"),
        "canonical_symbol": data.get("canonical_symbol"),
        "account": data.get("account"),
        "scope": dict(data.get("scope") or {}) if isinstance(data.get("scope"), dict) else {},
        "trace_count": data.get("trace_count"),
        "status_counts": dict(data.get("status_counts") or {}) if isinstance(data.get("status_counts"), dict) else {},
        "function_counts": dict(data.get("function_counts") or {}) if isinstance(data.get("function_counts"), dict) else {},
        "functions": functions,
    }


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


def _synthesis_rows_with_metadata(
    value: Any,
    *,
    row_count: int | None,
    default_limit: int,
    truncated: bool = False,
) -> tuple[list[Any], bool | None, int | None]:
    if not isinstance(value, list) and row_count is None:
        return [], None, None
    limit = default_limit
    if not truncated and isinstance(value, list):
        observed_count = len(value)
        if observed_count <= 50 and (row_count is None or row_count <= 50):
            limit = max(observed_count, row_count or 0)
    rows = _clip_list(value, limit=limit)
    complete = _synthesis_rows_complete(rows=rows, row_count=row_count, truncated=truncated)
    return rows, complete, limit


def _synthesis_rows_complete(*, rows: list[Any], row_count: int | None, truncated: bool) -> bool:
    if truncated:
        return False
    if row_count is not None:
        return row_count <= len(rows)
    return bool(rows)


def _optional_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except Exception:
        return None


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
    return tool_name_for_intent(intent_name)


def _intent_name_for_tool(tool_name: str) -> str | None:
    return primary_intent_name_for_tool(tool_name)


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
    "ModelTurnFn",
    "AgentLoopPlanningOutcome",
    "AssistantToolLoopOutcome",
    "EventNativePlanningResult",
    "FinalResponsePlan",
    "FOLLOWUP_DECISION_SCHEMA_VERSION",
    "GuardedModelToolCallExecution",
    "INTERNAL_TOOL_PLAN_NAME",
    "INTERNAL_TOOL_LOOP_NAME",
    "ModelTurnResult",
    "MAX_TOOL_PLAN_STEPS",
    "TOOL_PLAN_SCHEMA_VERSION",
    "ToolExecutionOutcome",
    "ToolExecutor",
    "ToolObservation",
    "build_tool_observation",
    "build_synthesis_observation",
    "execute_model_tool_call_event",
    "execute_tool_loop_payload",
    "create_model_turn_events",
    "run_assistant_tool_event_loop",
    "run_read_only_agent_loop",
    "skipped_llm_trace",
]
