from __future__ import annotations

from typing import Any

from src.application.agent_tools.materialization_impl import (
    close_advice_rows_summary,
    close_advice_tool,
    get_close_advice_tool,
    get_portfolio_context_tool,
    prepare_close_advice_inputs_tool,
    query_cash_headroom_tool,
    scan_opportunities_tool,
    scan_summary_rows,
)
from src.application.agent_tools.base import AgentTool, AgentToolContext, build_agent_tool


def _mask_path_str(ctx: AgentToolContext, value: Any) -> str:
    return ctx.mask_path(value) or "..."


def _query_cash_headroom_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return query_cash_headroom_tool(
        payload,
        load_runtime_config=ctx.load_runtime_config,
        resolve_public_data_config_path=ctx.resolve_public_data_config_path,
        normalize_broker=ctx.normalize_broker,
        resolve_output_root=ctx.resolve_output_root,
        query_sell_put_cash=ctx.query_sell_put_cash,
        repo_base=ctx.repo_base,
        mask_path=lambda value: _mask_path_str(ctx, value),
    )


def _get_portfolio_context_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return get_portfolio_context_tool(
        payload,
        load_runtime_config=ctx.load_runtime_config,
        resolve_public_data_config_path=ctx.resolve_public_data_config_path,
        normalize_broker=ctx.normalize_broker,
        resolve_output_root=ctx.resolve_output_root,
        load_portfolio_context=ctx.load_portfolio_context,
        repo_base=ctx.repo_base,
        mask_path=ctx.mask_path,
    )


def _scan_opportunities_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return scan_opportunities_tool(
        payload,
        load_runtime_config=ctx.load_runtime_config,
        resolve_data_config_ref=ctx.resolve_data_config_ref,
        resolve_output_root=ctx.resolve_output_root,
        repo_base=ctx.repo_base,
        load_config=ctx.load_runtime_pipeline_config,
        run_watchlist_pipeline_default=ctx.run_watchlist_pipeline_default,
        scan_summary_rows_fn=lambda rows: scan_summary_rows(rows, as_float=ctx.as_float),
    )


def _prepare_close_advice_inputs_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return prepare_close_advice_inputs_tool(
        payload,
        load_runtime_config=ctx.load_runtime_config,
        resolve_public_data_config_path=ctx.resolve_public_data_config_path,
        normalize_broker=ctx.normalize_broker,
        resolve_output_root=ctx.resolve_output_root,
        load_option_positions_context=ctx.load_option_positions_context,
        symbol_fetch_config_map_fn=ctx.symbol_fetch_config_map,
        extract_context_symbols_fn=ctx.extract_context_symbols,
        resolve_symbol_fetch_source=ctx.resolve_symbol_fetch_source,
        fetch_symbol_opend=ctx.fetch_symbol_opend,
        save_required_data_opend=ctx.save_required_data_opend,
        repo_base=ctx.repo_base,
        mask_path=ctx.mask_path,
    )


def _close_advice_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return close_advice_tool(
        payload,
        load_runtime_config=ctx.load_runtime_config,
        resolve_output_root=ctx.resolve_output_root,
        resolve_local_path=ctx.resolve_local_path,
        run_close_advice=ctx.run_close_advice,
        close_advice_rows_summary_fn=lambda csv_path, text_path: close_advice_rows_summary(
            csv_path,
            text_path,
            safe_read_csv=ctx.safe_read_csv,
            as_float=ctx.as_float,
        ),
        repo_base=ctx.repo_base,
        mask_path=ctx.mask_path,
    )


def _get_close_advice_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return get_close_advice_tool(
        payload,
        prepare_close_advice_inputs_tool_fn=lambda tool_payload: _prepare_close_advice_inputs_tool(ctx, tool_payload),
        close_advice_tool_fn=lambda tool_payload: _close_advice_tool(ctx, tool_payload),
    )


_CASH_HEADROOM_OUTPUT_CONTRACT = {
    "schema_version": "query_cash_headroom.output.v1",
    "canonical_renderer": "cash_headroom",
    "guard_profile": "cash_headroom",
    "source_label": "OM cash headroom query",
    "result_shape": "scalar",
    "fact_fields": [
        "account",
        "cash_secured_used_cny",
        "cash_available_total_cny",
        "cash_free_total_cny",
        "cash_secured_total_by_ccy",
        "cash_secured_usage_reliable",
    ],
    "missing_data_fields": ["cash_secured_unavailable_by_symbol", "cash_secured_unavailable_reason"],
}

_CASH_HEADROOM_PLANNER_NOTES = (
    "Use for sell put collateral vs account cash/cash-like sufficiency questions.",
    "Do not use healthcheck for cash sufficiency; healthcheck only reports system readiness.",
)

_CASH_HEADROOM_PLANNER_SEMANTICS = {
    "answers": [
        "sell put cash-secured collateral currently used",
        "cash plus cash-like assets converted to CNY",
        "whether used collateral exceeds cash-like assets",
    ],
    "not_promised": [
        "candidate recommendation ranking",
        "config max strike thresholds",
        "notification delivery health",
    ],
}


SCAN_OPPORTUNITIES_TOOL = build_agent_tool(
    name="scan_opportunities",
    description="Run the symbols scan pipeline and return normalized summary rows.",
    requires=("runtime_config", "opend"),
    capabilities=("scan", "read_only"),
    side_effects=("writes_local_reports",),
    input_schema={
        "config_key": "us|hk",
        "config_path": "optional explicit config path",
        "data_config": "optional explicit data config path",
        "symbols": "optional list[str] filter",
        "top_n": "optional int",
        "no_context": "optional bool",
    },
    handler=_scan_opportunities_tool,
    read_only=True,
    risk_level="local_write",
    safe_default_input={"top_n": 5},
    examples=({"input": {"config_key": "us", "top_n": 5}},),
)

