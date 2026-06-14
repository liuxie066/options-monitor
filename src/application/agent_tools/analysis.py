from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tool_operations import option_positions_read_tool
from src.application.agent_tool_scan import monthly_income_report_tool
from src.application.agent_tools.base import AgentTool, AgentToolContext, build_agent_tool
from src.application.candidate_filter_trace import infer_trace_scope_from_path, read_candidate_filter_trace


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


RETURN_SUMMARY_FIELDS: tuple[str, ...] = (
    "month",
    "account",
    "cash_secured_cny",
    "cash_secured_by_ccy",
    "net_income_cny",
    "net_income_by_ccy",
    "realized_pnl_cny",
    "realized_pnl_by_ccy",
    "premium_income_cny",
    "premium_income_by_ccy",
    "net_return_rate",
    "realized_return_rate",
    "premium_return_rate",
    "annualized_net_return_rate",
    "annualized_basis_days",
    "return_basis",
    "calculation_method",
)


COMBINED_RETURN_SUMMARY_FIELDS: tuple[str, ...] = (
    "month",
    "account",
    "account_scope",
    "accounts",
    "cash_secured_cny",
    "cash_secured_by_ccy",
    "net_income_cny",
    "net_income_by_ccy",
    "realized_pnl_cny",
    "realized_pnl_by_ccy",
    "premium_income_cny",
    "premium_income_by_ccy",
    "net_return_rate",
    "realized_return_rate",
    "premium_return_rate",
    "annualized_net_return_rate",
    "annualized_basis_days",
    "return_basis",
    "calculation_method",
)


ACCOUNT_MONTHLY_COMPONENT_FIELDS: tuple[str, ...] = (
    "month",
    "account",
    "component",
    "component_label",
    "component_order",
    "amount_cny",
    "amount_by_ccy",
    "source_view",
    "source_field",
    "included_in_net_income",
    "calculation_method",
)


ASSIGNED_STOCK_POSITION_PNL_FIELDS: tuple[str, ...] = (
    "account",
    "symbol",
    "currency",
    "status",
    "review_status",
    "stock_lot_id",
    "shares_opened",
    "shares_remaining",
    "shares_sold",
    "stock_cost_per_share",
    "remaining_stock_cost_basis",
    "spot",
    "spot_time",
    "quote_source",
    "quote_status",
    "remaining_market_value",
    "assigned_stock_unrealized_pnl",
    "assigned_stock_realized_pnl",
    "option_premium_attribution",
    "assignment_lifecycle_pnl",
)


ASSIGNED_STOCK_SALE_EVENT_FIELDS: tuple[str, ...] = (
    "month",
    "account",
    "symbol",
    "currency",
    "stock_lot_id",
    "stock_event_id",
    "event_at",
    "shares",
    "sale_price",
    "fees",
    "cash_in_gross",
    "stock_sale_cash_in_net",
    "stock_cost_basis_sold",
    "assigned_stock_realized_pnl",
    "source",
    "source_deal_id",
)


OPEN_OPTION_EXPOSURE_FIELDS: tuple[str, ...] = (
    "account",
    "symbol",
    "status",
    "side",
    "option_type",
    "strike",
    "expiration_ymd",
    "dte",
    "contracts_open",
    "currency",
    "cash_secured_amount",
    "strategy",
    "risk_model",
)


EXPIRATION_RISK_BUCKET_FIELDS: tuple[str, ...] = (
    "account",
    "expiration_bucket",
    "currency",
    "position_count",
    "contracts_open",
    "cash_secured_amount",
    "nearest_expiration_ymd",
)


SYMBOL_INCOME_ATTRIBUTION_FIELDS: tuple[str, ...] = (
    "month",
    "account",
    "symbol",
    "component",
    "currency",
    "amount_gross",
    "source_view",
)


STRATEGY_CONFIG_BY_SYMBOL_ACCOUNT_FIELDS: tuple[str, ...] = (
    "symbol",
    "account",
    "broker",
    "strategy_family",
    "enabled",
    "min_strike",
    "max_strike",
    "min_annualized",
    "config_source",
)


CANDIDATE_FILTER_DIAGNOSTIC_FIELDS: tuple[str, ...] = (
    "run_id",
    "account",
    "symbol",
    "option_type",
    "function",
    "status",
    "stage",
    "rule",
    "metric_value",
    "threshold",
    "contract_symbol",
    "expiration",
    "strike",
    "message",
    "source",
)


CLOSE_ADVICE_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "account",
    "position_id",
    "advice_run_id",
    "symbol",
    "option_type",
    "side",
    "expiration",
    "strike",
    "contracts_open",
    "tier",
    "close_action",
    "evaluation_status",
    "quote_status",
    "reason",
    "realized_if_close",
    "remaining_premium",
    "dte",
    "strategy_family",
    "risk_model",
)


RUNTIME_TICK_STATUS_FIELDS: tuple[str, ...] = (
    "market",
    "account",
    "latest_run_id",
    "latest_status",
    "freshness_status",
    "freshness_age_seconds",
    "notification_exists",
    "warning_count",
    "warning_codes",
    "source",
)


QUOTE_FRESHNESS_FIELDS: tuple[str, ...] = (
    "symbol",
    "market",
    "source",
    "quote_status",
    "spot",
    "spot_time",
    "account",
    "view",
)


def _field_meta(
    type_name: str,
    *,
    aggregation: str,
    source: str,
    freshness: str,
    unit: str | None = None,
    currency: str | None = None,
    formula: str | None = None,
    null_meaning: str | None = None,
    join_keys: Iterable[str] = (),
    do_not: Iterable[str] = (),
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "type": type_name,
        "aggregation": aggregation,
        "source": source,
        "freshness": freshness,
    }
    if unit:
        meta["unit"] = unit
    if currency:
        meta["currency"] = currency
    if formula:
        meta["formula"] = formula
    if null_meaning:
        meta["null_meaning"] = null_meaning
    join_keys_out = [str(item) for item in join_keys if str(item).strip()]
    if join_keys_out:
        meta["join_keys"] = join_keys_out
    do_not_out = [str(item) for item in do_not if str(item).strip()]
    if do_not_out:
        meta["do_not"] = do_not_out
    return meta


def _default_field_meta(field: str, *, source: str, freshness: str) -> dict[str, Any]:
    name = field.lower()
    if name == "month":
        return _field_meta("date", aggregation="group_only", source=source, freshness=freshness, unit="month", join_keys=("month",))
    if name in {"expiration_ymd", "trade_time_beijing"}:
        return _field_meta("date", aggregation="group_only", source=source, freshness=freshness)
    if name == "account":
        return _field_meta("text", aggregation="group_only", source=source, freshness=freshness, unit="account", join_keys=("account",))
    if name in {"component", "component_label", "strategy", "strategy_family", "risk_model", "expiration_bucket", "config_source"}:
        return _field_meta("text", aggregation="group_only", source=source, freshness=freshness)
    if name == "symbol":
        return _field_meta("symbol", aggregation="group_only", source=source, freshness=freshness, join_keys=("symbol",))
    if name == "currency":
        return _field_meta("text", aggregation="group_only", source=source, freshness=freshness, unit="currency", join_keys=("currency",))
    if name in {"accounts"} or name.endswith("_json") or name.endswith("_by_ccy"):
        return _field_meta("json", aggregation="none", source=source, freshness=freshness)
    if name.endswith("_rate"):
        return _field_meta(
            "rate",
            aggregation="weighted_recompute",
            source=source,
            freshness=freshness,
            unit="percent",
            null_meaning="denominator missing, zero, or not applicable",
            do_not=("avg", "sum"),
        )
    if name == "component_order":
        return _field_meta("count", aggregation="max", source=source, freshness=freshness, unit="order")
    if name.endswith("_days") or name == "dte":
        return _field_meta("count", aggregation="max", source=source, freshness=freshness, unit="days")
    if "shares" in name:
        return _field_meta("shares", aggregation="sum", source=source, freshness=freshness, unit="shares")
    if name in {"contracts", "contracts_open", "contracts_closed"}:
        return _field_meta("contracts", aggregation="sum", source=source, freshness=freshness, unit="contracts")
    if (
        name.endswith("_cny")
        or name.endswith("_gross")
        or name.endswith("_amount")
        or name.endswith("_pnl")
        or name.endswith("_premium")
        or "income" in name
        or "cashflow" in name
        or name in {"cash_secured_cny"}
    ):
        currency = "CNY" if name.endswith("_cny") or name == "cash_secured_cny" else "currency field"
        return _field_meta("money", aggregation="sum", source=source, freshness=freshness, currency=currency)
    if name in {
        "strike",
        "price",
        "sale_price",
        "spot",
        "stock_cost_per_share",
        "remaining_stock_cost_basis",
        "remaining_market_value",
        "fees",
        "cash_in_gross",
        "stock_sale_cash_in_net",
        "stock_cost_basis_sold",
        "sell_put_max_strike",
        "sell_call_min_strike",
    }:
        return _field_meta("money", aggregation="none", source=source, freshness=freshness, currency="currency field")
    if name.endswith("_enabled") or name == "included_in_net_income":
        return _field_meta("status", aggregation="group_only", source=source, freshness=freshness)
    if name in {
        "status",
        "side",
        "option_type",
        "position_effect",
        "trade_action",
        "close_type",
        "return_basis",
        "calculation_method",
        "account_scope",
        "broker",
        "use",
        "message",
        "stock_lot_id",
        "stock_event_id",
        "source_view",
        "source_field",
        "quote_source",
        "source_deal_id",
        "review_status",
    }:
        return _field_meta("text", aggregation="group_only", source=source, freshness=freshness)
    return _field_meta("text", aggregation="none", source=source, freshness=freshness)


