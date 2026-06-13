from __future__ import annotations

from collections.abc import Iterable
import json
import re
import sqlite3
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tool_operations import option_positions_read_tool
from src.application.agent_tool_scan import monthly_income_report_tool
from src.application.agent_tools.base import AgentTool, AgentToolContext, build_agent_tool


MAX_QUERY_LIMIT = 200
DEFAULT_QUERY_LIMIT = 80
MAX_MATERIALIZED_ROWS = 5000
MAX_INPUT_SQL_CHARS = 4000
SQLITE_PROGRESS_OPCODE_LIMIT = 20000
ALLOWED_SQL_FUNCTIONS = {
    "abs",
    "avg",
    "coalesce",
    "count",
    "date",
    "datetime",
    "ifnull",
    "julianday",
    "length",
    "lower",
    "max",
    "min",
    "nullif",
    "round",
    "rtrim",
    "substr",
    "substring",
    "sum",
    "total",
    "trim",
    "upper",
}


VIEW_SPECS: dict[str, dict[str, Any]] = {
    "monthly_income_summary": {
        "description": "monthly income by month/account/currency from OM local ledger",
        "fields": (
            "month",
            "account",
            "currency",
            "net_cashflow_gross",
            "realized_pnl_gross",
            "premium_received_gross",
            "assignment_stock_net_cashflow_gross",
        ),
    },
    "monthly_income_return_summary": {
        "description": "account-level monthly return rows with CNY amounts and return rates",
        "fields": (
            "month",
            "account",
            "net_income_cny",
            "realized_pnl_cny",
            "premium_income_cny",
            "net_return_rate",
            "realized_return_rate",
            "premium_return_rate",
            "cash_secured_cny",
        ),
    },
    "monthly_income_combined_return_summary": {
        "description": "all-account monthly return rows aggregated by month",
        "fields": (
            "month",
            "account",
            "account_scope",
            "net_income_cny",
            "realized_pnl_cny",
            "premium_income_cny",
            "net_return_rate",
            "cash_secured_cny",
        ),
    },
    "monthly_income_cashflow_rows": {
        "description": "cashflow detail rows from trade events",
        "fields": (
            "month",
            "account",
            "symbol",
            "option_type",
            "trade_action",
            "contracts",
            "currency",
            "net_cashflow_gross",
            "strike",
            "expiration_ymd",
        ),
    },
    "monthly_income_realized_rows": {
        "description": "realized PnL detail rows by close/expiry/assignment/exercise",
        "fields": (
            "month",
            "account",
            "symbol",
            "option_type",
            "close_type",
            "contracts_closed",
            "currency",
            "realized_gross",
            "strike",
            "expiration_ymd",
        ),
    },
    "monthly_income_premium_rows": {
        "description": "premium attribution rows from sell-open option events",
        "fields": (
            "month",
            "account",
            "symbol",
            "option_type",
            "contracts",
            "currency",
            "premium_received_gross",
            "strike",
            "expiration_ymd",
        ),
    },
    "assigned_stock_lifecycle": {
        "description": "Sell Put assignment stock lots with stock PnL and lifecycle PnL",
        "fields": (
            "account",
            "symbol",
            "currency",
            "status",
            "shares_remaining",
            "shares_sold",
            "stock_cost_per_share",
            "spot",
            "assigned_stock_unrealized_pnl",
            "assigned_stock_realized_pnl",
            "option_premium_attribution",
            "assignment_lifecycle_pnl",
            "quote_status",
        ),
    },
    "assigned_stock_sales": {
        "description": "recorded assigned-stock sale events and realized stock PnL",
        "fields": (
            "account",
            "symbol",
            "currency",
            "shares",
            "sale_price",
            "assigned_stock_realized_pnl",
            "stock_lot_id",
        ),
    },
    "assigned_stock_review": {
        "description": "assigned-stock lifecycle review rows such as missing_quote or missing_stock_sale",
        "fields": ("account", "symbol", "currency", "status", "message", "stock_lot_id"),
    },
    "position_lots": {
        "description": "canonical option position lots from local SQLite projection",
        "fields": (
            "account",
            "symbol",
            "status",
            "side",
            "option_type",
            "strike",
            "expiration_ymd",
            "contracts_open",
            "currency",
            "cash_secured_amount",
        ),
    },
    "trade_events": {
        "description": "canonical option trade events from local SQLite trade_events",
        "fields": (
            "trade_time_beijing",
            "account",
            "symbol",
            "position_effect",
            "side",
            "option_type",
            "contracts",
            "price",
            "strike",
            "expiration_ymd",
            "currency",
        ),
    },
    "symbol_strategy_config": {
        "description": "monitored symbol strategy config flattened for analysis",
        "fields": (
            "symbol",
            "broker",
            "accounts",
            "use",
            "sell_put_enabled",
            "sell_put_max_strike",
            "sell_call_enabled",
            "sell_call_min_strike",
            "combo_yield_enabled",
        ),
    },
}


