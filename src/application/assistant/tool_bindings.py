from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ScopePolicy = Literal[
    "none",
    "config_required",
    "config_optional",
    "symbol_market_config_required",
    "symbol_market_config_optional",
]


@dataclass(frozen=True)
class AssistantToolBinding:
    intent_name: str
    tool_name: str | None
    commands: tuple[str, ...] = ()
    display_name: str = ""
    arguments: tuple[str, ...] = ()
    read_only: bool = True
    llm_allowed: bool = True
    llm_visible: bool = True
    supported: bool = True
    risk_level: str | None = None
    examples: tuple[str, ...] = ()
    summary: str = ""
    kind: str | None = None
    planner_allowed: bool | None = None
    direct_executable: bool | None = None
    requires_pending: bool | None = None
    requires_confirm: bool | None = None
    scope_policy: ScopePolicy = "none"
    renderer_key: str | None = None
    required_arguments: tuple[str, ...] = ()
    planner_notes: tuple[str, ...] = ()
    planner_semantics: dict[str, Any] = field(default_factory=dict)
    primary_for_tool: bool = True


READ_TOOL_BINDINGS: tuple[AssistantToolBinding, ...] = (
    AssistantToolBinding(
        intent_name="help",
        tool_name=None,
        commands=("/help", "/?"),
        display_name="帮助",
        examples=("帮助", "/help"),
        summary="show supported inbound commands",
        kind="local",
    ),
    AssistantToolBinding(
        intent_name="runtime_status",
        tool_name="runtime_status",
        commands=("/status",),
        display_name="状态",
        examples=("状态", "/status"),
        summary="show runtime status",
        scope_policy="config_required",
    ),
    AssistantToolBinding(
        intent_name="healthcheck",
        tool_name="healthcheck",
        commands=("/health", "/doctor"),
        display_name="健康检查",
        examples=("健康检查", "/health"),
        summary="run read-only health checks",
        scope_policy="config_required",
    ),
    AssistantToolBinding(
        intent_name="config_validate",
        tool_name="config_validate",
        commands=("/config-check", "/config"),
        display_name="配置检查",
        examples=("配置检查", "/config-check"),
        summary="validate runtime config",
        scope_policy="config_required",
    ),
    AssistantToolBinding(
        intent_name="symbol_config_query",
        tool_name="symbol_config_read",
        display_name="标的配置",
        arguments=("symbol", "strategy", "field"),
        required_arguments=("symbol",),
        examples=("现在泡泡玛特 sell put 的 max strike 是多少", "查询 9992.HK sell_put.max_strike"),
        summary="read current monitored-symbol strategy config for a symbol",
        scope_policy="symbol_market_config_required",
    ),
    AssistantToolBinding(
        intent_name="symbol_resolve",
        tool_name="symbol_resolve",
        display_name="标的解析",
        arguments=("symbol",),
        required_arguments=("symbol",),
        examples=("泡泡玛特是什么 symbol", "POP 对应哪个标的", "HK.09992 解析成什么"),
        summary="resolve a user-provided symbol/name/alias/Futu code to canonical OM symbol identity",
        scope_policy="symbol_market_config_required",
        planner_notes=(
            "Use when the user asks what a symbol/name/alias maps to, or before SQL-style analysis that needs a canonical symbol.",
            "The tool resolves Chinese names, configured aliases, Futu codes, HK numeric codes, and canonical US/HK symbols.",
            "This tool only resolves identity; it does not answer whether the symbol was filtered, held, profitable, or configured.",
        ),
        planner_semantics={
            "data_source": "OM symbol identity resolver plus runtime config aliases when scoped config is injected",
            "answer_capabilities": {
                "symbol_resolve": "maps a raw symbol/name/alias to canonical_symbol, market, currency, and futu_code",
                "read_only": "does not mutate config or runtime state",
            },
            "scope_semantics": {
                "config injected": "runtime config aliases are included; HK/US sibling config may be selected from the symbol market",
                "config omitted": "built-in canonicalization and fallback aliases only",
            },
            "not_promised": [
                "market data lookup",
                "watchlist membership",
                "candidate filter diagnosis",
            ],
        },
    ),
    AssistantToolBinding(
        intent_name="candidate_filter_explain",
        tool_name="candidate_filter_explain",
        display_name="候选过滤诊断",
        arguments=("symbol", "account", "function", "run_id"),
        required_arguments=("symbol",),
        examples=("泡泡玛特被哪个参数过滤了？", "为什么 NVDA 没出现在候选里？", "lx NVDA sell_put 为什么被过滤？"),
        summary="explain a single symbol's observed candidate filter/rejection/missing trace rows from runtime candidate_filter_trace artifacts",
        scope_policy="symbol_market_config_optional",
        planner_notes=(
            "Use for single-symbol candidate filter, rejection, missing-candidate, or 被哪个参数过滤 questions.",
            "symbol can be canonical, Chinese name, Futu code, or alias such as 泡泡玛特; the tool resolves it before matching trace rows.",
            "account is optional scan/run scope only, not business semantics for symbol identity.",
            "For aggregation/comparison/trend across many symbols, rules, accounts, or runs, use analysis_query over candidate_filter_diagnostics instead.",
        ),
        planner_semantics={
            "data_source": "candidate_filter_trace.jsonl artifacts discovered from runtime root/latest output_runs",
            "answer_capabilities": {
                "filter_explain": "explains observed accepted/rejected/post-filtered/not-observed candidate trace rows for one symbol",
                "candidate_filter_trace": "uses scan-time trace artifacts as the fact source",
                "read_only": "does not run scans, fetch market data, send notifications, or write reports",
            },
            "scope_semantics": {
                "account": "scan/run scope only; omit to search all account trace artifacts in scope",
                "function": "optional filter function such as sell_put, sell_call, cash_reserve, or share_coverage",
                "run_id omitted": "searches runtime last-run pointer, recent output_runs, and shared trace fallbacks; pass run_id when a specific run is required",
            },
            "not_promised": [
                "inferring root cause when trace rows are missing",
                "rerunning candidate scans",
                "aggregated rule comparisons across runs",
            ],
            "answer_rules": [
                "If trace_count is zero, say the candidate diagnostic is missing and cannot determine the exact filtering parameter.",
                "Use rule, metric_value, threshold, status, stage, contract_symbol, expiration, and strike from tool events as evidence.",
                "Do not present account as symbol identity or business ownership.",
            ],
        },
    ),
    AssistantToolBinding(
        intent_name="analysis_catalog",
        tool_name="analysis_catalog",
        display_name="分析目录",
        arguments=("view", "views"),
        examples=("有哪些数据可以分析", "收益和指派正股能查询哪些字段"),
        summary="inspect Tool OS read-only analysis views and SQL rules",
        scope_policy="config_optional",
    ),
    AssistantToolBinding(
        intent_name="analysis_query",
        tool_name="analysis_query",
        display_name="通用分析",
        arguments=("sql", "query", "limit", "account", "month"),
        examples=(
            "对比 lx 和 sy 的账户收益，有什么不同？",
            "指派正股浮盈亏按账户汇总",
            "按标的统计已实现收益",
        ),
        summary=(
            "run SELECT-only queries over whitelisted OM analysis views for comparisons, rankings, trends, "
            "breakdowns, and cross-domain analytical answers"
        ),
        scope_policy="config_optional",
    ),
    AssistantToolBinding(
        intent_name="operation_timeline",
        tool_name="operation_timeline",
        display_name="操作时间线",
        arguments=("operation_id", "operation_types", "statuses", "limit"),
        llm_allowed=False,
        llm_visible=False,
        planner_allowed=True,
        direct_executable=False,
        risk_level="read_only",
        examples=("查询升级 command_id 的操作时间线",),
        summary="planner-only read surface for operation audit evidence such as upgrade status and receipt diagnostics",
    ),
    AssistantToolBinding(
        intent_name="position_query",
        tool_name="option_positions_read",
        commands=("/positions",),
        display_name="持仓",
        arguments=("account", "status", "symbol", "option_type", "side", "strike", "expiration", "limit"),
        examples=("持仓", "持仓 [账户]", "持仓 [到期月份/到期日/标的/类型/方向]", "/positions [lx|sy|all]"),
        summary="list option positions",
        scope_policy="config_required",
    ),
    AssistantToolBinding(
        intent_name="assigned_stock_position_query",
        tool_name="option_positions_read",
        commands=("/assigned-stock",),
        display_name="指派正股",
        arguments=("account", "symbol", "assigned_stock_status", "stock_lot_id", "refresh_quotes"),
        examples=(
            "指派正股持仓盈亏",
            "查看 lx 被指派正股浮盈亏",
            "NVDA 指派正股盈亏",
            "/assigned-stock [lx|sy|all] [symbol] [open|partially_sold|closed|all]",
        ),
        summary="show assigned stock lots from Sell Put assignment, including cost basis, realtime spot, and holding PnL",
        scope_policy="config_required",
        primary_for_tool=False,
    ),
    AssistantToolBinding(
        intent_name="position_exit_analysis",
        tool_name="close_advice_read",
        display_name="平仓/止盈分析",
        arguments=("account", "symbol", "option_type", "side", "strike", "expiration", "limit"),
        risk_level="read_only",
        examples=("分析 long call 是不是应该平仓", "泡泡玛特 long call 的持仓应该止盈吗"),
        summary="analyze matching option positions using the latest generated close-advice report",
        scope_policy="config_required",
    ),
    AssistantToolBinding(
        intent_name="monthly_income_report",
        tool_name="monthly_income_report",
        commands=("/income",),
        display_name="收益",
        arguments=("account", "month"),
        examples=("收益", "收益 [账户]", "收益 [账户] [YYYY-MM|6月|本月|上月]", "/income [lx|sy] [YYYY-MM|6月|本月|上月]"),
        summary="show monthly income report",
        scope_policy="config_required",
    ),
    AssistantToolBinding(
        intent_name="runtime_runs",
        tool_name="runtime_runs",
        commands=("/runs",),
        display_name="运行记录",
        arguments=("limit",),
        examples=("最近运行", "/runs [limit]"),
        summary="list recent runtime runs",
    ),
    AssistantToolBinding(
        intent_name="runtime_logs",
        tool_name="runtime_logs",
        commands=("/logs",),
        display_name="日志",
        arguments=("run_id", "kind", "lines"),
        required_arguments=("run_id",),
        examples=("日志 <run_id>", "/logs <run_id>"),
        summary="show runtime logs for a run",
    ),
    AssistantToolBinding(
        intent_name="symbol_list",
        tool_name="inbound.symbols",
        commands=("/symbols",),
        display_name="监控标的",
        examples=("查看监控标的", "/symbols"),
        summary="list monitored symbols",
        planner_allowed=False,
        scope_policy="config_required",
    ),
    AssistantToolBinding(
        intent_name="pending_operations",
        tool_name="inbound.pending",
        commands=("/pending",),
        display_name="待确认",
        examples=("待确认", "/pending"),
        summary="list pending preview operations",
        planner_allowed=False,
    ),
    AssistantToolBinding(
        intent_name="model_list",
        tool_name="inbound.model",
        commands=("/model",),
        display_name="模型",
        llm_allowed=False,
        examples=("/model", "/model list"),
        summary="list configured assistant model profiles",
    ),
)


