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
from src.application.assistant.context_projection import build_context_projection, context_projection_trace
from src.application.assistant.context_validation import context_validation_trace, validate_context_use
from src.application.assistant.coverage_verifier import CoverageResult, verify_coverage
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
from src.application.assistant.renderer import render_canonical_tool_result, render_inbound_text
from src.application.assistant.session import build_agent_session_snapshot
from src.application.assistant.settings import AssistantLlmSettings, AssistantSettings
from src.application.assistant.task_contract import build_task_contract
from src.application.assistant.tool_bindings import (
    planner_binding_for_tool,
    planner_config_scoped_tool_names,
    primary_intent_name_for_tool,
    symbol_market_config_tool_names,
    tool_name_for_intent,
)
from src.application.assistant.verifier_hooks import (
    hook_results_from_answer_trace,
    hook_results_from_coverage,
    hook_results_from_tool_check,
)
from src.application.assistant.time_filters import extract_month_filter
from src.application.assistant.tool_policy import DEFAULT_TOOL_POLICY
from src.application.assistant.user_profile import user_profile_trace
from src.infrastructure.openai_chat_completions import (
    OpenAIChatCompletionsError,
    extract_chat_completion_text,
)
from src.infrastructure.openai_responses import OpenAIResponsesError, extract_response_text

AGENT_LOOP_SCHEMA_VERSION = "om-agent-loop-v1"
TOOL_PLAN_SCHEMA_VERSION = "om-tool-plan-v2"
TOOL_PLAN_SYNTHESIS_SCHEMA_VERSION = "om-tool-plan-synthesis-v1"
PLANNER_CONTEXT_USE_SCHEMA_VERSION = "om-planner-context-use-v1"
FOLLOWUP_DECISION_SCHEMA_VERSION = "om-agent-loop-followup-decision-v1"
TOOL_CHECK_SCHEMA_VERSION = "om-agent-tool-check-v1"
INTERNAL_TOOL_PLAN_NAME = "assistant.tool_plan"
MAX_TOOL_PLAN_STEPS = 3
MAX_AGENT_LOOP_ITERATIONS = 3
MAX_AGENT_LOOP_TOOL_CALLS = 5
MAX_PLANNER_ANALYSIS_VIEWS = 12
PLANNER_CONTEXT_USE_MODES = ("none", "carry", "refine", "override", "ambiguous")
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
_INVESTIGATION_RECIPES: tuple[dict[str, Any], ...] = (
    {
        "name": "income_analysis_breakdown",
        "domains": ["income"],
        "task_modes": ["analyze", "compare"],
        "evidence_needs": ["summary", "driver_or_breakdown", "same_scope_comparable_data"],
        "primary_views": ["account_monthly_performance", "account_monthly_income_components", "symbol_income_attribution"],
        "source_tools": ["analysis_query"],
        "followup_tool": "analysis_query",
        "answer_shape": ["conclusion", "drivers", "source_policy"],
    },
    {
        "name": "position_or_quote_diagnosis",
        "domains": ["position", "runtime"],
        "task_modes": ["diagnose", "explain"],
        "evidence_needs": ["current_state", "diagnostic_evidence", "quote_freshness"],
        "primary_views": ["assigned_stock_position_pnl", "quote_freshness", "runtime_tick_status"],
        "source_tools": ["analysis_query"],
        "followup_tool": "analysis_query",
        "answer_shape": ["observation", "cause_chain", "evidence_boundary"],
    },
    {
        "name": "operation_status_readback",
        "domains": ["operation"],
        "task_modes": ["diagnose", "explain"],
        "evidence_needs": ["observed_status", "operation_readback", "receipt_status"],
        "primary_views": ["upgrade_operation_status"],
        "source_tools": ["operation_timeline", "assistant_trace"],
        "followup_tool": "operation_timeline",
        "answer_shape": ["observation", "cause_chain", "evidence_boundary", "next_step"],
    },
    {
        "name": "strategy_replay_review",
        "domains": ["strategy", "candidate"],
        "task_modes": ["analyze", "recommend"],
        "evidence_needs": ["current_state", "constraints", "risk_premise", "dry_run_or_replay"],
        "primary_views": [
            "candidate_filter_diagnostics",
            "close_advice_snapshot",
            "strategy_config_by_symbol_account",
            "strategy_replay_read_surface",
        ],
        "source_tools": ["analysis_query"],
        "followup_tool": "analysis_query",
        "answer_shape": ["judgement", "options", "risk", "premise"],
    },
    {
        "name": "action_lifecycle_audit",
        "domains": ["operation"],
        "task_modes": ["preview_write", "diagnose"],
        "evidence_needs": ["permission_request", "preview_receipt", "operation_readback", "audit"],
        "primary_views": ["upgrade_operation_status"],
        "source_tools": ["operation_timeline", "assistant_trace"],
        "followup_tool": "operation_timeline",
        "answer_shape": ["preview_summary", "risk", "confirmation_handle", "verification_status"],
    },
)
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
    required_capabilities: tuple[str, ...] = ()
    task_contract: dict[str, Any] | None = None
    selected_recipe: dict[str, Any] | None = None
    context_use: dict[str, Any] | None = None
    schema_version: str = TOOL_PLAN_SCHEMA_VERSION

    def public_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "goal": self.goal,
            "required_capabilities": list(self.required_capabilities),
            "context_use": _safe_context_use_payload(self.context_use),
            "steps": [step.public_payload() for step in self.steps],
        }
        if isinstance(self.task_contract, dict) and self.task_contract:
            payload["task_contract"] = _safe_task_contract_payload(self.task_contract)
        if isinstance(self.selected_recipe, dict) and self.selected_recipe:
            payload["selected_recipe"] = _safe_selected_recipe_payload(self.selected_recipe)
        return payload


@dataclass(frozen=True)
class LlmPlannerResult:
    plan: PlannerPlan | None
    trace: dict[str, Any]
    error: AgentToolError | None = None