_ANALYSIS_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "analysis_query.output.v1",
    "canonical_renderer": "analysis_result",
    "source_label": "OM read-only analysis workspace",
    "guard_profile": "analysis_result",
    "primary_rows": "rows",
    "row_count_field": "row_count",
    "fact_fields": [
        "rows[].month",
        "rows[].account",
        "rows[].symbol",
        "rows[].currency",
        "rows[].status",
    ],
}


def _analysis_catalog_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    views_filter = _requested_views(payload.get("views") or payload.get("view"))
    config_path, _cfg = ctx.load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
    specs = {
        name: dict(spec)
        for name, spec in VIEW_SPECS.items()
        if not views_filter or name in views_filter
    }
    return {
        "schema_version": "analysis.catalog.v1",
        "source_label": "OM read-only analysis workspace",
        "views": specs,
        "sql_rules": {
            "allowed_statements": ["SELECT", "WITH"],
            "single_statement_only": True,
            "writes_allowed": False,
            "max_limit": MAX_QUERY_LIMIT,
            "whitelisted_views": sorted(VIEW_SPECS),
        },
        "examples": [
            {
                "question": "对比 lx 和 sy 的账户收益，有什么不同？",
                "sql": (
                    "select month, account, net_income_cny, net_return_rate "
                    "from monthly_income_return_summary "
                    "where account in ('lx','sy') order by month, account"
                ),
            },
            {
                "question": "指派正股当前浮盈亏按账户汇总",
                "sql": (
                    "select account, currency, sum(assigned_stock_unrealized_pnl) as unrealized_pnl "
                    "from assigned_stock_lifecycle group by account, currency"
                ),
            },
        ],
    }, [], {"config_path": ctx.mask_path(config_path)}


def _analysis_query_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    sql = _validated_sql(payload.get("sql") or payload.get("query"))
    limit = _bounded_limit(payload.get("limit"))
    warnings: list[str] = []
    views = _materialize_views(ctx, payload, warnings=warnings)
    rows, columns, views_used = _execute_select(sql, views, limit=limit)
    truncated = len(rows) > limit
    rows = rows[:limit]
    cell_refs = _cell_refs(rows)
    data = {
        "schema_version": "analysis.query.output.v1",
        "source_label": "OM read-only analysis workspace",
        "query": {"sql": sql, "limit": limit},
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "views_used": views_used,
        "available_views": sorted(VIEW_SPECS),
        "cell_refs": cell_refs,
        "fallback_text": _render_fallback_table(rows=rows, columns=columns, row_count=len(rows), truncated=truncated),
    }
    return data, warnings, {
        "source": "in_memory_sqlite",
        "views_used": views_used,
        "view_count": len(views),
    }


def _requested_views(value: Any) -> set[str]:
    raw_values: list[Any]
    if isinstance(value, str):
        raw_values = [item.strip() for item in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    elif value not in (None, ""):
        raise AgentToolError(
            code="INPUT_ERROR",
            message="analysis view filter must be a string or list of strings",
            details={"allowed_views": sorted(VIEW_SPECS)},
        )
    else:
        raw_values = []
    requested = {str(item or "").strip() for item in raw_values if str(item or "").strip()}
    unknown = sorted(requested - set(VIEW_SPECS))
    if unknown:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"unknown analysis view: {', '.join(unknown)}",
            details={"allowed_views": sorted(VIEW_SPECS), "unknown_views": unknown},
        )
    return requested


