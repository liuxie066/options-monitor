from __future__ import annotations

from typing import Any, Mapping

from src.application.agent_tools.operations_impl import option_positions_read_tool
from src.application.agent_tools.materialization_impl import option_performance_report_tool
from src.application.agent_tools.base import AgentTool, build_agent_tool
from src.application.positions.inspection import build_lot_event_history
from src.application.positions.inspection import inspect_projection_state
from src.application.ledger.api import list_position_rows
from src.application.ledger.api import open_performance_evidence_repository
from src.application.agent_tool_config import load_runtime_config
from src.application.agent_tool_contracts import mask_path
from domain.domain.option_position_identity import normalize_account
from src.application.agent_tools.runtime_helpers import normalize_broker
from src.application.positions.assigned_stock_quotes import refresh_assigned_stock_quote_snapshots as refresh_assigned_stock_quotes
from src.application.agent_tool_config import repo_base
from src.application.ledger.api import open_position_ledger_from_data_config as resolve_option_positions_repo
from src.application.agent_tools.runtime_helpers import resolve_public_data_config_path
from src.application.performance.service import build_option_period_performance


_OPTION_PERFORMANCE_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "option_performance_report.output.v1",
    "source_label": "OM 本地账本 + 显式估值/汇率证据",
    "primary_rows": "rows",
    "fact_fields": [
        "period.kind",
        "period.requested_start_date",
        "period.requested_end_date",
        "scope.accounts",
        "scope.brokers",
        "activity.premium_collected_gross",
        "activity.premium_paid_gross",
        "activity.assigned_stock_shares_opened",
        "activity.assigned_stock_shares_sold",
        "cash.option_trade_cash_gross",
        "cash.option_fee_cash",
        "cash.stock_settlement_cash_gross",
        "cash.stock_settlement_fee_cash",
        "cash.assigned_stock_sale_cash_gross",
        "cash.assigned_stock_sale_fee_cash",
        "cash.total_cash_change_net",
        "pnl.realized_gross",
        "pnl.realized_net",
        "pnl.option_realized_gross",
        "pnl.option_realized_net",
        "pnl.assigned_stock_realized_gross",
        "pnl.assigned_stock_realized_net",
        "pnl.period_total_gross",
        "pnl.period_total_net",
        "capital.period_realized_net_annualized_efficiency",
        "capital.period_total_net_annualized_efficiency",
        "assignment_lifecycle.period",
        "breakdowns.monthly",
        "breakdowns.accounts",
        "breakdowns.symbols",
    ],
    "missing_data_fields": [
        "quality.missing",
        "quality.warnings",
        "capital.coverage.missing",
        "assignment_lifecycle.review",
    ],
    "freshness_fields": [
        "period.status",
        "evidence.schema_state",
        "evidence.collection.status",
    ],
    "model_preview_fields": [
        "period",
        "scope",
        "activity",
        "cash",
        "pnl",
        "capital",
        "quality",
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
    "freshness_fields": ["freshness.kind"],
    "model_preview_fields": ["scope", "coverage", "evidence_scope", "rows", "bootstrap"],
}

_OPTION_POSITIONS_ASSIGNED_STOCK_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "option_positions_read.assigned_stock_output.v2",
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
        "rows[].fee_missing_components",
        "rows[].covered_call_allocation_status",
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
        "rows[].assigned_date",
        "rows[].inventory_days",
        "rows[].actual_fees",
        "rows[].estimated_fees",
        "rows[].fees_used",
        "rows[].fee_basis",
        "rows[].fee_missing_components",
        "rows[].fee_evidence",
        "rows[].covered_call_pnl",
        "rows[].covered_call_allocation_status",
        "rows[].put_capital_days",
        "rows[].stock_capital_days",
        "rows[].capital_days",
        "rows[].lifecycle_pnl_net",
        "rows[].annualized_capital_efficiency",
        "rows[].lifecycle_quality",
        "rows[].quote_status",
        "rows[].spot_time",
        "rows[].quote_source",
        "assigned_stock_review_rows[].status",
    ],
    "model_preview_fields": ["scope", "coverage", "freshness", "rows", "quote_refresh", "warnings"],
}


def _mask_path_str(value: Any) -> str:
    return mask_path(value) or "..."