@dataclass(frozen=True)
class _PlannerProviderAttempt:
    payload: dict[str, Any] | None
    planner_input: dict[str, Any]
    reason: str
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
        authorization_event = {
            "phase": "authorize_tool",
            "tool_name": tool_name,
            "allowed": bool(action_policy.allowed),
            "decision": action_policy_payload,
            "action_policy": action_policy_payload,
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
        if precheck.get("status") == "fail":
            error = AgentToolError(
                code="PRE_TOOL_CHECK_FAILED",
                message="tool call failed pre-tool safety checks",
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
    plan_result = (plan_tools_fn or plan_read_only_tools)(text, settings, loop_context)
    plan_result = _normalize_tool_plan_result(plan_result, question=text, today=today)
    planning, planned_steps = _planning_outcome_from_tool_plan_result(
        plan_result,
        question=text,
        conversation_context=loop_context,
    )
    trace = dict(planning.trace or plan_result.trace)
    trace["agent_loop"] = {
        "schema_version": AGENT_LOOP_SCHEMA_VERSION,
        "enabled": True,
        "planner": "llm_tool_plan",
        "max_steps": steps,
        "steps_used": len(planned_steps),
        "writes_allowed": False,
        "preview_operations_allowed": True,
        "steps": [step.public_payload() for step in planned_steps],
        "final_response": FinalResponsePlan(
            status="pending_tool_execution" if planning.perception is not None else "no_plan",
            reason="canonical renderer will own factual output"
            if planning.perception is not None
            else "planner did not produce an executable assistant capability",
        ).public_payload(),
    }
    return AgentLoopResult(planning=planning, trace=trace, steps=planned_steps)


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
    action_status = "pass" if action_policy.get("allowed") else "deny"
    safety_payload = action_safety if isinstance(action_safety, dict) else {}
    safety_status = str(safety_payload.get("status") or "not_applicable")
    safety_check_status = "pass" if safety_status in {"allow", "allow_followup", "allow_preview"} else safety_status
    planner_status = "fail" if planner_banned else ("pass" if plan_arguments is not None else "not_applicable")
    write_status = "pass" if str(action_policy.get("allowed_effect") or "") in {"read", "none", "preview"} else "fail"
    scope_status, scope_reason = _scope_guard_status(payload=payload, task_contract=task_contract)
    status = "pass"
    if action_status == "deny":
        status = "deny"
    if (
        planner_status == "fail"
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
            {"name": "scope_guard", "status": scope_status, "reason": scope_reason},
            {"name": "write_guard", "status": write_status},
        ],
        "tool_name": str(tool_name or ""),
        "payload_keys": sorted(str(key) for key in (payload or {}).keys()),
        "banned_arguments": planner_banned,
        "action_safety": dict(safety_payload) if safety_payload else {},
    }


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
    output_contract = _output_contract_for_tool(tool_name, payload)
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
    output_contract = _output_contract_for_tool(tool_name, payload)
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
    plan = _normalize_tool_plan(plan, question=question, today=_planner_today_from_context(conversation_context))
    context_validation = _validate_planner_context_use(
        text=question,
        conversation_context=conversation_context,
        plan=plan,
        planner_manifest=_planner_tool_manifest(),
    )
    context_error = _context_validation_error(context_validation)
    if context_error is not None:
        raise context_error
    validate_tool_plan(plan, allow_preview=False)
    task_contract = build_task_contract(
        question=question,
        plan=plan.public_payload(),
        request_context=request.public_payload(),
        today=_planner_today_from_context(conversation_context),
    )
    selected_recipe = _selected_recipe_for_task_contract(task_contract=task_contract.public_payload(), plan=plan)
    plan = _with_selected_recipe(plan, selected_recipe)
    if selected_recipe:
        task_contract = replace(task_contract, selected_recipe=_safe_selected_recipe_payload(selected_recipe))
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
    coverage_result: CoverageResult | None = None
    coverage_payload: dict[str, Any] = {}
    followup_decisions: list[dict[str, Any]] = []
    attempted_followup_gap_signatures: set[str] = set()
    iteration = 1
    tool_events.append(
        {
            "phase": "task_contract",
            "contract": task_contract.public_payload(),
            "selected_recipe": _safe_selected_recipe_payload(selected_recipe),
            "evidence_needs": list(task_contract.required_evidence),
        }
    )

    while True:
        ok, error_payload = _execute_read_plan_steps(
            request=request,
            plan=plan,
            task_contract=task_contract.public_payload(),
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
        coverage_result = verify_coverage(task_contract=task_contract, evidence_bundle=evidence_bundle)
        coverage_payload = coverage_result.public_payload()
        coverage_hook_results = hook_results_from_coverage(coverage_payload)
        tool_events.append({"phase": "coverage_verify", **coverage_payload, "hook_results": coverage_hook_results})
        evidence_gaps = _merge_evidence_gaps(
            _assess_evidence_gaps(plan=plan, evidence_bundle=evidence_bundle, observations=fact_observations),
            list(coverage_result.gaps),
        )
        followup_evidence_gaps = _followup_evidence_gaps(evidence_gaps)
        unattempted_followup_evidence_gaps = _unattempted_followup_evidence_gaps(
            followup_evidence_gaps,
            attempted_followup_gap_signatures=attempted_followup_gap_signatures,
        )
        if followup_evidence_gaps and not unattempted_followup_evidence_gaps:
            stop_decision = _followup_already_attempted_stop_decision(
                evidence_gaps=followup_evidence_gaps,
                revision=len(plan_revisions) + 1,
            )
            followup_decisions.append(stop_decision)
            tool_events.append({"phase": "followup_decision", **stop_decision})
            tool_events.append(
                {
                    "phase": "replan",
                    "status": "gap_already_attempted",
                    "evidence_gaps": [dict(item) for item in followup_evidence_gaps],
                }
            )
            break
        if not _should_replan_read_only(
            ok=ok,
            plan_tools_fn=plan_tools_fn,
            evidence_gaps=unattempted_followup_evidence_gaps,
            iteration=iteration,
            tool_call_count=len(observations),
        ):
            stop_decision = _followup_stop_decision(
                ok=ok,
                plan_tools_fn=plan_tools_fn,
                evidence_gaps=unattempted_followup_evidence_gaps,
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
            evidence_gaps=unattempted_followup_evidence_gaps,
            prior_plan=plan,
            observations=observations,
            revision=len(plan_revisions) + 1,
            followup_decisions=followup_decisions,
            tool_events=tool_events,
        )
        if next_plan is None:
            break
        _record_followup_gap_attempts(
            unattempted_followup_evidence_gaps,
            attempted_followup_gap_signatures=attempted_followup_gap_signatures,
        )
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
        if selected_recipe:
            plan = _with_selected_recipe(plan, selected_recipe)
        plan_revisions.append(_plan_revision_payload(len(plan_revisions) + 1, plan=plan, reason="follow-up evidence-gap plan"))
        iteration += 1

    synthesis = _build_final_response(
        question=question,
        settings=settings,
        plan=plan,
        task_contract=task_contract.public_payload(),
        coverage=coverage_payload,
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
    base_synthesis_trace = _audit_synthesis_trace(dict(synthesis.trace))
    answer_hook_results = hook_results_from_answer_trace(
        synthesis_trace=base_synthesis_trace,
        final_response=final_response_payload,
    )
    synthesis_trace = {
        **base_synthesis_trace,
        "context": _answer_context_trace(question, conversation_context),
        "hook_results": answer_hook_results,
    }
    agent_session = build_agent_session_snapshot(
        request=request,
        command_id=command_id,
        question=question,
        plan=plan.public_payload(),
        plan_revisions=plan_revisions,
        tool_events=tool_events,
        observations=observations,
        task_contract=task_contract.public_payload(),
        evidence_bundle=evidence_bundle,
        coverage=coverage_payload,
        final_response=final_response_payload,
        synthesis_trace=synthesis_trace,
        ok=ok,
    )
    data = {
        "response_text": synthesis.response_text or "",
        "plan": plan.public_payload(),
        "observations": observations,
        "synthesis_observations": llm_observations,
        "task_contract": task_contract.public_payload(),
        "selected_recipe": _safe_selected_recipe_payload(selected_recipe),
        "evidence_bundle": evidence_bundle.public_payload(),
        "coverage": coverage_payload,
        "agent_session": agent_session.public_payload(),
        "tool_events": tool_events,
        "tool_calls_used": len(observations),
        "writes_allowed": False,
        "final_response": final_response_payload,
        "synthesis": synthesis_trace,
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
    task_contract: dict[str, Any],
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
    executor = ToolExecutor(execute_tool_fn=execute_tool_fn, source="agent_loop")
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
        outcome = executor.execute_read_tool(
            request=request,
            task_contract=task_contract,
            index=index,
            tool_name=step.tool_name,
            payload=payload,
            plan_arguments=step.arguments,
        )
        tool_events.append(outcome.authorization_event)
        if not outcome.allowed:
            return False, outcome.error_payload
        if outcome.tool_result is not None:
            tool_results.append(outcome.tool_result)
        if outcome.observation is not None:
            observations.append(outcome.observation)
        if outcome.fact_observation is not None:
            fact_observations.append(outcome.fact_observation)
        if outcome.synthesis_observation is not None:
            llm_observations.append(outcome.synthesis_observation)
        if outcome.result_event is not None:
            tool_events.append(outcome.result_event)
        if not outcome.ok:
            return False, outcome.error_payload
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


def _followup_evidence_gaps(evidence_gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(gap) for gap in evidence_gaps if isinstance(gap, dict) and _evidence_gap_allows_followup(gap)]


def _unattempted_followup_evidence_gaps(
    evidence_gaps: list[dict[str, Any]],
    *,
    attempted_followup_gap_signatures: set[str],
) -> list[dict[str, Any]]:
    return [
        dict(gap)
        for gap in evidence_gaps
        if _followup_gap_signature(gap) not in attempted_followup_gap_signatures
    ]


def _record_followup_gap_attempts(
    evidence_gaps: list[dict[str, Any]],
    *,
    attempted_followup_gap_signatures: set[str],
) -> None:
    for gap in evidence_gaps:
        attempted_followup_gap_signatures.add(_followup_gap_signature(gap))


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


def _followup_already_attempted_stop_decision(
    *,
    evidence_gaps: list[dict[str, Any]],
    revision: int,
) -> dict[str, Any]:
    return _followup_decision_payload(
        revision=revision,
        decision="stop_with_gap",
        status="stopped",
        reason="recoverable evidence gap already attempted once for this scope",
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
            clarification_reason = _clarification_reason_code(clarification)
            if _followup_clarification_should_ask(
                clarification=clarification,
                clarification_reason=clarification_reason,
                evidence_gaps=evidence_gaps,
            ):
                decision = _followup_decision_payload(
                    revision=revision,
                    decision="ask_clarification",
                    status="stopped",
                    reason=clarification,
                    evidence_gaps=evidence_gaps,
                    clarification=clarification,
                    clarification_context=context,
                    clarification_reason=clarification_reason,
                )
                replan_status = "needs_clarification"
            else:
                decision = _followup_decision_payload(
                    revision=revision,
                    decision="stop_with_gap",
                    status="stopped",
                    reason=f"follow-up planner requested clarification for non-critical evidence gap: {clarification}",
                    evidence_gaps=evidence_gaps,
                    clarification_reason=clarification_reason,
                    planner_clarification=clarification,
                )
                replan_status = "clarification_downgraded_to_gap"
            followup_decisions.append(decision)
            tool_events.append({"phase": "followup_decision", **decision})
            tool_events.append(
                {
                    "phase": "replan",
                    "status": replan_status,
                    "error_code": plan_result.error.code,
                    "clarification_reason": clarification_reason,
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
    allowlist_rejection = _followup_tool_allowlist_rejection(next_plan, evidence_gaps=evidence_gaps)
    if allowlist_rejection:
        decision = _followup_decision_payload(
            revision=revision,
            decision="call_tool",
            status="rejected",
            reason=allowlist_rejection,
            evidence_gaps=evidence_gaps,
            plan=next_plan,
        )
        followup_decisions.append(decision)
        tool_events.append({"phase": "followup_decision", **decision})
        tool_events.append(
            {
                "phase": "replan",
                "status": "tool_not_allowed_for_gap",
                "reason": allowlist_rejection,
                "evidence_gaps": [dict(item) for item in evidence_gaps],
                "steps": [step.public_payload() for step in next_plan.steps],
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
    if prior_plan.selected_recipe and not next_plan.selected_recipe:
        next_plan = _with_selected_recipe(next_plan, _safe_selected_recipe_payload(prior_plan.selected_recipe))
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


def _followup_decision_payload(
    *,
    revision: int,
    decision: str,
    status: str,
    reason: str,
    evidence_gaps: list[dict[str, Any]],
    plan: PlannerPlan | None = None,
    clarification: str | None = None,
    clarification_context: dict[str, Any] | None = None,
    clarification_reason: str | None = None,
    planner_clarification: str | None = None,
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
        payload["clarification_request"] = _clarification_request_payload(
            clarification,
            context=clarification_context,
        )
    if clarification_reason:
        payload["clarification_reason"] = str(clarification_reason).strip()
    if planner_clarification:
        payload["planner_clarification"] = str(planner_clarification).strip()
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


def _followup_duplicate_rejection(
    plan: PlannerPlan,
    *,
    prior_plan: PlannerPlan,
    observations: list[dict[str, Any]],
) -> str:
    if _followup_plan_duplicate_payload(plan) == _followup_plan_duplicate_payload(prior_plan):
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


def _followup_plan_duplicate_payload(plan: PlannerPlan) -> dict[str, Any]:
    payload = dict(plan.public_payload())
    payload.pop("selected_recipe", None)
    return payload


_FOLLOWUP_GAP_SIGNATURE_KEYS = (
    "kind",
    "code",
    "recoverable_by",
    "suggested_tool",
    "missing_accounts",
    "covered_accounts",
    "accounts",
    "symbols",
    "months",
    "periods",
    "suggested_views",
    "suggested_fields",
    "unknown_column",
    "error_code",
    "operation_ids",
    "command_ids",
)


def _followup_gap_signature(gap: dict[str, Any]) -> str:
    scoped = {
        key: _canonical_followup_signature_value(gap.get(key))
        for key in _FOLLOWUP_GAP_SIGNATURE_KEYS
        if key in gap and _has_followup_signature_value(gap.get(key))
    }
    if not scoped:
        return json.dumps({"kind": str(gap.get("kind") or gap.get("code") or "")}, ensure_ascii=False, sort_keys=True)
    return json.dumps(scoped, ensure_ascii=False, sort_keys=True, default=str)


def _has_followup_signature_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _canonical_followup_signature_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _canonical_followup_signature_value(value[key])
            for key in sorted(value, key=lambda item: str(item))
            if _has_followup_signature_value(value[key])
        }
    if isinstance(value, (list, tuple, set)):
        items = [_canonical_followup_signature_value(item) for item in value if _has_followup_signature_value(item)]
        return sorted(items, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str))
    if isinstance(value, str):
        return value.strip()
    return value


def _followup_observation_signature(observation: dict[str, Any]) -> str:
    payload = observation.get("payload") if isinstance(observation.get("payload"), dict) else {}
    return _followup_step_signature(str(observation.get("tool_name") or ""), payload)


def _followup_tool_allowlist_rejection(plan: PlannerPlan, *, evidence_gaps: list[dict[str, Any]]) -> str:
    allowed_tools = set(_followup_allowed_tools(evidence_gaps))
    for step in plan.steps:
        if step.tool_name not in allowed_tools:
            return f"follow-up plan used {step.tool_name}, which is not allowed for the recoverable evidence gap"
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
    if analysis_gaps:
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
    operation_gaps = [
        gap
        for gap in evidence_gaps
        if isinstance(gap, dict)
        and str(gap.get("recoverable_by") or "") == "operation_timeline"
    ]
    if not operation_gaps:
        return ""
    if not any(step.tool_name == "operation_timeline" for step in plan.steps):
        return "follow-up plan did not include operation_timeline for the operation evidence gap"
    for step in plan.steps:
        if step.tool_name != "operation_timeline":
            return "follow-up plan for operation evidence gap used a non-operation-timeline tool"
    for gap in operation_gaps:
        if any(_step_closes_operation_gap(step, gap=gap) for step in plan.steps):
            continue
        return "follow-up operation query did not target upgrade operation status evidence"
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


def _step_closes_operation_gap(step: PlannerPlanStep, *, gap: dict[str, Any]) -> bool:
    if step.tool_name == "operation_timeline":
        operation_types = step.arguments.get("operation_types") or step.arguments.get("operation_type") or []
        if isinstance(operation_types, str):
            operation_type_values = [operation_types]
        elif isinstance(operation_types, (list, tuple, set)):
            operation_type_values = [str(item) for item in operation_types]
        else:
            operation_type_values = []
        if not operation_type_values:
            return True
        return any("upgrade" in item for item in operation_type_values)
    if step.tool_name == "analysis_catalog":
        return _step_uses_any_analysis_view(step, {"upgrade_operation_status"})
    if step.tool_name == "analysis_query":
        return _step_uses_any_analysis_view(step, {"upgrade_operation_status"})
    return False


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


def _merge_evidence_gaps(*gap_groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for gaps in gap_groups:
        for gap in gaps:
            if not isinstance(gap, dict):
                continue
            signature = (
                gap.get("kind"),
                gap.get("recoverable_by"),
                tuple(gap.get("missing_accounts") or []),
                tuple(gap.get("covered_accounts") or []),
                tuple(gap.get("symbols") or []),
                tuple(gap.get("accounts") or []),
                tuple(gap.get("suggested_views") or []),
                gap.get("suggested_tool"),
            )
            if signature in seen:
                continue
            out.append(dict(gap))
            seen.add(signature)
    return out


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

    response_fn = create_response_fn or provider_create_response_fn(provider)
    planner_payload = _planner_input_payload(text, conversation_context=conversation_context)
    attempt = _planner_provider_attempt(
        planner_payload=planner_payload,
        response_fn=response_fn,
        api_key=api_key,
        provider=provider,
        llm_settings=llm_settings,
    )
    if attempt.error is not None:
        return LlmPlannerResult(
            plan=None,
            trace=_llm_trace(
                llm_settings,
                attempted=True,
                reason=attempt.reason,
                error_code=attempt.error.code,
                conversation_context=conversation_context,
                planner_input=attempt.planner_input,
            ),
            error=attempt.error,
        )
    if attempt.payload is None:
        return LlmPlannerResult(
            plan=None,
            trace=_llm_trace(
                llm_settings,
                attempted=True,
                reason=attempt.reason,
                error_code="LLM_PROVIDER_ERROR",
                conversation_context=conversation_context,
                planner_input=attempt.planner_input,
            ),
            error=AgentToolError(
                code="LLM_PROVIDER_ERROR",
                message="LLM planner returned invalid JSON.",
                details={"provider": provider},
            ),
        )

    plan, planner_context_use, context_validation, plan_error = _validated_planner_payload(
        attempt.payload,
        text=text,
        conversation_context=conversation_context,
        planner_manifest=planner_payload.get("tools") if isinstance(planner_payload.get("tools"), list) else [],
    )
    if plan_error is not None:
        if _context_validation_repair_allowed(plan_error):
            repair_result = _repair_context_validation_plan(
                text=text,
                settings=settings,
                conversation_context=conversation_context,
                response_fn=response_fn,
                api_key=api_key,
                provider=provider,
                initial_validation=context_validation,
                initial_planner_input=attempt.planner_input,
                initial_context_use=planner_context_use,
                initial_error=plan_error,
            )
            if repair_result is not None:
                return repair_result
        return LlmPlannerResult(
            plan=None,
            trace=_llm_trace(
                llm_settings,
                attempted=True,
                reason=_planner_error_trace_reason(plan_error),
                error_code=plan_error.code,
                conversation_context=conversation_context,
                planner_input=attempt.planner_input,
                planner_context_use=planner_context_use,
                context_validation=context_validation,
            ),
            error=plan_error,
        )
    return LlmPlannerResult(
        plan=plan,
        trace=_llm_trace(
            llm_settings,
            attempted=True,
            reason="accepted",
            schema_version=TOOL_PLAN_SCHEMA_VERSION,
            conversation_context=conversation_context,
            planner_input=attempt.planner_input,
            planner_context_use=planner_context_use,
            context_validation=context_validation,
        ),
    )


def _planner_provider_attempt(
    *,
    planner_payload: dict[str, Any],
    response_fn: CreateStructuredResponseFn,
    api_key: str,
    provider: str,
    llm_settings: AssistantLlmSettings,
) -> _PlannerProviderAttempt:
    planner_input_text = json.dumps(planner_payload, ensure_ascii=False, sort_keys=True)
    planner_input_trace = _planner_input_trace(planner_payload, planner_input_text)
    try:
        response = response_fn(
            api_key=api_key,
            base_url=llm_settings.base_url,
            model=llm_settings.model,
            input_text=planner_input_text,
            instructions=_planner_instructions(),
            json_schema=tool_plan_json_schema(),
            timeout=int(llm_settings.timeout_seconds),
            max_output_tokens=int(llm_settings.max_output_tokens),
        )
    except (OpenAIResponsesError, OpenAIChatCompletionsError) as err:
        return _PlannerProviderAttempt(
            payload=None,
            planner_input=planner_input_trace,
            reason="provider_error",
            error=AgentToolError(
                code="LLM_PROVIDER_ERROR",
                message=str(err),
                details={"provider": provider, "http_status": err.http_status},
            ),
        )
    except Exception as err:
        return _PlannerProviderAttempt(
            payload=None,
            planner_input=planner_input_trace,
            reason="provider_error",
            error=AgentToolError(
                code="LLM_PROVIDER_ERROR",
                message=f"LLM planner provider failed: {type(err).__name__}: {err}",
                details={"provider": provider},
            ),
        )
    payload = _parse_provider_payload(response)
    if payload is None:
        return _PlannerProviderAttempt(
            payload=None,
            planner_input=planner_input_trace,
            reason="invalid_provider_output",
        )
    return _PlannerProviderAttempt(
        payload=payload,
        planner_input=planner_input_trace,
        reason="accepted",
    )


def _validated_planner_payload(
    payload: dict[str, Any],
    *,
    text: str,
    conversation_context: dict[str, Any] | None,
    planner_manifest: list[dict[str, Any]],
) -> tuple[PlannerPlan | None, dict[str, Any] | None, dict[str, Any] | None, AgentToolError | None]:
    planner_context_use: dict[str, Any] | None = None
    context_validation: dict[str, Any] | None = None
    try:
        plan = parse_tool_plan_payload(payload)
        plan = _normalize_tool_plan(plan, question=text, today=_planner_today_from_context(conversation_context))
        planner_context_use = plan.context_use
        context_validation = _validate_planner_context_use(
            text=text,
            conversation_context=conversation_context,
            plan=plan,
            planner_manifest=planner_manifest,
        )
        context_error = _context_validation_error(context_validation)
        if context_error is not None:
            return None, planner_context_use, context_validation, context_error
        if not plan.steps:
            return None, planner_context_use, context_validation, _no_tool_plan_error()
        validate_tool_plan(plan, question=text)
    except AgentToolError as err:
        return None, planner_context_use, context_validation, err
    return plan, planner_context_use, context_validation, None


def _repair_context_validation_plan(
    *,
    text: str,
    settings: AssistantSettings,
    conversation_context: dict[str, Any] | None,
    response_fn: CreateStructuredResponseFn,
    api_key: str,
    provider: str,
    initial_validation: dict[str, Any] | None,
    initial_planner_input: dict[str, Any],
    initial_context_use: dict[str, Any] | None,
    initial_error: AgentToolError,
) -> LlmPlannerResult | None:
    if not isinstance(initial_validation, dict):
        return None
    llm_settings = settings.llm
    repair_context = _conversation_context_with_validation_repair(conversation_context, initial_validation)
    repair_payload = _planner_input_payload(text, conversation_context=repair_context)
    repair_attempt = _planner_provider_attempt(
        planner_payload=repair_payload,
        response_fn=response_fn,
        api_key=api_key,
        provider=provider,
        llm_settings=llm_settings,
    )
    repair_trace = _context_validation_repair_trace(
        attempted=True,
        status="provider_error" if repair_attempt.error is not None else "planned",
        initial_validation=initial_validation,
        initial_planner_input=initial_planner_input,
        repair_planner_input=repair_attempt.planner_input,
        error=repair_attempt.error,
    )
    if repair_attempt.error is not None or repair_attempt.payload is None:
        return LlmPlannerResult(
            plan=None,
            trace=_llm_trace(
                llm_settings,
                attempted=True,
                reason=_planner_error_trace_reason(initial_error),
                error_code=initial_error.code,
                conversation_context=conversation_context,
                planner_input=initial_planner_input,
                planner_context_use=initial_context_use,
                context_validation=initial_validation,
                context_validation_repair=repair_trace,
            ),
            error=initial_error,
        )
    plan, planner_context_use, context_validation, plan_error = _validated_planner_payload(
        repair_attempt.payload,
        text=text,
        conversation_context=conversation_context,
        planner_manifest=repair_payload.get("tools") if isinstance(repair_payload.get("tools"), list) else [],
    )
    if plan_error is not None:
        repair_trace = _context_validation_repair_trace(
            attempted=True,
            status="rejected",
            initial_validation=initial_validation,
            initial_planner_input=initial_planner_input,
            repair_planner_input=repair_attempt.planner_input,
            repaired_validation=context_validation,
            error=plan_error,
        )
        return LlmPlannerResult(
            plan=None,
            trace=_llm_trace(
                llm_settings,
                attempted=True,
                reason=_planner_error_trace_reason(plan_error),
                error_code=plan_error.code,
                conversation_context=conversation_context,
                planner_input=repair_attempt.planner_input,
                planner_context_use=planner_context_use,
                context_validation=context_validation,
                context_validation_repair=repair_trace,
            ),
            error=plan_error,
        )
    repair_trace = _context_validation_repair_trace(
        attempted=True,
        status="accepted",
        initial_validation=initial_validation,
        initial_planner_input=initial_planner_input,
        repair_planner_input=repair_attempt.planner_input,
        repaired_validation=context_validation,
    )
    return LlmPlannerResult(
        plan=plan,
        trace=_llm_trace(
            llm_settings,
            attempted=True,
            reason="accepted",
            schema_version=TOOL_PLAN_SCHEMA_VERSION,
            conversation_context=conversation_context,
            planner_input=repair_attempt.planner_input,
            planner_context_use=planner_context_use,
            context_validation=context_validation,
            context_validation_repair=repair_trace,
        ),
    )


def _validate_planner_context_use(
    *,
    text: str,
    conversation_context: dict[str, Any] | None,
    plan: PlannerPlan,
    planner_manifest: list[dict[str, Any]],
) -> dict[str, Any]:
    return validate_context_use(
        current_user_message=text,
        context_projection=conversation_context.get("context_projection")
        if isinstance(conversation_context, dict) and isinstance(conversation_context.get("context_projection"), dict)
        else None,
        plan_payload=plan.public_payload(),
        planner_manifest=planner_manifest,
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
                "planner_repairable": False,
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
                "planner_repairable": True,
            },
        )
    return AgentToolError(
        code="PLAN_CONTEXT_INVALID",
        message="Planner 上下文校验未通过，已阻止执行工具。",
        details={"context_validation": dict(validation), "planner_repairable": False},
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


def _context_validation_repair_allowed(err: AgentToolError) -> bool:
    details = err.details if isinstance(err.details, dict) else {}
    validation = details.get("context_validation") if isinstance(details.get("context_validation"), dict) else {}
    return err.code == "PLAN_CONTEXT_INVALID" and str(validation.get("status") or "") == "blocked"


def _planner_error_trace_reason(err: AgentToolError) -> str:
    if err.code == "PLAN_CONTEXT_AMBIGUOUS":
        return "context_validation_ask_clarification"
    if err.code == "PLAN_CONTEXT_INVALID":
        return "context_validation_blocked"
    if err.code == "NEEDS_CLARIFICATION":
        return "no_plan"
    return "invalid_plan"


def _conversation_context_with_validation_repair(
    conversation_context: dict[str, Any] | None,
    validation: dict[str, Any],
) -> dict[str, Any]:
    context = dict(conversation_context or {})
    context["context_validation_repair"] = _context_validation_repair_payload(validation)
    return context


def _context_validation_repair_payload(validation: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "schema_version": "om-context-validation-repair-v1",
        "status": str(validation.get("status") or ""),
        "code": str(validation.get("code") or ""),
        "context_use_mode": str(validation.get("context_use_mode") or ""),
        "referenced_turn_ids": list(validation.get("referenced_turn_ids") or []),
        "referenced_evidence_refs": list(validation.get("referenced_evidence_refs") or []),
        "validation": context_validation_trace(validation),
        "instruction": (
            "The previous plan failed deterministic context validation. Return a corrected plan that only "
            "uses declared, visible context refs. If no safe reference exists, return context_use.mode=ambiguous, "
            "requires_clarification=true, and steps=[]."
        ),
    }
    violation = validation.get("violation") if isinstance(validation.get("violation"), dict) else {}
    if violation:
        payload["violation"] = _safe_context_validation_violation(violation)
    slots = validation.get("validated_slots") if isinstance(validation.get("validated_slots"), dict) else {}
    if slots:
        payload["validated_slots"] = dict(slots)
    return payload


def _safe_context_validation_violation(violation: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "reason",
        "slot",
        "tool_name",
        "missing_turn_ids",
        "missing_evidence_refs",
        "declared_values",
        "available_values",
        "plan_values",
        "current_values",
        "inherited_values",
        "extra_arguments",
        "allowed_arguments",
        "banned_arguments",
        "step_count",
    )
    out: dict[str, Any] = {}
    for key in allowed:
        value = violation.get(key)
        if value not in (None, "", [], {}):
            out[key] = value
    return out


def _context_validation_repair_trace(
    *,
    attempted: bool,
    status: str,
    initial_validation: dict[str, Any],
    initial_planner_input: dict[str, Any],
    repair_planner_input: dict[str, Any] | None = None,
    repaired_validation: dict[str, Any] | None = None,
    error: AgentToolError | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "om-context-validation-repair-trace-v1",
        "attempted": bool(attempted),
        "status": str(status or ""),
        "initial_validation": context_validation_trace(initial_validation),
        "initial_planner_input": dict(initial_planner_input),
    }
    if repair_planner_input is not None:
        payload["repair_planner_input"] = dict(repair_planner_input)
    if repaired_validation is not None:
        payload["repaired_validation"] = context_validation_trace(repaired_validation)
    if error is not None:
        payload["error_code"] = error.code
    return payload


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
    allowed_keys = {
        "schema_version",
        "goal",
        "required_capabilities",
        "steps",
        "task_contract",
        "selected_recipe",
        "context_use",
    }
    extra_keys = sorted(str(key) for key in payload if str(key) not in allowed_keys)
    if extra_keys:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"tool plan has unsupported top-level fields: {', '.join(extra_keys)}",
            details={"extra_fields": extra_keys, "allowed_fields": sorted(allowed_keys)},
        )
    required_keys = {"schema_version", "goal", "required_capabilities", "steps"}
    missing_keys = sorted(key for key in required_keys if key not in payload)
    if missing_keys:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"tool plan is missing required top-level fields: {', '.join(missing_keys)}",
            details={"missing_fields": missing_keys, "required_fields": sorted(required_keys)},
        )
    schema_version = str(payload.get("schema_version") or "").strip()
    if schema_version != TOOL_PLAN_SCHEMA_VERSION:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="unsupported tool plan schema version",
            details={"schema_version": schema_version, "expected": TOOL_PLAN_SCHEMA_VERSION},
        )
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
        required_capabilities=_normalized_capabilities(payload.get("required_capabilities")),
        task_contract=_normalized_planner_task_contract(payload.get("task_contract")),
        selected_recipe=_normalized_selected_recipe(payload.get("selected_recipe")),
        context_use=_normalized_context_use(payload.get("context_use")),
        schema_version=schema_version,
    )


def _normalized_planner_task_contract(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
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
    out = {str(key): value[key] for key in value if str(key) in allowed}
    return out or None


def _normalized_selected_recipe(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    name = str(value.get("name") or "").strip()
    canonical = _recipe_by_name(name)
    if canonical is None:
        return None
    out = dict(canonical)
    reason = str(value.get("reason") or "").strip()
    if reason:
        out["reason"] = reason
    return out


def _default_context_use() -> dict[str, Any]:
    return {
        "schema_version": PLANNER_CONTEXT_USE_SCHEMA_VERSION,
        "mode": "none",
        "referenced_turn_ids": [],
        "referenced_evidence_refs": [],
        "inherited_slots": {},
        "current_message_slots": {},
        "override_slots": {},
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
    out["inherited_slots"] = _context_use_slots(value.get("inherited_slots"))
    out["current_message_slots"] = _context_use_slots(value.get("current_message_slots"))
    out["override_slots"] = _context_use_slots(value.get("override_slots"))
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
        if question is not None and not _question_requests_preview_operation(question):
            raise AgentToolError(
                code="PLAN_RISK_MISMATCH",
                message="LLM 规划了写入预览，但用户原文不像写入预览请求。",
                hint="preview-write 只能用于明确的记录、修改、升级或模型切换请求；普通查询必须规划为只读工具。",
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
    trade_update_tokens = ("premium", "权利金", "合约数", "张数", "close", "平仓价")
    if any(token in compact for token in ("是多少", "多少", "是什么", "当前", "现在", "目前", "查询", "查看")):
        return False
    if "改成" in compact and any(token in compact for token in trade_update_tokens):
        return True
    if ("设置" in compact or "修改监控" in compact or "配置标的" in compact) and any(token in compact for token in symbol_setting_tokens):
        return True
    return "立即升级" in compact or "切换模型" in compact or "使用模型" in compact


def _planning_outcome_from_tool_plan_result(
    result: LlmPlannerResult,
    *,
    question: str,
    conversation_context: dict[str, Any] | None,
) -> tuple[AgentLoopPlanningOutcome, tuple[AgentLoopStep, ...]]:
    if result.error is not None:
        return AgentLoopPlanningOutcome(perception=None, trace=dict(result.trace), error=result.error), ()
    if result.plan is None:
        return AgentLoopPlanningOutcome(perception=None, trace=dict(result.trace)), ()
    if not result.plan.steps:
        return AgentLoopPlanningOutcome(perception=None, trace={**dict(result.trace), "reason": "no_plan"}, error=_no_tool_plan_error()), ()
    try:
        validate_tool_plan(result.plan, question=question)
    except AgentToolError as err:
        return AgentLoopPlanningOutcome(perception=None, trace={**dict(result.trace), "reason": "invalid_plan", "error_code": err.code}, error=err), ()
    planned_steps = tuple(
        _agent_loop_step_from_plan_step(index=index, step=step, question=question)
        for index, step in enumerate(result.plan.steps, start=1)
    )
    precheck_error = _planned_step_precheck_error(planned_steps)
    if precheck_error is not None:
        return (
            AgentLoopPlanningOutcome(
                perception=None,
                trace={**dict(result.trace), "reason": "pre_tool_check_failed", "error_code": precheck_error.code},
                error=precheck_error,
            ),
            planned_steps,
        )
    preview_step = _single_preview_step(result.plan)
    if preview_step is not None:
        return (
            AgentLoopPlanningOutcome(
                perception=PerceptionResult(
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
        AgentLoopPlanningOutcome(
            perception=PerceptionResult(
                intent_name="tool_plan",
                arguments={"plan": result.plan.public_payload()},
                source="agent_loop_plan",
                confidence=1.0,
            ),
            trace=dict(result.trace),
        ),
        planned_steps,
    )


def _planned_step_precheck_error(steps: tuple[AgentLoopStep, ...]) -> AgentToolError | None:
    for step in steps:
        precheck = step.precheck if isinstance(step.precheck, dict) else {}
        if precheck.get("status") != "fail":
            continue
        return AgentToolError(
            code="PRE_TOOL_CHECK_FAILED",
            message="planned tool call failed pre-tool safety checks",
            details={"tool_name": step.tool_name, "precheck": precheck},
        )
    return None


def _agent_loop_step_from_plan_step(*, index: int, step: PlannerPlanStep, question: str = "") -> AgentLoopStep:
    kind = _plan_step_kind(step.tool_name)
    action_policy_payload: dict[str, Any] | None = None
    action_safety_payload: dict[str, Any] | None = None
    precheck: dict[str, Any] | None = None
    hook_results: tuple[dict[str, Any], ...] = ()
    if kind == "preview":
        call = ToolCall(tool_name=step.tool_name, payload=dict(step.arguments))
        action_policy = decide_tool_action_policy(
            call=call,
            request=None,
            task_contract=None,
            source="agent_loop_plan",
            tool_policy=DEFAULT_TOOL_POLICY,
        )
        action_policy_payload = action_policy.public_payload()
        action_safety = assess_action_safety(
            question=question,
            task_contract=None,
            tool_name=step.tool_name,
            payload=dict(step.arguments),
            action_policy=action_policy_payload,
            source="agent_loop_plan",
        )
        action_safety_payload = action_safety.public_payload()
        precheck = _pre_tool_check(
            tool_name=step.tool_name,
            payload=dict(step.arguments),
            plan_arguments=dict(step.arguments),
            task_contract=None,
            action_policy=action_policy_payload,
            action_safety=action_safety_payload,
        )
        hook_results = tuple(hook_results_from_tool_check(precheck))
    return AgentLoopStep(
        index=index,
        phase="plan_tool",
        status="planned",
        intent_name=step.tool_name if kind == "preview" else None,
        tool_name=step.tool_name,
        arguments=dict(step.arguments),
        purpose=step.purpose,
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
        "strategy",
        "field",
        "function",
        "option_type",
        "side",
        "strike",
        "expiration",
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


def _recipe_by_name(name: str) -> dict[str, Any] | None:
    normalized = str(name or "").strip()
    for recipe in _INVESTIGATION_RECIPES:
        if recipe.get("name") == normalized:
            return _visible_recipe_payload(recipe)
    return None


def _visible_recipe_payload(recipe: dict[str, Any]) -> dict[str, Any]:
    payload = {key: recipe[key] for key in recipe if key != "reason"}
    views = [
        str(view)
        for view in payload.get("primary_views") or []
        if str(view) in ANALYSIS_VIEW_SPECS or str(view) == "upgrade_operation_status"
    ]
    payload["primary_views"] = views
    return _safe_selected_recipe_payload(payload)


def _selected_recipe_for_task_contract(*, task_contract: dict[str, Any], plan: PlannerPlan) -> dict[str, Any]:
    declared = _safe_selected_recipe_payload(plan.selected_recipe or {}) if isinstance(plan.selected_recipe, dict) else {}
    if declared.get("name"):
        return {**declared, "match_source": "planner_declared"}
    inferred = _infer_recipe_from_task_contract(task_contract)
    if inferred:
        return {**inferred, "match_source": "runtime_inferred"}
    return {}


def _infer_recipe_from_task_contract(task_contract: dict[str, Any]) -> dict[str, Any]:
    domain = str(task_contract.get("domain") or "").strip()
    task_mode = str(task_contract.get("task_mode") or "").strip()
    requested_effect = str(task_contract.get("requested_effect") or "").strip()
    required_evidence = {str(item).strip() for item in task_contract.get("required_evidence") or [] if str(item).strip()}
    required_answer = {str(item).strip() for item in task_contract.get("required_answer") or [] if str(item).strip()}
    intent_families = {str(item).strip() for item in task_contract.get("intent_families") or [] if str(item).strip()}
    if requested_effect == "preview_write" or required_evidence.intersection({"permission_request", "preview_receipt", "audit"}):
        recipe = _recipe_by_name("action_lifecycle_audit")
    elif domain == "income" and (
        task_mode in {"analyze", "compare"} or required_evidence.intersection({"driver_or_breakdown", "same_scope_comparable_data"})
    ):
        recipe = _recipe_by_name("income_analysis_breakdown")
    elif domain == "strategy" and (
        task_mode in {"analyze", "recommend"}
        or required_evidence.intersection({"dry_run_or_replay", "risk_premise"})
    ):
        recipe = _recipe_by_name("strategy_replay_review")
    elif domain == "candidate" and (
        task_mode in {"analyze", "recommend"} and required_evidence.intersection({"dry_run_or_replay", "risk_premise"})
    ):
        recipe = _recipe_by_name("strategy_replay_review")
    elif domain == "operation" and (
        task_mode in {"diagnose", "explain"}
        or required_evidence.intersection({"operation_readback", "receipt_status", "observed_status"})
        or required_answer.intersection({"command_status", "current_version", "target_version", "release_status"})
    ):
        recipe = _recipe_by_name("operation_status_readback")
    elif domain in {"position", "runtime"} and (
        task_mode in {"diagnose", "explain"}
        or required_evidence.intersection({"diagnostic_evidence", "quote_freshness"})
        or intent_families.intersection({"assigned_stock_pnl", "runtime_status"})
    ):
        recipe = _recipe_by_name("position_or_quote_diagnosis")
    else:
        recipe = None
    return recipe or {}


def _with_selected_recipe(plan: PlannerPlan, selected_recipe: dict[str, Any]) -> PlannerPlan:
    recipe = _safe_selected_recipe_payload(selected_recipe)
    if not recipe:
        return plan
    if plan.selected_recipe == recipe:
        return plan
    return PlannerPlan(
        goal=plan.goal,
        steps=plan.steps,
        required_capabilities=plan.required_capabilities,
        task_contract=dict(plan.task_contract) if isinstance(plan.task_contract, dict) else None,
        selected_recipe=recipe,
        context_use=dict(plan.context_use) if isinstance(plan.context_use, dict) else None,
        schema_version=plan.schema_version,
    )


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
    changed = False
    steps: list[PlannerPlanStep] = []
    monthly_step_index = 0
    monthly_step_count = sum(1 for step in plan.steps if step.tool_name == "monthly_income_report")
    for step in plan.steps:
        arguments = dict(step.arguments)
        if step.tool_name == "monthly_income_report":
            monthly_step_index += 1
            purpose_months = _extract_month_filters(step.purpose, today=today)
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
        if arguments == step.arguments:
            steps.append(step)
        else:
            steps.append(PlannerPlanStep(id=step.id, tool_name=step.tool_name, arguments=arguments, purpose=step.purpose))
    if not changed:
        return plan
    return PlannerPlan(
        goal=plan.goal,
        steps=tuple(steps),
        required_capabilities=plan.required_capabilities,
        task_contract=dict(plan.task_contract) if isinstance(plan.task_contract, dict) else None,
        selected_recipe=dict(plan.selected_recipe) if isinstance(plan.selected_recipe, dict) else None,
        context_use=dict(plan.context_use) if isinstance(plan.context_use, dict) else None,
        schema_version=plan.schema_version,
    )


def _tool_plan_step_action(arguments: dict[str, Any]) -> str:
    return str(arguments.get("action") or "").strip().lower()


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
            "task_contract": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "schema_version": {"type": "string", "enum": ["om-agent-task-contract-v1"]},
                    "goal": {"type": "string"},
                    "domain": {
                        "type": "string",
                        "enum": ["income", "position", "candidate", "config", "operation", "runtime", "strategy", "general"],
                    },
                    "task_mode": {
                        "type": "string",
                        "enum": ["summarize", "analyze", "compare", "diagnose", "explain", "recommend", "preview_write"],
                    },
                    "requested_effect": {"type": "string", "enum": ["read", "preview_write", "prohibited"]},
                    "intent_families": {"type": "array", "items": {"type": "string"}},
                    "scope": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "accounts": {"type": "array", "items": {"type": "string"}},
                            "requested_accounts": {"type": "array", "items": {"type": "string"}},
                            "symbols": {"type": "array", "items": {"type": "string"}},
                            "requested_symbols": {"type": "array", "items": {"type": "string"}},
                            "months": {"type": "array", "items": {"type": "string"}},
                            "requested_months": {"type": "array", "items": {"type": "string"}},
                            "config_keys": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "required_answer": {"type": "array", "items": {"type": "string"}},
                    "optional_answer": {"type": "array", "items": {"type": "string"}},
                    "required_evidence": {"type": "array", "items": {"type": "string"}},
                    "answer_shape": {"type": "array", "items": {"type": "string"}},
                    "constraints": {"type": "array", "items": {"type": "string"}},
                    "ambiguities": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "schema_version",
                    "goal",
                    "domain",
                    "task_mode",
                    "requested_effect",
                    "scope",
                    "required_answer",
                    "required_evidence",
                    "answer_shape",
                ],
            },
            "selected_recipe": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": [
                            "income_analysis_breakdown",
                            "position_or_quote_diagnosis",
                            "operation_status_readback",
                            "strategy_replay_review",
                            "action_lifecycle_audit",
                        ],
                    },
                    "reason": {"type": "string"},
                    "domains": {"type": "array", "items": {"type": "string"}},
                    "task_modes": {"type": "array", "items": {"type": "string"}},
                    "evidence_needs": {"type": "array", "items": {"type": "string"}},
                    "primary_views": {"type": "array", "items": {"type": "string"}},
                    "source_tools": {"type": "array", "items": {"type": "string"}},
                    "external_evidence": {"type": "array", "items": {"type": "string"}},
                    "followup_tool": {"type": "string"},
                    "answer_shape": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name"],
            },
            "context_use": _planner_context_use_json_schema(),
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
        "required": ["schema_version", "goal", "required_capabilities", "steps"],
    }


def _planner_context_use_json_schema() -> dict[str, Any]:
    slot_values_schema: dict[str, Any] = {
        "type": "array",
        "items": {"type": ["string", "number", "boolean"]},
    }
    slots_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": slot_values_schema,
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_version": {"type": "string", "enum": [PLANNER_CONTEXT_USE_SCHEMA_VERSION]},
            "mode": {"type": "string", "enum": list(PLANNER_CONTEXT_USE_MODES)},
            "referenced_turn_ids": {"type": "array", "items": {"type": "string"}},
            "referenced_evidence_refs": {"type": "array", "items": {"type": "string"}},
            "inherited_slots": slots_schema,
            "current_message_slots": slots_schema,
            "override_slots": slots_schema,
            "requires_clarification": {"type": "boolean"},
            "clarification_question": {"type": ["string", "null"]},
            "reason": {"type": "string"},
        },
        "required": [
            "schema_version",
            "mode",
            "referenced_turn_ids",
            "referenced_evidence_refs",
            "inherited_slots",
            "current_message_slots",
            "override_slots",
            "requires_clarification",
            "clarification_question",
        ],
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
    task_contract: dict[str, Any] | None = None,
    coverage: dict[str, Any] | None = None,
    evidence_bundle: Any | None = None,
    fact_observations: list[dict[str, Any]],
    llm_observations: list[dict[str, Any]],
    conversation_context: dict[str, Any] | None,
    synthesize_response_fn: AgentLoopSynthesizeFn | None,
    ok: bool,
    error_payload: dict[str, Any] | None,
    followup_decisions: list[dict[str, Any]] | None = None,
) -> LlmSynthesisResult:
    clarification_decision = _followup_clarification_decision(followup_decisions or [])
    clarification = _clarification_text_from_decision(clarification_decision)
    if clarification:
        clarification_request = (
            clarification_decision.get("clarification_request")
            if isinstance(clarification_decision.get("clarification_request"), dict)
            else _clarification_request_payload(clarification, context=conversation_context)
        )
        return LlmSynthesisResult(
            response_text=clarification,
            trace={
                "attempted": False,
                "reason": "ask_clarification",
                "schema_version": TOOL_PLAN_SYNTHESIS_SCHEMA_VERSION,
                "clarification_request": clarification_request,
            },
        )
    if not ok:
        return LlmSynthesisResult(
            response_text=_fallback_response(
                plan=plan,
                observations=llm_observations,
                error_payload=error_payload,
            ),
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
            task_contract=task_contract,
            coverage=coverage,
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
                guard = _verify_answer_guard(
                    synthesis.response_text,
                    observations=fact_observations,
                    evidence_bundle=evidence_bundle,
                    task_contract=task_contract,
                    coverage=coverage,
                )
                if not guard["violations"]:
                    return LlmSynthesisResult(
                        response_text=_combine_grounded_response(fact_text, synthesis.response_text),
                        trace={
                            **dict(synthesis.trace),
                            "reason": "grounded_renderer_with_analysis",
                            "grounded_facts": True,
                            "answer_guard": _answer_guard_trace_payload("passed", guard),
                        },
                        error=synthesis.error,
                    )
                retry_observations = _with_answer_guard_feedback(grounded_observations, guard)
                retry = synthesizer(question, settings, plan, retry_observations, conversation_context)
                if retry.response_text:
                    retry_guard = _verify_answer_guard(
                        retry.response_text,
                        observations=fact_observations,
                        evidence_bundle=evidence_bundle,
                        task_contract=task_contract,
                        coverage=coverage,
                    )
                    if not retry_guard["violations"]:
                        return LlmSynthesisResult(
                            response_text=_combine_grounded_response(fact_text, retry.response_text),
                            trace={
                                **dict(retry.trace),
                                "reason": "grounded_renderer_with_analysis",
                                "grounded_facts": True,
                                "answer_guard": _answer_guard_trace_payload("failed_then_rewritten", guard),
                            },
                            error=retry.error,
                        )
                    guard = {
                        **guard,
                        "retry_violations": retry_guard["violations"],
                        "retry_contract_verifier": retry_guard.get("contract_verifier"),
                        "retry_shape_verifier": retry_guard.get("shape_verifier"),
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
                trace["answer_guard"] = {
                    **guard,
                    **_answer_guard_trace_payload("failed_then_fallback", guard),
                }
            return LlmSynthesisResult(response_text=fact_text, trace=trace)
    synthesizer = synthesize_response_fn or synthesize_tool_plan_response
    synthesis = synthesizer(question, settings, plan, llm_observations, conversation_context)
    if synthesis.response_text:
        guard = _verify_answer_guard(
            synthesis.response_text,
            observations=fact_observations,
            evidence_bundle=evidence_bundle,
            task_contract=task_contract,
            coverage=coverage,
        )
        if not guard["violations"]:
            return LlmSynthesisResult(
                response_text=synthesis.response_text,
                trace={**dict(synthesis.trace), "answer_guard": _answer_guard_trace_payload("passed", guard)},
                error=synthesis.error,
            )
        retry_observations = _with_answer_guard_feedback(llm_observations, guard)
        retry = synthesizer(question, settings, plan, retry_observations, conversation_context)
        if retry.response_text:
            retry_guard = _verify_answer_guard(
                retry.response_text,
                observations=fact_observations,
                evidence_bundle=evidence_bundle,
                task_contract=task_contract,
                coverage=coverage,
            )
            if not retry_guard["violations"]:
                return LlmSynthesisResult(
                    response_text=retry.response_text,
                    trace={
                        **dict(retry.trace),
                        "reason": "synthesized_after_answer_guard",
                        "answer_guard": _answer_guard_trace_payload("failed_then_rewritten", guard),
                    },
                    error=retry.error,
                )
            guard = {
                **guard,
                "retry_violations": retry_guard["violations"],
                "retry_contract_verifier": retry_guard.get("contract_verifier"),
                "retry_shape_verifier": retry_guard.get("shape_verifier"),
            }
    task_fallback = (
        _task_contract_fallback_response(
            plan=plan,
            evidence_bundle=evidence_bundle,
            task_contract=task_contract,
            coverage=coverage,
        )
        if "guard" in locals() and _task_contract_fallback_should_handle(guard=guard, task_contract=task_contract)
        else ""
    )
    if task_fallback:
        trace = {
            **dict(synthesis.trace),
            "reason": "task_contract_fallback",
            "fallback": "task_contract",
            "error_code": synthesis.error.code if synthesis.error else None,
        }
        if "guard" in locals():
            trace["answer_guard"] = {
                **guard,
                **_answer_guard_trace_payload("failed_then_fallback", guard),
            }
        return LlmSynthesisResult(response_text=task_fallback, trace=trace, error=synthesis.error)
    if len(plan.steps) == 1 and fact_observations:
        text = _canonical_response(plan.steps[0], _first_tool_observation(plan.steps[0], fact_observations))
        if text:
            trace = {
                **dict(synthesis.trace),
                "reason": "agent_renderer_fallback",
                "fallback": "canonical_renderer",
                "error_code": synthesis.error.code if synthesis.error else None,
            }
            if "guard" in locals():
                trace["answer_guard"] = {
                    **guard,
                    **_answer_guard_trace_payload("failed_then_fallback", guard),
                }
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
            trace["answer_guard"] = {
                **guard,
                **_answer_guard_trace_payload("failed_then_fallback", guard),
            }
        return LlmSynthesisResult(
            response_text=analysis_fallback,
            trace=trace,
            error=synthesis.error,
        )
    return LlmSynthesisResult(
        response_text=_fallback_response(
            plan=plan,
            observations=llm_observations,
            error_payload=error_payload,
        ),
        trace={
            **dict(synthesis.trace),
            "fallback": "structured_observation_summary",
            "error_code": synthesis.error.code if synthesis.error else None,
        },
        error=synthesis.error,
    )


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
    return True


def _compose_agent_response(
    *,
    question: str,
    settings: AssistantSettings,
    plan: PlannerPlan,
    evidence: AnswerEvidence,
    task_contract: dict[str, Any] | None,
    coverage: dict[str, Any] | None,
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
        guard = _verify_answer_guard(
            synthesis.response_text,
            observations=fact_observations,
            evidence_bundle=evidence_bundle,
            task_contract=task_contract,
            coverage=coverage,
        )
        if not guard["violations"]:
            return LlmSynthesisResult(
                response_text=_append_provenance(synthesis.response_text, evidence.provenance_lines),
                trace={
                    **dict(synthesis.trace),
                    "reason": "agent_composed_response",
                    "answer_guard": _answer_guard_trace_payload("passed", guard),
                    "answer_evidence": dict(evidence.trace or {}),
                    "provenance_appended": bool(evidence.provenance_lines),
                },
                error=synthesis.error,
            )
        retry_observations = _with_answer_guard_feedback(observations, guard)
        retry = synthesizer(question, settings, plan, retry_observations, conversation_context)
        if retry.response_text:
            retry_guard = _verify_answer_guard(
                retry.response_text,
                observations=fact_observations,
                evidence_bundle=evidence_bundle,
                task_contract=task_contract,
                coverage=coverage,
            )
            if not retry_guard["violations"]:
                return LlmSynthesisResult(
                    response_text=_append_provenance(retry.response_text, evidence.provenance_lines),
                    trace={
                        **dict(retry.trace),
                        "reason": "agent_composed_response",
                        "answer_guard": _answer_guard_trace_payload("failed_then_rewritten", guard),
                        "answer_evidence": dict(evidence.trace or {}),
                        "provenance_appended": bool(evidence.provenance_lines),
                    },
                    error=retry.error,
                )
            guard = {
                **guard,
                "retry_violations": retry_guard["violations"],
                "retry_contract_verifier": retry_guard.get("contract_verifier"),
                "retry_shape_verifier": retry_guard.get("shape_verifier"),
            }
    task_fallback_text = (
        _task_contract_fallback_response(
            plan=plan,
            evidence_bundle=evidence_bundle,
            task_contract=task_contract,
            coverage=coverage,
        )
        if "guard" in locals() and _task_contract_fallback_should_handle(guard=guard, task_contract=task_contract)
        else ""
    )
    fallback_text = task_fallback_text or evidence.fallback_text or _fallback_response(
        plan=plan,
        observations=observations,
        error_payload=error_payload,
    )
    trace = {
        **dict(synthesis.trace),
        "attempted": bool(synthesis.trace.get("attempted", False)),
        "reason": "task_contract_fallback" if task_fallback_text else "agent_renderer_fallback",
        "fallback": "task_contract"
        if task_fallback_text
        else "canonical_renderer"
        if evidence.fallback_text
        else "structured_observation_summary",
        "error_code": synthesis.error.code if synthesis.error else None,
        "answer_evidence": dict(evidence.trace or {}),
    }
    if "guard" in locals():
        trace["answer_guard"] = {
            **guard,
            **_answer_guard_trace_payload("failed_then_fallback", guard),
        }
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
        payload = FinalResponsePlan(
            status="needs_clarification",
            reason="follow-up planning determined that user scope is required",
            canonical_renderer_required=False,
            llm_may_summarize=False,
        ).public_payload()
        clarification_request = synthesis.trace.get("clarification_request")
        if isinstance(clarification_request, dict):
            payload["clarification_request"] = dict(clarification_request)
        return payload
    if reason == "agent_renderer_fallback":
        return FinalResponsePlan(
            status="rendered",
            reason="deterministic fallback renderer used after agent composition was unavailable or unsafe",
            canonical_renderer_required=True,
            llm_may_summarize=True,
        ).public_payload()
    if reason == "analysis_result_fallback":
        return FinalResponsePlan(
            status="rendered",
            reason="analysis result renderer used after synthesis was unavailable or unsafe",
            canonical_renderer_required=True,
            llm_may_summarize=True,
        ).public_payload()
    if reason == "task_contract_fallback":
        return FinalResponsePlan(
            status="rendered",
            reason="task-shaped deterministic fallback used after synthesis was unavailable or unsafe",
            canonical_renderer_required=False,
            llm_may_summarize=False,
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


def _audit_synthesis_trace(trace: dict[str, Any]) -> dict[str, Any]:
    out = dict(trace)
    if "composer" not in out:
        out["composer"] = {
            "attempted": bool(out.get("attempted", False)),
            "reason": out.get("reason"),
            "schema_version": out.get("schema_version"),
        }
    if "guard" not in out and isinstance(out.get("answer_guard"), dict):
        out["guard"] = dict(out["answer_guard"])
    return out


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
    return _answer_policy_for_step(step) == "facts_then_analysis"


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


def _task_contract_fallback_should_handle(*, guard: dict[str, Any], task_contract: dict[str, Any] | None) -> bool:
    contract = task_contract if isinstance(task_contract, dict) else {}
    families = {str(item) for item in contract.get("intent_families") or [] if str(item).strip()}
    violations = [item for item in guard.get("violations") or [] if isinstance(item, dict)]
    retry_violations = [item for item in guard.get("retry_violations") or [] if isinstance(item, dict)]
    relevant = violations + retry_violations
    if "account_comparison" in families:
        return any(
            str(item.get("type") or "") == "answer_shape_missing_required_key"
            and str(item.get("required_answer_key") or "") in {"comparison_winner", "amount_difference", "rate_difference"}
            for item in relevant
        )
    if "upgrade_status" in families:
        return any(
            str(item.get("type") or "") == "answer_shape_missing_required_key"
            and str(item.get("required_answer_key") or "")
            in {"command_status", "current_version", "target_version", "release_status", "source_and_policy"}
            for item in relevant
        )
    return False


def _task_contract_fallback_response(
    *,
    plan: PlannerPlan,
    evidence_bundle: Any | None,
    task_contract: dict[str, Any] | None,
    coverage: dict[str, Any] | None,
) -> str:
    contract = task_contract if isinstance(task_contract, dict) else {}
    if not contract:
        return ""
    coverage_payload = coverage if isinstance(coverage, dict) else {}
    missing_text = _coverage_missing_fallback(plan=plan, evidence_bundle=evidence_bundle, coverage=coverage_payload)
    if missing_text:
        return missing_text
    families = {str(item) for item in contract.get("intent_families") or [] if str(item).strip()}
    if "account_comparison" in families:
        return _account_comparison_fallback(evidence_bundle=evidence_bundle, task_contract=contract)
    return ""


def _coverage_missing_fallback(*, plan: PlannerPlan, evidence_bundle: Any | None, coverage: dict[str, Any]) -> str:
    gaps = [item for item in coverage.get("gaps") or [] if isinstance(item, dict)]
    missing = [str(item) for item in coverage.get("missing") or [] if str(item).strip()]
    if not gaps and not missing:
        return ""
    lines = [f"{plan.goal or '分析'}：当前证据不足，只能给部分结论。"]
    for gap in gaps:
        kind = str(gap.get("kind") or "")
        if kind == "analysis_missing_account_coverage":
            accounts = ", ".join(str(item) for item in gap.get("missing_accounts") or [] if str(item).strip())
            lines.append(f"- 缺少账户覆盖：{accounts or '-'}，无法完成同口径账户对比。")
        elif kind == "analysis_breakdown_needed":
            lines.append("- 缺少收益组成或标的归因明细，不能判断主要来源。")
        elif kind == "recoverable_missing_quote":
            symbols = ", ".join(str(item) for item in gap.get("symbols") or [] if str(item).strip())
            lines.append(f"- 缺少实时行情：{symbols or '-'}，不能计算当前正股浮盈亏和生命周期PnL。")
        elif kind == "upgrade_current_version_missing":
            lines.append("- 缺少当前版本：无法展示或校验升级前版本。")
        elif kind == "upgrade_target_version_missing":
            lines.append("- 缺少目标版本：无法展示或校验本次升级目标版本。")
        elif kind == "upgrade_receipt_missing":
            lines.append("- 缺少最终回执证据：无法证明升级完成后的成功/失败回执已经送达。")
        elif kind == "upgrade_command_status_missing":
            lines.append("- 缺少命令状态：无法确认升级命令当前进度。")
        elif kind == "upgrade_release_publication_status_missing":
            lines.append("- 缺少 release 发布状态：只有 release tag，无法证明 GitHub Release 已发布或发布失败。")
        elif kind == "upgrade_status_conflict":
            lines.append("- 升级状态证据冲突：不能给出单一成功或失败结论。")
    if missing:
        labels = ", ".join(_required_answer_label(item) for item in missing)
        lines.append(f"- 未覆盖的必答项：{labels}。")
    source = _evidence_source_line(evidence_bundle)
    if source:
        lines.append(source)
    return "\n".join(lines)


def _account_comparison_fallback(*, evidence_bundle: Any | None, task_contract: dict[str, Any]) -> str:
    payload = evidence_bundle.public_payload() if hasattr(evidence_bundle, "public_payload") else {}
    facts = [item for item in payload.get("facts") or [] if isinstance(item, dict)]
    evidence_scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
    scope = task_contract.get("scope") if isinstance(task_contract.get("scope"), dict) else {}
    requested = [str(item).strip().lower() for item in scope.get("requested_accounts") or [] if str(item).strip()]
    if len(requested) < 2:
        return ""
    metric = _comparison_metric(facts=facts, requested_accounts=requested)
    if not metric:
        return ""
    values = metric["values"]
    winner = max(values, key=lambda item: item[1])
    loser = min(values, key=lambda item: item[1])
    currency = str(metric.get("currency") or "").upper()
    amount = abs(float(winner[1]) - float(loser[1]))
    value_text = f"{currency} {_format_plain_number(amount)}" if currency else _format_plain_number(amount)
    months = [str(item).strip() for item in evidence_scope.get("months") or [] if str(item).strip()]
    period_prefix = f"{months[0]} " if len(months) == 1 else ""
    lines = [f"{period_prefix}{winner[0]} 高于 {loser[0]}（{winner[0]} 更高），差额 {value_text}。"]
    rate_metric = _comparison_metric(facts=facts, requested_accounts=requested, rate=True)
    if rate_metric:
        rate_values = rate_metric["values"]
        rate_winner = max(rate_values, key=lambda item: item[1])
        rate_loser = min(rate_values, key=lambda item: item[1])
        rate_diff = abs(float(rate_winner[1]) - float(rate_loser[1])) * 100
        lines.append(f"收益率差 {_format_plain_number(rate_diff)} 个百分点，{rate_winner[0]} 更高。")
    source = _evidence_source_line(evidence_bundle)
    if source:
        lines.append(source)
    return "\n".join(lines)


def _comparison_metric(
    *,
    facts: list[dict[str, Any]],
    requested_accounts: list[str],
    rate: bool = False,
) -> dict[str, Any]:
    preferred = (
        ("net_return_rate", "return_rate", "cashflow_rate", "rate")
        if rate
        else ("net_income_cny", "net_cashflow_cny", "net_income", "net_cashflow", "pnl", "income", "amount")
    )
    requested = set(requested_accounts)
    by_metric: dict[tuple[str, str, str], dict[str, tuple[float, str]]] = {}
    for fact in facts:
        account, metric_name = _comparison_fact_account_metric(fact, requested_accounts=requested)
        if account not in requested:
            continue
        value = _safe_float(fact.get("value"))
        if value is None:
            continue
        if rate:
            if "rate" not in metric_name and "return" not in metric_name and "percent" not in metric_name:
                continue
        elif not any(token in metric_name for token in preferred):
            continue
        period = _comparison_fact_period(fact)
        currency = "" if rate else str(fact.get("currency") or _currency_from_field(metric_name) or "").upper()
        if not period or (not rate and not currency):
            continue
        by_metric.setdefault((metric_name, period, currency), {})[account] = (float(value), currency)
    for token in preferred:
        for (metric_name, period, currency), account_values in by_metric.items():
            if token not in metric_name:
                continue
            if not requested <= set(account_values):
                continue
            values = [(account, account_values[account][0]) for account in requested_accounts]
            return {"metric": metric_name, "values": values, "currency": currency, "period": period}
    return {}


def _comparison_fact_account_metric(fact: dict[str, Any], *, requested_accounts: set[str]) -> tuple[str, str]:
    field_name = _comparison_fact_field_name(fact)
    account = str(fact.get("account") or "").strip().lower()
    if account:
        return account, field_name
    for requested in sorted(requested_accounts, key=len, reverse=True):
        prefix = f"{requested}_"
        if field_name.startswith(prefix):
            return requested, field_name[len(prefix) :]
    return "", field_name


def _comparison_fact_field_name(fact: dict[str, Any]) -> str:
    source_path = str(fact.get("source_path") or "")
    if "." in source_path:
        return source_path.rsplit(".", 1)[-1].lower()
    return str(fact.get("path") or "").lower().rsplit(".", 1)[-1]


def _comparison_fact_period(fact: dict[str, Any]) -> str:
    as_of = str(fact.get("as_of") or "").strip()
    if as_of:
        return as_of
    source_path = str(fact.get("source_path") or "")
    if source_path.startswith("rows[") and "]." in source_path:
        return source_path.split("].", 1)[0] + "]"
    return ""


def _currency_from_field(field_name: str) -> str:
    if field_name.endswith("_cny"):
        return "CNY"
    if field_name.endswith("_hkd"):
        return "HKD"
    if field_name.endswith("_usd"):
        return "USD"
    return ""


def _evidence_source_line(evidence_bundle: Any | None) -> str:
    payload = evidence_bundle.public_payload() if hasattr(evidence_bundle, "public_payload") else {}
    datasets = [item for item in payload.get("datasets") or [] if isinstance(item, dict)]
    sources = sorted({str(item.get("source_label") or "").strip() for item in datasets if str(item.get("source_label") or "").strip()})
    if not sources:
        return ""
    return "数据来源：" + "；".join(sources)


def _required_answer_label(key: str) -> str:
    return {
        "comparison_winner": "谁更高",
        "amount_difference": "金额差额",
        "rate_difference": "收益率差",
        "main_drivers": "主要来源",
        "spot_freshness": "行情新鲜度",
        "unrealized_pnl": "正股浮盈亏",
        "lifecycle_pnl": "生命周期PnL",
        "command_status": "命令状态",
        "current_version": "当前版本",
        "target_version": "目标版本",
        "release_status": "release 发布状态",
    }.get(str(key), str(key))


def _format_plain_number(value: float) -> str:
    number = float(value)
    if abs(number - round(number)) < 0.000001:
        return f"{number:,.0f}"
    return f"{number:,.2f}".rstrip("0").rstrip(".")



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
            return ""
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
    if tool_name == "close_advice_read":
        payload.setdefault("market_scope", "all")
    if tool_name == "operation_timeline" and request.audit_db:
        payload.setdefault("audit_db", request.audit_db)
    if tool_name == "runtime_runs":
        payload.setdefault("limit", 10)
    if tool_name == "runtime_logs":
        payload.setdefault("kind", "all")
        payload.setdefault("lines", 50)
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
        "policy": {
            "current_message_wins": bool(policy.get("current_message_wins", True)),
            "context_is_hint": bool(policy.get("context_is_hint", True)),
            "ask_when_ambiguous": bool(policy.get("ask_when_ambiguous", True)),
            "declare_context_use": bool(policy.get("declare_context_use", True)),
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
        for key in ("recent_turns", "recent_successful_tools", "available_evidence_refs", "open_evidence_gaps"):
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
    tools = _planner_tool_manifest(analysis_view_names=selection["selected_analysis_views"])
    payload: dict[str, Any] = {
        "message": str(text or ""),
        "current_user_message": {"text": str(text or "")},
        "tools": tools,
        "manifest_budget": _planner_manifest_budget(tools=tools, analysis_view_selection=selection),
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


def _planner_manifest_budget(*, tools: list[dict[str, Any]], analysis_view_selection: dict[str, Any]) -> dict[str, Any]:
    selected = [
        str(item)
        for item in analysis_view_selection.get("selected_analysis_views") or []
        if str(item).strip()
    ]
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
        "fallback": "use analysis_catalog first when a needed analysis view or field is not included",
    }


def _planner_instructions() -> str:
    return """\
You are the options-monitor assistant capability planner.
Return only JSON that matches the requested schema.

Rules:
- Produce 1 to 3 read-only tool calls, or exactly 1 preview-write capability call.
- Also fill task_contract when you can safely infer it. The task_contract is your structured understanding of the user goal; the steps are your executable plan. If unsure, keep task_contract conservative rather than guessing.
- Also fill selected_recipe.name when one investigation recipe matches the task_contract and planned evidence path. Use income_analysis_breakdown for income analysis/breakdown, position_or_quote_diagnosis for position/runtime diagnosis, operation_status_readback for operation status/readback questions, strategy_replay_review for strategy/candidate replay or recommendation, and action_lifecycle_audit for preview-write lifecycle/audit questions.
- task_contract.domain is one of income, position, candidate, config, operation, runtime, strategy, general. task_contract.task_mode is one of summarize, analyze, compare, diagnose, explain, recommend, preview_write.
- Use task_mode=analyze for analysis/复盘/表现/来源/结构 questions, compare for same-scope comparisons, diagnose for why/missing/failure/abnormal status questions, explain for rules or accounting policies, recommend for advisory options, and preview_write only for approved preview operations.
- task_contract.required_evidence should name evidence categories needed to complete the answer, not tool names. Examples: summary, driver_or_breakdown, same_scope_comparable_data, observed_status, diagnostic_evidence, rule_or_config_source, current_state, constraints, risk_premise, source_policy.
- task_contract.answer_shape should name what the final answer must cover, such as conclusion, drivers, same_scope_comparison, cause_chain, evidence_boundary, risk, premise, options, source_policy.
- Always fill context_use when the schema includes it. Use mode=none when the current message is self-contained; use carry/refine/override only when the plan intentionally depends on prior conversation state.
- Current user message wins over context.context_projection. If the user explicitly changes account, symbol, month, domain, strategy, operation, or requested effect, declare mode=override and put the replacement values in current_message_slots or override_slots.
- Use context.context_projection as the only conversation-state authority for planning. recent_turns, recent_successful_tools, available_evidence_refs, open_evidence_gaps, pending_operations, and safe_slots are planner-visible hints, not hidden truth.
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
- For "记录开仓", "记录平仓", Futu 成交提醒, 成功卖出/买入 option fills, use manual_trade_open or manual_trade_close with raw_text set to the original user message.
- For current monitored-symbol config questions such as "max strike 是多少", "当前配置", or "查询 sell_put.max_strike", use symbol_config_read with symbol plus optional strategy/field.
- For user-provided company names, Chinese names, aliases, Futu codes, or uncertain market suffixes, use symbol_resolve when the user asks for identity resolution or when a later SQL-style analysis needs a canonical symbol. Symbol-aware tools may also receive the original alias/name and resolve it internally.
- For single-symbol candidate filter/rejection questions such as "为什么 X 没出现在候选里", "X 被哪个参数过滤了", "why was X filtered", or "why missing candidate", use candidate_filter_explain with symbol plus optional account/function/run_id. Do not use analysis_query for that single-symbol root-cause shape unless the user asks to compare/group/trend across symbols, accounts, rules, or runs.
- For non-monthly-income open-ended analytical questions such as 分析, 复盘, 表现, 对比, 有什么不同, 排名, 趋势, 组成, 来源, 按账户/月份/标的汇总, or cross-domain questions across income/positions/trades/assigned stock/config, prefer analysis_query over narrow business renderers. Use analysis_catalog first only when fields/views are unknown.
- For analysis_query, use only columns listed in the tool manifest analysis_views. Never invent SQL columns. If the needed fields are not clear from the manifest, plan analysis_catalog before analysis_query.
- For monitored-symbol setting changes such as covered call min strike 85, use symbol_edit. Do not use symbol_edit for questions about the current value.
- For model switch requests, use model_use. For immediate software upgrade requests, use upgrade_now.
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
) -> list[dict[str, Any]]:
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
        binding = planner_binding_for_tool(name)
        notes: list[str] = list(binding.planner_notes) if binding is not None else []
        semantics: dict[str, Any] = dict(binding.planner_semantics) if binding is not None else {}
        if name == "monthly_income_report":
            notes.append("Set include_rows=true for income analysis/review/performance, cashflow details, composition, source, 分析, 复盘, 表现, 明细, 组成, 构成, 来源, or 由什么组成.")
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
                    "include_rows": "include detail rows for income analysis, composition, or source questions",
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
            notes.append("Use investigation_recipes to map task_contract domains, task modes, and evidence gaps to the safest generic views/tools.")
            notes.append("This is a pure-read catalog; it does not answer the business question by itself.")
            semantics = {
                "data_source": "OM Tool OS catalog",
                "answer_capabilities": {
                    "analysis_catalog": "lists whitelisted read-only views, SQL rules, and investigation recipes",
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
            notes.append("For aggregate candidate diagnostics across symbols/rules/accounts/runs, use candidate_filter_diagnostics; for one-symbol filter reason questions, use candidate_filter_explain instead.")
            notes.append("For close-advice questions, runtime push/scan diagnostics, or quote freshness gaps, use close_advice_snapshot, runtime_tick_status, and quote_freshness.")
            notes.append("Do not avg/sum return-rate fields directly; recompute weighted rates from money numerator and cash_secured_cny when aggregating rows.")
            notes.append("Tool result rows/cell_refs are evidence; if synthesis fails, the analysis_result renderer preserves the task-shaped table.")
            semantics = {
                "data_source": "OM read-only analysis workspace backed by local ledger/config/runtime read tools",
                "analysis_views": _analysis_views_for_planner_manifest(analysis_view_names),
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


def _analysis_views_for_planner_manifest(
    view_names: list[str] | tuple[str, ...] | set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    views: dict[str, dict[str, Any]] = {}
    names = (
        [str(item) for item in view_names if str(item) in ANALYSIS_VIEW_SPECS]
        if view_names is not None
        else list(ANALYSIS_VIEW_SPECS)
    )
    for name in names:
        spec = ANALYSIS_VIEW_SPECS[name]
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
    if tool_name == "candidate_filter_explain":
        return _candidate_filter_synthesis_data(data)
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
    "AgentLoopPlanFn",
    "AgentLoopSynthesizeFn",
    "AgentLoopPlanningOutcome",
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
    "ToolExecutionOutcome",
    "ToolExecutor",
    "ToolObservation",
    "build_tool_observation",
    "build_synthesis_observation",
    "execute_tool_plan",
    "parse_tool_plan_payload",
    "plan_read_only_tools",
    "run_read_only_agent_loop",
    "skipped_llm_trace",
    "synthesize_tool_plan_response",
    "tool_plan_json_schema",
    "validate_tool_plan",
]
