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
from src.application.agent_tool_registry import get_tool_definition
from src.application.agent_tools.analysis import VIEW_SPECS as ANALYSIS_VIEW_SPECS
from src.application.assistant.capability_catalog import (
    ACCOUNT_VALUES,
    is_llm_copilot_preview_spec,
    planner_preview_specs as copilot_preview_specs,
    planner_read_specs as copilot_read_specs,
    spec_by_intent,
)
from src.application.assistant.action_safety import assess_action_safety
from src.application.assistant.action_policy import decide_tool_action_policy
from src.application.assistant.contracts import AssistantRequest, PerceptionResult, ToolCall
from src.application.assistant.context_projection import SAFE_SLOT_KEYS, build_context_projection, context_projection_trace
from src.application.assistant.copilot import (
    compose_answer as compose_copilot_answer,
    covered_views_from_results as copilot_covered_views_from_results,
    derive_task_frame as derive_copilot_task_frame,
    plan_evidence as plan_copilot_evidence,
)
from src.application.assistant.model_events import (
    ModelFinalAnswerEvent,
    ModelToolCallEvent,
    ToolGuardDecisionEvent,
    ToolResultAdapterOutput,
    adapt_tool_result,
    event_transcript_payload,
)
from src.application.assistant.model_evidence import (
    ModelEvidenceBundle,
    build_model_evidence_bundle,
    user_fallback_from_tool_results,
)
from src.application.assistant.settings import AssistantSettings
from src.application.assistant.task_contract import (
    preview_authority_from_text,
    preview_request_kind_from_text,
)
from src.application.assistant.task_completion import check_task_completion
from src.application.assistant.tool_bindings import (
    planner_config_scoped_tool_names as copilot_config_scoped_tool_names,
    symbol_market_config_tool_names,
)
from src.application.assistant.tool_contracts import resolve_output_contract
from src.application.assistant.verifier_hooks import (
    hook_results_from_tool_check,
)
from src.application.assistant.tool_policy import DEFAULT_TOOL_POLICY
from src.application.assistant.user_profile import user_profile_trace
from src.application.tool_input_schema import build_tool_input_json_schema, validate_tool_input_payload

AGENT_LOOP_SCHEMA_VERSION = "om-agent-loop-v1"
COPILOT_CONTEXT_USE_SCHEMA_VERSION = "om-copilot-context-use-v1"
TOOL_CHECK_SCHEMA_VERSION = "om-agent-tool-check-v1"
INTERNAL_TOOL_LOOP_NAME = "assistant.tool_loop"
MAX_COPILOT_STEPS = 5
MAX_AGENT_LOOP_TOOL_CALLS = 10
MAX_COPILOT_ANALYSIS_VIEWS = 12
_CURRENT_SCOPE_OPTIONAL_FILTER_SLOTS = frozenset({"function", "strategy"})
COPILOT_CONTEXT_USE_MODES = ("none", "carry", "refine", "override", "frame_delta", "ambiguous")
_DEFAULT_COPILOT_ANALYSIS_VIEWS: tuple[str, ...] = (
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
_COPILOT_CONTEXT_TOOL_HINTS: dict[str, tuple[str, ...]] = {
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
    for spec in copilot_read_specs()
    if spec.tool_name is not None and get_tool_definition(str(spec.tool_name)) is not None
)
AGENT_LOOP_PREVIEW_CAPABILITIES = frozenset(spec.intent_name for spec in copilot_preview_specs())
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
_CONFIG_SCOPED_COPILOT_TOOLS = copilot_config_scoped_tool_names()
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
    answer_verification: Any | None = None
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
    answer_verification: Any | None = None,
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
        ),
        "capability_selection": _assistant_tool_loop_capability_selection(events),
        "read_agent_mode": _assistant_tool_loop_read_agent_mode(task_contract),
        "evidence_summary": evidence_summary,
        "copilot_plan_used": True,
    }
    if isinstance(task_contract.get("copilot_task"), dict) and task_contract["copilot_task"]:
        trace["copilot_task"] = dict(task_contract["copilot_task"])
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
    answer_verification: Any | None,
) -> str:
    if answer_verification is not None and not answer_verification.passed and answer_verification.fallback_text:
        trace = answer_verification.trace if isinstance(answer_verification.trace, dict) else {}
        if str(trace.get("fallback") or "").strip() == "user_fallback":
            return "user_fallback"
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
) -> bool:
    return any(not adapter.event.ok for adapter in tool_results)


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
    if stop_reason in {"repeated_recoverable_error", "invalid_model_event"}:
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


def _build_tool_loop_response_from_outcome(
    *,
    outcome: AssistantToolLoopOutcome,
    question: str,
    command_id: str | None,
    task_contract: dict[str, Any],
) -> dict[str, Any]:
    response_text = _assistant_tool_loop_response_text(outcome)
    requires_model_answer = _task_contract_requires_synthesis(task_contract)
    ok = outcome.status == "done" and (
        bool(outcome.final_answer) or (not requires_model_answer and any(result.event.ok for result in outcome.tool_results))
    )
    if outcome.status == "stopped" and outcome.stop_reason != "answer_verification_failed":
        ok = bool(not requires_model_answer and any(result.event.ok for result in outcome.tool_results))
    final_response_status = "synthesized" if outcome.final_answer else "rendered"
    if not outcome.final_answer and requires_model_answer:
        final_response_status = "needs_copilot_evidence"
    copilot_composed = str(outcome.trace.get("answer_route") or "").strip() == "copilot_answer"
    final_response = {
        "status": final_response_status,
        "reason": outcome.stop_reason or outcome.status,
        "canonical_renderer_required": bool(not outcome.final_answer and not requires_model_answer),
        "llm_may_summarize": bool((outcome.final_answer or requires_model_answer) and not copilot_composed),
    }
    if copilot_composed:
        final_response["copilot_composed"] = True
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