QUERY_CASH_HEADROOM_TOOL = build_agent_tool(
    name="query_cash_headroom",
    description="Return sell-put cash usage and available/free cash summary.",
    requires=("runtime_config", "sqlite_data_config", "opend"),
    capabilities=("cash_query", "read_only"),
    input_schema={
        "config_key": "us|hk",
        "config_path": "optional explicit config path",
        "data_config": "optional explicit data config path",
        "account": "optional account label",
        "broker": "optional broker name, preferred public field",
        "top": "optional int",
        "no_exchange_rates": "optional bool",
    },
    handler=_query_cash_headroom_tool,
    pure_read=True,
    safe_default_input={},
    examples=(
        {"input": {"config_key": "us", "account": "lx"}},
        {"input": {"config_key": "us", "account": "sy"}},
    ),
    output_contract=_CASH_HEADROOM_OUTPUT_CONTRACT,
    planner_notes=_CASH_HEADROOM_PLANNER_NOTES,
    planner_semantics=_CASH_HEADROOM_PLANNER_SEMANTICS,
)

GET_PORTFOLIO_CONTEXT_TOOL = build_agent_tool(
    name="get_portfolio_context",
    description="Fetch holdings/Futu-backed portfolio context for one account.",
    requires=("runtime_config", "opend"),
    capabilities=("portfolio_context", "read_only"),
    side_effects=("writes_local_cache",),
    input_schema={
        "config_key": "us|hk",
        "config_path": "optional explicit config path",
        "data_config": "optional explicit data config path",
        "account": "optional account label",
        "broker": "optional broker name, preferred public field",
        "ttl_sec": "optional int",
        "timeout_sec": "optional int",
    },
    handler=_get_portfolio_context_tool,
    read_only=True,
    risk_level="local_write",
    safe_default_input={},
    examples=({"input": {"config_key": "us", "account": "lx"}},),
)

PREPARE_CLOSE_ADVICE_INPUTS_TOOL = build_agent_tool(
    name="prepare_close_advice_inputs",
    description="Refresh local option positions context and required_data cache needed by close_advice.",
    requires=("runtime_config", "sqlite_data_config", "opend"),
    capabilities=("close_advice_prepare", "read_only"),
    side_effects=("writes_local_cache",),
    input_schema={
        "config_key": "us|hk",
        "config_path": "optional explicit config path",
        "data_config": "optional explicit data config path",
        "account": "optional account label",
        "broker": "optional broker name, preferred public field",
        "output_dir": "optional output root; defaults to output_shared/agent_tools",
        "ttl_sec": "optional int",
        "timeout_sec": "optional int",
    },
    handler=_prepare_close_advice_inputs_tool,
    read_only=True,
    risk_level="local_write",
    safe_default_input={},
    examples=({"input": {"config_key": "us"}},),
)

CLOSE_ADVICE_TOOL = build_agent_tool(
    name="close_advice",
    description="Build close-advice rows from cached option positions context and required_data quotes.",
    requires=("prepared_close_advice_inputs",),
    capabilities=("close_advice", "read_only"),
    side_effects=("writes_local_reports",),
    input_schema={
        "config_key": "us|hk",
        "config_path": "optional explicit config path",
        "output_dir": "optional output root; defaults to output_shared/agent_tools",
        "context_path": "optional explicit option_positions_context.json path",
        "required_data_root": "optional explicit required_data root",
    },
    handler=_close_advice_tool,
    read_only=True,
    risk_level="local_write",
    safe_default_input={},
    examples=({"input": {"config_key": "us"}},),
)

GET_CLOSE_ADVICE_TOOL = build_agent_tool(
    name="get_close_advice",
    description="One-shot close-advice entrypoint: prepare local inputs, then build close-advice output.",
    requires=("runtime_config", "sqlite_data_config", "opend"),
    capabilities=("close_advice", "read_only", "recommended_flow"),
    side_effects=("writes_local_cache", "writes_local_reports"),
    input_schema={
        "config_key": "us|hk",
        "config_path": "optional explicit config path",
        "data_config": "optional explicit data config path",
        "account": "optional account label",
        "broker": "optional broker name, preferred public field",
        "output_dir": "optional output root; defaults to output_shared/agent_tools",
        "ttl_sec": "optional int",
        "timeout_sec": "optional int",
    },
    handler=_get_close_advice_tool,
    read_only=True,
    risk_level="local_write",
    safe_default_input={},
    examples=({"input": {"config_key": "us"}},),
)

TOOLS: tuple[AgentTool, ...] = (
    SCAN_OPPORTUNITIES_TOOL,
    QUERY_CASH_HEADROOM_TOOL,
    GET_PORTFOLIO_CONTEXT_TOOL,
    PREPARE_CLOSE_ADVICE_INPUTS_TOOL,
    CLOSE_ADVICE_TOOL,
    GET_CLOSE_ADVICE_TOOL,
)


__all__ = [
    "CLOSE_ADVICE_TOOL",
    "GET_CLOSE_ADVICE_TOOL",
    "GET_PORTFOLIO_CONTEXT_TOOL",
    "PREPARE_CLOSE_ADVICE_INPUTS_TOOL",
    "QUERY_CASH_HEADROOM_TOOL",
    "SCAN_OPPORTUNITIES_TOOL",
    "TOOLS",
]
