from __future__ import annotations

from typing import Any

from src.application.agent_tool_operations import option_positions_read_tool
from src.application.agent_tool_scan import monthly_income_report_tool
from src.application.agent_tools.base import AgentTool, AgentToolContext, build_agent_tool


_MONTHLY_INCOME_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "monthly_income_report.output.v1",
    "canonical_renderer": "monthly_income",
    "source_label": "OM 本地账本",
    "guard_profile": "income_summary",
    "primary_rows": "summary",
    "row_count_field": "row_count",
    "fact_fields": [
        "summary[].month",
        "summary[].account",
        "summary[].currency",
        "summary[].net_cashflow_gross",
        "return_summary[].month",
        "return_summary[].account",
        "return_summary[].net_income_cny",
        "return_summary[].net_return_rate",
    ],
}

_MONTHLY_INCOME_DETAIL_OUTPUT_CONTRACT: dict[str, Any] = {
    **_MONTHLY_INCOME_OUTPUT_CONTRACT,
    "schema_version": "monthly_income_report.detail_output.v1",
    "guard_profile": "income_rows",
    "primary_rows": "cashflow_rows",
    "fact_fields": [
        *_MONTHLY_INCOME_OUTPUT_CONTRACT["fact_fields"],
        "cashflow_rows[].month",
        "cashflow_rows[].account",
        "cashflow_rows[].symbol",
        "cashflow_rows[].option_type",
        "cashflow_rows[].contracts",
        "cashflow_rows[].currency",
        "cashflow_rows[].net_cashflow_gross",
        "realized_rows[].contracts_closed",
        "realized_rows[].realized_gross",
        "premium_rows[].contracts",
        "premium_rows[].premium_received_gross",
    ],
}

_OPTION_POSITIONS_LIST_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "option_positions_read.list_output.v1",
    "canonical_renderer": "position_rows",
    "source_label": "OM 本地 SQLite position_lots",
    "guard_profile": "position_rows",
    "primary_rows": "rows",
    "row_count_field": "row_count",
    "stable_order": "expiration_asc_missing_last",
    "fact_fields": [
        "rows[].account",
        "rows[].symbol",
        "rows[].side",
        "rows[].option_type",
        "rows[].strike",
        "rows[].expiration_ymd",
        "rows[].contracts_open",
        "rows[].status",
    ],
}


def _mask_path_str(ctx: AgentToolContext, value: Any) -> str:
    return ctx.mask_path(value) or "..."


def _monthly_income_report_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return monthly_income_report_tool(
        payload,
        load_runtime_config=ctx.load_runtime_config,
        resolve_public_data_config_path=ctx.resolve_public_data_config_path,
        normalize_broker=ctx.normalize_broker,
        resolve_option_positions_repo=ctx.resolve_option_positions_repo,
        build_monthly_income_report=ctx.build_monthly_income_report,
        get_exchange_rates=ctx.get_exchange_rates,
        repo_base=ctx.repo_base,
        mask_path=ctx.mask_path,
    )


def _option_positions_read_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return option_positions_read_tool(
        payload,
        load_runtime_config=ctx.load_runtime_config,
        resolve_public_data_config_path=ctx.resolve_public_data_config_path,
        normalize_broker=ctx.normalize_broker,
        normalize_account=ctx.normalize_account,
        resolve_option_positions_repo=ctx.resolve_option_positions_repo,
        list_position_rows=ctx.list_position_rows,
        build_lot_event_history=ctx.build_lot_event_history,
        inspect_projection_state=ctx.inspect_projection_state,
        repo_base=ctx.repo_base,
        mask_path=lambda value: _mask_path_str(ctx, value),
    )


def _monthly_income_answer_policy(payload: dict[str, Any]) -> str:
    if payload.get("include_rows") is True:
        return "facts_then_analysis"
    return "default"


def _option_positions_answer_policy(payload: dict[str, Any]) -> str:
    action = str(payload.get("action") or "list").strip().lower()
    if action == "list":
        return "facts_then_analysis"
    return "default"