def _assistant_tool_loop_response_text(outcome: AssistantToolLoopOutcome) -> str:
    if outcome.final_answer:
        return outcome.final_answer
    if outcome.stop_reason == "answer_verification_failed":
        return "需要先读取相关 OM 证据后才能回答；本次没有执行工具。"
    task_completion_text = _assistant_task_completion_text(
        trace=outcome.trace,
        tool_results=outcome.tool_results,
    )
    if task_completion_text:
        return task_completion_text
    fallback = user_fallback_from_tool_results(outcome.tool_results)
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


def _assistant_task_completion_text(
    *,
    trace: dict[str, Any] | None,
    tool_results: tuple[ToolResultAdapterOutput, ...],
) -> str:
    if not tool_results:
        return ""
    payload = trace if isinstance(trace, dict) else {}
    task = _assistant_task_payload_from_trace(payload)
    if not _task_payload_requires_synthesis(task):
        return ""
    if not task:
        return ""
    completion = check_task_completion(
        task=task,
        covered_views=_assistant_tool_result_covered_views(tool_results),
        successful_tool_count=_assistant_successful_tool_result_count(tool_results),
    )
    payload["task_completion"] = completion.public_payload()
    if completion.reason == "no_successful_evidence":
        return "OM 没有成功读取形成结论所需证据；本次工具调用没有产生可用的只读结果。"
    if completion.missing_views:
        return f"已读取到部分 OM 证据，但还不能形成结论；缺少证据视图：{', '.join(completion.missing_views[:6])}。"
    return "已读取到所需 OM 证据，但本轮没有生成可用结论；需要继续综合分析。"


def _assistant_task_payload_from_trace(payload: dict[str, Any]) -> dict[str, Any]:
    copilot_task = payload.get("copilot_task") if isinstance(payload.get("copilot_task"), dict) else {}
    return dict(copilot_task) if copilot_task else {}


def _task_payload_requires_synthesis(task: dict[str, Any]) -> bool:
    if bool(task.get("requires_synthesis")):
        return True
    profiles = {str(item).strip() for item in task.get("profile_names") or [] if str(item).strip()}
    task_mode = str(task.get("task_mode") or "").strip()
    return bool(profiles and task_mode in {"analyze", "compare", "diagnose", "recommend"})


def _task_contract_requires_synthesis(task_contract: dict[str, Any] | None) -> bool:
    contract = task_contract if isinstance(task_contract, dict) else {}
    copilot_task = contract.get("copilot_task") if isinstance(contract.get("copilot_task"), dict) else {}
    return _task_payload_requires_synthesis(copilot_task)


def _assistant_trace_string_tuple(value: Any) -> tuple[str, ...]:
    return tuple(str(item).strip() for item in value or [] if str(item).strip())


def _assistant_tool_result_covered_views(tool_results: tuple[ToolResultAdapterOutput, ...]) -> set[str]:
    views: set[str] = set()
    for adapter in tool_results:
        raw_result = adapter.raw_result if isinstance(adapter.raw_result, dict) else {}
        if not adapter.event.ok or not bool(raw_result.get("ok")):
            continue
        data = raw_result.get("data") if isinstance(raw_result.get("data"), dict) else {}
        views.update(_assistant_trace_string_tuple(data.get("views_used")))
        evidence = data.get("evidence") if isinstance(data.get("evidence"), dict) else {}
        coverage = evidence.get("coverage") if isinstance(evidence.get("coverage"), dict) else {}
        views.update(_assistant_trace_string_tuple(coverage.get("views")))
        view_datasets = data.get("view_datasets")
        if isinstance(view_datasets, dict):
            views.update(str(name).strip() for name in view_datasets if str(name).strip())
    return views


def _assistant_successful_tool_result_count(tool_results: tuple[ToolResultAdapterOutput, ...]) -> int:
    count = 0
    for adapter in tool_results:
        raw_result = adapter.raw_result if isinstance(adapter.raw_result, dict) else {}
        if adapter.event.ok and bool(raw_result.get("ok")):
            count += 1
    return count


