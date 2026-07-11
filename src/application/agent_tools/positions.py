from __future__ import annotations

from typing import Any

from src.application.agent_tools.operations_impl import option_positions_read_tool
from src.application.agent_tools.materialization_impl import monthly_income_report_tool
from src.application.agent_tools.base import AgentTool, AgentToolContext, build_agent_tool


_MONTHLY_INCOME_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "monthly_income_report.output.v1",
    "source_label": "OM 本地账本",
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
        "diagnostics[].income_record_status",
        "diagnostics[].income_amount_status",
        "diagnostics[].position_lot_snapshots_count",
    ],
    "missing_data_fields": [
        "diagnostics[].income_amount_status",
        "diagnostics[].missing_fields",
        "diagnostics[].warnings",
    ],
}

_MONTHLY_INCOME_DETAIL_OUTPUT_CONTRACT: dict[str, Any] = {
    **_MONTHLY_INCOME_OUTPUT_CONTRACT,
    "schema_version": "monthly_income_report.detail_output.v1",
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
        "assignment_lifecycle_rows[].stock_lot_id",
        "assignment_lifecycle_rows[].stock_cost_per_share",
        "assignment_lifecycle_rows[].assigned_stock_unrealized_pnl",
        "assignment_lifecycle_rows[].assigned_stock_realized_pnl",
        "assignment_lifecycle_rows[].option_premium_attribution",
        "assignment_lifecycle_rows[].assignment_lifecycle_pnl",
        "assignment_lifecycle_rows[].quote_status",
        "assigned_stock_review_rows[].status",
        "realized_rows[].contracts_closed",
        "realized_rows[].realized_gross",
        "premium_rows[].contracts",
        "premium_rows[].premium_received_gross",
    ],
}

_OPTION_POSITIONS_LIST_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "option_positions_read.list_output.v1",
    "source_label": "OM 本地 SQLite position_lots",
    "primary_rows": "rows",
    "row_count_field": "row_count",
    "stable_order": "expiration_asc_missing_last",
    "fact_fields": [
        "evidence_scope.ledger_positions",
        "evidence_scope.broker_settlement",
        "evidence_scope.market_price",
        "evidence_scope.margin_state",
        "rows[].account",
        "rows[].symbol",
        "rows[].side",
        "rows[].option_type",
        "rows[].strike",
        "rows[].expiration_ymd",
        "rows[].expiration_state",
        "rows[].state_warning",
        "rows[].contracts_open",
        "rows[].status",
        "rows[].cash_secured_amount_role",
    ],
    "missing_data_fields": [
        "evidence_scope.broker_settlement",
        "evidence_scope.market_price",
        "evidence_scope.margin_state",
    ],
}

_OPTION_POSITIONS_ASSIGNED_STOCK_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "option_positions_read.assigned_stock_output.v1",
    "source_label": "OM 本地 SQLite assigned_stock_events + trade_events",
    "primary_rows": "rows",
    "row_count_field": "row_count",
    "freshness_fields": [
        "rows[].quote_status",
        "quote_refresh.status",
        "quote_refresh.quote_source",
    ],
    "missing_data_fields": [
        "quote_refresh.missing_symbols",
        "rows[].quote_status",
    ],
    "fact_fields": [
        "rows[].stock_lot_id",
        "rows[].account",
        "rows[].symbol",
        "rows[].currency",
        "rows[].status",
        "rows[].shares_remaining",
        "rows[].stock_cost_per_share",
        "rows[].remaining_stock_cost_basis",
        "rows[].remaining_market_value",
        "rows[].spot",
        "rows[].assigned_stock_unrealized_pnl",
        "rows[].assigned_stock_realized_pnl",
        "rows[].option_premium_attribution",
        "rows[].assignment_lifecycle_pnl",
        "rows[].quote_status",
        "assigned_stock_review_rows[].status",
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
        refresh_assigned_stock_quotes=ctx.refresh_assigned_stock_quotes,
        resolve_option_positions_repo=ctx.resolve_option_positions_repo,
        list_position_rows=ctx.list_position_rows,
        build_lot_event_history=ctx.build_lot_event_history,
        inspect_projection_state=ctx.inspect_projection_state,
        repo_base=ctx.repo_base,
        mask_path=lambda value: _mask_path_str(ctx, value),
    )


def _option_positions_action(payload: dict[str, Any]) -> str:
    value = payload.get("action")
    if isinstance(value, (list, tuple, set)):
        items = [item for item in value if item not in (None, "")]
        if len(items) == 1:
            value = items[0]
        elif not items:
            value = "list"
    return str(value or "list").strip().lower()