def assistant_tool_bindings() -> tuple[AssistantToolBinding, ...]:
    return READ_TOOL_BINDINGS


def binding_by_intent() -> dict[str, AssistantToolBinding]:
    return {binding.intent_name: binding for binding in READ_TOOL_BINDINGS}


def binding_for_intent(intent_name: str) -> AssistantToolBinding | None:
    return binding_by_intent().get(str(intent_name or ""))


def primary_intent_name_for_tool(tool_name: str) -> str | None:
    normalized = str(tool_name or "")
    for binding in READ_TOOL_BINDINGS:
        if binding.tool_name == normalized and binding.primary_for_tool:
            return binding.intent_name
    for binding in READ_TOOL_BINDINGS:
        if binding.tool_name == normalized:
            return binding.intent_name
    return None


def tool_name_for_intent(intent_name: str) -> str | None:
    binding = binding_for_intent(intent_name)
    return binding.tool_name if binding is not None else None


def config_required_intent_names() -> frozenset[str]:
    return frozenset(
        binding.intent_name
        for binding in READ_TOOL_BINDINGS
        if binding.scope_policy in {"config_required", "symbol_market_config_required"}
    )


def planner_config_scoped_tool_names() -> frozenset[str]:
    return frozenset(
        str(binding.tool_name)
        for binding in READ_TOOL_BINDINGS
        if binding.tool_name is not None and binding.scope_policy != "none"
    )


def symbol_market_config_tool_names() -> frozenset[str]:
    return frozenset(
        str(binding.tool_name)
        for binding in READ_TOOL_BINDINGS
        if binding.tool_name is not None and binding.scope_policy.startswith("symbol_market_config")
    )


def planner_binding_for_tool(tool_name: str) -> AssistantToolBinding | None:
    normalized = str(tool_name or "")
    for binding in READ_TOOL_BINDINGS:
        if binding.tool_name == normalized and binding.primary_for_tool:
            return binding
    for binding in READ_TOOL_BINDINGS:
        if binding.tool_name == normalized:
            return binding
    return None