def _validated_sql(value: Any) -> str:
    sql = str(value or "").strip()
    if not sql:
        raise AgentToolError(code="INPUT_ERROR", message="analysis_query.sql is required")
    if len(sql) > MAX_INPUT_SQL_CHARS:
        raise AgentToolError(code="INPUT_ERROR", message=f"analysis_query.sql is too long; max {MAX_INPUT_SQL_CHARS} chars")
    if "\x00" in sql:
        raise AgentToolError(code="INPUT_ERROR", message="analysis_query.sql contains invalid characters")
    without_trailing = sql.rstrip().rstrip(";").strip()
    if ";" in without_trailing:
        raise AgentToolError(code="PERMISSION_DENIED", message="analysis_query accepts exactly one SQL statement")
    first = re.match(r"(?is)^\s*([a-z_]+)", without_trailing)
    keyword = first.group(1).lower() if first else ""
    if keyword not in {"select", "with"}:
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message="analysis_query only accepts SELECT or WITH queries",
            details={"first_keyword": keyword or None},
        )
    blocked = re.search(
        r"(?is)\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex)\b",
        without_trailing,
    )
    if blocked:
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message=f"analysis_query rejected non-read SQL keyword: {blocked.group(1).upper()}",
        )
    return without_trailing


def _bounded_limit(value: Any) -> int:
    try:
        limit = int(value)
    except Exception:
        limit = DEFAULT_QUERY_LIMIT
    return max(1, min(MAX_QUERY_LIMIT, limit))


def _materialize_views(ctx: AgentToolContext, payload: dict[str, Any], *, warnings: list[str]) -> dict[str, list[dict[str, Any]]]:
    monthly_data, monthly_warnings, _monthly_meta = monthly_income_report_tool(
        {
            "config_key": payload.get("config_key"),
            "config_path": payload.get("config_path"),
            "data_config": payload.get("data_config"),
            "month": payload.get("month"),
            "account": payload.get("account"),
            "include_rows": True,
        },
        load_runtime_config=ctx.load_runtime_config,
        resolve_public_data_config_path=ctx.resolve_public_data_config_path,
        normalize_broker=ctx.normalize_broker,
        resolve_option_positions_repo=ctx.resolve_option_positions_repo,
        build_monthly_income_report=ctx.build_monthly_income_report,
        get_exchange_rates=ctx.get_exchange_rates,
        repo_base=ctx.repo_base,
        mask_path=ctx.mask_path,
    )
    warnings.extend(str(item) for item in monthly_warnings if str(item).strip())

    position_data, position_warnings, _position_meta = option_positions_read_tool(
        {
            "config_key": payload.get("config_key"),
            "config_path": payload.get("config_path"),
            "data_config": payload.get("data_config"),
            "action": "list",
            "status": "all",
            "limit": MAX_MATERIALIZED_ROWS,
        },
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
        mask_path=lambda value: ctx.mask_path(value) or "...",
    )
    warnings.extend(str(item) for item in position_warnings if str(item).strip())

    event_data, event_warnings, _event_meta = option_positions_read_tool(
        {
            "config_key": payload.get("config_key"),
            "config_path": payload.get("config_path"),
            "data_config": payload.get("data_config"),
            "action": "events",
            "limit": MAX_MATERIALIZED_ROWS,
        },
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
        mask_path=lambda value: ctx.mask_path(value) or "...",
    )
    warnings.extend(str(item) for item in event_warnings if str(item).strip())

    config_path, cfg = ctx.load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
    symbol_rows = _symbol_strategy_rows(
        ctx.list_symbol_rows(
            cfg,
            resolve_watchlist_config=ctx.resolve_watchlist_config,
            normalize_accounts=ctx.normalize_accounts,
        )
    )
    if not config_path:
        warnings.append("runtime config path unavailable; symbol strategy config may be incomplete")

    return {
        "monthly_income_summary": _normalize_rows(monthly_data.get("summary")),
        "monthly_income_return_summary": _normalize_rows(monthly_data.get("return_summary")),
        "monthly_income_combined_return_summary": _normalize_rows(monthly_data.get("combined_return_summary")),
        "monthly_income_cashflow_rows": _normalize_rows(monthly_data.get("cashflow_rows")),
        "monthly_income_realized_rows": _normalize_rows(monthly_data.get("realized_rows")),
        "monthly_income_premium_rows": _normalize_rows(monthly_data.get("premium_rows")),
        "assigned_stock_lifecycle": _normalize_rows(monthly_data.get("assignment_lifecycle_rows")),
        "assigned_stock_sales": _normalize_rows(monthly_data.get("assigned_stock_sale_rows")),
        "assigned_stock_review": _normalize_rows(monthly_data.get("assigned_stock_review_rows")),
        "position_lots": _normalize_rows(position_data.get("rows")),
        "trade_events": _normalize_rows(event_data.get("rows")),
        "symbol_strategy_config": _normalize_rows(symbol_rows),
    }


