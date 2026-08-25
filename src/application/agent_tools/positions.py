from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.application.agent_tools.operations_impl import (
    normalize_option_positions_read_input,
    option_positions_read_tool,
)
from src.application.agent_tools.materialization_impl import option_performance_report_tool
from src.application.agent_tools.base import AgentTool, build_agent_tool
from src.application.positions.inspection import build_lot_event_history
from src.application.positions.inspection import inspect_projection_state
from src.application.ledger.api import (
    ledger_store_payload,
    ledger_store_write_guard,
    list_position_rows,
    open_position_ledger_from_runtime_config,
)
from src.application.ledger.api import open_performance_evidence_repository
from src.application.agent_tool_config import load_runtime_config
from src.application.agent_tool_contracts import AgentToolError, mask_path
from src.application.quality.gate import QualityGateBlocked, assert_quality_allows
from domain.domain.option_position_identity import normalize_account
from src.application.agent_tools.runtime_helpers import normalize_broker
from src.application.positions.assigned_stock_quotes import refresh_assigned_stock_quote_snapshots as refresh_assigned_stock_quotes
from src.application.agent_tool_config import repo_base
from src.application.ledger.api import open_position_ledger_from_data_config as resolve_option_positions_repo
from src.application.agent_tools.runtime_helpers import resolve_public_data_config_path
from src.application.performance.service import build_option_period_performance
from src.application.wheel import (
    build_wheel_read_model,
    cancel_wheel_call_intent,
    confirm_wheel_call_linkage,
    create_wheel_call_intent,
    end_wheel_lifecycle,
    load_wheel_candidate_snapshot,
    reject_wheel_call_linkage,
)
from src.application.wheel.capacity import load_shared_coverage_fact


_OPTION_PERFORMANCE_OUTPUT_CONTRACT: dict[str, Any] = {
    "evidence_type": "aggregate",
    "bounded_projection": "contract_fields",
    "coverage": "source_declared",
    "freshness": "source_declared",
    "pagination": {"mode": "none"},
    "schema_version": "option_performance_report.output.v1",
    "source_label": "OM 本地账本 + 显式估值/汇率证据",
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
        "cash.option_net_cashflow",
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
        "cashflow_return.capital_basis",
        "cashflow_return.period_duration_days",
        "cashflow_return.capital_days_by_currency",
        "cashflow_return.average_incremental_capital_by_currency",
        "cashflow_return.period_return",
        "cashflow_return.annualized_return",
        "cashflow_return.coverage",
        "assignment_lifecycle.period",
        "breakdowns.monthly",
        "breakdowns.accounts",
        "breakdowns.symbols",
        "presentation",
    ],
    "missing_data_fields": [
        "quality.missing",
        "quality.warnings",
        "capital.coverage.missing",
        "cashflow_return.coverage.missing_by_currency",
        "cashflow_return.coverage.global_missing",
        "assignment_lifecycle.review",
    ],
    "freshness_fields": [
        "freshness.status",
        "freshness.as_of",
        "period.status",
        "evidence.schema_state",
        "evidence.collection.status",
    ],
    "model_preview_fields": [
        "presentation",
        "period",
        "scope",
        "evidence",
    ],
    "model_value_fields": [
        "presentation",
        "period",
        "scope",
        "evidence.schema_state",
    ],
    "model_missing_data_fields": ["presentation.limitations"],
}

