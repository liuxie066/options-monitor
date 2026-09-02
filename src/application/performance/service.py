from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from domain.domain.option_position_identity import normalize_broker
from domain.domain.performance.period import PeriodWindow
from domain.domain.performance.weighted_reducer import (
    PerformanceReduction,
    PerformanceScopeError,
    WeightedOptionFact,
    reduce_option_performance,
)
from src.application.ledger import api as ledger_api


_CONTROL_GRAPH_CODES = {
    "target_event_ambiguous",
    "target_event_not_found",
    "target_event_self_reference",
    "target_event_type_invalid",
}
_PARTIAL_PROJECTION_CODES = {
    "cc_lp_membership_incomplete",
    "combo_membership_incomplete",
    "csp_lc_membership_incomplete",
    "economic_adjust_invalid",
    "economic_adjust_non_conserving",
    "target_economic_units_mismatch",
}


class OptionPerformanceReadError(RuntimeError):
    def __init__(self, *reason_codes: str) -> None:
        self.reason_codes = tuple(sorted(set(reason_codes)))
        super().__init__("option performance ledger input is unavailable")


def build_option_period_performance(
    repo: Any,
    *,
    period: PeriodWindow,
    config_key: str,
    configured_accounts: Iterable[str],
    account: str | None = None,
    broker: str | None = None,
    include_rows: bool = False,
) -> dict[str, Any]:
    authority_accounts = tuple(
        sorted(
            {
                str(item or "").strip().lower()
                for item in configured_accounts
                if str(item or "").strip()
            }
        )
    )
    requested_account = str(account or "").strip().lower() or None
    if not authority_accounts or (
        requested_account and requested_account not in authority_accounts
    ):
        raise OptionPerformanceReadError("scope_unproven")
    selected_accounts = (
        (requested_account,) if requested_account else authority_accounts
    )
    broker_scope = normalize_broker(str(broker or "").strip()) if broker else None

    try:
        rows = ledger_api.trade_event_log(repo)
    except Exception as exc:
        raise OptionPerformanceReadError("ledger_read_failed") from exc
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise OptionPerformanceReadError("ledger_tuple_invalid")

    try:
        ledger_input_hash = _ledger_input_hash(rows)
        published = ledger_api.project_trade_event_log(rows)
    except Exception as exc:
        raise OptionPerformanceReadError("ledger_tuple_invalid") from exc

    fatal_reasons = _fatal_projection_reasons(
        published.diagnostics,
        accounts=set(selected_accounts),
        broker=broker_scope,
    )
    if fatal_reasons:
        raise OptionPerformanceReadError(*fatal_reasons)

    try:
        reduction = reduce_option_performance(
            published.ledger_projection,
            period=period,
            accounts=selected_accounts,
            broker=broker_scope,
        )
    except (PerformanceScopeError, TypeError, ValueError) as exc:
        raise OptionPerformanceReadError("ledger_tuple_invalid") from exc

    observed_brokers = {
        lot.contract_key.broker
        for lot in published.ledger_projection.lots
        if lot.contract_key.account in selected_accounts
        and (not broker_scope or lot.contract_key.broker == broker_scope)
    }
    if broker_scope:
        observed_brokers.add(broker_scope)
    return _serialize_report(
        reduction,
        config_key=str(config_key),
        accounts=selected_accounts,
        brokers=tuple(sorted(observed_brokers)),
        ledger_input_hash=ledger_input_hash,
        include_rows=include_rows,
    )