def _build_view_specs(raw_specs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}
    for name, raw_spec in raw_specs.items():
        spec = {key: value for key, value in raw_spec.items() if key != "_field_semantics"}
        fields = tuple(str(field) for field in spec.get("fields") or ())
        spec["fields"] = fields
        source = str(spec.get("semantic_source") or spec.get("description") or name)
        freshness = str(spec.get("freshness") or "snapshot")
        field_semantics = {
            field: _default_field_meta(field, source=source, freshness=freshness)
            for field in fields
        }
        overrides = raw_spec.get("_field_semantics")
        if isinstance(overrides, dict):
            for field, override in overrides.items():
                if not isinstance(override, dict):
                    continue
                field_name = str(field)
                base = dict(field_semantics.get(field_name) or _default_field_meta(field_name, source=source, freshness=freshness))
                base.update(override)
                field_semantics[field_name] = base
        spec["field_semantics"] = field_semantics
        specs[name] = spec
    return specs


_RETURN_FIELD_OVERRIDES: dict[str, dict[str, Any]] = {
    "cash_secured_cny": {
        "formula": "current_open_cash_secured converted to CNY",
        "aggregation": "sum",
        "currency": "CNY",
    },
    "net_income_cny": {
        "formula": "income_cashflow_ex_assignment_stock converted to CNY",
        "aggregation": "sum",
        "currency": "CNY",
    },
    "realized_pnl_cny": {
        "formula": "realized option/assignment/stock sale PnL converted to CNY",
        "aggregation": "sum",
        "currency": "CNY",
    },
    "premium_income_cny": {
        "formula": "sell-open option premium converted to CNY",
        "aggregation": "sum",
        "currency": "CNY",
    },
    "net_return_rate": {
        "formula": "net_income_cny / cash_secured_cny",
        "aggregation": "weighted_recompute",
        "do_not": ["avg", "sum"],
    },
    "realized_return_rate": {
        "formula": "realized_pnl_cny / cash_secured_cny",
        "aggregation": "weighted_recompute",
        "do_not": ["avg", "sum"],
    },
    "premium_return_rate": {
        "formula": "premium_income_cny / cash_secured_cny",
        "aggregation": "weighted_recompute",
        "do_not": ["avg", "sum"],
    },
    "annualized_net_return_rate": {
        "formula": "net_return_rate * 365 / annualized_basis_days",
        "aggregation": "weighted_recompute",
        "do_not": ["avg", "sum"],
    },
}


VIEW_SPECS: dict[str, dict[str, Any]] = _build_view_specs({
    "account_monthly_performance": {
        "description": "semantic account-level monthly performance view for comparing income, rates, premium, realized PnL, and cash-secured basis",
        "fields": RETURN_SUMMARY_FIELDS,
        "row_grain": "month + account",
        "primary_keys": ("month", "account"),
        "time_grain": "month",
        "source_tools": ("monthly_income_report",),
        "semantic_source": "monthly_income_report.return_summary",
        "freshness": "snapshot",
        "recommended_filters": ("month", "account"),
        "safe_join_keys": ("month", "account"),
        "alias_of": "monthly_income_return_summary",
        "_field_semantics": _RETURN_FIELD_OVERRIDES,
    },
    "account_monthly_income_components": {
        "description": "account-level monthly income composition rows with included and excluded components",
        "fields": ACCOUNT_MONTHLY_COMPONENT_FIELDS,
        "row_grain": "month + account + component",
        "primary_keys": ("month", "account", "component"),
        "time_grain": "month",
        "source_tools": ("monthly_income_report",),
        "semantic_source": "monthly_income_report.return_summary + monthly_income_report.summary",
        "freshness": "snapshot",
        "recommended_filters": ("month", "account", "component"),
        "safe_join_keys": ("month", "account"),
        "_field_semantics": {
            "amount_cny": {
                "type": "money",
                "currency": "CNY",
                "aggregation": "sum",
                "source": "monthly_income_report",
                "freshness": "snapshot",
            },
            "included_in_net_income": {
                "type": "status",
                "aggregation": "group_only",
                "source": "monthly_income_report",
                "freshness": "snapshot",
            },
        },
    },
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
        "row_grain": "month + account + currency",
        "primary_keys": ("month", "account", "currency"),
        "time_grain": "month",
        "source_tools": ("monthly_income_report",),
        "semantic_source": "monthly_income_report.summary",
        "freshness": "snapshot",
        "recommended_filters": ("month", "account", "currency"),
        "safe_join_keys": ("month", "account", "currency"),
    },
    "monthly_income_return_summary": {
        "description": "account-level monthly return rows with CNY amounts and return rates",
        "fields": RETURN_SUMMARY_FIELDS,
        "row_grain": "month + account",
        "primary_keys": ("month", "account"),
        "time_grain": "month",
        "source_tools": ("monthly_income_report",),
        "semantic_source": "monthly_income_report.return_summary",
        "freshness": "snapshot",
        "recommended_filters": ("month", "account"),
        "safe_join_keys": ("month", "account"),
        "_field_semantics": _RETURN_FIELD_OVERRIDES,
    },
    "monthly_income_combined_return_summary": {
        "description": "all-account monthly return rows aggregated by month",
        "fields": COMBINED_RETURN_SUMMARY_FIELDS,
        "row_grain": "month + account_scope",
        "primary_keys": ("month", "account_scope"),
        "time_grain": "month",
        "source_tools": ("monthly_income_report",),
        "semantic_source": "monthly_income_report.combined_return_summary",
        "freshness": "snapshot",
        "recommended_filters": ("month",),
        "safe_join_keys": ("month",),
        "_field_semantics": _RETURN_FIELD_OVERRIDES,
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
        "row_grain": "month + account + trade event",
        "primary_keys": ("month", "account", "symbol", "option_type", "trade_action", "expiration_ymd", "strike"),
        "time_grain": "month",
        "source_tools": ("monthly_income_report",),
        "semantic_source": "monthly_income_report.cashflow_rows",
        "freshness": "snapshot",
        "recommended_filters": ("month", "account", "symbol", "currency"),
        "safe_join_keys": ("month", "account", "symbol", "currency", "option_type", "strike", "expiration_ymd"),
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
        "row_grain": "month + account + realized event",
        "primary_keys": ("month", "account", "symbol", "option_type", "close_type", "expiration_ymd", "strike"),
        "time_grain": "month",
        "source_tools": ("monthly_income_report",),
        "semantic_source": "monthly_income_report.realized_rows",
        "freshness": "snapshot",
        "recommended_filters": ("month", "account", "symbol", "currency"),
        "safe_join_keys": ("month", "account", "symbol", "currency", "option_type", "strike", "expiration_ymd"),
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
        "row_grain": "month + account + premium event",
        "primary_keys": ("month", "account", "symbol", "option_type", "expiration_ymd", "strike"),
        "time_grain": "month",
        "source_tools": ("monthly_income_report",),
        "semantic_source": "monthly_income_report.premium_rows",
        "freshness": "snapshot",
        "recommended_filters": ("month", "account", "symbol", "currency"),
        "safe_join_keys": ("month", "account", "symbol", "currency", "option_type", "strike", "expiration_ymd"),
    },
    "assigned_stock_position_pnl": {
        "description": "semantic assigned-stock lot PnL view for current holding, realized sale, and lifecycle PnL analysis",
        "fields": ASSIGNED_STOCK_POSITION_PNL_FIELDS,
        "row_grain": "account + symbol + stock_lot_id",
        "primary_keys": ("account", "symbol", "stock_lot_id"),
        "source_tools": ("monthly_income_report", "option_positions_read"),
        "semantic_source": "monthly_income_report.assignment_lifecycle_rows",
        "freshness": "realtime_or_cached_quote_snapshot",
        "recommended_filters": ("account", "symbol", "status", "currency", "quote_status"),
        "safe_join_keys": ("account", "symbol", "currency", "stock_lot_id"),
        "alias_of": "assigned_stock_lifecycle",
    },
    "assigned_stock_sale_events": {
        "description": "semantic assigned-stock sale events view with realized stock PnL attribution",
        "fields": ASSIGNED_STOCK_SALE_EVENT_FIELDS,
        "row_grain": "account + symbol + stock_lot_id + sale event",
        "primary_keys": ("account", "symbol", "stock_lot_id", "stock_event_id"),
        "time_grain": "month",
        "source_tools": ("monthly_income_report", "option_positions_read"),
        "semantic_source": "monthly_income_report.assigned_stock_sale_rows",
        "freshness": "snapshot",
        "recommended_filters": ("month", "account", "symbol", "currency"),
        "safe_join_keys": ("month", "account", "symbol", "currency", "stock_lot_id"),
        "alias_of": "assigned_stock_sales",
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
        "row_grain": "account + symbol + assigned stock lot",
        "primary_keys": ("account", "symbol"),
        "source_tools": ("monthly_income_report", "option_positions_read"),
        "semantic_source": "monthly_income_report.assignment_lifecycle_rows",
        "freshness": "realtime_or_cached_quote_snapshot",
        "recommended_filters": ("account", "symbol", "status", "currency"),
        "safe_join_keys": ("account", "symbol", "currency"),
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
        "row_grain": "account + symbol + stock_lot_id + sale event",
        "primary_keys": ("account", "symbol", "stock_lot_id"),
        "source_tools": ("monthly_income_report", "option_positions_read"),
        "semantic_source": "monthly_income_report.assigned_stock_sale_rows",
        "freshness": "snapshot",
        "recommended_filters": ("account", "symbol", "currency"),
        "safe_join_keys": ("account", "symbol", "currency", "stock_lot_id"),
    },
    "assigned_stock_review": {
        "description": "assigned-stock lifecycle review rows such as missing_quote or missing_stock_sale",
        "fields": ("account", "symbol", "currency", "status", "message", "stock_lot_id"),
        "row_grain": "account + symbol + review finding",
        "primary_keys": ("account", "symbol", "stock_lot_id", "status"),
        "source_tools": ("monthly_income_report", "option_positions_read"),
        "semantic_source": "monthly_income_report.assigned_stock_review_rows",
        "freshness": "snapshot",
        "recommended_filters": ("account", "symbol", "status"),
        "safe_join_keys": ("account", "symbol", "currency", "stock_lot_id"),
    },
    "open_option_exposure": {
        "description": "semantic open option exposure view with strategy and risk-model labels",
        "fields": OPEN_OPTION_EXPOSURE_FIELDS,
        "row_grain": "account + symbol + option_type + side + strike + expiration",
        "primary_keys": ("account", "symbol", "side", "option_type", "strike", "expiration_ymd"),
        "source_tools": ("option_positions_read",),
        "semantic_source": "option_positions_read.list",
        "freshness": "snapshot",
        "recommended_filters": ("account", "symbol", "option_type", "expiration_ymd", "strategy"),
        "safe_join_keys": ("account", "symbol", "currency", "option_type", "strike", "expiration_ymd"),
        "alias_of": "position_lots",
    },
    "expiration_risk_buckets": {
        "description": "open option exposure grouped into DTE buckets by account and currency",
        "fields": EXPIRATION_RISK_BUCKET_FIELDS,
        "row_grain": "account + expiration_bucket + currency",
        "primary_keys": ("account", "expiration_bucket", "currency"),
        "source_tools": ("option_positions_read",),
        "semantic_source": "open_option_exposure",
        "freshness": "snapshot",
        "recommended_filters": ("account", "expiration_bucket", "currency"),
        "safe_join_keys": ("account", "currency"),
    },
    "symbol_income_attribution": {
        "description": "symbol-level income attribution rows by month/account/component",
        "fields": SYMBOL_INCOME_ATTRIBUTION_FIELDS,
        "row_grain": "month + account + symbol + component + currency",
        "primary_keys": ("month", "account", "symbol", "component", "currency"),
        "time_grain": "month",
        "source_tools": ("monthly_income_report",),
        "semantic_source": "monthly_income_report detail rows",
        "freshness": "snapshot",
        "recommended_filters": ("month", "account", "symbol", "component", "currency"),
        "safe_join_keys": ("month", "account", "symbol", "currency"),
    },
    "strategy_config_by_symbol_account": {
        "description": "strategy config rows expanded by symbol/account/strategy family",
        "fields": STRATEGY_CONFIG_BY_SYMBOL_ACCOUNT_FIELDS,
        "row_grain": "symbol + account + strategy_family",
        "primary_keys": ("symbol", "account", "strategy_family"),
        "source_tools": ("runtime_config",),
        "semantic_source": "runtime_config.symbol_rows",
        "freshness": "runtime_config_snapshot",
        "recommended_filters": ("symbol", "account", "strategy_family", "enabled"),
        "safe_join_keys": ("symbol", "account"),
    },
    "candidate_filter_diagnostics": {
        "description": "candidate filter trace diagnostics by run/account/symbol/function/rule",
        "fields": CANDIDATE_FILTER_DIAGNOSTIC_FIELDS,
        "row_grain": "run_id + account + symbol + option_type + rule",
        "primary_keys": ("run_id", "account", "symbol", "function", "rule", "contract_symbol"),
        "source_tools": ("candidate_filter_trace",),
        "semantic_source": "candidate_filter_trace.jsonl artifacts",
        "freshness": "artifact_snapshot",
        "recommended_filters": ("run_id", "account", "symbol", "function", "status", "rule"),
        "safe_join_keys": ("run_id", "account", "symbol", "option_type", "expiration", "strike"),
    },
    "close_advice_snapshot": {
        "description": "latest close-advice rows with action, tier, reason, quote status, and close PnL context",
        "fields": CLOSE_ADVICE_SNAPSHOT_FIELDS,
        "row_grain": "account + position_id + advice_run_id",
        "primary_keys": ("account", "position_id", "advice_run_id"),
        "source_tools": ("close_advice_read",),
        "semantic_source": "close_advice.csv artifacts",
        "freshness": "artifact_snapshot",
        "recommended_filters": ("account", "symbol", "tier", "close_action", "evaluation_status", "quote_status"),
        "safe_join_keys": ("account", "symbol", "option_type", "expiration", "strike"),
    },
    "runtime_tick_status": {
        "description": "runtime latest-run and notification freshness status by market/account",
        "fields": RUNTIME_TICK_STATUS_FIELDS,
        "row_grain": "market + account + latest_run",
        "primary_keys": ("market", "account", "latest_run_id"),
        "source_tools": ("runtime_status",),
        "semantic_source": "runtime_status and scheduler artifacts",
        "freshness": "runtime_snapshot",
        "recommended_filters": ("market", "account", "freshness_status", "latest_status"),
        "safe_join_keys": ("account",),
    },
    "quote_freshness": {
        "description": "quote freshness rows derived from existing quote status surfaces",
        "fields": QUOTE_FRESHNESS_FIELDS,
        "row_grain": "symbol + market + source",
        "primary_keys": ("symbol", "market", "source", "account"),
        "source_tools": ("monthly_income_report", "runtime_status"),
        "semantic_source": "assigned_stock quote status and runtime quote snapshots",
        "freshness": "realtime_or_cached_quote_snapshot",
        "recommended_filters": ("symbol", "market", "quote_status", "account"),
        "safe_join_keys": ("account", "symbol"),
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
        "row_grain": "account + symbol + option lot",
        "primary_keys": ("account", "symbol", "side", "option_type", "strike", "expiration_ymd"),
        "source_tools": ("option_positions_read",),
        "semantic_source": "option_positions_read.list",
        "freshness": "snapshot",
        "recommended_filters": ("account", "symbol", "status"),
        "safe_join_keys": ("account", "symbol", "currency", "option_type", "strike", "expiration_ymd"),
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
        "row_grain": "trade event",
        "primary_keys": ("trade_time_beijing", "account", "symbol", "side", "option_type", "strike", "expiration_ymd"),
        "source_tools": ("option_positions_read",),
        "semantic_source": "option_positions_read.events",
        "freshness": "snapshot",
        "recommended_filters": ("account", "symbol", "currency", "expiration_ymd"),
        "safe_join_keys": ("account", "symbol", "currency", "option_type", "strike", "expiration_ymd"),
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
        "row_grain": "symbol + broker config row",
        "primary_keys": ("symbol", "broker"),
        "source_tools": ("runtime_config",),
        "semantic_source": "runtime_config.symbol_rows",
        "freshness": "runtime_config_snapshot",
        "recommended_filters": ("symbol", "broker", "accounts"),
        "safe_join_keys": ("symbol",),
    },
})