_OPTION_POSITIONS_LIST_OUTPUT_CONTRACT: dict[str, Any] = {
    "evidence_type": "collection",
    "bounded_projection": "contract_fields",
    "coverage": "primary_rows",
    "freshness": "source_declared",
    "pagination": {"mode": "none"},
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

_OPTION_POSITIONS_EVENTS_OUTPUT_CONTRACT = {
    "schema_version": "option_positions_read.events_output.v1",
    "source_label": "OM canonical SQLite trade_events",
    "evidence_type": "collection",
    "bounded_projection": "contract_fields",
    "coverage": "source_declared",
    "freshness": "source_declared",
    "primary_rows": "rows",
    "row_count_field": "returned_count",
    "stable_order": "trade_time_ms_desc,event_id_desc",
    "pagination": {
        "mode": "keyset",
        "cursor_ttl_seconds": 1800,
        "snapshot_boundary": "ingest_seq",
        "order": ["trade_time_ms DESC", "event_id DESC"],
    },
    "freshness_fields": ["as_of"],
    "fact_fields": [
        "rows",
        "requested_limit",
        "returned_count",
        "total_count",
        "stream_id",
        "as_of",
        "has_more",
        "snapshot_exhausted",
        "next_cursor",
    ],
    "model_preview_fields": [
        "requested_limit",
        "returned_count",
        "total_count",
        "stream_id",
        "as_of",
        "has_more",
        "snapshot_exhausted",
        "next_cursor",
        "rows",
    ],
}

_OPTION_POSITIONS_ASSIGNED_STOCK_OUTPUT_CONTRACT: dict[str, Any] = {
    "evidence_type": "collection",
    "bounded_projection": "contract_fields",
    "coverage": "primary_rows",
    "freshness": "source_declared",
    "pagination": {"mode": "none"},
    "schema_version": "option_positions_read.assigned_stock_output.v2",
    "source_label": "OM 本地 SQLite assigned_stock_events + trade_events",
    "primary_rows": "rows",
    "row_count_field": "row_count",
    "freshness_fields": [
        "rows[].quote_status",
        "quote_refresh.status",
        "quote_refresh.quote_source",
        "quote_refresh.route_source",
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
        "rows[].wheel.stock_lot_id",
        "rows[].wheel.lifecycle_status",
        "rows[].wheel.phase",
        "rows[].wheel.integrity_status",
        "rows[].wheel.reason_codes",
        "rows[].wheel.shares_remaining",
        "rows[].wheel.batch_generation_hash",
        "rows[].wheel.projection_hash",
        "rows[].wheel.start_event_id",
        "rows[].wheel.terminal_event_id",
        "rows[].wheel.active_call_lot_ids",
        "rows[].wheel.active_intent_ids",
        "rows[].wheel.candidate",
        "assigned_stock_review_rows[].status",
        "quote_refresh.route_source",
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
    try:
        normalized = normalize_option_positions_read_input(payload)
    except ValueError as exc:
        raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc

    def assert_quality(*, account: str | None, market: str | None) -> None:
        try:
            assert_quality_allows(
                "option_position_report",
                account=account,
                market=market,
            )
        except QualityGateBlocked as exc:
            raise AgentToolError(
                code="QUALITY_GATE_BLOCKED",
                message=str(exc),
                details={
                    "consumer": exc.consumer,
                    "reason_code": exc.reason_code,
                    "blocked_by": list(exc.blocked_by),
                    "retryable": False,
                },
            ) from exc

    action = _option_positions_action(normalized)
    if action != "events":
        assert_quality(
            account=str(normalized.get("account") or "").strip().lower() or None,
            market=str(normalized.get("config_key") or "").strip().lower() or None,
        )

    def admit_event_query(
        query: dict[str, Any], authority_scope: dict[str, Any]
    ) -> None:
        assert_quality(
            account=str(query.get("account") or "").strip().lower() or None,
            market=str(authority_scope.get("market") or "").strip().lower() or None,
        )

    return option_positions_read_tool(
        normalized,
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
        admit_event_query=admit_event_query if action == "events" else None,
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
    try:
        normalized = normalize_option_positions_read_input(payload)
    except ValueError:
        return None
    action = _option_positions_action(normalized)
    if action == "list":
        return _OPTION_POSITIONS_LIST_OUTPUT_CONTRACT
    if action == "assigned-stock":
        return _OPTION_POSITIONS_ASSIGNED_STOCK_OUTPUT_CONTRACT
    if action == "events":
        return _OPTION_POSITIONS_EVENTS_OUTPUT_CONTRACT
    return None


def _validate_option_positions_input(payload: dict[str, Any]) -> None:
    try:
        normalize_option_positions_read_input(payload)
    except ValueError as exc:
        raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc


def _wheel_now_ms(payload: Mapping[str, Any]) -> int:
    return int(payload.get("as_of_ms") or datetime.now(timezone.utc).timestamp() * 1000)


def _wheel_runtime(payload: dict[str, Any]) -> tuple[Path, dict[str, Any], Any, dict[str, Any]]:
    config_path, cfg = load_runtime_config(
        config_key=payload.get("config_key"),
        config_path=payload.get("config_path"),
    )
    portfolio = cfg.get("portfolio")
    portfolio = portfolio if isinstance(portfolio, dict) else {}
    data_config = resolve_public_data_config_path(payload, portfolio)
    runtime_root = str(payload.get("runtime_root") or "").strip() or None
    if bool(payload.get("apply", False)):
        guard = ledger_store_write_guard(
            data_config,
            runtime_root=runtime_root,
            config_path=config_path,
        )
        if not guard.get("ok"):
            raise AgentToolError(
                code="LEDGER_STORE_GUARD_FAILED",
                message="Wheel write target is ambiguous",
                details={"errors": guard.get("errors") or []},
            )
    _resolved, repo = open_position_ledger_from_runtime_config(
        base=repo_base(),
        cfg=cfg,
        data_config=data_config,
        config_path=config_path,
        runtime_root=runtime_root,
    )
    store = ledger_store_payload(data_config, repo)
    return config_path, cfg, repo, {
        "config_path": _mask_path_str(config_path),
        "ledger_store": {
            **store,
            "data_config_path": _mask_path_str(store.get("data_config_path")),
            "sqlite_path": _mask_path_str(store.get("sqlite_path")),
            "runtime_root": _mask_path_str(store.get("runtime_root")),
        },
    }


def _wheel_batch(model: Mapping[str, Any], stock_lot_id: Any) -> dict[str, Any]:
    lot_id = str(stock_lot_id or "").strip()
    matches = [
        dict(item)
        for item in model.get("batches") or []
        if isinstance(item, Mapping) and str(item.get("stock_lot_id") or "").strip() == lot_id
    ]
    if len(matches) != 1:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"Wheel batch must resolve uniquely: {lot_id}",
        )
    return matches[0]


def _wheel_coverage(
    repo: Any,
    cfg: dict[str, Any],
    payload: Mapping[str, Any],
    batch: Mapping[str, Any],
    instant: int,
) -> dict[str, Any]:
    portfolio = cfg.get("portfolio")
    portfolio = portfolio if isinstance(portfolio, dict) else {}
    return load_shared_coverage_fact(
        repo,
        config=cfg,
        account=str(payload.get("account") or ""),
        symbol=str(batch.get("symbol") or ""),
        broker=str(batch.get("broker") or portfolio.get("broker") or "富途"),
        as_of_ms=instant,
        source_identity=str(payload.get("request_id") or ""),
    )


def _wheel_common(payload: Mapping[str, Any], *, instant: int) -> dict[str, Any]:
    return {
        "account": str(payload.get("account") or ""),
        "stock_lot_id": str(payload.get("stock_lot_id") or ""),
        "expected_batch_generation_hash": str(
            payload.get("expected_batch_generation_hash") or ""
        ),
        "request_id": str(payload.get("request_id") or ""),
        "actor": str(payload.get("actor") or ""),
        "apply_changes": bool(payload.get("apply", False)),
        "as_of_ms": instant,
    }


def _wheel_result(call: Any) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    try:
        return call()
    except AgentToolError:
        raise
    except ValueError as exc:
        raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc


def _wheel_end_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    def _run() -> tuple[dict[str, Any], list[str], dict[str, Any]]:
        _config_path, _cfg, repo, meta = _wheel_runtime(payload)
        result = end_wheel_lifecycle(
            repo,
            **_wheel_common(payload, instant=_wheel_now_ms(payload)),
        )
        return result, [], meta

    return _wheel_result(_run)


def _wheel_call_intent_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    def _run() -> tuple[dict[str, Any], list[str], dict[str, Any]]:
        config_path, cfg, repo, meta = _wheel_runtime(payload)
        instant = _wheel_now_ms(payload)
        common = _wheel_common(payload, instant=instant)
        if payload["action"] == "cancel":
            result = cancel_wheel_call_intent(
                repo,
                **common,
                intent_id=str(payload.get("intent_id") or ""),
                broker_order_inactive_confirmed=bool(
                    payload.get("broker_order_inactive_confirmed", False)
                ),
                reason=str(payload.get("reason") or ""),
            )
            return result, [], meta
        snapshot_base = Path(str(payload.get("runtime_root") or config_path.parent)).resolve()
        snapshot = load_wheel_candidate_snapshot(
            base=snapshot_base,
            run_id=str(payload.get("run_id") or ""),
            account=str(payload.get("account") or ""),
        )
        model = build_wheel_read_model(repo, str(payload.get("account") or ""), instant)
        batch = _wheel_batch(model, payload.get("stock_lot_id"))
        result = create_wheel_call_intent(
            repo,
            **common,
            candidate_snapshot=snapshot,
            final_candidate_id=str(payload.get("final_candidate_id") or ""),
            expected_snapshot_hash=str(payload.get("expected_snapshot_hash") or ""),
            expires_at_ms=int(payload.get("expires_at_ms") or 0),
            broker_order_id=str(payload.get("broker_order_id") or "").strip() or None,
            coverage_fact=_wheel_coverage(repo, cfg, payload, batch, instant),
        )
        return result, [], meta

    return _wheel_result(_run)


def _wheel_call_linkage_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    def _run() -> tuple[dict[str, Any], list[str], dict[str, Any]]:
        _config_path, cfg, repo, meta = _wheel_runtime(payload)
        instant = _wheel_now_ms(payload)
        common = _wheel_common(payload, instant=instant)
        args = {
            **common,
            "call_record_id": str(payload.get("call_record_id") or ""),
            "linkage_candidate_id": str(payload.get("linkage_candidate_id") or ""),
            "expected_input_hash": str(payload.get("expected_input_hash") or ""),
        }
        if payload["action"] == "reject":
            result = reject_wheel_call_linkage(
                repo,
                **args,
                reason=str(payload.get("reason") or ""),
            )
            return result, [], meta
        model = build_wheel_read_model(repo, str(payload.get("account") or ""), instant)
        batch = _wheel_batch(model, payload.get("stock_lot_id"))
        result = confirm_wheel_call_linkage(
            repo,
            **args,
            coverage_fact=_wheel_coverage(repo, cfg, payload, batch, instant),
        )
        return result, [], meta

    return _wheel_result(_run)


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
_OPTION_PERFORMANCE_COPILOT_ALL_SCOPE_MARKERS = frozenset({"all", ":all", "__omit__"})


def _normalize_option_performance_copilot_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    for name in ("account", "broker"):
        value = normalized.get(name)
        if name not in normalized or not isinstance(value, str):
            continue
        stripped = value.strip()
        if not stripped:
            raise ValueError(f"{name} must be non-empty when provided")
        if stripped.lower() in _OPTION_PERFORMANCE_COPILOT_ALL_SCOPE_MARKERS:
            normalized.pop(name)
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
    catalog_summary="读取期权表现汇总与收益分解。",
    description=(
        "Primary read-only option performance report. Separates premium activity, cash movement, realized PnL, "
        "period total PnL, assigned-stock lifecycle, and capital efficiency. Supports MTD, YTD, natural month, "
        "natural year, and explicit date ranges. Omit account or broker to aggregate all matching ledger facts; "
        "native-currency amounts remain authoritative and CNY is null when FX evidence is incomplete. "
        "cash.option_trade_cash_gross is signed option-trade cash only and excludes assigned-stock settlement "
        "and sale cash."
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
            "account": {
                "type": "string",
                "description": "Optional account filter. Omit or use all for all accounts.",
            },
            "broker": {
                "type": "string",
                "description": "Optional broker filter. Omit or use all for all brokers.",
            },
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
    catalog_summary="读取授权账户的期权持仓、交易事件与最近平仓记录。",
    description=(
        "Read local option position lots, trade events, lot history, assigned-stock lots, or projection "
        "inspection state. For current exposure use action=list and status=open; preserve account and currency. "
        "For canonical trade history use action=events: each page contains 1-20 rows in stable descending "
        "trade-time order, and next_cursor continues the same signed snapshot without repeating filters. "
        "Do not silently clamp a request above 20: disclose the per-page limit and offer pagination. "
        "When has_more=true, say that more records are available; when snapshot_exhausted=true, say that "
        "the frozen query is exhausted. A missing or expired cursor requires a new query, which may overlap "
        "records already returned. "
        "For the latest N closed trades or close records use action=events, position_effect=close, and limit=N; "
        "omit account when the user asks across all authorized accounts. "
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
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
            "description": "Maximum rows: list allows 1-500; events allows 1-20",
        },
        "cursor": {
            "type": "string",
            "minLength": 1,
            "description": "Events-only opaque continuation cursor",
        },
        "include_total": {
            "type": "boolean",
            "description": "Events-only exact count over the frozen snapshot",
        },
        "position_effect": {
            "type": "string",
            "enum": ["close"],
            "description": "Events-only canonical close-effect filter",
        },
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
        "opend_host": "assigned-stock refresh_quotes optional read-only diagnostic OpenD host override",
        "opend_port": "assigned-stock refresh_quotes optional read-only diagnostic OpenD port override",
        "as_of_ms": "assigned-stock optional as-of timestamp for quote snapshot selection",
    },
    handler=_option_positions_read_tool,
    pure_read=True,
    safe_default_input={"action": "list"},
    examples=(
        {"input": {"config_key": "us", "action": "list", "query": {"account": "lx", "status": "open"}}},
        {"input": {"config_key": "us", "action": "history", "record_id": "rec_xxx"}},
        {"input": {"config_key": "us", "action": "events", "position_effect": "close", "limit": 10}},
    ),
    output_contract={
        "schema_version": "option_positions_read.output",
        "payload_dependent": True,
        "evidence_type": "collection",
        "bounded_projection": "contract_fields",
        "coverage": "primary_rows",
        "freshness": "source_declared",
        "pagination": {"mode": "none"},
    },
    output_contract_resolver=_option_positions_output_contract,
    copilot_input_fields=(
        "config_key", "action", "broker", "account", "status", "query", "limit",
        "exp_within_days", "expiration_month", "expiration_exact", "expiration_before",
        "expiration_after", "record_id", "symbol", "option_type", "side", "strike",
        "exp", "stock_lot_id", "refresh_quotes", "as_of_ms", "cursor", "include_total", "position_effect",
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
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "description": "list: 1-500; events: 1-20",
            },
            "cursor": {
                "type": "string",
                "minLength": 1,
                "description": "Opaque events continuation cursor",
            },
            "include_total": {"type": "boolean"},
            "position_effect": {"type": "string", "enum": ["close"]},
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
        "allOf": [
            {
                "if": {
                    "properties": {"action": {"const": "events"}},
                    "required": ["action"],
                },
                "then": {"properties": {"limit": {"maximum": 20}}},
            },
            {
                "if": {
                    "anyOf": [
                        {"required": ["cursor"]},
                        {"required": ["include_total"]},
                        {"required": ["position_effect"]},
                    ]
                },
                "then": {
                    "properties": {
                        "action": {"const": "events"},
                        "limit": {"maximum": 20},
                    }
                },
            },
        ],
        "additionalProperties": False,
    },
    input_validator=_validate_option_positions_input,
    copilot_input_normalizer=normalize_option_positions_read_input,
)


_WHEEL_COMMON_INPUT: dict[str, Any] = {
    "config_key": {
        "type": "string",
        "enum": ["us", "hk"],
        "required": True,
        "description": "Market runtime config",
    },
    "config_path": "optional explicit runtime config path",
    "data_config": "optional explicit portfolio data config path",
    "runtime_root": "optional explicit runtime root",
    "account": {"type": "string", "minLength": 1, "required": True},
    "stock_lot_id": {"type": "string", "minLength": 1, "required": True},
    "expected_batch_generation_hash": {
        "type": "string",
        "minLength": 1,
        "required": True,
    },
    "request_id": {"type": "string", "minLength": 1, "required": True},
    "actor": {"type": "string", "minLength": 1, "required": True},
    "as_of_ms": {"type": "integer", "minimum": 1},
    "apply": {"type": "boolean", "description": "default false previews only"},
    "confirm": {"type": "boolean", "description": "required true with apply=true"},
}

_WHEEL_WRITE_OUTPUT: dict[str, Any] = {
    "source_label": "OM 本地 SQLite Wheel ledger",
    "fact_fields": [
        "stock_lot_id",
        "event_id",
        "intent_id",
        "call_record_id",
        "request_id",
        "status",
        "lifecycle_status_before",
        "lifecycle_status_after",
        "dry_run",
        "write_applied",
        "audit_id",
    ],
    "missing_data_fields": [],
    "freshness_fields": ["batch_generation_hash"],
}


def _wheel_write_requested(payload: dict[str, Any]) -> bool:
    return bool(payload.get("apply", False))


def _require_wheel_fields(payload: Mapping[str, Any], *fields: str) -> None:
    missing = [field for field in fields if payload.get(field) in (None, "")]
    if missing:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"missing required Wheel fields: {', '.join(missing)}",
        )
    if bool(payload.get("apply", False)) and payload.get("confirm") is not True:
        raise AgentToolError(
            code="CONFIRMATION_REQUIRED",
            message="confirm=true is required when apply=true",
        )