def _ledger_input_hash(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _fatal_projection_reasons(
    diagnostics: Iterable[Any],
    *,
    accounts: set[str],
    broker: str | None,
) -> tuple[str, ...]:
    reasons: set[str] = set()
    for item in diagnostics:
        if str(getattr(item, "severity", "")) != "error":
            continue
        item_account = str(getattr(item, "account", "") or "").strip().lower()
        item_broker = normalize_broker(
            str(getattr(item, "broker", "") or "").strip()
        )
        if item_account and accounts and item_account not in accounts:
            continue
        if item_broker and broker and item_broker != broker:
            continue
        code = str(getattr(item, "code", "") or "").strip()
        if code in _PARTIAL_PROJECTION_CODES:
            continue
        reasons.add(
            "ledger_control_graph_invalid"
            if code in _CONTROL_GRAPH_CODES
            else "ledger_tuple_invalid"
        )
    return tuple(sorted(reasons))


def _serialize_report(
    reduction: PerformanceReduction,
    *,
    config_key: str,
    accounts: tuple[str, ...],
    brokers: tuple[str, ...],
    ledger_input_hash: str,
    include_rows: bool,
) -> dict[str, Any]:
    period = reduction.period
    bundle = reduction.bundle
    freshness_as_of = datetime.fromtimestamp(
        (period.effective_end_exclusive_at_ms - 1) / 1000,
        tz=ZoneInfo(period.reporting_timezone),
    ).isoformat(timespec="milliseconds")
    result: dict[str, Any] = {
        "period": {
            "kind": period.kind,
            "start_date": period.requested_start_date,
            "as_of_date": period.requested_end_date,
            "start_at_ms": period.effective_start_at_ms,
            "end_exclusive_at_ms": period.effective_end_exclusive_at_ms,
            "statistic_days": _json_value(period.statistic_days),
            "reporting_timezone": period.reporting_timezone,
            "freshness_status": "current" if period.is_current else "historical",
        },
        "scope": {
            "config_key": config_key,
            "accounts": list(accounts),
            "brokers": list(brokers),
        },
        "coverage": {
            "status": "complete",
            "complete_for": "full_query",
            "included_count": 1,
            "total_count": 1,
            "omitted_count": 0,
        },
        "freshness": {
            "status": "current" if period.is_current else "historical",
            "as_of": freshness_as_of,
        },
        "option_net_cashflow": _json_value(bundle["option_net_cashflow"]),
        "sell_option_win_rate": _json_value(bundle["sell_option_win_rate"]),
        "buy_option_win_rate": _json_value(bundle["buy_option_win_rate"]),
        "option_return": _json_value(bundle["option_return"]),
        "breakdowns": {
            name: _json_value(reduction.breakdowns.get(name, ()))
            for name in (
                "opening_years",
                "opening_months",
                "accounts",
                "currencies",
                "leg_types",
                "attribution_strategies",
                "parent_universes",
                "symbols",
            )
        },
        "quality": {
            "status": _json_value(bundle["status"]),
            "missing": _json_value(bundle["missing"]),
            "diagnostics": [
                _json_value(item.to_dict()) for item in reduction.diagnostics
            ],
            "ledger_input_hash": ledger_input_hash,
        },
    }
    if include_rows:
        result["rows"] = [
            _fact_payload(item)
            for item in reduction.facts
            if item.state != "failed"
        ]
    return result


def _fact_payload(fact: WeightedOptionFact) -> dict[str, Any]:
    return {
        "fact_id": fact.fact_id,
        "open_lot_id": fact.open_lot_id,
        "open_event_id": fact.open_event_id,
        "terminal_event_id": fact.terminal_event_id,
        "account": fact.account,
        "broker": fact.broker,
        "symbol": fact.symbol,
        "currency": fact.currency,
        "leg_type": fact.leg_type,
        "attribution_strategy": fact.attribution_strategy,
        "strategy_group_id": fact.strategy_group_id,
        "source_stock_lot_id": fact.source_stock_lot_id,
        "opened_at_ms": fact.opened_at_ms,
        "terminal_at_ms": fact.terminal_at_ms,
        "expiration_ymd": fact.expiration_ymd,
        "terminal_kind": fact.terminal_kind,
        "state": fact.state,
        "strike": _json_value(fact.strike),
        "multiplier": _json_value(fact.multiplier),
        "contracts": fact.contracts,
        "opening_option_cash": _json_value(fact.opening_option_cash),
        "opening_actual_fee": _json_value(fact.opening_actual_fee),
        "terminal_option_cash": _json_value(fact.terminal_option_cash),
        "terminal_actual_fee": _json_value(fact.terminal_actual_fee),
        "option_net_cashflow": _json_value(fact.option_net_cashflow),
        "occupied_capital": _json_value(fact.occupied_capital),
        "capital_days": _json_value(fact.capital_days),
        "win_eligible": fact.win_eligible,
        "win": fact.win,
        "status": _json_value(fact.status),
        "missing": _json_value(fact.missing),
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


__all__ = ["OptionPerformanceReadError", "build_option_period_performance"]