def _symbol_strategy_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sell_put = row.get("sell_put") if isinstance(row.get("sell_put"), dict) else {}
        sell_call = row.get("sell_call") if isinstance(row.get("sell_call"), dict) else {}
        combo_yield = row.get("combo_yield") if isinstance(row.get("combo_yield"), dict) else {}
        out.append(
            {
                "symbol": row.get("symbol"),
                "broker": row.get("broker"),
                "accounts": row.get("accounts"),
                "use": row.get("use"),
                "sell_put_enabled": sell_put.get("enabled"),
                "sell_put_max_strike": sell_put.get("max_strike"),
                "sell_put_min_annualized": sell_put.get("min_annualized"),
                "sell_call_enabled": sell_call.get("enabled"),
                "sell_call_min_strike": sell_call.get("min_strike"),
                "sell_call_min_annualized": sell_call.get("min_annualized"),
                "combo_yield_enabled": combo_yield.get("enabled"),
                "sell_put_json": sell_put,
                "sell_call_json": sell_call,
                "combo_yield_json": combo_yield,
            }
        )
    return out


def _normalize_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_flatten_row(item) for item in value if isinstance(item, dict)]


def _flatten_row(row: dict[str, Any]) -> dict[str, Any]:
    return {_safe_column_name(key): _sqlite_value(value) for key, value in row.items() if _safe_column_name(key)}


def _safe_column_name(value: Any) -> str:
    name = re.sub(r"[^0-9A-Za-z_]", "_", str(value or "").strip())
    name = re.sub(r"_+", "_", name).strip("_")
    if not name:
        return ""
    if name[0].isdigit():
        name = f"c_{name}"
    return name[:80]