_MONTHLY_SOURCE_VIEWS: set[str] = {
    "account_monthly_performance",
    "account_monthly_income_components",
    "monthly_income_summary",
    "monthly_income_return_summary",
    "monthly_income_combined_return_summary",
    "monthly_income_cashflow_rows",
    "monthly_income_realized_rows",
    "monthly_income_premium_rows",
    "assigned_stock_position_pnl",
    "assigned_stock_sale_events",
    "assigned_stock_lifecycle",
    "assigned_stock_sales",
    "assigned_stock_review",
    "symbol_income_attribution",
}
_POSITION_SOURCE_VIEWS: set[str] = {
    "position_lots",
    "open_option_exposure",
    "expiration_risk_buckets",
}
_EVENT_SOURCE_VIEWS: set[str] = {"trade_events"}
_CONFIG_SOURCE_VIEWS: set[str] = {
    "symbol_strategy_config",
    "strategy_config_by_symbol_account",
}
_ARTIFACT_SOURCE_VIEWS: set[str] = {
    "candidate_filter_diagnostics",
    "close_advice_snapshot",
    "runtime_tick_status",
}
_QUOTE_SOURCE_VIEWS: set[str] = {"quote_freshness"}


_ANALYSIS_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "analysis_query.output.v2",
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
    query_patterns = [
        {
            "question": "对比 lx 和 sy 的账户收益，有什么不同？",
            "sql": (
                "select month, account, net_income_cny, net_return_rate "
                "from account_monthly_performance "
                "where account in ('lx','sy') order by month, account"
            ),
        },
        {
            "question": "lx 和 sy 收益差异主要来自哪里？",
            "sql": (
                "select month, account, component, amount_cny, included_in_net_income "
                "from account_monthly_income_components "
                "where account in ('lx','sy') order by month, account, component_order"
            ),
        },
        {
            "question": "指派正股当前浮盈亏按账户汇总",
            "sql": (
                "select account, currency, sum(assigned_stock_unrealized_pnl) as unrealized_pnl "
                "from assigned_stock_position_pnl group by account, currency"
            ),
        },
    ]
    return {
        "schema_version": "analysis.catalog.v2",
        "source_label": "OM read-only analysis workspace",
        "views": specs,
        "field_types": _catalog_field_types(specs),
        "aggregation_policies": _catalog_aggregation_policies(specs),
        "join_policies": _catalog_join_policies(specs),
        "sql_rules": {
            "allowed_statements": ["SELECT", "WITH"],
            "single_statement_only": True,
            "writes_allowed": False,
            "max_limit": MAX_QUERY_LIMIT,
            "whitelisted_views": sorted(VIEW_SPECS),
        },
        "query_patterns": query_patterns,
        "examples": query_patterns,
        "anti_patterns": [
            "Do not invent unlisted columns such as net_cashflow, total_return, return_rate, or open_basis_pnl.",
            "Do not average or sum *_return_rate fields directly; recompute weighted rates from money numerator and cash_secured_cny.",
            "Do not join views unless the join fields are listed in each view's safe_join_keys.",
        ],
    }, [], {"config_path": ctx.mask_path(config_path)}