def _monthly_income_output_contract(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("include_rows") is True:
        return _MONTHLY_INCOME_DETAIL_OUTPUT_CONTRACT
    return _MONTHLY_INCOME_OUTPUT_CONTRACT


def _option_positions_output_contract(payload: dict[str, Any]) -> dict[str, Any] | None:
    action = str(payload.get("action") or "list").strip().lower()
    if action == "list":
        return _OPTION_POSITIONS_LIST_OUTPUT_CONTRACT
    return None


MONTHLY_INCOME_REPORT_TOOL = build_agent_tool(
    name="monthly_income_report",
    description=(
        "Return monthly option income statistics by cashflow, realized PnL, and open-basis attribution "
        "from local option positions without running market data or notification workflows."
    ),
    requires=("runtime_config", "sqlite_data_config"),
    capabilities=("income_report", "option_positions", "read_only"),
    input_schema={
        "config_key": "us|hk",
        "config_path": "optional explicit config path",
        "data_config": "optional explicit data config path",
        "account": "optional account label",
        "broker": "optional broker name, preferred public field",
        "month": "optional YYYY-MM filter",
        "include_rows": "optional bool; include cashflow, realized, open-basis, and premium detail rows",
    },
    handler=_monthly_income_report_tool,
    pure_read=True,
    safe_default_input={},
    examples=({"input": {"config_key": "us", "account": "lx", "month": "2026-04"}},),
    answer_policy="payload_dependent",
    answer_policy_resolver=_monthly_income_answer_policy,
    output_contract={"schema_version": "monthly_income_report.output", "payload_dependent": True},
    output_contract_resolver=_monthly_income_output_contract,
)

OPTION_POSITIONS_READ_TOOL = build_agent_tool(
    name="option_positions_read",
    description="Read local option position lots, trade events, lot history, or projection inspection state.",
    requires=("runtime_config", "sqlite_data_config"),
    capabilities=("option_positions", "read_only", "ledger_diagnostics"),
    input_schema={
        "config_key": "us|hk",
        "config_path": "optional explicit config path",
        "data_config": "optional explicit data config path",
        "action": "list|events|history|inspect",
        "broker": "optional broker name, preferred public field",
        "account": "optional account label",
        "status": "list-only open|close|all",
        "query": "list-only structured PositionQuery: account/status/symbol/option_type/side/strike/expiration/limit",
        "limit": "optional int, max 500",
        "exp_within_days": "list-only optional int",
        "expiration_month": "list-only optional YYYY-MM",
        "expiration_exact": "list-only optional YYYY-MM-DD",
        "expiration_before": "list-only optional YYYY-MM-DD inclusive",
        "expiration_after": "list-only optional YYYY-MM-DD inclusive",
        "record_id": "history/inspect selector",
        "symbol": "list/events/inspect selector",
        "option_type": "list/events/inspect put|call selector",
        "side": "list-only short|long selector",
        "strike": "list/events/inspect numeric selector",
        "exp": "events/inspect YYYY-MM-DD selector",
    },
    handler=_option_positions_read_tool,
    pure_read=True,
    safe_default_input={"action": "list"},
    examples=(
        {"input": {"config_key": "us", "action": "list", "query": {"account": "lx", "status": "open"}}},
        {"input": {"config_key": "us", "action": "history", "record_id": "rec_xxx"}},
    ),
    answer_policy="payload_dependent",
    answer_policy_resolver=_option_positions_answer_policy,
    output_contract={"schema_version": "option_positions_read.output", "payload_dependent": True},
    output_contract_resolver=_option_positions_output_contract,
)

TOOLS: tuple[AgentTool, ...] = (
    MONTHLY_INCOME_REPORT_TOOL,
    OPTION_POSITIONS_READ_TOOL,
)


__all__ = ["MONTHLY_INCOME_REPORT_TOOL", "OPTION_POSITIONS_READ_TOOL", "TOOLS"]