def _sqlite_value(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if value is None or isinstance(value, (int, float, str)):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _execute_select(
    sql: str,
    views: dict[str, list[dict[str, Any]]],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        for view_name, rows in views.items():
            _create_view_table(conn, view_name, rows)

        views_used: set[str] = set()

        def authorizer(action: int, arg1: str | None, arg2: str | None, db_name: str | None, trigger: str | None) -> int:
            del db_name, trigger
            if action == sqlite3.SQLITE_READ:
                table = str(arg1 or "")
                if table not in VIEW_SPECS:
                    return sqlite3.SQLITE_DENY
                views_used.add(table)
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION:
                function_name = str(arg2 or arg1 or "").lower()
                if function_name in ALLOWED_SQL_FUNCTIONS:
                    return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_DENY

        conn.set_authorizer(authorizer)
        conn.set_progress_handler(lambda: 1, SQLITE_PROGRESS_OPCODE_LIMIT)
        try:
            cursor = conn.execute(f"select * from ({sql}) limit {limit + 1}")
        except sqlite3.Error as exc:
            raise AgentToolError(
                code="INPUT_ERROR",
                message=f"analysis_query failed: {exc}",
                hint="Use analysis_catalog to inspect available view names and fields.",
            ) from exc
        columns = [str(item[0]) for item in (cursor.description or [])]
        result_rows = [{column: _public_cell_value(row[column]) for column in columns} for row in cursor.fetchall()]
        return result_rows, columns, sorted(views_used)
    finally:
        conn.close()


def _create_view_table(conn: sqlite3.Connection, name: str, rows: list[dict[str, Any]]) -> None:
    columns = _columns_for_rows(rows, VIEW_SPECS.get(name, {}).get("fields") or ())
    column_defs = ", ".join(f'"{column}" {_sqlite_type(rows, column)}' for column in columns)
    conn.execute(f'create table "{name}" ({column_defs})')
    if not rows:
        return
    placeholders = ", ".join("?" for _ in columns)
    quoted_columns = ", ".join(f'"{column}"' for column in columns)
    conn.executemany(
        f'insert into "{name}" ({quoted_columns}) values ({placeholders})',
        [[row.get(column) for column in columns] for row in rows],
    )


def _columns_for_rows(rows: list[dict[str, Any]], preferred: Iterable[Any]) -> list[str]:
    columns: list[str] = []
    for raw in preferred:
        column = _safe_column_name(raw)
        if column and column not in columns:
            columns.append(column)
    for row in rows:
        for raw in row:
            column = _safe_column_name(raw)
            if column and column not in columns:
                columns.append(column)
    return columns or ["_empty"]


def _sqlite_type(rows: list[dict[str, Any]], column: str) -> str:
    for row in rows:
        value = row.get(column)
        if isinstance(value, bool):
            return "INTEGER"
        if isinstance(value, int):
            return "INTEGER"
        if isinstance(value, float):
            return "REAL"
    return "TEXT"


def _public_cell_value(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    return value


def _cell_refs(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    refs: dict[str, dict[str, Any]] = {}
    for row_index, row in enumerate(rows, start=1):
        for column, value in row.items():
            refs[f"r{row_index}.{column}"] = {
                "row": row_index,
                "column": column,
                "value": value,
            }
    return refs


def _render_fallback_table(
    *,
    rows: list[dict[str, Any]],
    columns: list[str],
    row_count: int,
    truncated: bool,
) -> str:
    if not columns:
        return "分析查询完成：0 行。\n数据来源：OM read-only analysis workspace"
    display_rows = rows[:12]
    lines = [f"分析查询结果：{row_count} 行" + ("（已截断）" if truncated else "")]
    lines.append("| " + " | ".join(columns) + " |")
    lines.append("| " + " | ".join("---" for _ in columns) + " |")
    for row in display_rows:
        lines.append("| " + " | ".join(_format_cell(row.get(column)) for column in columns) + " |")
    if len(rows) > len(display_rows):
        lines.append(f"其余 {len(rows) - len(display_rows)} 行已省略。")
    lines.append("数据来源：OM read-only analysis workspace")
    return "\n".join(lines)


def _format_cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.6f}".rstrip("0").rstrip(".")
    return str(value).replace("|", "\\|")


ANALYSIS_CATALOG_TOOL = build_agent_tool(
    name="analysis_catalog",
    description="Return the read-only Tool OS analysis view catalog and SQL rules.",
    requires=("runtime_config",),
    capabilities=("analysis_catalog", "read_only"),
    input_schema={
        "config_key": "us|hk",
        "config_path": "optional explicit config path",
        "view": "optional single view name",
        "views": "optional list of view names",
    },
    handler=_analysis_catalog_tool,
    pure_read=True,
    safe_default_input={},
    examples=({"input": {"config_key": "us"}},),
    output_contract={
        "schema_version": "analysis_catalog.output.v1",
        "source_label": "OM read-only analysis workspace",
        "guard_profile": "analysis_catalog",
        "primary_rows": "views",
    },
)

ANALYSIS_QUERY_TOOL = build_agent_tool(
    name="analysis_query",
    description=(
        "Run a SELECT-only query against whitelisted in-memory OM analysis views for comparisons, "
        "rankings, trends, breakdowns, and other open-ended analytical questions."
    ),
    requires=("runtime_config", "sqlite_data_config"),
    capabilities=("analysis_query", "read_only", "analysis_workspace"),
    input_schema={
        "config_key": "us|hk",
        "config_path": "optional explicit config path",
        "data_config": "optional explicit data config path",
        "sql": "required SELECT or WITH query over analysis_catalog views",
        "query": "alias for sql",
        "limit": f"optional int, max {MAX_QUERY_LIMIT}",
        "account": "optional materialization account filter",
        "month": "optional materialization month filter",
    },
    handler=_analysis_query_tool,
    pure_read=True,
    safe_default_input={
        "sql": "select 1 as ok from monthly_income_return_summary limit 0",
    },
    examples=(
        {
            "input": {
                "config_key": "us",
                "sql": (
                    "select month, account, net_income_cny, net_return_rate "
                    "from monthly_income_return_summary order by month, account"
                ),
            }
        },
    ),
    output_contract=_ANALYSIS_OUTPUT_CONTRACT,
)

TOOLS: tuple[AgentTool, ...] = (
    ANALYSIS_CATALOG_TOOL,
    ANALYSIS_QUERY_TOOL,
)


__all__ = ["ANALYSIS_CATALOG_TOOL", "ANALYSIS_QUERY_TOOL", "TOOLS", "VIEW_SPECS"]