def _option_performance_report_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return option_performance_report_tool(
        payload,
        load_runtime_config=load_runtime_config,
        resolve_public_data_config_path=resolve_public_data_config_path,
        normalize_broker=normalize_broker,
        resolve_option_positions_repo=resolve_option_positions_repo,
        open_performance_evidence_repository=open_performance_evidence_repository,
        build_option_period_performance=build_option_period_performance,
        repo_base=repo_base,
        mask_path=mask_path,
    )


def _option_positions_read_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return option_positions_read_tool(
        payload,
        load_runtime_config=load_runtime_config,
        resolve_public_data_config_path=resolve_public_data_config_path,
        normalize_broker=normalize_broker,
        normalize_account=normalize_account,
        refresh_assigned_stock_quotes=refresh_assigned_stock_quotes,
        resolve_option_positions_repo=resolve_option_positions_repo,
        list_position_rows=list_position_rows,
        build_lot_event_history=build_lot_event_history,
        inspect_projection_state=inspect_projection_state,
        repo_base=repo_base,
        mask_path=lambda value: _mask_path_str(value),
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


def _option_positions_output_contract(payload: dict[str, Any]) -> dict[str, Any] | None:
    action = _option_positions_action(payload)
    if action == "list":
        return _OPTION_POSITIONS_LIST_OUTPUT_CONTRACT
    if action == "assigned-stock":
        return _OPTION_POSITIONS_ASSIGNED_STOCK_OUTPUT_CONTRACT
    return None


_OPTION_PERFORMANCE_PERIOD_FIELDS = frozenset(
    {"as_of_date", "month", "year", "start_date", "end_date"}
)
_OPTION_PERFORMANCE_PERIOD_FIELDS_BY_KIND = {
    "mtd": frozenset({"as_of_date"}),
    "ytd": frozenset({"as_of_date"}),
    "month": frozenset({"month"}),
    "year": frozenset({"year"}),
    "range": frozenset({"start_date", "end_date"}),
}


def _normalize_option_performance_copilot_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    for name in ("account", "broker"):
        value = normalized.get(name)
        if name in normalized and isinstance(value, str) and not value.strip():
            raise ValueError(f"{name} must be non-empty when provided")
    period_value = normalized.get("period")
    if not isinstance(period_value, str) or period_value not in _OPTION_PERFORMANCE_PERIOD_FIELDS_BY_KIND:
        return normalized
    relevant = _OPTION_PERFORMANCE_PERIOD_FIELDS_BY_KIND[period_value]
    for name in _OPTION_PERFORMANCE_PERIOD_FIELDS - relevant:
        normalized.pop(name, None)
    for name in relevant:
        value = normalized.get(name)
        if name in normalized and isinstance(value, str) and not value.strip():
            raise ValueError(f"{name} must be non-empty when provided")
    return normalized


OPTION_PERFORMANCE_REPORT_TOOL = build_agent_tool(
    name="option_performance_report",
    description=(
        "Primary read-only option performance report. Separates premium activity, cash movement, realized PnL, "
        "period total PnL, assigned-stock lifecycle, and capital efficiency. Supports MTD, YTD, natural month, "
        "natural year, and explicit date ranges. Omit account or broker to aggregate all matching ledger facts; "
        "native-currency amounts remain authoritative and CNY is null when FX evidence is incomplete."
    ),
    requires=("runtime_config", "sqlite_data_config"),
    capabilities=("option_performance", "income_report", "option_positions", "read_only"),
    input_schema={
        "config_key": {"type": "string", "enum": ["us", "hk"], "description": "Market config"},
        "config_path": "optional explicit config path",
        "data_config": "optional explicit data config path",
        "account": "optional account label; omitted aggregates all accounts",
        "broker": "optional broker filter; omitted aggregates all brokers",
        "period": {
            "type": "string",
            "enum": ["mtd", "ytd", "month", "year", "range"],
        },
        "as_of_date": {"type": ["string", "null"], "format": "date"},
        "month": {"type": ["string", "null"], "pattern": r"^\d{4}-(0[1-9]|1[0-2])$"},
        "year": {"type": ["integer", "string", "null"]},
        "start_date": {"type": ["string", "null"], "format": "date"},
        "end_date": {"type": ["string", "null"], "format": "date"},
        "include_rows": {"type": "boolean"},
        "refresh_quotes": {"type": "boolean"},
    },
    handler=_option_performance_report_tool,
    pure_read=True,
    safe_default_input={
        "config_key": "us",
        "period": "mtd",
        "include_rows": False,
        "refresh_quotes": True,
    },
    examples=(
        {"input": {"period": "ytd", "as_of_date": "2026-07-17"}},
        {"input": {"period": "month", "month": "2026-06", "include_rows": True}},
    ),
    output_contract=_OPTION_PERFORMANCE_OUTPUT_CONTRACT,
    copilot_input_fields=(
        "config_key",
        "account",
        "broker",
        "period",
        "as_of_date",
        "month",
        "year",
        "start_date",
        "end_date",
        "include_rows",
        "refresh_quotes",
    ),
    copilot_input_schema={
        "type": "object",
        "properties": {
            "config_key": {"type": "string", "enum": ["us", "hk"]},
            "account": {"type": "string"},
            "broker": {"type": "string"},
            "period": {"type": "string", "enum": ["mtd", "ytd", "month", "year", "range"]},
            "as_of_date": {"type": "string", "format": "date"},
            "month": {"type": "string", "pattern": r"^\d{4}-(0[1-9]|1[0-2])$"},
            "year": {"type": ["integer", "string"]},
            "start_date": {"type": "string", "format": "date"},
            "end_date": {"type": "string", "format": "date"},
            "include_rows": {"type": "boolean"},
            "refresh_quotes": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
    copilot_input_normalizer=_normalize_option_performance_copilot_input,
)

OPTION_POSITIONS_READ_TOOL = build_agent_tool(
    name="option_positions_read",
    description=(
        "Read local option position lots, trade events, lot history, assigned-stock lots, or projection "
        "inspection state. For current exposure use action=list and status=open; preserve account and currency. "
        "An expired_position_marked_open warning identifies a local ledger-state inconsistency only; it does not "
        "prove broker settlement, assignment, liquidation, or a pending order. cash_secured_amount is assignment "
        "collateral, not profit, available cash, or loss. action=assigned-stock can add read-only quote evidence "
        "for current stock P&L; other actions do not provide market-price P&L evidence."
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
        "quote_snapshots": "assigned-stock optional quote snapshot list/dict; supplying it disables implicit realtime refresh",
        "refresh_quotes": "assigned-stock optional bool; current queries refresh realtime OpenD spot by default, false disables it, true with historical as_of_ms is skipped",
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
    copilot_input_fields=(
        "config_key", "action", "broker", "account", "status", "query", "limit",
        "exp_within_days", "expiration_month", "expiration_exact", "expiration_before",
        "expiration_after", "record_id", "symbol", "option_type", "side", "strike",
        "exp", "stock_lot_id", "refresh_quotes", "as_of_ms",
    ),
    copilot_input_schema={
        "type": "object",
        "properties": {
            "config_key": {"type": "string", "enum": ["us", "hk"], "description": "Market config"},
            "action": {
                "type": "string",
                "enum": ["list", "events", "history", "inspect", "assigned-stock"],
                "description": "Evidence surface to read",
            },
            "broker": {"type": "string", "description": "Optional broker name"},
            "account": {"type": "string", "description": "Optional account label"},
            "status": {
                "type": "string",
                "enum": ["open", "close", "all"],
                "description": "Position status filter for action=list",
            },
            "query": {"type": "object", "description": "Structured list filter"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            "exp_within_days": {"type": "integer", "minimum": 0},
            "expiration_month": {"type": "string", "pattern": r"^\d{4}-(0[1-9]|1[0-2])$"},
            "expiration_exact": {"type": "string", "format": "date"},
            "expiration_before": {"type": "string", "format": "date"},
            "expiration_after": {"type": "string", "format": "date"},
            "record_id": {"type": "string"},
            "symbol": {"type": "string"},
            "option_type": {"type": "string", "enum": ["put", "call"]},
            "side": {"type": "string", "enum": ["short", "long"]},
            "strike": {"type": "number"},
            "exp": {"type": "string", "format": "date"},
            "stock_lot_id": {"type": "string"},
            "refresh_quotes": {"type": "boolean"},
            "as_of_ms": {"type": "integer"},
        },
        "additionalProperties": False,
    },
)

TOOLS: tuple[AgentTool, ...] = (
    OPTION_PERFORMANCE_REPORT_TOOL,
    OPTION_POSITIONS_READ_TOOL,
)


__all__ = [
    "OPTION_PERFORMANCE_REPORT_TOOL",
    "OPTION_POSITIONS_READ_TOOL",
    "TOOLS",
]
