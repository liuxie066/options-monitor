from __future__ import annotations

from typing import Any

from src.application.agent_tools.materialization_impl import (
    get_portfolio_context_tool,
    query_cash_headroom_tool,
    scan_opportunities_tool,
    scan_summary_rows,
)
from src.application.agent_tools.base import AgentTool, build_agent_tool
from src.application.agent_tools.runtime_helpers import as_float
from src.application.pipeline_context import load_portfolio_context
from src.application.agent_tool_config import load_runtime_config
from src.application.config_loader import load_config as load_runtime_pipeline_config
from src.application.agent_tool_contracts import mask_path
from src.application.agent_tools.runtime_helpers import normalize_broker
from src.application.cash_headroom_query import query_sell_put_cash
from src.application.agent_tool_config import repo_base
from src.application.agent_tools.runtime_helpers import resolve_data_config_ref
from src.application.agent_tool_config import resolve_output_root
from src.application.agent_tools.runtime_helpers import resolve_public_data_config_path
from src.application.pipeline_watchlist import run_watchlist_pipeline_default


def _mask_path_str(value: Any) -> str:
    return mask_path(value) or "..."


def _query_cash_headroom_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return query_cash_headroom_tool(
        payload,
        load_runtime_config=load_runtime_config,
        resolve_public_data_config_path=resolve_public_data_config_path,
        normalize_broker=normalize_broker,
        resolve_output_root=resolve_output_root,
        query_sell_put_cash=query_sell_put_cash,
        repo_base=repo_base,
        mask_path=lambda value: _mask_path_str(value),
    )


def _get_portfolio_context_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return get_portfolio_context_tool(
        payload,
        load_runtime_config=load_runtime_config,
        resolve_public_data_config_path=resolve_public_data_config_path,
        normalize_broker=normalize_broker,
        resolve_output_root=resolve_output_root,
        load_portfolio_context=load_portfolio_context,
        repo_base=repo_base,
        mask_path=mask_path,
    )


def _scan_opportunities_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return scan_opportunities_tool(
        payload,
        load_runtime_config=load_runtime_config,
        resolve_data_config_ref=resolve_data_config_ref,
        resolve_output_root=resolve_output_root,
        repo_base=repo_base,
        load_config=load_runtime_pipeline_config,
        run_watchlist_pipeline_default=run_watchlist_pipeline_default,
        scan_summary_rows_fn=lambda rows: scan_summary_rows(rows, as_float=as_float),
    )


_CASH_HEADROOM_OUTPUT_CONTRACT = {
    "schema_version": "query_cash_headroom.output.v1",
    "evidence_type": "point",
    "bounded_projection": "contract_fields",
    "coverage": "point",
    "freshness": "source_declared",
    "pagination": {"mode": "none"},
    "source_label": "OM cash headroom query",
    "result_shape": "scalar",
    "fact_fields": [
        "account",
        "cash_secured_used_cny",
        "cash_available_total_cny",
        "cash_free_total_cny",
        "cash_secured_total_by_ccy",
        "cash_secured_usage_reliable",
        "cash_available_by_currency",
        "cash_snapshot",
        "cash_source_observed_at",
        "cash_source_observation_status",
        "cash_balance_reliable",
        "cash_balance_unavailable_by_row",
        "cny_conversion_complete",
        "cny_conversion_missing_rates",
    ],
    "model_value_fields": [
        "account",
        "cash_secured_used_cny",
        "cash_available_total_cny",
        "cash_free_total_cny",
        "cash_secured_total_by_ccy",
        "cash_secured_usage_reliable",
        "cash_available_by_currency",
        "cash_snapshot",
        "cash_source_observed_at",
        "cash_source_observation_status",
        "cash_balance_reliable",
        "cash_balance_unavailable_by_row",
        "exchange_rates",
        "cny_conversion_complete",
        "cny_conversion_missing_rates",
        "cash_secured_unavailable_by_symbol",
        "cash_secured_unavailable_reason",
    ],
    "missing_data_fields": [
        "cash_secured_unavailable_by_symbol",
        "cash_secured_unavailable_reason",
        "cash_snapshot",
        "cash_source_observed_at",
        "cash_source_observation_status",
        "cash_balance_reliable",
        "cash_balance_unavailable_by_row",
        "cny_conversion_missing_rates",
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
    catalog_summary="读取账户现金头寸与可用空间。",
    description="Return sell-put cash usage and available/free cash summary.",
    requires=("runtime_config", "sqlite_data_config", "opend"),
    capabilities=("cash_query", "read_only"),
    input_schema={
        "config_key": "us|hk",
        "config_path": "optional explicit config path",
        "data_config": "optional explicit data config path",
        "account": {
            "type": "string",
            "required": True,
            "description": "Account label required by the portfolio cash source, for example lx or sy",
        },
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
    bot_input_fields=("config_key", "account", "broker", "top", "no_exchange_rates"),
)

GET_PORTFOLIO_CONTEXT_TOOL = build_agent_tool(
    name="get_portfolio_context",
    description="Read Futu portfolio context using the shared account cash policy.",
    requires=("runtime_config", "opend"),
    capabilities=("portfolio_context", "read_only"),
    side_effects=("writes_local_cache",),
    input_schema={
        "config_key": "us|hk",
        "config_path": "optional explicit config path",
        "data_config": "optional explicit data config path",
        "account": "optional account label",
        "broker": "optional broker name, preferred public field",
        "timeout_sec": "optional int",
    },
    handler=_get_portfolio_context_tool,
    read_only=True,
    risk_level="local_write",
    safe_default_input={},
    examples=({"input": {"config_key": "us", "account": "lx"}},),
)


TOOLS: tuple[AgentTool, ...] = (
    SCAN_OPPORTUNITIES_TOOL,
    QUERY_CASH_HEADROOM_TOOL,
    GET_PORTFOLIO_CONTEXT_TOOL,
)


__all__ = [
    "GET_PORTFOLIO_CONTEXT_TOOL",
    "QUERY_CASH_HEADROOM_TOOL",
    "SCAN_OPPORTUNITIES_TOOL",
    "TOOLS",
]
