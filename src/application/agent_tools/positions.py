from __future__ import annotations

from typing import Any

from src.application.agent_tools.operations_impl import option_positions_read_tool
from src.application.agent_tools.materialization_impl import monthly_income_report_tool
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

_OPTION_POSITIONS_ASSIGNED_STOCK_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "option_positions_read.assigned_stock_output.v1",
    "canonical_renderer": "assigned_stock_lifecycle",
    "source_label": "OM 本地 SQLite assigned_stock_events + trade_events",
    "guard_profile": "position_rows",
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


def _monthly_income_answer_policy(payload: dict[str, Any]) -> str:
    if payload.get("include_rows") is True:
        return "facts_then_analysis"
    return "default"


def _option_positions_action(payload: dict[str, Any]) -> str:
    value = payload.get("action")
    if isinstance(value, (list, tuple, set)):
        items = [item for item in value if item not in (None, "")]
        if len(items) == 1:
            value = items[0]
        elif not items:
            value = "list"
    return str(value or "list").strip().lower()


def _option_positions_answer_policy(payload: dict[str, Any]) -> str:
    action = _option_positions_action(payload)
    if action in {"list", "assigned-stock"}:
        return "facts_then_analysis"
    return "default"


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


_MONTHLY_INCOME_PLANNER_NOTES: tuple[str, ...] = (
    "Set include_rows=true for income analysis/review/performance, cashflow details, composition, source, 分析, 复盘, 表现, 明细, 组成, 构成, 来源, or 由什么组成.",
    "Use for monthly income source/breakdown/composition questions; not for current assigned-stock holding PnL, which belongs to option_positions_read action=assigned-stock.",
    "When include_rows=true, canonical factual rows are rendered by the system; synthesis should only add analysis.",
    "Data comes from OM local ledger, not broker realtime cash statements.",
    "If month is omitted, the tool reads all months currently available in the OM local ledger.",
    "If account is omitted, the tool reads all ledger accounts available for the selected broker/config.",
    "For combined/all-account return questions, require capability combined_account_return; the response must use combined_return_summary when available.",
)

_MONTHLY_INCOME_PLANNER_SEMANTICS: dict[str, Any] = {
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

_OPTION_POSITIONS_PLANNER_NOTES: tuple[str, ...] = (
    "Use for current option position list/detail requests, including 持仓明细, 持仓明晰, 持仓详情, 当前仓位, or current positions.",
    "For assigned stock / 被指派正股 / 指派正股 holding PnL, use action=assigned-stock with status=open by default and refresh_quotes=true when the user asks current 盈亏, spot, 浮盈亏, or 持仓盈亏; use synthesis so the Agent composer can answer from tool evidence.",
    "Use assigned-stock action for current assigned-stock holding PnL; not for monthly income source breakdown, realized income composition, or account performance summaries.",
    "For ordinary position list/detail requests, required_capabilities should be [] because option_positions_read itself provides option_positions/read_only.",
    "Use action=list for current lots; use action=history or action=inspect only when the user explicitly asks for event history, projection, repair, or ledger diagnostics.",
    "For action=list or action=assigned-stock, tool rows are evidence; deterministic renderers are fallback/provenance, not the default user-visible mode.",
)

_OPTION_POSITIONS_PLANNER_SEMANTICS: dict[str, Any] = {
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
    planner_notes=_MONTHLY_INCOME_PLANNER_NOTES,
    planner_semantics=_MONTHLY_INCOME_PLANNER_SEMANTICS,
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
    answer_policy="payload_dependent",
    answer_policy_resolver=_option_positions_answer_policy,
    output_contract={"schema_version": "option_positions_read.output", "payload_dependent": True},
    output_contract_resolver=_option_positions_output_contract,
    planner_notes=_OPTION_POSITIONS_PLANNER_NOTES,
    planner_semantics=_OPTION_POSITIONS_PLANNER_SEMANTICS,
)

TOOLS: tuple[AgentTool, ...] = (
    MONTHLY_INCOME_REPORT_TOOL,
    OPTION_POSITIONS_READ_TOOL,
)


__all__ = ["MONTHLY_INCOME_REPORT_TOOL", "OPTION_POSITIONS_READ_TOOL", "TOOLS"]