def _assistant_tool_loop_tool_events(outcome: AssistantToolLoopOutcome) -> list[dict[str, Any]]:
    events = event_transcript_payload(outcome.events)
    out: list[dict[str, Any]] = [
        {
            "phase": "event_loop",
            "status": outcome.status,
            "stop_reason": outcome.stop_reason,
            "event_count": len(events),
            "copilot_plan_used": True,
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


def run_read_only_agent_loop(
    text: str,
    *,
    settings: AssistantSettings,
    conversation_context: dict[str, Any] | None,
    request: AssistantRequest | None = None,
    execute_tool_fn: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    now_fn: Callable[[], date] | None = None,
    max_steps: int = MAX_COPILOT_STEPS,
) -> AgentLoopResult:
    """Run the OM Copilot task loop."""
    steps = max(1, min(int(max_steps), MAX_COPILOT_STEPS))
    today = _copilot_today(now_fn)
    loop_context = _with_temporal_context(conversation_context, today=today)
    if request is None or execute_tool_fn is None:
        error = AgentToolError(
            code="TOOL_LOOP_CONTEXT_MISSING",
            message="OM Copilot requires request and execute_tool_fn.",
        )
        trace = {"enabled": True, "attempted": True, "reason": "copilot_context_missing"}
        trace["agent_loop"] = _copilot_rejection_trace(
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
    return _run_copilot_agent_loop(
        text=text,
        request=request,
        execute_tool_fn=execute_tool_fn,
        conversation_context=loop_context,
        today=today,
        max_steps=steps,
    )


def _run_copilot_agent_loop(
    *,
    text: str,
    request: AssistantRequest,
    execute_tool_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
    conversation_context: dict[str, Any] | None,
    today: date,
    max_steps: int,
) -> AgentLoopResult:
    task = derive_copilot_task_frame(
        question=text,
        request_context=request.public_payload(),
        today=today,
        conversation_context=conversation_context,
    )
    evidence_plan = plan_copilot_evidence(task)
    task_contract = task.task_contract_payload()
    if not evidence_plan.calls:
        error = AgentToolError(
            code="NEEDS_CLARIFICATION",
            message="OM Copilot 没有识别出可读取的证据计划。",
            details={"copilot_task": task.public_payload()},
        )
        trace = _copilot_trace(
            task=task,
            evidence_plan=evidence_plan,
            outcome=None,
            max_steps=max_steps,
            error=error,
        )
        return AgentLoopResult(
            planning=AgentLoopPlanningOutcome(perception=None, trace=dict(trace), error=error),
            trace=trace,
            steps=(),
        )

    transcript: list[Any] = []
    tool_results: list[ToolResultAdapterOutput] = []
    steps: list[AgentLoopStep] = []
    attempted_signatures: set[str] = set()
    for index, call in enumerate(evidence_plan.calls, start=1):
        event = ModelToolCallEvent(
            event_id=f"copilot_evidence_{index}",
            tool_call_id=f"copilot_evidence_{index}",
            tool_name=call.tool_name,
            arguments=dict(call.arguments),
            purpose=call.purpose,
            provider="copilot",
            provider_metadata={"planner": "om_copilot"},
        )
        transcript.append(event)
        execution = execute_model_tool_call_event(
            model_event=event,
            request=request,
            task_contract=task_contract,
            execute_tool_fn=execute_tool_fn,
            attempted_signatures=attempted_signatures,
            tool_call_count=len(tool_results),
            max_tool_calls=max_steps,
            source="copilot",
        )
        transcript.append(execution.guard_event)
        transcript.append(execution.result_adapter.event)
        tool_results.append(execution.result_adapter)
        steps.append(_copilot_step_from_execution(index=index, execution=execution))
        if execution.preview_gate is not None:
            outcome = _assistant_tool_loop_outcome(
                status="preview_requested",
                stop_reason="preview_gate",
                question=text,
                task_contract=task_contract,
                transcript=transcript,
                tool_results=tool_results,
                preview_gate=execution.preview_gate,
                trace_extra={
                    "runtime": "copilot",
                    "copilot_plan_used": True,
                    "copilot_task": task.public_payload(),
                    "evidence_plan": evidence_plan.public_payload(),
                    "preview_receipt": _copilot_preview_receipt(execution.preview_gate),
                },
            )
            tool_loop_result = _build_tool_loop_response_from_outcome(
                outcome=outcome,
                question=text,
                command_id=None,
                task_contract=task_contract,
            )
            trace = _copilot_trace(
                task=task,
                evidence_plan=evidence_plan,
                outcome=outcome,
                max_steps=max_steps,
                error=None,
                steps=tuple(steps),
            )
            events = tuple(event for event in transcript if isinstance(event, ModelToolCallEvent))
            planning = AgentLoopPlanningOutcome(
                perception=PerceptionResult(
                    intent_name="tool_loop",
                    arguments={
                        "events": event_transcript_payload(events),
                        "task_contract": task_contract,
                        "provider": "copilot",
                    },
                    source="agent_loop_events",
                    confidence=1.0,
                ),
                trace=dict(trace),
            )
            return AgentLoopResult(
                planning=planning,
                trace=trace,
                steps=tuple(steps),
                tool_loop_result=tool_loop_result,
            )
        if not execution.ok:
            break

    raw_results = tuple(dict(adapter.raw_result) for adapter in tool_results)
    answer_text, answer_trace = compose_copilot_answer(task=task, tool_results=raw_results)
    final_event = ModelFinalAnswerEvent(
        event_id="copilot_final_answer",
        answer_text=answer_text,
        answer_route="copilot_answer",
        parent_event_id=str(getattr(transcript[-1], "event_id", "") or "") if transcript else None,
        provider_metadata={"answer_trace": dict(answer_trace)},
    )
    transcript.append(final_event)
    status = "done" if answer_text and any(adapter.event.ok for adapter in tool_results) else "stopped"
    stop_reason = "copilot_completed" if status == "done" else "copilot_no_evidence"
    outcome = _assistant_tool_loop_outcome(
        status=status,
        stop_reason=stop_reason,
        question=text,
        task_contract=task_contract,
        transcript=transcript,
        tool_results=tool_results,
        final_answer=answer_text if answer_text else None,
        final_answer_event=final_event if answer_text else None,
        trace_extra={
            "runtime": "copilot",
            "copilot_plan_used": True,
            "copilot_task": task.public_payload(),
            "evidence_plan": evidence_plan.public_payload(),
            "covered_views": sorted(copilot_covered_views_from_results(raw_results)),
            "answer_trace": dict(answer_trace),
        },
    )
    tool_loop_result = _build_tool_loop_response_from_outcome(
        outcome=outcome,
        question=text,
        command_id=None,
        task_contract=task_contract,
    )
    trace = _copilot_trace(
        task=task,
        evidence_plan=evidence_plan,
        outcome=outcome,
        max_steps=max_steps,
        error=None,
        steps=tuple(steps),
    )
    events = tuple(event for event in transcript if isinstance(event, ModelToolCallEvent))
    planning = AgentLoopPlanningOutcome(
        perception=PerceptionResult(
            intent_name="tool_loop",
            arguments={
                "events": event_transcript_payload(events),
                "task_contract": task_contract,
                "provider": "copilot",
            },
            source="agent_loop_events",
            confidence=1.0,
        ),
        trace=dict(trace),
    )
    return AgentLoopResult(
        planning=planning,
        trace=trace,
        steps=tuple(steps),
        tool_loop_result=tool_loop_result,
    )


def _copilot_step_from_execution(*, index: int, execution: GuardedModelToolCallExecution) -> AgentLoopStep:
    event = execution.model_event
    authorization = execution.authorization_event if isinstance(execution.authorization_event, dict) else {}
    return AgentLoopStep(
        index=index,
        phase="copilot_evidence",
        status="ok" if execution.ok else "error",
        intent_name=event.tool_name,
        tool_name=event.tool_name,
        arguments=dict(event.arguments),
        purpose=event.purpose,
        action_policy=dict(authorization.get("action_policy") or {}) if isinstance(authorization.get("action_policy"), dict) else None,
        action_safety=dict(authorization.get("action_safety") or {}) if isinstance(authorization.get("action_safety"), dict) else None,
        precheck=dict(authorization.get("precheck") or {}) if isinstance(authorization.get("precheck"), dict) else None,
        hook_results=tuple(dict(item) for item in authorization.get("hook_results") or [] if isinstance(item, dict)),
        preview_receipt=dict(execution.preview_gate) if isinstance(execution.preview_gate, dict) else None,
    )


def _copilot_preview_receipt(preview_gate: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(preview_gate, dict):
        return {}
    intent_name = str(preview_gate.get("intent_name") or "").strip()
    handler_by_intent = {
        "manual_assignment": "inbound.manual_trade",
        "manual_expiry": "inbound.manual_trade",
        "manual_trade_open": "inbound.manual_trade",
        "manual_trade_close": "inbound.manual_trade",
        "manual_trade_update": "inbound.manual_trade",
        "symbol_edit": "inbound.symbols",
        "upgrade_now": "inbound.upgrade",
        "model_use": "inbound.model",
        "monitor_run_now": "inbound.monitor_run",
    }
    return {
        "operation_type": intent_name,
        "handler_tool": handler_by_intent.get(intent_name, ""),
        "preview_gate": dict(preview_gate),
    }


def _copilot_trace(
    *,
    task: Any,
    evidence_plan: Any,
    outcome: AssistantToolLoopOutcome | None,
    max_steps: int,
    error: AgentToolError | None,
    steps: tuple[AgentLoopStep, ...] = (),
) -> dict[str, Any]:
    trace: dict[str, Any] = {
        "enabled": False,
        "attempted": False,
        "reason": "not_used_copilot_runtime",
        "provider": "",
        "runtime": "copilot",
        "copilot": {
            "enabled": True,
            "attempted": True,
            "reason": "copilot_task_evidence_answer",
            "provider": "host",
        },
        "copilot_task": task.public_payload(),
        "evidence_plan": evidence_plan.public_payload(),
        "max_steps": max_steps,
    }
    if outcome is not None:
        trace["agent_loop"] = _copilot_agent_loop_trace(
            outcome=outcome,
            max_steps=max_steps,
            steps=steps,
            final_response=(
                dict(outcome.trace.get("final_response") or {})
                if isinstance(outcome.trace.get("final_response"), dict)
                else {}
            ),
        )
    if error is not None:
        trace["error"] = build_error_payload(error)
        trace["agent_loop"] = _copilot_rejection_trace(error=error, max_steps=max_steps)
    return trace


def _copilot_agent_loop_trace(
    *,
    outcome: AssistantToolLoopOutcome,
    max_steps: int,
    steps: tuple[AgentLoopStep, ...],
    final_response: dict[str, Any],
) -> dict[str, Any]:
    loop_trace = dict(outcome.trace)
    copilot_composed = str(loop_trace.get("answer_route") or "").strip() == "copilot_answer"
    return {
        **loop_trace,
        "schema_version": AGENT_LOOP_SCHEMA_VERSION,
        "enabled": True,
        "runtime": "copilot",
        "copilot_turns": 1,
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
            llm_may_summarize=bool(outcome.final_answer and not copilot_composed),
        ).public_payload(),
    }


def _copilot_rejection_trace(
    *,
    error: AgentToolError,
    max_steps: int,
) -> dict[str, Any]:
    status = "needs_clarification" if error.code == "NEEDS_CLARIFICATION" else "rejected"
    return {
        "schema_version": AGENT_LOOP_SCHEMA_VERSION,
        "enabled": True,
        "runtime": "copilot",
        "copilot_turns": 1,
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
            reason=str(error.message or error.code or "copilot rejected by host validation"),
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


def _symbol_setting_delta_frames(projection: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        frame
        for frame in projection.get("active_frames") or []
        if isinstance(frame, dict)
        and frame.get("type") == "symbol_setting"
        and "set_value" in {str(item) for item in frame.get("allowed_deltas") or []}
    ]


def _default_context_use() -> dict[str, Any]:
    return {
        "schema_version": COPILOT_CONTEXT_USE_SCHEMA_VERSION,
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
    if mode in COPILOT_CONTEXT_USE_MODES:
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
        if spec is not None and is_llm_copilot_preview_spec(spec):
            return "preview"
    return None


def _allowed_plan_arguments(tool_name: str) -> set[str]:
    if tool_name in AGENT_LOOP_READ_TOOLS:
        definition = get_tool_definition(tool_name)
        return _filter_plan_arguments(definition.input_schema) if definition is not None else set()
    spec = _COMMAND_SPECS_BY_INTENT.get(tool_name)
    if spec is None or not is_llm_copilot_preview_spec(spec):
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
    if spec is None or not is_llm_copilot_preview_spec(spec):
        return {}
    return build_tool_input_json_schema(
        _copilot_preview_input_schema(tool_name),
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


def _question_requests_candidate_filter_read(question: str) -> bool:
    compact = re.sub(r"\s+", "", str(question or "").lower())
    if not any(token in compact for token in ("候选", "过滤", "筛选", "推荐", "candidate", "filter", "filtered")):
        return False
    return any(token in compact for token in ("为什么", "为何", "解释", "原因", "没", "没有", "未", "不在", "missing", "why", "rejected"))


def _question_requests_operation_status_read(question: str) -> bool:
    compact = re.sub(r"\s+", "", str(question or "").lower())
    if not any(token in compact for token in ("更新", "升级", "update", "upgrade")):
        return False
    return any(token in compact for token in ("了吗", "了没", "是否", "状态", "进度", "回执", "成功", "完成", "status"))


def _copilot_omitted_read_tools_for_question(
    question: str,
    *,
    analysis_view_selection: dict[str, Any] | None = None,
    preview_authority: dict[str, Any] | None = None,
) -> frozenset[str]:
    if _question_requests_cash_headroom(question):
        return frozenset()
    preview_read_scope = _copilot_preview_read_tool_scope(preview_authority)
    if preview_read_scope is not None:
        _scope, allowed = preview_read_scope
        return frozenset(AGENT_LOOP_READ_TOOLS - allowed)
    matched_groups = {
        str(item)
        for item in (analysis_view_selection or {}).get("matched_view_groups") or ()
        if str(item).strip()
    }
    if _question_requests_candidate_filter_read(question):
        allowed = {"analysis_catalog", "analysis_query", "candidate_filter_explain", "symbol_resolve"}
        return frozenset(AGENT_LOOP_READ_TOOLS - allowed)
    if _question_requests_operation_status_read(question):
        allowed = {"analysis_catalog", "analysis_query", "operation_timeline"}
        return frozenset(AGENT_LOOP_READ_TOOLS - allowed)
    if _question_requests_symbol_config_read(question):
        allowed = {"symbol_config_read", "symbol_resolve"}
        return frozenset(AGENT_LOOP_READ_TOOLS - allowed)
    if "config" in matched_groups:
        allowed = {"analysis_catalog", "analysis_query", "symbol_config_read", "symbol_resolve"}
        return frozenset(AGENT_LOOP_READ_TOOLS - allowed)
    if "assigned_stock" in matched_groups:
        allowed = {"analysis_catalog", "analysis_query", "monthly_income_report", "option_positions_read", "symbol_resolve"}
        return frozenset(AGENT_LOOP_READ_TOOLS - allowed)
    if "position" in matched_groups:
        allowed = {"analysis_catalog", "analysis_query", "close_advice_read", "option_positions_read", "symbol_resolve"}
        return frozenset(AGENT_LOOP_READ_TOOLS - allowed)
    if "runtime" in matched_groups:
        allowed = {
            "analysis_catalog",
            "analysis_query",
            "config_validate",
            "healthcheck",
            "notification_perception_read",
            "operation_timeline",
            "runtime_logs",
            "runtime_runs",
            "runtime_status",
        }
        return frozenset(AGENT_LOOP_READ_TOOLS - allowed)
    if "income" in matched_groups:
        allowed = {"analysis_catalog", "analysis_query", "monthly_income_report", "symbol_resolve"}
        return frozenset(AGENT_LOOP_READ_TOOLS - allowed)
    if "candidate_strategy" in matched_groups:
        allowed = {"analysis_catalog", "analysis_query", "candidate_filter_explain", "close_advice_read", "symbol_resolve"}
        return frozenset(AGENT_LOOP_READ_TOOLS - allowed)
    return frozenset({"query_cash_headroom"})


def _copilot_read_tool_selection_sources(
    question: str,
    *,
    analysis_view_selection: dict[str, Any] | None = None,
    omitted_read_tools: frozenset[str] | None = None,
    preview_authority: dict[str, Any] | None = None,
) -> list[str]:
    if not omitted_read_tools:
        return ["read_tool_scope:all"]
    preview_read_scope = _copilot_preview_read_tool_scope(preview_authority)
    if preview_read_scope is not None:
        scope, _allowed = preview_read_scope
        return [scope]
    matched_groups = {
        str(item)
        for item in (analysis_view_selection or {}).get("matched_view_groups") or ()
        if str(item).strip()
    }
    if _question_requests_candidate_filter_read(question):
        return ["read_tool_scope:candidate_filter"]
    if _question_requests_operation_status_read(question):
        return ["read_tool_scope:operation_status"]
    if _question_requests_symbol_config_read(question):
        return ["read_tool_scope:symbol_config"]
    if "config" in matched_groups:
        return ["read_tool_scope:symbol_config"]
    if "assigned_stock" in matched_groups:
        return ["read_tool_scope:assigned_stock"]
    if "position" in matched_groups:
        return ["read_tool_scope:position"]
    if "runtime" in matched_groups:
        return ["read_tool_scope:runtime"]
    if "income" in matched_groups:
        return ["read_tool_scope:income"]
    if "candidate_strategy" in matched_groups:
        return ["read_tool_scope:candidate_strategy"]
    return ["read_tool_scope:default_without_cash_headroom"]


def _copilot_preview_read_tool_scope(preview_authority: dict[str, Any] | None) -> tuple[str, set[str]] | None:
    if not isinstance(preview_authority, dict) or not bool(preview_authority.get("allowed", False)):
        return None
    intents = [str(item) for item in preview_authority.get("allowed_preview_intents") or () if str(item).strip()]
    if intents == ["upgrade_now"]:
        return ("read_tool_scope:preview_upgrade", {"analysis_catalog", "analysis_query", "operation_timeline"})
    if intents == ["symbol_edit"]:
        return (
            "read_tool_scope:preview_symbol_edit",
            {"analysis_catalog", "analysis_query", "symbol_config_read", "symbol_resolve"},
        )
    if intents == ["manual_assignment"]:
        return (
            "read_tool_scope:preview_assignment",
            {"analysis_catalog", "analysis_query", "option_positions_read", "symbol_resolve"},
        )
    if intents == ["manual_expiry"]:
        return (
            "read_tool_scope:preview_expiry",
            {"analysis_catalog", "analysis_query", "option_positions_read", "symbol_resolve"},
        )
    if set(intents) == {"manual_trade_open", "manual_trade_close"}:
        return (
            "read_tool_scope:preview_manual_trade",
            {"analysis_catalog", "analysis_query", "option_positions_read", "symbol_resolve"},
        )
    return None


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


def _copilot_today(now_fn: Callable[[], date] | None) -> date:
    if now_fn is not None:
        return now_fn()
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _copilot_today_from_context(conversation_context: dict[str, Any] | None) -> date:
    temporal = conversation_context.get("temporal_context") if isinstance(conversation_context, dict) else None
    current_date = temporal.get("current_date") if isinstance(temporal, dict) else None
    if isinstance(current_date, str):
        try:
            return date.fromisoformat(current_date)
        except ValueError:
            pass
    return _copilot_today(None)


def _with_temporal_context(conversation_context: dict[str, Any] | None, *, today: date) -> dict[str, Any]:
    context = dict(conversation_context or {})
    context["temporal_context"] = {
        "current_date": today.isoformat(),
        "timezone": "Asia/Shanghai",
    }
    return context


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


def _inject_system_fields(arguments: dict[str, Any], *, request: AssistantRequest, tool_name: str) -> dict[str, Any]:
    payload = dict(arguments or {})
    if tool_name in _CONFIG_SCOPED_COPILOT_TOOLS:
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


def _copilot_context_projection(
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


def _copilot_projection_payload(projection: dict[str, Any], *, current_user_message: str) -> dict[str, Any]:
    policy = projection.get("policy") if isinstance(projection.get("policy"), dict) else {}
    budget = projection.get("budget") if isinstance(projection.get("budget"), dict) else {}
    out: dict[str, Any] = {
        "schema_version": str(projection.get("schema_version") or "om-context-projection-v1"),
        "current_user_message": {"text": str(current_user_message or "")},
        "recent_turns": _copilot_projection_items(
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
        "recent_successful_tools": _copilot_projection_items(
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
        "available_evidence_refs": _copilot_projection_items(
            projection.get("available_evidence_refs"),
            allowed_keys={"ref_id", "turn_id", "source_type", "source_tool", "label", "safe_slots", "data_shape"},
            limit=12,
        ),
        "active_frames": _copilot_projection_items(
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
        "open_evidence_gaps": _copilot_projection_items(
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
        "pending_operations": _copilot_projection_items(
            projection.get("pending_operations"),
            allowed_keys={"operation_id", "operation_type", "status", "summary", "created_at", "expires_at", "safe_slots"},
            limit=5,
        ),
        "user_profile": _copilot_projection_sanitize(projection.get("user_profile")),
        "relevant_memories": _copilot_projection_items(
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
        "budget": _copilot_projection_sanitize(budget) if budget else {"truncated": False},
    }
    system_events = _copilot_projection_items(
        projection.get("system_events"),
        allowed_keys={"schema_version", "event_id", "event_type", "summary", "reason"},
        limit=3,
    )
    if system_events:
        out["system_events"] = system_events
    _trim_copilot_projection_payload(out)
    return out


def _copilot_projection_items(
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
            str(key): _copilot_projection_sanitize(item.get(key))
            for key in allowed_keys
            if item.get(key) not in (None, "", [], {})
        }
        if compact:
            out.append(compact)
    return out


def _copilot_projection_sanitize(value: Any, *, string_limit: int = 600) -> Any:
    if value in (None, "", [], {}):
        return value
    if isinstance(value, str):
        return value[:string_limit]
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, list):
        return [
            _copilot_projection_sanitize(item, string_limit=string_limit)
            for item in value[:20]
            if not isinstance(item, dict) or item
        ]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key or "").strip()
            if not key_text:
                continue
            sanitized = _copilot_projection_sanitize(item, string_limit=string_limit)
            if sanitized not in (None, "", [], {}):
                out[key_text] = sanitized
        return out
    return str(value)[:string_limit]


def _trim_copilot_projection_payload(payload: dict[str, Any]) -> None:
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
                    budget["truncation_reason"] = "copilot_projection_budget"
                break
        else:
            break


def _copilot_input_payload(text: str, *, conversation_context: dict[str, Any] | None) -> dict[str, Any]:
    copilot_task = derive_copilot_task_frame(
        question=text,
        request_context=None,
        today=_copilot_today_from_context(conversation_context),
        conversation_context=conversation_context,
    )
    evidence_plan = plan_copilot_evidence(copilot_task)
    selection = _copilot_analysis_view_selection(
        text,
        conversation_context=conversation_context,
        evidence_plan=evidence_plan.public_payload(),
    )
    preview_authority = _copilot_preview_authority(text)
    projection_payload: dict[str, Any] | None = None
    if isinstance(conversation_context, dict):
        projection = _copilot_context_projection(text, conversation_context=conversation_context)
        projection_payload = _copilot_projection_payload(projection, current_user_message=text)
        if (
            bool(preview_authority.get("allowed", False))
            and not preview_authority.get("allowed_preview_intents")
            and len(_symbol_setting_delta_frames(projection_payload)) == 1
        ):
            preview_authority = {**preview_authority, "allowed_preview_intents": ["symbol_edit"]}
    omitted_read_tools = _copilot_omitted_read_tools_for_question(
        text,
        analysis_view_selection=selection,
        preview_authority=preview_authority,
    )
    read_tool_selection_sources = _copilot_read_tool_selection_sources(
        text,
        analysis_view_selection=selection,
        omitted_read_tools=omitted_read_tools,
        preview_authority=preview_authority,
    )
    tools = _copilot_tool_manifest(
        analysis_view_names=selection["selected_analysis_views"],
        include_read_tools=True,
        include_preview_capabilities=bool(preview_authority.get("allowed", False) and preview_authority.get("allowed_preview_intents")),
        omit_read_tools=omitted_read_tools,
        allowed_preview_intents=preview_authority.get("allowed_preview_intents"),
    )
    payload: dict[str, Any] = {
        "message": str(text or ""),
        "current_user_message": {"text": str(text or "")},
        "copilot_task": copilot_task.public_payload(),
        "evidence_plan": evidence_plan.public_payload(),
        "tools": tools,
        "preview_authority": preview_authority,
        "manifest_budget": _copilot_manifest_budget(
            tools=tools,
            analysis_view_selection=selection,
            preview_authority=preview_authority,
            read_tool_selection_sources=read_tool_selection_sources,
        ),
    }
    if projection_payload is not None:
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


def _copilot_preview_authority(text: str) -> dict[str, Any]:
    authority = preview_authority_from_text(text)
    kind = preview_request_kind_from_text(text)
    allowed_preview_intents = _preview_intents_for_request_kind(kind)
    if bool(authority.get("allowed", False)) and allowed_preview_intents and not authority.get("allowed_preview_intents"):
        authority = {**authority, "allowed_preview_intents": allowed_preview_intents}
    return {
        **authority,
        "schema_version": "om-copilot-preview-authority-v1",
        "policy": (
            "The model may select exactly one preview capability only when the current user message explicitly "
            "asks to record/change/administer something or contains a broker lifecycle/fill notice. Ambiguous "
            "admin update wording may select only the exposed preview capability or ask for clarification. The "
            "host injects raw_text and never lets the model confirm, apply, notify, or mutate state directly."
        ),
    }


def _preview_intents_for_request_kind(kind: str | None) -> list[str]:
    value = str(kind or "").strip()
    if value == "manual_trade":
        intents = ("manual_trade_open", "manual_trade_close")
    elif value in AGENT_LOOP_PREVIEW_CAPABILITIES:
        intents = (value,)
    else:
        intents = ()
    return [intent for intent in intents if intent in AGENT_LOOP_PREVIEW_CAPABILITIES]


def _copilot_input_text(text: str, *, conversation_context: dict[str, Any] | None) -> str:
    return json.dumps(
        _copilot_input_payload(text, conversation_context=conversation_context),
        ensure_ascii=False,
        sort_keys=True,
    )


def _copilot_input_trace(payload: dict[str, Any], input_text: str) -> dict[str, Any]:
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


def _copilot_manifest_budget(
    *,
    tools: list[dict[str, Any]],
    analysis_view_selection: dict[str, Any],
    preview_authority: dict[str, Any] | None = None,
    read_tool_selection_sources: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    selected = [
        str(item)
        for item in analysis_view_selection.get("selected_analysis_views") or []
        if str(item).strip()
    ]
    preview = preview_authority if isinstance(preview_authority, dict) else {}
    read_tools_included = sorted(
        {
            str(tool.get("name") or "")
            for tool in tools
            if str(tool.get("name") or "") in AGENT_LOOP_READ_TOOLS
        }
    )
    return {
        "schema_version": "om-copilot-manifest-budget-v1",
        "mode": analysis_view_selection.get("mode"),
        "manifest_chars": len(json.dumps(tools, ensure_ascii=False, sort_keys=True)),
        "tool_count": len(tools),
        "read_tool_count": len(read_tools_included),
        "read_tools_included": read_tools_included,
        "read_tools_omitted": sorted(AGENT_LOOP_READ_TOOLS - set(read_tools_included)),
        "read_tool_selection_sources": list(read_tool_selection_sources or []),
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


def _copilot_analysis_view_selection(
    text: str,
    *,
    conversation_context: dict[str, Any] | None,
    evidence_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected: list[str] = []
    matched_groups: list[str] = []
    selection_sources: list[str] = []
    plan_views = [
        str(item)
        for item in (evidence_plan or {}).get("required_views", [])
        if str(item).strip()
    ]
    if plan_views:
        selected.extend(plan_views)
        matched_groups.append(str((evidence_plan or {}).get("task_name") or "task_profile"))
        selection_sources.append("copilot.evidence_plan")
    message_groups, message_views = _copilot_matched_analysis_groups(_copilot_selection_haystack([text]))
    if message_views:
        matched_groups.extend(message_groups)
        selected.extend(message_views)
        selection_sources.append("message")
    projection = _copilot_context_projection(text, conversation_context=conversation_context)
    gap_views = _copilot_context_suggested_analysis_views(text, conversation_context)
    if gap_views:
        selected.extend(gap_views)
        matched_groups.extend(_copilot_groups_for_analysis_views(gap_views))
        selection_sources.append("context_projection.open_evidence_gaps")

    projection_has_recent_evidence = bool(
        projection.get("recent_turns") or projection.get("recent_successful_tools") or projection.get("available_evidence_refs")
    )
    if projection_has_recent_evidence and (
        not message_views or _copilot_should_blend_context_for_term_followup(text, conversation_context)
    ):
        context_hints = _copilot_selection_context_hints(conversation_context, text=text)
        context_groups, context_group_views = _copilot_matched_analysis_groups(
            _copilot_selection_haystack(context_hints)
        )
        if context_group_views:
            matched_groups.extend(context_groups)
            selected.extend(context_group_views)
            selection_sources.append("context_projection.recent_evidence")

    if not selected:
        ref_views = _copilot_projection_views_from_refs(projection)
        if ref_views:
            selected.extend(ref_views)
            matched_groups.extend(_copilot_groups_for_analysis_views(ref_views))
            selection_sources.append("context_projection.available_evidence_refs")

    if not selected:
        selected.extend(_DEFAULT_COPILOT_ANALYSIS_VIEWS)
        selection_sources.append("default")
    selected = [
        view
        for view in _unique_strings([str(item) for item in selected])
        if view in ANALYSIS_VIEW_SPECS
    ][:MAX_COPILOT_ANALYSIS_VIEWS]
    if not selected:
        selected = sorted(ANALYSIS_VIEW_SPECS)[:MAX_COPILOT_ANALYSIS_VIEWS]
        selection_sources.append("catalog_fallback")
    return {
        "mode": "scoped_analysis_views",
        "selected_analysis_views": selected,
        "matched_view_groups": matched_groups or ["default"],
        "selection_sources": _unique_strings(selection_sources),
    }


def _copilot_matched_analysis_groups(haystack: str) -> tuple[list[str], list[str]]:
    matched_groups: list[str] = []
    selected: list[str] = []
    if not haystack.strip():
        return matched_groups, selected
    for group_name, keywords, views in _ANALYSIS_VIEW_GROUPS:
        if not any(_copilot_keyword_matches(haystack, keyword) for keyword in keywords):
            continue
        matched_groups.append(group_name)
        selected.extend(views)
    return matched_groups, selected


def _copilot_should_blend_context_for_term_followup(
    text: str,
    conversation_context: dict[str, Any] | None,
) -> bool:
    projection = _copilot_context_projection(text, conversation_context=conversation_context)
    if not _copilot_selection_context_hints(conversation_context, text=text):
        return False
    if not (projection.get("recent_turns") or projection.get("recent_successful_tools") or projection.get("available_evidence_refs")):
        return False
    compact = re.sub(r"\s+", "", str(text or "").lower())
    if not compact:
        return False
    if _copilot_message_has_explicit_context_switch(compact):
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
        "结论",
        "总结",
        "why",
        "how",
        "calculate",
        "computed",
        "meaning",
    )
    return any(token in compact for token in term_tokens)


def _copilot_message_has_explicit_context_switch(compact_text: str) -> bool:
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


def _copilot_selection_haystack(parts: list[Any] | tuple[Any, ...]) -> str:
    lower = " ".join(str(part or "") for part in parts if str(part or "").strip()).lower()
    compact = re.sub(r"\s+", "", lower)
    return f"{lower} {compact}"


def _copilot_keyword_matches(haystack: str, keyword: str) -> bool:
    raw = str(keyword or "").lower().strip()
    if not raw:
        return False
    return raw in haystack or re.sub(r"\s+", "", raw) in haystack


def _copilot_selection_context_hints(conversation_context: dict[str, Any] | None, *, text: str | None = None) -> list[str]:
    projection = _copilot_context_projection(str(text or ""), conversation_context=conversation_context)
    hints: list[str] = []
    for gap in projection.get("open_evidence_gaps") or []:
        if not isinstance(gap, dict):
            continue
        hints.append(str(gap.get("kind") or ""))
        hints.append(str(gap.get("summary") or ""))
        hints.append(_compact_json_for_copilot_selection(gap.get("suggested_tools")))
        hints.append(_compact_json_for_copilot_selection(gap.get("suggested_views")))
        hints.append(_compact_json_for_copilot_selection(gap.get("safe_slots")))
    for item in projection.get("recent_turns") or []:
        if not isinstance(item, dict):
            continue
        hints.append(str(item.get("user_summary") or ""))
        hints.append(str(item.get("assistant_summary") or ""))
        hints.append(_compact_json_for_copilot_selection(item.get("tools")))
        hints.append(_compact_json_for_copilot_selection(item.get("safe_slots")))
    for item in projection.get("recent_successful_tools") or []:
        if not isinstance(item, dict):
            continue
        tool_name = str(item.get("tool_name") or "").strip()
        if tool_name:
            hints.append(tool_name)
            hints.extend(_COPILOT_CONTEXT_TOOL_HINTS.get(tool_name, ()))
        hints.append(str(item.get("purpose") or ""))
        hints.append(_compact_json_for_copilot_selection(item.get("safe_payload")))
        hints.append(_compact_json_for_copilot_selection(item.get("safe_slots")))
        hints.append(_compact_json_for_copilot_selection(item.get("data_shape")))
    for ref in projection.get("available_evidence_refs") or []:
        if not isinstance(ref, dict):
            continue
        tool_name = str(ref.get("source_tool") or "").strip()
        if tool_name:
            hints.append(tool_name)
            hints.extend(_COPILOT_CONTEXT_TOOL_HINTS.get(tool_name, ()))
        hints.append(str(ref.get("label") or ""))
        hints.append(_compact_json_for_copilot_selection(ref.get("safe_slots")))
        hints.append(_compact_json_for_copilot_selection(ref.get("data_shape")))

    return [hint for hint in hints if str(hint or "").strip()]


def _copilot_context_suggested_analysis_views(text: str, conversation_context: dict[str, Any] | None) -> list[str]:
    projection = _copilot_context_projection(text, conversation_context=conversation_context)
    views: list[str] = []
    for gap in projection.get("open_evidence_gaps") or []:
        if not isinstance(gap, dict):
            continue
        for view in gap.get("suggested_views") or []:
            name = str(view or "").strip()
            if name in ANALYSIS_VIEW_SPECS:
                views.append(name)
    return _unique_strings(views)


def _copilot_projection_views_from_refs(projection: dict[str, Any]) -> list[str]:
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


def _copilot_groups_for_analysis_views(views: list[str]) -> list[str]:
    view_set = {str(item) for item in views if str(item).strip()}
    groups: list[str] = []
    for group_name, _keywords, group_views in _ANALYSIS_VIEW_GROUPS:
        if view_set.intersection(str(view) for view in group_views):
            groups.append(group_name)
    return _unique_strings(groups)


def _compact_json_for_copilot_selection(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(value)
    return text[:1000]


def _copilot_tool_manifest(
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
            "copilot_notes": notes,
        }
        if semantics:
            item["semantics"] = semantics
        tools.append(item)
    allowed_preview = {str(item) for item in allowed_preview_intents or () if str(item).strip()}
    for spec in copilot_preview_specs() if include_preview_capabilities else ():
        if allowed_preview and spec.intent_name not in allowed_preview:
            continue
        item = {
            "name": spec.intent_name,
            "description": spec.summary,
            "capabilities": ["preview_operation"],
            "input_schema": _copilot_preview_input_schema(spec.intent_name),
            "safe_default_input": {},
            "risk_level": spec.risk_level,
            "operation_action": "preview",
            "operation_target": spec.operation_target,
            "copilot_notes": _copilot_preview_notes(spec.intent_name),
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


def _copilot_preview_input_schema(intent_name: str) -> dict[str, Any]:
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


def _copilot_preview_notes(intent_name: str) -> list[str]:
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
            "not for explaining assignment notices; not for assigned-stock PnL questions or status questions; those are read-only analysis/position requests.",
            "Extract visible lifecycle fields such as symbol, option_type, position_side, contracts_to_close, strike, expiration_ymd, stock_side, stock_qty, and stock_price; omit uncertain fields.",
            "Do not provide raw_text; the host injects the original user message and validates your structured fields against open lots before creating a preview.",
            "Creates only a pending preview; it never writes the ledger until a deterministic confirm command is received.",
        ]
    if intent_name == "manual_expiry":
        return [
            "Use for Futu 期权到期失效通知 or 已到期失效 lifecycle notices.",
            "not for explaining expiry notices or status questions; those are read-only analysis/position requests.",
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


def _llm_trace(
    settings: Any,
    *,
    attempted: bool,
    reason: str,
    missing: list[str] | None = None,
    error_code: str | None = None,
    schema_version: str | None = None,
    conversation_context: dict[str, Any] | None = None,
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


__all__ = [
    "AGENT_LOOP_SCHEMA_VERSION",
    "AGENT_LOOP_ALLOWED_TOOLS",
    "AgentLoopResult",
    "AgentLoopStep",
    "AgentLoopPlanningOutcome",
    "AssistantToolLoopOutcome",
    "FinalResponsePlan",
    "GuardedModelToolCallExecution",
    "INTERNAL_TOOL_LOOP_NAME",
    "MAX_COPILOT_STEPS",
    "ToolExecutionOutcome",
    "ToolExecutor",
    "ToolObservation",
    "build_tool_observation",
    "build_synthesis_observation",
    "execute_model_tool_call_event",
    "run_read_only_agent_loop",
    "skipped_llm_trace",
]