def _validate_wheel_end(payload: dict[str, Any]) -> None:
    _require_wheel_fields(payload)


def _validate_wheel_intent(payload: dict[str, Any]) -> None:
    if payload.get("action") == "create":
        _require_wheel_fields(
            payload,
            "run_id",
            "final_candidate_id",
            "expected_snapshot_hash",
            "expires_at_ms",
        )
        return
    _require_wheel_fields(
        payload,
        "intent_id",
        "reason",
        "broker_order_inactive_confirmed",
    )
    if payload.get("broker_order_inactive_confirmed") is not True:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="broker_order_inactive_confirmed=true is required for cancel",
        )


def _validate_wheel_linkage(payload: dict[str, Any]) -> None:
    required = ["call_record_id", "linkage_candidate_id", "expected_input_hash"]
    if payload.get("action") == "reject":
        required.append("reason")
    _require_wheel_fields(payload, *required)


WHEEL_END_TOOL = build_agent_tool(
    name="wheel_end",
    description="Preview or manually end one Wheel lifecycle. Does not sell stock or close a Call.",
    requires=("runtime_config", "sqlite_data_config"),
    capabilities=("wheel", "local_write"),
    side_effects=("appends_wheel_event",),
    input_schema=dict(_WHEEL_COMMON_INPUT),
    handler=_wheel_end_tool,
    read_only=False,
    risk_level="local_write",
    requires_confirm=True,
    requires_env=("OM_AGENT_ENABLE_WRITE_TOOLS=true for apply=true",),
    safe_default_input={"apply": False},
    write_request_predicate=_wheel_write_requested,
    input_validator=_validate_wheel_end,
    output_contract={"schema_version": "wheel_end.output.v1", **_WHEEL_WRITE_OUTPUT},
    allow_additional_input=False,
)