def _monthly_income_output_contract(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("include_rows") is True:
        return _MONTHLY_INCOME_DETAIL_OUTPUT_CONTRACT
    return _MONTHLY_INCOME_OUTPUT_CONTRACT


def _option_positions_output_contract(payload: dict[str, Any]) -> dict[str, Any] | None:
    action = _option_positions_action(payload)
    if action == "list":
        return _OPTION_POSITIONS_LIST_OUTPUT_CONTRACT
    if action == "assigned-stock":
        return _OPTION_POSITIONS_ASSIGNED_STOCK_OUTPUT_CONTRACT
    return None


MONTHLY_INCOME_REPORT_TOOL = build_agent_tool(
    name="monthly_income_report",
    description=(
        "Return monthly option income statistics by cashflow, realized PnL, and open-basis attribution "
        "from the local ledger without running market data or notification workflows. Use YYYY-MM for "
        "a requested month; omit account to aggregate every available account while preserving currency. "
        "When diagnostics.income_amount_status is not_reported, the ledger has not reported a numeric "
        "income amount for that scope; empty rows must not be interpreted as zero income. "
        "diagnostics.position_lot_snapshots_count counts ledger lot snapshots only; it does not "
        "prove activity, profit, loss, or settlement in the requested month."
    ),
    requires=("runtime_config", "sqlite_data_config"),
    capabilities=("income_report", "option_positions", "read_only"),
    input_schema={
        "config_key": {"type": "string", "enum": ["us", "hk"], "description": "Market config"},
        "config_path": "optional explicit config path",
        "data_config": "optional explicit data config path",
        "account": "optional account label",
        "broker": "optional broker name, preferred public field",
        "month": {
            "type": "string",
            "pattern": r"^\d{4}-(0[1-9]|1[0-2])$",
            "description": "Optional calendar month in YYYY-MM format",
        },
        "include_rows": {
            "type": "boolean",
            "description": "Include cashflow, realized, open-basis, and premium detail rows",
        },
    },
    handler=_monthly_income_report_tool,
    pure_read=True,
    safe_default_input={},
    examples=({"input": {"config_key": "us", "account": "lx", "month": "2026-04"}},),
    output_contract={"schema_version": "monthly_income_report.output", "payload_dependent": True},
    output_contract_resolver=_monthly_income_output_contract,
)

OPTION_POSITIONS_READ_TOOL = build_agent_tool(
    name="option_positions_read",
    description=(
        "Read local option position lots, trade events, lot history, assigned-stock lots, or projection "
        "inspection state. For current exposure use action=list and status=open; preserve account and currency. "
        "An expired_position_marked_open warning identifies a local ledger-state inconsistency only; it does not "
        "prove broker settlement, assignment, liquidation, or a pending order. cash_secured_amount is assignment "
        "collateral, not profit, available cash, or loss; this tool does not provide market-price P&L evidence."
    ),
    requires=("runtime_config", "sqlite_data_config"),
    capabilities=("option_positions", "read_only", "ledger_diagnostics"),
    input_schema={
        "config_key": {"type": "string", "enum": ["us", "hk"], "description": "Market config"},
        "config_path": "optional explicit config path",
        "data_config": "optional explicit data config path",
        "action": {
            "type": ["string", "array"],
            "items": {"type": "string"},
            "description": "list|events|history|inspect|assigned-stock; legacy callers may pass a single-item list",
        },
        "broker": "optional broker name, preferred public field",
        "account": "optional account label",
        "status": {
            "type": ["string", "array"],
            "items": {"type": "string"},
            "description": "list-only open|close|all; legacy callers may pass a single-item list",
        },
        "query": "list-only structured PositionQuery: account/status/symbol/option_type/side/strike/expiration/limit",
        "limit": {"type": "integer", "minimum": 1, "maximum": 500, "description": "Maximum rows"},
        "exp_within_days": {"type": "integer", "minimum": 0, "description": "List-only expiration horizon"},
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
        "stock_lot_id": "assigned-stock selector",
        "quote_snapshots": "assigned-stock optional quote snapshot list/dict",
        "refresh_quotes": "assigned-stock optional bool; explicitly fetch realtime OpenD spot for open assigned-stock lots",
        "opend_host": "assigned-stock refresh_quotes optional OpenD host override",
        "opend_port": "assigned-stock refresh_quotes optional OpenD port override",
        "as_of_ms": "assigned-stock optional as-of timestamp for quote snapshot selection",
    },
    handler=_option_positions_read_tool,
    pure_read=True,
    safe_default_input={"action": "list"},
    examples=(
        {"input": {"config_key": "us", "action": "list", "query": {"account": "lx", "status": "open"}}},
        {"input": {"config_key": "us", "action": "history", "record_id": "rec_xxx"}},
    ),
    output_contract={"schema_version": "option_positions_read.output", "payload_dependent": True},
    output_contract_resolver=_option_positions_output_contract,
)

TOOLS: tuple[AgentTool, ...] = (
    MONTHLY_INCOME_REPORT_TOOL,
    OPTION_POSITIONS_READ_TOOL,
)


__all__ = ["MONTHLY_INCOME_REPORT_TOOL", "OPTION_POSITIONS_READ_TOOL", "TOOLS"]
