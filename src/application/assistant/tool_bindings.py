from __future__ import annotations

import importlib
from dataclasses import dataclass, replace
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
    supported: bool = True
    risk_level: str | None = None
    examples: tuple[str, ...] = ()
    summary: str = ""
    kind: str | None = None
    direct_executable: bool | None = None
    requires_pending: bool | None = None
    requires_confirm: bool | None = None
    scope_policy: ScopePolicy = "none"
    renderer_key: str | None = None
    required_arguments: tuple[str, ...] = ()
    primary_for_tool: bool = True


_LOCAL_BINDINGS: tuple[AssistantToolBinding, ...] = (
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
        intent_name="symbol_list",
        tool_name="inbound.symbols",
        commands=("/symbols",),
        display_name="监控标的",
        examples=("查看监控标的", "/symbols"),
        summary="list monitored symbols",
        scope_policy="config_required",
    ),
    AssistantToolBinding(
        intent_name="pending_operations",
        tool_name="inbound.pending",
        commands=("/pending",),
        display_name="待确认",
        examples=("待确认", "/pending"),
        summary="list pending preview operations",
    ),
    AssistantToolBinding(
        intent_name="model_list",
        tool_name="inbound.model",
        commands=("/model",),
        display_name="模型",
        examples=("/model", "/model list"),
        summary="list configured assistant model profiles",
    ),
)

_SYSTEM_ARGUMENTS = frozenset(
    {
        "audit_db",
        "config_key",
        "config_path",
        "data_config",
        "output_dir",
        "runtime_root",
        "state_dir",
    }
)

_TOOL_OVERRIDES: dict[str, dict[str, Any]] = {
    "runtime_status": {
        "commands": ("/status",),
        "display_name": "状态",
        "direct_executable": True,
        "examples": ("状态", "/status"),
        "summary": "show runtime status",
        "scope_policy": "config_required",
    },
    "healthcheck": {
        "commands": ("/health", "/doctor"),
        "display_name": "健康检查",
        "direct_executable": True,
        "examples": ("健康检查", "/health"),
        "summary": "run read-only health checks",
        "scope_policy": "config_required",
    },
    "config_validate": {
        "commands": ("/config-check", "/config"),
        "display_name": "配置检查",
        "direct_executable": True,
        "examples": ("配置检查", "/config-check"),
        "summary": "validate runtime config",
        "scope_policy": "config_required",
    },
    "symbol_config_read": {
        "intent_name": "symbol_config_query",
        "display_name": "标的配置",
        "arguments": ("symbol", "strategy", "field"),
        "required_arguments": ("symbol",),
        "direct_executable": True,
        "examples": ("现在泡泡玛特 sell put 的 max strike 是多少", "查询 9992.HK sell_put.max_strike"),
        "summary": "read current monitored-symbol strategy config for a symbol",
        "scope_policy": "symbol_market_config_required",
    },
    "symbol_resolve": {
        "display_name": "标的解析",
        "arguments": ("symbol",),
        "required_arguments": ("symbol",),
        "direct_executable": True,
        "examples": ("泡泡玛特是什么 symbol", "POP 对应哪个标的", "HK.09992 解析成什么"),
        "summary": "resolve a user-provided symbol/name/alias/Futu code to canonical OM symbol identity",
        "scope_policy": "symbol_market_config_required",
    },
    "candidate_filter_explain": {
        "display_name": "候选过滤诊断",
        "arguments": ("symbol", "account", "function", "run_id"),
        "required_arguments": ("symbol",),
        "direct_executable": True,
        "examples": ("泡泡玛特被哪个参数过滤了？", "为什么 NVDA 没出现在候选里？", "lx NVDA sell_put 为什么被过滤？"),
        "summary": "explain a single symbol's observed candidate filter/rejection/missing trace rows from runtime candidate_filter_trace artifacts",
        "scope_policy": "symbol_market_config_optional",
    },
    "analysis_catalog": {
        "display_name": "分析目录",
        "arguments": ("view", "views"),
        "examples": ("有哪些数据可以分析", "收益和指派正股能查询哪些字段"),
        "summary": "inspect Tool OS read-only analysis views and SQL rules",
        "scope_policy": "config_optional",
    },
    "analysis_query": {
        "display_name": "通用分析",
        "arguments": ("sql", "query", "limit", "account", "month"),
        "examples": (
            "对比 lx 和 sy 的账户收益，有什么不同？",
            "指派正股浮盈亏按账户汇总",
            "按标的统计已实现收益",
        ),
        "summary": (
            "run SELECT-only queries over whitelisted OM analysis views for comparisons, rankings, trends, "
            "breakdowns, and cross-domain analytical answers"
        ),
        "scope_policy": "config_optional",
    },
    "operation_timeline": {
        "display_name": "操作时间线",
        "arguments": ("operation_id", "operation_types", "statuses", "limit"),
        "direct_executable": False,
        "risk_level": "read_only",
        "examples": ("查询升级 command_id 的操作时间线",),
        "summary": "read operation audit receipts such as upgrade status and diagnostics",
    },
    "option_positions_read": {
        "intent_name": "position_query",
        "commands": ("/positions",),
        "display_name": "持仓",
        "arguments": ("account", "status", "symbol", "option_type", "side", "strike", "expiration", "limit"),
        "direct_executable": True,
        "examples": ("持仓", "持仓 [账户]", "持仓 [到期月份/到期日/标的/类型/方向]", "/positions [lx|sy|all]"),
        "summary": "list option positions",
        "scope_policy": "config_required",
    },
    "close_advice_read": {
        "intent_name": "position_exit_analysis",
        "display_name": "平仓/止盈分析",
        "arguments": ("account", "symbol", "option_type", "side", "strike", "expiration", "limit"),
        "direct_executable": True,
        "risk_level": "read_only",
        "examples": ("分析 long call 是不是应该平仓", "泡泡玛特 long call 的持仓应该止盈吗"),
        "summary": "analyze matching option positions using the latest generated close-advice report",
        "scope_policy": "config_required",
    },
    "option_performance_report": {
        "commands": ("/income",),
        "display_name": "期权收益",
        "arguments": ("account", "broker", "period", "as_of_date", "month", "year", "start_date", "end_date"),
        "direct_executable": True,
        "examples": (
            "收益",
            "收益 [账户] [mtd|ytd|YYYY|YYYY-MM|本月|上月]",
            "/income [lx|sy] [mtd|ytd|YYYY|YYYY-MM|本月|上月]",
        ),
        "summary": "show option performance with separate PnL, cash, and premium activity namespaces",
        "scope_policy": "config_required",
        "renderer_key": "option_performance",
    },
    "monthly_income_report": {
        "commands": (),
        "display_name": "月度收益（已弃用）",
        "arguments": ("account", "month"),
        "direct_executable": False,
        "examples": ("monthly_income_report（deprecated）",),
        "summary": "deprecated compatibility adapter; use option_performance_report",
        "scope_policy": "config_required",
        "primary_for_tool": False,
    },
    "runtime_runs": {
        "commands": ("/runs",),
        "display_name": "运行记录",
        "arguments": ("limit",),
        "direct_executable": True,
        "examples": ("最近运行", "/runs [limit]"),
        "summary": "list recent runtime runs",
    },
    "runtime_logs": {
        "commands": ("/logs",),
        "display_name": "日志",
        "arguments": ("run_id", "kind", "lines"),
        "required_arguments": ("run_id",),
        "direct_executable": True,
        "examples": ("日志 <run_id>", "/logs <run_id>"),
        "summary": "show runtime logs for a run",
    },
    "notification_perception_read": {
        "display_name": "通知感知",
        "arguments": ("run_id", "conversation_id", "event_kind", "limit"),
        "examples": ("刚才通知发生了什么", "最近一次通知为什么没发", "查看这个 run 的通知感知事件"),
        "summary": "read compressed tick notification perception events from audit artifacts",
    },
    "daily_decision_brief_read": {
        "display_name": "期权监控",
        "arguments": ("account", "market", "date", "revision"),
        "direct_executable": True,
        "examples": ("期权监控", "最新期权报告", "港股期权", "美股期权", "lx 期权", "sy 期权"),
        "summary": "read the latest successful option-monitor snapshot without scanning or sending",
    },
    "query_cash_headroom": {
        "intent_name": "cash_headroom_query",
        "display_name": "现金余量",
        "arguments": ("account", "broker", "top", "no_exchange_rates"),
        "examples": ("lx账户sell put需要的资金是不是已经超过了账户现有的现金加货基？",),
        "summary": "compare sell put cash-secured usage with account cash-like assets",
        "scope_policy": "config_required",
    },
}