def _catalog_field_types(specs: dict[str, dict[str, Any]]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for view_name, spec in specs.items():
        field_semantics = spec.get("field_semantics") if isinstance(spec.get("field_semantics"), dict) else {}
        out[view_name] = {
            str(field): str(meta.get("type") or "text")
            for field, meta in field_semantics.items()
            if isinstance(meta, dict)
        }
    return out


def _catalog_aggregation_policies(specs: dict[str, dict[str, Any]]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for view_name, spec in specs.items():
        field_semantics = spec.get("field_semantics") if isinstance(spec.get("field_semantics"), dict) else {}
        out[view_name] = {
            str(field): str(meta.get("aggregation") or "none")
            for field, meta in field_semantics.items()
            if isinstance(meta, dict)
        }
    return out


def _catalog_join_policies(specs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        view_name: {
            "row_grain": str(spec.get("row_grain") or ""),
            "safe_join_keys": [str(item) for item in spec.get("safe_join_keys") or ()],
            "primary_keys": [str(item) for item in spec.get("primary_keys") or ()],
        }
        for view_name, spec in specs.items()
    }


def _analysis_query_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    sql = _validated_sql(payload.get("sql") or payload.get("query"))
    limit = _bounded_limit(payload.get("limit"))
    warnings: list[str] = []
    requested_views = set(_referenced_analysis_views(sql))
    views = _materialize_views(ctx, payload, warnings=warnings, requested_views=requested_views)
    rows, columns, views_used = _execute_select(sql, views, limit=limit)
    truncated = len(rows) > limit
    rows = rows[:limit]
    cell_refs = _cell_refs(rows)
    query_explain, query_warnings, evidence = _query_explain_and_evidence(
        sql=sql,
        rows=rows,
        columns=columns,
        views_used=views_used,
        materialization_warnings=warnings,
    )
    warnings.extend(query_warnings)
    data = {
        "schema_version": "analysis.query.output.v2",
        "source_label": "OM read-only analysis workspace",
        "query": {"sql": sql, "limit": limit},
        "preflight": {"ok": True, "warnings": query_warnings},
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "views_used": views_used,
        "available_views": sorted(VIEW_SPECS),
        "query_explain": query_explain,
        "evidence": evidence,
        "cell_refs": cell_refs,
        "fallback_text": _render_fallback_table(rows=rows, columns=columns, row_count=len(rows), truncated=truncated),
    }
    return data, warnings, {
        "source": "in_memory_sqlite",
        "requested_views": sorted(requested_views),
        "materialized_views": sorted(views),
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


def _materialize_views(
    ctx: AgentToolContext,
    payload: dict[str, Any],
    *,
    warnings: list[str],
    requested_views: set[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    requested = set(requested_views) if requested_views is not None else set(VIEW_SPECS)
    if not requested:
        return {}
    monthly_data: dict[str, Any] = {}
    position_data: dict[str, Any] = {}
    event_data: dict[str, Any] = {}
    symbol_rows: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    close_advice_rows: list[dict[str, Any]] = []
    runtime_rows: list[dict[str, Any]] = []

    if requested & (_MONTHLY_SOURCE_VIEWS | _QUOTE_SOURCE_VIEWS):
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

    if requested & _POSITION_SOURCE_VIEWS:
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

    if requested & _EVENT_SOURCE_VIEWS:
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

    if requested & _CONFIG_SOURCE_VIEWS:
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

    if requested & {"candidate_filter_diagnostics"}:
        candidate_rows, candidate_warnings = _candidate_filter_diagnostic_rows(ctx, payload)
        warnings.extend(candidate_warnings)

    if requested & {"close_advice_snapshot"}:
        close_advice_rows, close_advice_warnings = _close_advice_snapshot_rows(ctx, payload)
        warnings.extend(close_advice_warnings)

    if requested & {"runtime_tick_status"}:
        runtime_rows, runtime_warnings = _runtime_tick_status_rows(ctx, payload)
        warnings.extend(runtime_warnings)

    open_option_exposure_rows = _open_option_exposure_rows(position_data.get("rows"))
    quote_rows = _quote_freshness_rows(
        assignment_rows=monthly_data.get("assignment_lifecycle_rows"),
        runtime_rows=runtime_rows,
    )
    materialized = {
        "account_monthly_performance": _normalize_rows(monthly_data.get("return_summary")),
        "account_monthly_income_components": _normalize_rows(
            _account_monthly_income_component_rows(
                monthly_data.get("return_summary"),
                monthly_data.get("summary"),
            )
        ),
        "monthly_income_summary": _normalize_rows(monthly_data.get("summary")),
        "monthly_income_return_summary": _normalize_rows(monthly_data.get("return_summary")),
        "monthly_income_combined_return_summary": _normalize_rows(monthly_data.get("combined_return_summary")),
        "monthly_income_cashflow_rows": _normalize_rows(monthly_data.get("cashflow_rows")),
        "monthly_income_realized_rows": _normalize_rows(monthly_data.get("realized_rows")),
        "monthly_income_premium_rows": _normalize_rows(monthly_data.get("premium_rows")),
        "assigned_stock_position_pnl": _normalize_rows(
            _assigned_stock_position_pnl_rows(monthly_data.get("assignment_lifecycle_rows"))
        ),
        "assigned_stock_sale_events": _normalize_rows(
            _assigned_stock_sale_event_rows(monthly_data.get("assigned_stock_sale_rows"))
        ),
        "assigned_stock_lifecycle": _normalize_rows(monthly_data.get("assignment_lifecycle_rows")),
        "assigned_stock_sales": _normalize_rows(_assigned_stock_sale_event_rows(monthly_data.get("assigned_stock_sale_rows"))),
        "assigned_stock_review": _normalize_rows(monthly_data.get("assigned_stock_review_rows")),
        "open_option_exposure": _normalize_rows(open_option_exposure_rows),
        "expiration_risk_buckets": _normalize_rows(_expiration_risk_bucket_rows(open_option_exposure_rows)),
        "symbol_income_attribution": _normalize_rows(
            _symbol_income_attribution_rows(
                cashflow_rows=monthly_data.get("cashflow_rows"),
                realized_rows=monthly_data.get("realized_rows"),
                premium_rows=monthly_data.get("premium_rows"),
            )
        ),
        "strategy_config_by_symbol_account": _normalize_rows(_strategy_config_by_symbol_account_rows(symbol_rows)),
        "candidate_filter_diagnostics": _normalize_rows(candidate_rows),
        "close_advice_snapshot": _normalize_rows(close_advice_rows),
        "runtime_tick_status": _normalize_rows(runtime_rows),
        "quote_freshness": _normalize_rows(quote_rows),
        "position_lots": _normalize_rows(position_data.get("rows")),
        "trade_events": _normalize_rows(event_data.get("rows")),
        "symbol_strategy_config": _normalize_rows(symbol_rows),
    }
    return {view_name: rows for view_name, rows in materialized.items() if view_name in requested}


def _account_monthly_income_component_rows(return_rows: Any, summary_rows: Any) -> list[dict[str, Any]]:
    if not isinstance(return_rows, list):
        return []
    excluded_assignment = _assignment_stock_cashflow_by_month_account(summary_rows)
    rows: list[dict[str, Any]] = []
    for raw_row in return_rows:
        if not isinstance(raw_row, dict):
            continue
        month = str(raw_row.get("month") or "").strip()
        account = str(raw_row.get("account") or "").strip()
        if not month or not account:
            continue
        net_income = _float_or_none(raw_row.get("net_income_cny"))
        premium_income = _float_or_none(raw_row.get("premium_income_cny"))
        realized_pnl = _float_or_none(raw_row.get("realized_pnl_cny"))
        component_inputs = [
            (
                "premium_income",
                "option premium income",
                10,
                premium_income,
                raw_row.get("premium_income_by_ccy"),
                "premium_income_cny",
                "sell-open option premium converted to CNY",
            ),
            (
                "realized_pnl",
                "realized PnL",
                20,
                realized_pnl,
                raw_row.get("realized_pnl_by_ccy"),
                "realized_pnl_cny",
                "realized option/assignment/assigned-stock sale PnL converted to CNY",
            ),
        ]
        for component, label, order, amount_cny, amount_by_ccy, source_field, method in component_inputs:
            if amount_cny is None and not _dict_has_amount(amount_by_ccy):
                continue
            rows.append(
                _income_component_row(
                    month=month,
                    account=account,
                    component=component,
                    label=label,
                    order=order,
                    amount_cny=amount_cny,
                    amount_by_ccy=amount_by_ccy,
                    source_field=source_field,
                    included_in_net_income=True,
                    calculation_method=method,
                )
            )
        residual = _residual_net_income(net_income, premium_income, realized_pnl)
        if residual is not None and abs(residual) >= 0.000001:
            rows.append(
                _income_component_row(
                    month=month,
                    account=account,
                    component="other_net_income",
                    label="other net income",
                    order=30,
                    amount_cny=residual,
                    amount_by_ccy={},
                    source_field="net_income_cny - premium_income_cny - realized_pnl_cny",
                    included_in_net_income=True,
                    calculation_method="residual component such as long-option recovery or other cashflow included in net income",
                )
            )
        excluded = excluded_assignment.get((month, account))
        if excluded:
            rows.append(
                _income_component_row(
                    month=month,
                    account=account,
                    component="excluded_assignment_stock_principal",
                    label="assigned-stock principal cashflow excluded from return numerator",
                    order=90,
                    amount_cny=excluded.get("amount_cny"),
                    amount_by_ccy=excluded.get("amount_by_ccy"),
                    source_field="assignment_stock_net_cashflow_gross",
                    included_in_net_income=False,
                    calculation_method="assignment stock principal cashflow is recorded as cash movement but excluded from net income return numerator",
                )
            )
    return rows


def _income_component_row(
    *,
    month: str,
    account: str,
    component: str,
    label: str,
    order: int,
    amount_cny: Any,
    amount_by_ccy: Any,
    source_field: str,
    included_in_net_income: bool,
    calculation_method: str,
) -> dict[str, Any]:
    return {
        "month": month,
        "account": account,
        "component": component,
        "component_label": label,
        "component_order": order,
        "amount_cny": _rounded_float_or_none(amount_cny),
        "amount_by_ccy": amount_by_ccy if isinstance(amount_by_ccy, dict) else {},
        "source_view": "monthly_income_report",
        "source_field": source_field,
        "included_in_net_income": included_in_net_income,
        "calculation_method": calculation_method,
    }


def _assignment_stock_cashflow_by_month_account(summary_rows: Any) -> dict[tuple[str, str], dict[str, Any]]:
    if not isinstance(summary_rows, list):
        return {}
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in summary_rows:
        if not isinstance(row, dict):
            continue
        month = str(row.get("month") or "").strip()
        account = str(row.get("account") or "").strip()
        currency = str(row.get("currency") or "").strip()
        if not month or not account:
            continue
        amount = _float_or_none(row.get("assignment_stock_net_cashflow_gross"))
        amount_cny = _float_or_none(row.get("assignment_stock_net_cashflow_gross_cny"))
        if (amount is None or abs(amount) < 0.000001) and (amount_cny is None or abs(amount_cny) < 0.000001):
            continue
        bucket = grouped.setdefault((month, account), {"amount_cny": 0.0, "amount_by_ccy": {}})
        if amount_cny is not None:
            bucket["amount_cny"] = _round_public_float(float(bucket.get("amount_cny") or 0.0) + amount_cny)
        if currency and amount is not None:
            amount_by_ccy = bucket.setdefault("amount_by_ccy", {})
            amount_by_ccy[currency] = _round_public_float(float(amount_by_ccy.get(currency) or 0.0) + amount)
    return grouped


def _assigned_stock_position_pnl_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    fields = ASSIGNED_STOCK_POSITION_PNL_FIELDS
    return [
        {field: row.get(field) for field in fields}
        for row in value
        if isinstance(row, dict)
    ]


def _assigned_stock_sale_event_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        out = {field: row.get(field) for field in ASSIGNED_STOCK_SALE_EVENT_FIELDS}
        out["sale_price"] = row.get("sale_price", row.get("price"))
        rows.append(out)
    return rows


def _residual_net_income(*values: float | None) -> float | None:
    net_income, premium_income, realized_pnl = values
    if net_income is None:
        return None
    known = [value for value in (premium_income, realized_pnl) if value is not None]
    if len(known) < 2:
        return None
    return _round_public_float(net_income - sum(known))


def _dict_has_amount(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return any(_float_or_none(item) is not None for item in value.values())


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rounded_float_or_none(value: Any) -> float | None:
    number = _float_or_none(value)
    if number is None:
        return None
    return _round_public_float(number)


def _round_public_float(value: float) -> float:
    return round(float(value), 6)


def _open_option_exposure_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").strip().lower()
        contracts_open = _float_or_none(row.get("contracts_open"))
        if status and status != "open":
            continue
        if contracts_open is not None and contracts_open <= 0:
            continue
        option_type = str(row.get("option_type") or "").strip().lower()
        side = str(row.get("side") or "").strip().lower()
        strategy, risk_model = _option_strategy_and_risk(side=side, option_type=option_type)
        rows.append(
            {
                "account": row.get("account"),
                "symbol": row.get("symbol"),
                "status": row.get("status"),
                "side": row.get("side"),
                "option_type": row.get("option_type"),
                "strike": row.get("strike"),
                "expiration_ymd": row.get("expiration_ymd"),
                "dte": _dte(row.get("expiration_ymd")),
                "contracts_open": row.get("contracts_open"),
                "currency": row.get("currency"),
                "cash_secured_amount": row.get("cash_secured_amount"),
                "strategy": strategy,
                "risk_model": risk_model,
            }
        )
    return rows


def _option_strategy_and_risk(*, side: str, option_type: str) -> tuple[str, str]:
    if side == "short" and option_type == "put":
        return "sell_put", "cash_secured_put"
    if side == "short" and option_type == "call":
        return "covered_call", "stock_delivery_or_assignment_risk"
    if side == "long":
        return "long_option", "premium_at_risk"
    return "option_position", "option_exposure"


def _dte(expiration_ymd: Any) -> int | None:
    text = str(expiration_ymd or "").strip()
    if not text:
        return None
    try:
        exp = date.fromisoformat(text)
    except ValueError:
        return None
    return (exp - date.today()).days


def _expiration_risk_bucket_rows(open_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in open_rows:
        account = str(row.get("account") or "").strip() or "-"
        currency = str(row.get("currency") or "").strip() or "-"
        bucket_name = _expiration_bucket(row.get("dte"))
        key = (account, bucket_name, currency)
        bucket = grouped.setdefault(
            key,
            {
                "account": account,
                "expiration_bucket": bucket_name,
                "currency": currency,
                "position_count": 0,
                "contracts_open": 0.0,
                "cash_secured_amount": 0.0,
                "nearest_expiration_ymd": None,
            },
        )
        bucket["position_count"] = int(bucket.get("position_count") or 0) + 1
        bucket["contracts_open"] = _round_public_float(
            float(bucket.get("contracts_open") or 0.0) + float(_float_or_none(row.get("contracts_open")) or 0.0)
        )
        bucket["cash_secured_amount"] = _round_public_float(
            float(bucket.get("cash_secured_amount") or 0.0)
            + float(_float_or_none(row.get("cash_secured_amount")) or 0.0)
        )
        expiration = str(row.get("expiration_ymd") or "").strip()
        if expiration and (not bucket.get("nearest_expiration_ymd") or expiration < str(bucket.get("nearest_expiration_ymd"))):
            bucket["nearest_expiration_ymd"] = expiration
    return sorted(grouped.values(), key=lambda item: (str(item.get("account")), _expiration_bucket_sort(str(item.get("expiration_bucket"))), str(item.get("currency"))))


def _expiration_bucket(value: Any) -> str:
    dte = _float_or_none(value)
    if dte is None:
        return "unknown"
    if dte < 0:
        return "expired"
    if dte <= 7:
        return "0-7d"
    if dte <= 30:
        return "8-30d"
    if dte <= 60:
        return "31-60d"
    return "61d+"


def _expiration_bucket_sort(bucket: str) -> int:
    return {
        "expired": 0,
        "0-7d": 1,
        "8-30d": 2,
        "31-60d": 3,
        "61d+": 4,
        "unknown": 5,
    }.get(bucket, 99)


def _symbol_income_attribution_rows(*, cashflow_rows: Any, realized_rows: Any, premium_rows: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rows.extend(
        _symbol_component_rows(
            premium_rows,
            component="premium_income",
            amount_field="premium_received_gross",
            source_view="monthly_income_premium_rows",
        )
    )
    rows.extend(
        _symbol_component_rows(
            realized_rows,
            component="realized_pnl",
            amount_field="realized_gross",
            source_view="monthly_income_realized_rows",
        )
    )
    rows.extend(
        _symbol_component_rows(
            cashflow_rows,
            component="net_cashflow",
            amount_field="net_cashflow_gross",
            source_view="monthly_income_cashflow_rows",
        )
    )
    grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row.get("month") or ""),
            str(row.get("account") or ""),
            str(row.get("symbol") or ""),
            str(row.get("component") or ""),
            str(row.get("currency") or ""),
        )
        bucket = grouped.setdefault(
            key,
            {
                "month": key[0],
                "account": key[1],
                "symbol": key[2],
                "component": key[3],
                "currency": key[4],
                "amount_gross": 0.0,
                "source_view": row.get("source_view"),
            },
        )
        bucket["amount_gross"] = _round_public_float(
            float(bucket.get("amount_gross") or 0.0) + float(_float_or_none(row.get("amount_gross")) or 0.0)
        )
    return sorted(grouped.values(), key=lambda item: (str(item.get("month")), str(item.get("account")), str(item.get("symbol")), str(item.get("component")), str(item.get("currency"))))


def _symbol_component_rows(value: Any, *, component: str, amount_field: str, source_view: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        amount = _float_or_none(row.get(amount_field))
        if amount is None:
            continue
        rows.append(
            {
                "month": row.get("month"),
                "account": row.get("account"),
                "symbol": row.get("symbol"),
                "component": component,
                "currency": row.get("currency"),
                "amount_gross": amount,
                "source_view": source_view,
            }
        )
    return rows


def _strategy_config_by_symbol_account_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        broker = row.get("broker")
        accounts = _strategy_accounts(row.get("accounts"))
        for account in accounts:
            out.extend(
                [
                    _strategy_config_row(
                        row,
                        symbol=symbol,
                        account=account,
                        broker=broker,
                        strategy_family="sell_put",
                        enabled_field="sell_put_enabled",
                        min_strike_field=None,
                        max_strike_field="sell_put_max_strike",
                        min_annualized_field="sell_put_min_annualized",
                    ),
                    _strategy_config_row(
                        row,
                        symbol=symbol,
                        account=account,
                        broker=broker,
                        strategy_family="covered_call",
                        enabled_field="sell_call_enabled",
                        min_strike_field="sell_call_min_strike",
                        max_strike_field=None,
                        min_annualized_field="sell_call_min_annualized",
                    ),
                    _strategy_config_row(
                        row,
                        symbol=symbol,
                        account=account,
                        broker=broker,
                        strategy_family="combo_yield",
                        enabled_field="combo_yield_enabled",
                        min_strike_field=None,
                        max_strike_field=None,
                        min_annualized_field=None,
                    ),
                ]
            )
    return out


def _strategy_accounts(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = [item.strip() for item in value.split(",")]
        value = parsed
    if isinstance(value, (list, tuple, set)):
        accounts = [str(item).strip() for item in value if str(item).strip()]
        return accounts or ["all"]
    return ["all"]


def _strategy_config_row(
    row: dict[str, Any],
    *,
    symbol: Any,
    account: str,
    broker: Any,
    strategy_family: str,
    enabled_field: str,
    min_strike_field: str | None,
    max_strike_field: str | None,
    min_annualized_field: str | None,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "account": account,
        "broker": broker,
        "strategy_family": strategy_family,
        "enabled": row.get(enabled_field),
        "min_strike": row.get(min_strike_field) if min_strike_field else None,
        "max_strike": row.get(max_strike_field) if max_strike_field else None,
        "min_annualized": row.get(min_annualized_field) if min_annualized_field else None,
        "config_source": "runtime_config",
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


def _candidate_filter_diagnostic_rows(ctx: AgentToolContext, payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    paths = _candidate_filter_trace_paths(ctx, payload)
    if not paths:
        return [], ["candidate_filter_diagnostics missing: no candidate_filter_trace.jsonl artifacts found"]
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for path in paths:
        try:
            raw_rows = read_candidate_filter_trace(path)
        except Exception as exc:
            warnings.append(f"candidate_filter_diagnostics read_error: {path.name}: {type(exc).__name__}: {exc}")
            continue
        scope = infer_trace_scope_from_path(path)
        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            rows.append(_candidate_filter_diagnostic_row(row, scope=scope))
            if len(rows) >= MAX_MATERIALIZED_ROWS:
                warnings.append("candidate_filter_diagnostics truncated at materialization row cap")
                return rows, warnings
    if not rows:
        warnings.append("candidate_filter_diagnostics empty: trace artifacts contained no rows")
    return rows, warnings


def _candidate_filter_trace_paths(ctx: AgentToolContext, payload: dict[str, Any]) -> list[Path]:
    base = ctx.repo_base().resolve()
    account = str(payload.get("account") or "").strip().lower()
    candidates: list[Path] = []
    run_id = str(payload.get("run_id") or "").strip()
    if run_id:
        run_dir = (base / "output_runs" / run_id).resolve()
        if account:
            candidates.append(run_dir / "accounts" / account / "candidate_filter_trace.jsonl")
        else:
            candidates.extend(sorted(run_dir.glob("accounts/*/candidate_filter_trace.jsonl")))
    candidates.append(base / "output_shared" / "reports" / "candidate_filter_trace.jsonl")
    candidates.append(base / "output_shared" / "agent_tools" / "reports" / "candidate_filter_trace.jsonl")
    out: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen or not resolved.exists() or not resolved.is_file():
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def _candidate_filter_diagnostic_row(row: dict[str, Any], *, scope: dict[str, str | None]) -> dict[str, Any]:
    return {
        "run_id": row.get("run_id") or scope.get("run_id"),
        "account": row.get("account") or scope.get("account"),
        "symbol": row.get("symbol"),
        "option_type": row.get("option_type"),
        "function": row.get("function"),
        "status": row.get("status"),
        "stage": row.get("stage"),
        "rule": row.get("rule"),
        "metric_value": row.get("metric_value"),
        "threshold": row.get("threshold"),
        "contract_symbol": row.get("contract_symbol"),
        "expiration": row.get("expiration"),
        "strike": row.get("strike"),
        "message": row.get("message"),
        "source": "candidate_filter_trace",
    }


def _close_advice_snapshot_rows(ctx: AgentToolContext, payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    tool_payload = {
        "config_key": payload.get("config_key"),
        "config_path": payload.get("config_path"),
        "account": payload.get("account"),
        "symbol": payload.get("symbol"),
        "option_type": payload.get("option_type"),
        "side": payload.get("side"),
        "strike": payload.get("strike"),
        "expiration": payload.get("expiration"),
        "limit": MAX_MATERIALIZED_ROWS,
    }
    try:
        data, tool_warnings, _meta = _call_close_advice_read_tool(
            {key: value for key, value in tool_payload.items() if value not in (None, "")},
            load_runtime_config=ctx.load_runtime_config,
            resolve_output_root=ctx.resolve_output_root,
            repo_base=ctx.repo_base,
            mask_path=ctx.mask_path,
        )
    except AgentToolError as exc:
        if exc.code in {"DEPENDENCY_MISSING", "READ_ERROR"}:
            return [], [f"close_advice_snapshot missing: {exc.message}"]
        raise
    rows = [_close_advice_snapshot_row(row) for row in data.get("rows") or [] if isinstance(row, dict)]
    warnings = [str(item) for item in tool_warnings if str(item).strip()]
    if not rows:
        warnings.append("close_advice_snapshot empty: no close-advice rows matched")
    return rows, warnings


def _close_advice_snapshot_row(row: dict[str, Any]) -> dict[str, Any]:
    expiration = row.get("expiration") or row.get("expiration_ymd")
    out = {
        "account": row.get("account"),
        "position_id": _position_identity(row),
        "advice_run_id": row.get("source_run_id"),
        "symbol": row.get("symbol"),
        "option_type": row.get("option_type"),
        "side": row.get("side") or row.get("position_side"),
        "expiration": expiration,
        "strike": row.get("strike"),
        "contracts_open": row.get("contracts_open"),
        "tier": row.get("tier"),
        "close_action": row.get("close_action"),
        "evaluation_status": row.get("evaluation_status"),
        "quote_status": row.get("quote_status"),
        "reason": row.get("reason"),
        "realized_if_close": row.get("realized_if_close"),
        "remaining_premium": row.get("remaining_premium"),
        "dte": row.get("dte"),
        "strategy_family": row.get("strategy_family"),
        "risk_model": row.get("risk_model"),
    }
    return {field: out.get(field) for field in CLOSE_ADVICE_SNAPSHOT_FIELDS}


def _runtime_tick_status_rows(ctx: AgentToolContext, payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        data, tool_warnings, _meta = _call_runtime_status_tool(
            {
                "config_key": payload.get("config_key"),
                "config_path": payload.get("config_path"),
                "data_config": payload.get("data_config"),
                "accounts": [payload.get("account")] if payload.get("account") else payload.get("accounts"),
                "run_id": payload.get("run_id"),
                "max_notification_chars": 0,
            },
            load_runtime_config=ctx.load_runtime_config,
            normalize_accounts=ctx.normalize_accounts,
            accounts_from_config=ctx.accounts_from_config,
            read_json_object_or_empty=ctx.read_json_object_or_empty,
            repo_base=ctx.repo_base,
            mask_path=ctx.mask_path,
        )
    except AgentToolError as exc:
        return [], [f"runtime_tick_status unavailable: {exc.message}"]
    rows = _runtime_tick_status_rows_from_data(data)
    warnings = [str(item) for item in tool_warnings if str(item).strip()]
    if not rows:
        warnings.append("runtime_tick_status empty: runtime_status returned no account rows")
    return rows, warnings


def _call_close_advice_read_tool(payload: dict[str, Any], **kwargs: Any) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    from src.application.agent_tool_close_advice_read import close_advice_read_tool

    return close_advice_read_tool(payload, **kwargs)


def _call_runtime_status_tool(payload: dict[str, Any], **kwargs: Any) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    from src.application.agent_tool_runtime_status import runtime_status_tool

    return runtime_status_tool(payload, **kwargs)


def _runtime_tick_status_rows_from_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    config = data.get("config") if isinstance(data.get("config"), dict) else {}
    accounts_raw = config.get("accounts")
    accounts = [str(item) for item in accounts_raw] if isinstance(accounts_raw, list) else []
    if not accounts:
        accounts = ["all"]
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    freshness = data.get("freshness") if isinstance(data.get("freshness"), dict) else {}
    account_status = data.get("accounts") if isinstance(data.get("accounts"), dict) else {}
    latest_run_id = _run_id_from_runtime_summary(summary)
    market = str(config.get("config_key") or "").strip().upper()
    rows: list[dict[str, Any]] = []
    for account in accounts:
        account_payload = account_status.get(account) if isinstance(account_status.get(account), dict) else {}
        notification = account_payload.get("notification") if isinstance(account_payload.get("notification"), dict) else {}
        rows.append(
            {
                "market": market,
                "account": account,
                "latest_run_id": latest_run_id,
                "latest_status": summary.get("latest_status"),
                "freshness_status": freshness.get("status") or summary.get("freshness_status"),
                "freshness_age_seconds": freshness.get("age_seconds"),
                "notification_exists": bool(notification.get("exists")) if notification else None,
                "warning_count": summary.get("warning_count"),
                "warning_codes": summary.get("warning_codes") or [],
                "source": "runtime_status",
            }
        )
    return rows


def _run_id_from_runtime_summary(summary: dict[str, Any]) -> str | None:
    for key in ("latest_run_path", "latest_scanned_run_path"):
        text = str(summary.get(key) or "").strip()
        if text:
            return Path(text).name
    return None


def _quote_freshness_rows(*, assignment_rows: Any, runtime_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    del runtime_rows
    if not isinstance(assignment_rows, list):
        return []
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in assignment_rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip()
        quote_status = str(row.get("quote_status") or "").strip()
        if not symbol and not quote_status:
            continue
        source = str(row.get("quote_source") or "assigned_stock").strip()
        account = str(row.get("account") or "").strip()
        market = _market_from_symbol(symbol)
        key = (symbol, market, source, account)
        grouped[key] = {
            "symbol": symbol,
            "market": market,
            "source": source,
            "quote_status": quote_status or None,
            "spot": row.get("spot"),
            "spot_time": row.get("spot_time"),
            "account": account,
            "view": "assigned_stock_position_pnl",
        }
    return sorted(grouped.values(), key=lambda item: (str(item.get("symbol")), str(item.get("account")), str(item.get("source"))))


def _market_from_symbol(symbol: Any) -> str:
    text = str(symbol or "").strip().upper()
    if text.endswith(".HK"):
        return "HK"
    if text:
        return "US"
    return ""


def _position_identity(row: dict[str, Any]) -> str:
    parts = [
        row.get("account"),
        row.get("symbol"),
        row.get("side") or row.get("position_side"),
        row.get("option_type"),
        row.get("expiration") or row.get("expiration_ymd"),
        row.get("strike"),
    ]
    return "|".join(str(part or "-") for part in parts)


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
            raise _classified_sqlite_error(sql=sql, exc=exc, materialized_views=views) from exc
        columns = [str(item[0]) for item in (cursor.description or [])]
        result_rows = [{column: _public_cell_value(row[column]) for column in columns} for row in cursor.fetchall()]
        return result_rows, columns, sorted(views_used)
    finally:
        conn.close()


def _query_explain_and_evidence(
    *,
    sql: str,
    rows: list[dict[str, Any]],
    columns: list[str],
    views_used: list[str],
    materialization_warnings: list[str] | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    aggregations, aggregation_warnings = _query_aggregation_explain(sql=sql, views_used=views_used)
    coverage = _query_coverage(rows)
    freshness = _query_freshness(views_used)
    diagnostics = _query_diagnostics(
        rows=rows,
        views_used=views_used,
        warnings=materialization_warnings or [],
    )
    grain = _query_group_by_fields(sql)
    query_explain = {
        "views_used": views_used,
        "grain": grain,
        "aggregations": aggregations,
        "warnings": aggregation_warnings,
        "coverage": coverage,
        "diagnostics": diagnostics,
    }
    evidence = {
        "cells": _cell_refs(rows),
        "coverage": {"views": views_used, **coverage},
        "freshness": freshness,
        "aggregation_policy": [
            {
                "field": str(item.get("field") or ""),
                "function": str(item.get("function") or ""),
                "policy": str(item.get("policy") or ""),
                "status": str(item.get("status") or ""),
            }
            for item in aggregations
        ],
        "diagnostics": diagnostics,
        "columns": columns,
    }
    return query_explain, aggregation_warnings, evidence


def _query_diagnostics(
    *,
    rows: list[dict[str, Any]],
    views_used: list[str],
    warnings: list[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    warning_records = _diagnostic_records_from_warnings(views_used=views_used, warnings=warnings)
    records.extend(warning_records)
    warning_views = {str(item.get("view") or "") for item in warning_records}

    for view_name in views_used:
        if view_name not in {
            "candidate_filter_diagnostics",
            "close_advice_snapshot",
            "runtime_tick_status",
            "quote_freshness",
        }:
            continue
        if view_name in warning_views:
            continue
        view_records = _diagnostic_records_from_rows(view_name=view_name, rows=rows)
        records.extend(view_records)
    return records[:20]


def _diagnostic_records_from_warnings(
    *,
    views_used: list[str],
    warnings: list[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for warning in warnings:
        text = str(warning or "").strip()
        if not text:
            continue
        lower = text.lower()
        view_name = ""
        for candidate in (
            "candidate_filter_diagnostics",
            "close_advice_snapshot",
            "runtime_tick_status",
            "quote_freshness",
        ):
            if lower.startswith(candidate):
                view_name = candidate
                break
        if not view_name or view_name not in views_used:
            continue
        status = "diagnostic_missing"
        boundary = "cannot infer diagnostic root cause"
        if "read_error" in lower:
            status = "read_error"
            boundary = "diagnostic source read failed"
        elif " empty:" in f" {lower}" or lower.endswith(" empty"):
            status = "empty_artifact"
            boundary = "diagnostic source had no rows"
        elif "unavailable:" in lower:
            status = "diagnostic_missing"
            boundary = "runtime diagnostic source unavailable"
        records.append(
            {
                "view": view_name,
                "status": status,
                "severity": "warning",
                "summary": _diagnostic_warning_summary(view_name=view_name, warning=text, status=status),
                "answer_boundary": boundary,
            }
        )
    return records


def _diagnostic_records_from_rows(*, view_name: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if _rows_represent_no_matches(rows):
        return [
            {
                "view": view_name,
                "status": "no_matching_rows",
                "severity": "warning",
                "summary": f"{view_name} returned no matching diagnostic rows",
                "answer_boundary": "cannot infer absence of problem from empty diagnostic result",
            }
        ]
    if view_name == "candidate_filter_diagnostics":
        return _candidate_diagnostic_records(rows)
    if view_name == "close_advice_snapshot":
        return _close_advice_diagnostic_records(rows)
    if view_name == "runtime_tick_status":
        return _runtime_diagnostic_records(rows)
    if view_name == "quote_freshness":
        return _quote_diagnostic_records(rows)
    return []


def _rows_represent_no_matches(rows: list[dict[str, Any]]) -> bool:
    if not rows:
        return True
    if len(rows) == 1:
        row = rows[0]
        count_fields = [key for key in row if key.lower() in {"count", "row_count", "cnt"} or key.lower().startswith("count_")]
        if count_fields and all(_float_or_none(row.get(key)) == 0 for key in count_fields):
            return True
    return False


def _candidate_diagnostic_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    statuses = {str(row.get("status") or "").strip().lower() for row in rows if isinstance(row, dict)}
    rejection_statuses = {"reject", "rejected", "filtered", "excluded", "blocked", "skip", "skipped"}
    status = "observed_rejection" if statuses & rejection_statuses else "observed_candidate_diagnostic"
    rules = _sorted_unique_row_values(rows, "rule")
    return [
        {
            "view": "candidate_filter_diagnostics",
            "status": status,
            "severity": "info",
            "accounts": _sorted_unique_row_values(rows, "account"),
            "symbols": _sorted_unique_row_values(rows, "symbol"),
            "observed_rules": rules,
            "summary": _candidate_diagnostic_summary(status=status, rules=rules),
            "answer_boundary": "observed_filter_evidence_only",
        }
    ]


def _close_advice_diagnostic_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions = _sorted_unique_row_values(rows, "close_action")
    tiers = _sorted_unique_row_values(rows, "tier")
    return [
        {
            "view": "close_advice_snapshot",
            "status": "observed_close_advice",
            "severity": "info",
            "accounts": _sorted_unique_row_values(rows, "account"),
            "symbols": _sorted_unique_row_values(rows, "symbol"),
            "observed_actions": actions,
            "observed_tiers": tiers,
            "summary": _close_advice_diagnostic_summary(actions=actions, tiers=tiers),
            "answer_boundary": "recorded_close_policy_evidence_only",
        }
    ]


def _runtime_diagnostic_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    statuses = {str(row.get("latest_status") or "").strip().lower() for row in rows if isinstance(row, dict)}
    freshness = {str(row.get("freshness_status") or "").strip().lower() for row in rows if isinstance(row, dict)}
    warning_codes = {
        str(code or "").strip().lower()
        for row in rows
        if isinstance(row, dict)
        for code in _jsonish_list(row.get("warning_codes"))
    }
    notification_values = {row.get("notification_exists") for row in rows if isinstance(row, dict)}
    status = "observed_runtime_status"
    severity = "info"
    if statuses & {"failed", "error", "failed_run", "exec_failed"}:
        status = "observed_run_failure"
        severity = "warning"
    elif statuses & {"skip", "skipped", "locked", "outside_window"}:
        status = "observed_scheduler_skip"
        severity = "warning"
    elif warning_codes & {"no_candidates", "empty_candidates", "candidate_empty"}:
        status = "observed_no_candidates"
    elif False in notification_values:
        status = "observed_notification_missing"
        severity = "warning"
    elif freshness & {"missing", "stale", "unknown", "failed", "error"}:
        status = "observed_runtime_freshness_gap"
        severity = "warning"
    return [
        {
            "view": "runtime_tick_status",
            "status": status,
            "severity": severity,
            "accounts": _sorted_unique_row_values(rows, "account"),
            "summary": _runtime_diagnostic_summary(status=status),
            "answer_boundary": "observed_runtime_status_only",
        }
    ]


def _quote_diagnostic_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    quote_statuses = {str(row.get("quote_status") or "").strip().lower() for row in rows if isinstance(row, dict)}
    bad = quote_statuses & {"missing", "missing_quote", "stale", "unknown", "failed", "error"}
    return [
        {
            "view": "quote_freshness",
            "status": "observed_quote_freshness_gap" if bad else "observed_quote_freshness",
            "severity": "warning" if bad else "info",
            "accounts": _sorted_unique_row_values(rows, "account"),
            "symbols": _sorted_unique_row_values(rows, "symbol"),
            "quote_statuses": sorted(quote_statuses),
            "summary": _quote_diagnostic_summary(has_gap=bool(bad)),
            "answer_boundary": "quote_dependent_calculations_only",
        }
    ]


def _diagnostic_warning_summary(*, view_name: str, warning: str, status: str) -> str:
    if status == "read_error":
        return f"{view_name} diagnostic source could not be read"
    if status == "empty_artifact":
        return f"{view_name} diagnostic source had no rows"
    message = warning.partition(":")[2].strip()
    return message or f"{view_name} diagnostic source is missing"


def _candidate_diagnostic_summary(*, status: str, rules: list[Any]) -> str:
    if status == "observed_rejection":
        suffix = f" by rules: {', '.join(str(item) for item in rules[:5])}" if rules else ""
        return f"candidate diagnostic contains observed rejection/filter evidence{suffix}"
    return "candidate diagnostic rows were observed"


def _close_advice_diagnostic_summary(*, actions: list[Any], tiers: list[Any]) -> str:
    parts: list[str] = []
    if actions:
        parts.append("actions=" + ",".join(str(item) for item in actions[:5]))
    if tiers:
        parts.append("tiers=" + ",".join(str(item) for item in tiers[:5]))
    suffix = f" ({'; '.join(parts)})" if parts else ""
    return f"close-advice snapshot rows were observed{suffix}"


def _runtime_diagnostic_summary(*, status: str) -> str:
    return {
        "observed_run_failure": "runtime status indicates a failed latest run",
        "observed_scheduler_skip": "runtime status indicates the latest run was skipped",
        "observed_no_candidates": "runtime status indicates no candidate output",
        "observed_notification_missing": "runtime status indicates notification output is missing",
        "observed_runtime_freshness_gap": "runtime status indicates stale or missing freshness",
    }.get(status, "runtime status rows were observed")


def _quote_diagnostic_summary(*, has_gap: bool) -> str:
    if has_gap:
        return "quote freshness rows indicate stale or missing quote data"
    return "quote freshness rows were observed"


def _jsonish_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _query_aggregation_explain(*, sql: str, views_used: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    aggregations: list[dict[str, Any]] = []
    warnings: list[str] = []
    for match in re.finditer(r"(?is)\b(sum|avg|min|max|count|total)\s*\(\s*(?:distinct\s+)?([A-Za-z_][A-Za-z0-9_\.]*|\*)", sql):
        function_name = match.group(1).lower()
        raw_field = match.group(2)
        field = raw_field.split(".")[-1] if raw_field != "*" else "*"
        field_meta = _field_semantics_for_query_field(field, views_used)
        aggregation_policy = str(field_meta.get("aggregation") or "none") if field_meta else "unknown"
        status = "ok"
        warning: str | None = None
        if raw_field == "*":
            policy = "allowed"
        elif field_meta and str(field_meta.get("type") or "") == "rate" and function_name in {"avg", "sum", "total"}:
            policy = "invalid_rate_aggregation"
            status = "warning"
            warning = (
                f"{function_name}({field}) is unsafe for return-rate fields; recompute as "
                "sum(money numerator) / sum(cash_secured_cny) when aggregating."
            )
        elif aggregation_policy in {"sum", "count", "min", "max", "latest", "weighted_recompute"}:
            policy = "allowed" if function_name in {"sum", "count", "min", "max", "total"} else "review"
        else:
            policy = aggregation_policy
        item = {
            "field": field,
            "function": function_name,
            "field_type": str(field_meta.get("type") or "unknown") if field_meta else "unknown",
            "aggregation": aggregation_policy,
            "policy": policy,
            "status": status,
        }
        if warning:
            item["warning"] = warning
            warnings.append(warning)
        aggregations.append(item)
    return aggregations, warnings


def _field_semantics_for_query_field(field: str, views_used: list[str]) -> dict[str, Any]:
    for view_name in views_used:
        spec = VIEW_SPECS.get(view_name) or {}
        field_semantics = spec.get("field_semantics") if isinstance(spec.get("field_semantics"), dict) else {}
        meta = field_semantics.get(field)
        if isinstance(meta, dict):
            return meta
    for spec in VIEW_SPECS.values():
        field_semantics = spec.get("field_semantics") if isinstance(spec.get("field_semantics"), dict) else {}
        meta = field_semantics.get(field)
        if isinstance(meta, dict):
            return meta
    return {}


def _query_group_by_fields(sql: str) -> list[str]:
    match = re.search(r"(?is)\bgroup\s+by\s+(.+?)(?:\border\s+by\b|\blimit\b|\bhaving\b|$)", sql)
    if not match:
        return []
    fields: list[str] = []
    for raw in match.group(1).split(","):
        field = _strip_identifier(raw.strip().split()[-1]).split(".")[-1]
        if field and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", field):
            fields.append(field)
    return fields


def _query_coverage(rows: list[dict[str, Any]]) -> dict[str, list[Any]]:
    return {
        "months": _sorted_unique_row_values(rows, "month"),
        "accounts": _sorted_unique_row_values(rows, "account"),
        "symbols": _sorted_unique_row_values(rows, "symbol"),
        "currencies": _sorted_unique_row_values(rows, "currency"),
    }


def _sorted_unique_row_values(rows: list[dict[str, Any]], column: str) -> list[Any]:
    values = {row.get(column) for row in rows if isinstance(row, dict) and row.get(column) not in (None, "")}
    return sorted(values, key=lambda item: str(item))


def _query_freshness(views_used: list[str]) -> list[dict[str, str]]:
    freshness: list[dict[str, str]] = []
    for view_name in views_used:
        spec = VIEW_SPECS.get(view_name) or {}
        freshness.append(
            {
                "view": view_name,
                "source": str(spec.get("semantic_source") or ""),
                "freshness": str(spec.get("freshness") or ""),
                "status": "declared",
            }
        )
    return freshness


def _classified_sqlite_error(
    *,
    sql: str,
    exc: sqlite3.Error,
    materialized_views: dict[str, list[dict[str, Any]]],
) -> AgentToolError:
    message = str(exc)
    unknown_column = _parse_sqlite_unknown_column(message)
    if unknown_column:
        referenced_views = _referenced_analysis_views(sql)
        available_fields = _available_fields_by_view(referenced_views or materialized_views.keys() or VIEW_SPECS.keys())
        suggestions = _suggest_fields_for_unknown_column(unknown_column, available_fields)
        return AgentToolError(
            code="INPUT_ERROR",
            message=f"analysis_query failed: unknown column {unknown_column}",
            hint="Use analysis_catalog to inspect available fields, then retry with listed columns only.",
            details={
                "preflight": {
                    "ok": False,
                    "error_code": "UNKNOWN_COLUMN",
                    "message": f"column {unknown_column} does not exist in referenced analysis views",
                    "suggestions": suggestions,
                    "available_fields": available_fields,
                },
                "error_code": "UNKNOWN_COLUMN",
                "unknown_column": unknown_column,
                "referenced_views": referenced_views,
                "suggestions": suggestions,
                "available_fields": available_fields,
            },
        )
    unknown_view = _parse_sqlite_unknown_table(message)
    if unknown_view:
        return AgentToolError(
            code="INPUT_ERROR",
            message=f"analysis_query failed: unknown analysis view {unknown_view}",
            hint="Use analysis_catalog to inspect whitelisted view names.",
            details={
                "preflight": {
                    "ok": False,
                    "error_code": "UNKNOWN_VIEW",
                    "message": f"view {unknown_view} is not whitelisted",
                    "suggestions": _suggest_view_names(unknown_view),
                    "available_views": sorted(VIEW_SPECS),
                },
                "error_code": "UNKNOWN_VIEW",
                "unknown_view": unknown_view,
                "suggestions": _suggest_view_names(unknown_view),
                "available_views": sorted(VIEW_SPECS),
            },
        )
    return AgentToolError(
        code="INPUT_ERROR",
        message=f"analysis_query failed: {exc}",
        hint="Use analysis_catalog to inspect available view names and fields.",
    )


def _parse_sqlite_unknown_column(message: str) -> str | None:
    match = re.search(r"(?i)\bno such column:\s*([^\s]+)", message)
    if not match:
        return None
    return _strip_identifier(match.group(1)).split(".")[-1]


def _parse_sqlite_unknown_table(message: str) -> str | None:
    match = re.search(r"(?i)\bno such table:\s*([^\s]+)", message)
    if not match:
        return None
    return _strip_identifier(match.group(1))


def _strip_identifier(value: str) -> str:
    return str(value or "").strip().strip('"`[]')


def _referenced_analysis_views(sql: str) -> list[str]:
    views: list[str] = []
    for match in re.finditer(r"(?is)\b(?:from|join)\s+(?:\"([^\"]+)\"|`([^`]+)`|\[([^\]]+)\]|([A-Za-z_][A-Za-z0-9_]*))", sql):
        candidate = next((item for item in match.groups() if item), "")
        if candidate in VIEW_SPECS and candidate not in views:
            views.append(candidate)
    return views


def _available_fields_by_view(view_names: Iterable[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for view_name in view_names:
        spec = VIEW_SPECS.get(str(view_name))
        if not spec:
            continue
        out[str(view_name)] = [str(field) for field in spec.get("fields") or ()]
    return out


def _suggest_fields_for_unknown_column(unknown_column: str, available_fields: dict[str, list[str]]) -> list[str]:
    reference_view_count = len(available_fields)
    preferred = _preferred_unknown_column_replacements(unknown_column)
    suggestions: list[tuple[float, str]] = []
    seen: set[str] = set()
    for view_name, fields in available_fields.items():
        for field in fields:
            formatted = field if reference_view_count <= 1 else f"{view_name}.{field}"
            if formatted in seen:
                continue
            score = _field_similarity_score(unknown_column, field)
            if field in preferred:
                score += 0.75 - (preferred.index(field) * 0.05)
            if score < 0.58 and field not in preferred:
                continue
            seen.add(formatted)
            suggestions.append((score, formatted))
    suggestions.sort(key=lambda item: (-item[0], item[1]))
    return [field for _score, field in suggestions[:6]]


def _preferred_unknown_column_replacements(unknown_column: str) -> list[str]:
    normalized = _normalized_identifier(unknown_column)
    if normalized in {"netcashflow", "cashflow", "netcashflowcny"}:
        return [
            "net_income_cny",
            "net_income_by_ccy",
            "net_return_rate",
            "net_cashflow_gross",
            "net_cashflow_gross_cny",
        ]
    if normalized in {"returnrate", "totalreturnrate"}:
        return ["net_return_rate", "premium_return_rate", "realized_return_rate"]
    if normalized in {"totalreturn", "totalincome"}:
        return ["net_income_cny", "premium_income_cny", "realized_pnl_cny"]
    return []


def _field_similarity_score(unknown_column: str, field: str) -> float:
    unknown = _normalized_identifier(unknown_column)
    candidate = _normalized_identifier(field)
    score = SequenceMatcher(None, unknown, candidate).ratio()
    if "cashflow" in unknown and ("income" in candidate or "cashflow" in candidate):
        score += 0.12
    if "return" in unknown and "return" in candidate:
        score += 0.12
    if "pnl" in unknown and "pnl" in candidate:
        score += 0.12
    return score


def _normalized_identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _suggest_view_names(unknown_view: str) -> list[str]:
    normalized = _normalized_identifier(unknown_view)
    suggestions: list[tuple[float, str]] = []
    for view_name in VIEW_SPECS:
        score = SequenceMatcher(None, normalized, _normalized_identifier(view_name)).ratio()
        if score >= 0.45:
            suggestions.append((score, view_name))
    suggestions.sort(key=lambda item: (-item[0], item[1]))
    return [view_name for _score, view_name in suggestions[:6]]


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
        "schema_version": "analysis_catalog.output.v2",
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