WHEEL_CALL_INTENT_TOOL = build_agent_tool(
    name="wheel_call_intent",
    description="Preview or create/cancel a local Wheel Call intent. Does not place or cancel broker orders.",
    requires=("runtime_config", "sqlite_data_config", "broker_holdings_read"),
    capabilities=("wheel", "local_write"),
    side_effects=("appends_wheel_event",),
    input_schema={
        **_WHEEL_COMMON_INPUT,
        "action": {
            "type": "string",
            "enum": ["create", "cancel"],
            "required": True,
        },
        "run_id": "create-only candidate run id",
        "final_candidate_id": "create-only final Wheel candidate id",
        "expected_snapshot_hash": "create-only candidate snapshot hash",
        "expires_at_ms": {"type": "integer", "minimum": 1},
        "broker_order_id": "create-only optional broker order id",
        "intent_id": "cancel-only intent id",
        "broker_order_inactive_confirmed": {"type": "boolean"},
        "reason": "cancel-only reason",
    },
    handler=_wheel_call_intent_tool,
    read_only=False,
    risk_level="local_write",
    requires_confirm=True,
    requires_env=("OM_AGENT_ENABLE_WRITE_TOOLS=true for apply=true",),
    safe_default_input={"apply": False},
    write_request_predicate=_wheel_write_requested,
    input_validator=_validate_wheel_intent,
    output_contract={"schema_version": "wheel_call_intent.output.v1", **_WHEEL_WRITE_OUTPUT},
    allow_additional_input=False,
)