_EXTRA_TOOL_BINDINGS: dict[str, tuple[AssistantToolBinding, ...]] = {
    "option_positions_read": (
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
    ),
}


def _default_arguments(tool: Any) -> tuple[str, ...]:
    return tuple(str(key) for key in tool.input_schema if str(key) not in _SYSTEM_ARGUMENTS)


def _binding_from_tool(tool: Any) -> AssistantToolBinding:
    binding = AssistantToolBinding(
        intent_name=tool.name,
        tool_name=tool.name,
        display_name=tool.name,
        arguments=_default_arguments(tool),
        read_only=tool.read_only,
        risk_level=tool.resolved_risk_level(),
        requires_confirm=tool.requires_confirm,
        direct_executable=False,
        examples=(tool.name,),
        summary=tool.description,
        scope_policy="config_optional" if "config_key" in tool.input_schema else "none",
    )
    override = _TOOL_OVERRIDES.get(tool.name)
    if override:
        binding = replace(binding, **override)
    return binding


def assistant_tool_bindings() -> tuple[AssistantToolBinding, ...]:
    registry = importlib.import_module("src.application.agent_tool_registry")
    bindings: list[AssistantToolBinding] = list(_LOCAL_BINDINGS)
    for tool in registry.AGENT_TOOL_DEFINITIONS:
        if not tool.enabled or not tool.is_pure_read():
            continue
        bindings.append(_binding_from_tool(tool))
        bindings.extend(_EXTRA_TOOL_BINDINGS.get(tool.name, ()))
    return tuple(bindings)


def binding_by_intent() -> dict[str, AssistantToolBinding]:
    return {binding.intent_name: binding for binding in assistant_tool_bindings()}


def binding_for_intent(intent_name: str) -> AssistantToolBinding | None:
    return binding_by_intent().get(str(intent_name or ""))


def primary_intent_name_for_tool(tool_name: str) -> str | None:
    normalized = str(tool_name or "")
    for binding in assistant_tool_bindings():
        if binding.tool_name == normalized and binding.primary_for_tool:
            return binding.intent_name
    for binding in assistant_tool_bindings():
        if binding.tool_name == normalized:
            return binding.intent_name
    return None


def tool_name_for_intent(intent_name: str) -> str | None:
    binding = binding_for_intent(intent_name)
    return binding.tool_name if binding is not None else None


def config_required_intent_names() -> frozenset[str]:
    return frozenset(
        binding.intent_name
        for binding in assistant_tool_bindings()
        if binding.scope_policy in {"config_required", "symbol_market_config_required"}
    )


def symbol_market_config_tool_names() -> frozenset[str]:
    return frozenset(
        str(binding.tool_name)
        for binding in assistant_tool_bindings()
        if binding.tool_name is not None and binding.scope_policy.startswith("symbol_market_config")
    )