WHEEL_CALL_LINKAGE_TOOL = build_agent_tool(
    name="wheel_call_linkage",
    description="Preview or confirm/reject one exact Short Call to Wheel batch attribution.",
    requires=("runtime_config", "sqlite_data_config", "broker_holdings_read"),
    capabilities=("wheel", "local_write"),
    side_effects=("appends_trade_or_wheel_event",),
    input_schema={
        **_WHEEL_COMMON_INPUT,
        "action": {
            "type": "string",
            "enum": ["confirm", "reject"],
            "required": True,
        },
        "call_record_id": {"type": "string", "minLength": 1},
        "linkage_candidate_id": {"type": "string", "minLength": 1},
        "expected_input_hash": {"type": "string", "minLength": 1},
        "reason": "reject-only reason",
    },
    handler=_wheel_call_linkage_tool,
    read_only=False,
    risk_level="local_write",
    requires_confirm=True,
    requires_env=("OM_AGENT_ENABLE_WRITE_TOOLS=true for apply=true",),
    safe_default_input={"apply": False},
    write_request_predicate=_wheel_write_requested,
    input_validator=_validate_wheel_linkage,
    output_contract={"schema_version": "wheel_call_linkage.output.v1", **_WHEEL_WRITE_OUTPUT},
    allow_additional_input=False,
)

TOOLS: tuple[AgentTool, ...] = (
    OPTION_PERFORMANCE_REPORT_TOOL,
    OPTION_POSITIONS_READ_TOOL,
    WHEEL_END_TOOL,
    WHEEL_CALL_INTENT_TOOL,
    WHEEL_CALL_LINKAGE_TOOL,
)


__all__ = [
    "OPTION_PERFORMANCE_REPORT_TOOL",
    "OPTION_POSITIONS_READ_TOOL",
    "WHEEL_CALL_INTENT_TOOL",
    "WHEEL_CALL_LINKAGE_TOOL",
    "WHEEL_END_TOOL",
    "TOOLS",
]
