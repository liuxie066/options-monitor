from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo
from datetime import datetime

from domain.domain.ledger.economics import OptionEconomicAllocation, fee_fact_for_event
from domain.domain.ledger.events import CLOSE_EVENT_TYPES, TradeEvent, lot_id_for_open_event, validate_trade_event
from domain.domain.option_position_identity import normalize_account, normalize_broker
from domain.domain.performance.attribution import (
    resolve_allocation_attribution,
    resolve_event_attribution,
)
from domain.domain.performance.models import (
    CAPITAL_DAYS_QUANTUM,
    CapitalExposureSegment,
    DecimalAmountEnvelope,
    FXRateFact,
    FeeBasis,
    MetricQuality,
    MetricStatus,
    OptionValuationPosition,
    StrategyAttribution,
    ValuationMarkFact,
    normalize_currency,
    quantize_money,
    select_fx_rate,
    select_valuation_mark,
    to_decimal,
)
from domain.domain.performance.period import PeriodWindow, REPORTING_TIMEZONE


_MONETARY_KINDS = frozenset(
    {
        "premium_collected_gross",
        "premium_paid_gross",
        "option_trade_cash_gross",
        "option_fee_cash",
        "stock_settlement_cash_gross",
        "stock_settlement_fee_cash",
        "assigned_stock_sale_cash_gross",
        "assigned_stock_sale_fee_cash",
        "realized_gross",
        "realized_net",
        "opening_unrealized_gross",
        "opening_unrealized_net",
        "ending_unrealized_gross",
        "ending_unrealized_net",
    }
)
_CASH_NET_KINDS = frozenset(
    {
        "option_trade_cash_gross",
        "option_fee_cash",
        "stock_settlement_cash_gross",
        "stock_settlement_fee_cash",
        "assigned_stock_sale_cash_gross",
        "assigned_stock_sale_fee_cash",
    }
)


@dataclass(frozen=True)
class PerformanceFact:
    fact_kind: str
    effective_at_ms: int
    account: str
    broker: str
    symbol: str
    currency: str | None
    amount: Decimal | None = None
    quantity: int | None = None
    source_event_id: str = ""
    allocation_id: str | None = None
    missing_reason: str | None = None
    evidence_fact_ids: tuple[str, ...] = ()
    attribution: StrategyAttribution | None = None
    attribution_issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        kind = str(self.fact_kind or "").strip()
        if not kind:
            raise ValueError("fact_kind is required")
        account = normalize_account(self.account)
        broker = normalize_broker(self.broker)
        symbol = str(self.symbol or "").strip().upper()
        if not account or not broker or not symbol:
            raise ValueError("performance fact requires account, broker, and symbol")
        try:
            currency = normalize_currency(self.currency) if self.currency else None
        except ValueError:
            if self.amount is not None:
                raise
            currency = None
        amount = None if self.amount is None else quantize_money(self.amount)
        quantity = None if self.quantity is None else int(self.quantity)
        if kind in _MONETARY_KINDS and amount is not None and not currency:
            raise ValueError(f"{kind} requires currency")
        if kind in _MONETARY_KINDS and amount is None and not self.missing_reason:
            raise ValueError(f"{kind} missing amount requires missing_reason")
        if quantity is not None and quantity < 0:
            raise ValueError("quantity cannot be negative")
        object.__setattr__(self, "fact_kind", kind)
        object.__setattr__(self, "account", account)
        object.__setattr__(self, "broker", broker)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "source_event_id", str(self.source_event_id or "").strip())
        object.__setattr__(self, "allocation_id", str(self.allocation_id).strip() if self.allocation_id else None)
        object.__setattr__(self, "missing_reason", str(self.missing_reason).strip() if self.missing_reason else None)
        object.__setattr__(
            self,
            "evidence_fact_ids",
            tuple(dict.fromkeys(str(item) for item in self.evidence_fact_ids if str(item))),
        )
        object.__setattr__(
            self,
            "attribution_issues",
            tuple(sorted({str(item) for item in self.attribution_issues if str(item)})),
        )

    @property
    def fact_id(self) -> str:
        suffix = self.allocation_id or self.source_event_id or str(self.effective_at_ms)
        return f"{self.fact_kind}:{suffix}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "fact_kind": self.fact_kind,
            "effective_at_ms": self.effective_at_ms,
            "account": self.account,
            "broker": self.broker,
            "symbol": self.symbol,
            "currency": self.currency,
            "amount": None if self.amount is None else float(self.amount),
            "quantity": self.quantity,
            "source_event_id": self.source_event_id or None,
            "allocation_id": self.allocation_id,
            "missing_reason": self.missing_reason,
            "evidence_fact_ids": list(self.evidence_fact_ids),
            "attribution": None if self.attribution is None else self.attribution.to_dict(),
            "attribution_issues": list(self.attribution_issues),
        }


@dataclass(frozen=True)
class PeriodPerformance:
    period: PeriodWindow
    scope: Mapping[str, Any]
    activity: Mapping[str, Any]
    cash: Mapping[str, Any]
    pnl: Mapping[str, Any]
    capital: Mapping[str, Any]
    assigned_stock: Mapping[str, Any]
    breakdowns: Mapping[str, Any]
    quality: MetricQuality
    facts: tuple[PerformanceFact, ...]

    def to_dict(self, *, include_rows: bool = True) -> dict[str, Any]:
        payload = {
            "schema_version": "option_period_performance.core.v1",
            "period": self.period.to_dict(),
            "scope": dict(self.scope),
            "activity": dict(self.activity),
            "cash": dict(self.cash),
            "pnl": dict(self.pnl),
            "capital": dict(self.capital),
            "assigned_stock": dict(self.assigned_stock),
            "breakdowns": dict(self.breakdowns),
            "quality": self.quality.to_dict(),
        }
        if include_rows:
            payload["rows"] = [fact.to_dict() for fact in self.facts]
        return payload


def build_period_performance(
    *,
    events: Sequence[TradeEvent],
    allocations: Sequence[OptionEconomicAllocation],
    period: PeriodWindow,
    account: str | None = None,
    broker: str | None = None,
    diagnostics: Sequence[Mapping[str, Any] | str] = (),
    opening_positions: Sequence[OptionValuationPosition] = (),
    ending_positions: Sequence[OptionValuationPosition] = (),
    valuation_marks: Sequence[ValuationMarkFact] = (),
    fx_rates: Sequence[FXRateFact] = (),
    opening_assigned_stock: Mapping[str, Any] | None = None,
    ending_assigned_stock: Mapping[str, Any] | None = None,
) -> PeriodPerformance:
    account_filter = normalize_account(account) if account else ""
    broker_filter = normalize_broker(broker) if broker else ""
    effective_events = _effective_events(events)
    scoped_events = [
        event
        for event in effective_events
        if _matches_scope(event.contract_key.account, event.contract_key.broker, account_filter, broker_filter)
    ]
    period_events = [event for event in scoped_events if period.contains(event.event_time_ms)]
    scoped_all_allocations = [
        allocation
        for allocation in allocations
        if _matches_scope(
            allocation.contract_key.account,
            allocation.contract_key.broker,
            account_filter,
            broker_filter,
        )
    ]
    scoped_allocations = [
        allocation for allocation in scoped_all_allocations if period.contains(allocation.closed_at_ms)
    ]
    scoped_opening_positions = [
        position
        for position in opening_positions
        if _matches_scope(position.account, position.broker, account_filter, broker_filter)
    ]
    scoped_ending_positions = [
        position
        for position in ending_positions
        if _matches_scope(position.account, position.broker, account_filter, broker_filter)
    ]

    facts: list[PerformanceFact] = []
    for event in period_events:
        if event.event_type == "open":
            facts.extend(_open_event_facts(event))
        elif event.event_type in CLOSE_EVENT_TYPES:
            facts.extend(_close_event_cash_facts(event))
            facts.extend(_stock_settlement_facts(event))

    allocations_by_close = {allocation.close_event_id: allocation for allocation in scoped_allocations}
    for allocation in scoped_allocations:
        facts.extend(_allocation_realized_facts(allocation))
    for event in period_events:
        if event.event_type in CLOSE_EVENT_TYPES and event.event_id not in allocations_by_close:
            facts.extend(_missing_realized_facts(event))

    facts.extend(
        _option_valuation_facts(
            scoped_opening_positions,
            valuation_marks=valuation_marks,
            at_ms=period.valuation_open_at_ms,
            prefix="opening",
        )
    )
    facts.extend(
        _option_valuation_facts(
            scoped_ending_positions,
            valuation_marks=valuation_marks,
            at_ms=period.valuation_end_at_ms,
            prefix="ending",
        )
    )
    facts.extend(
        _assigned_stock_period_facts(
            opening_assigned_stock or {},
            ending_assigned_stock or {},
            period=period,
        )
    )

    ordered_facts = tuple(
        sorted(
            facts,
            key=lambda item: (
                item.effective_at_ms,
                item.fact_kind,
                item.source_event_id,
                item.allocation_id or "",
            ),
        )
    )
    summary = _summarize(ordered_facts, fx_rates=fx_rates)
    capital = _capital_report(
        events=scoped_events,
        allocations=scoped_all_allocations,
        period=period,
        ending_assigned_stock=ending_assigned_stock or {},
        pnl=summary["pnl"],
    )
    breakdowns = {
        "monthly": _breakdown(
            ordered_facts,
            key_name="month",
            key_fn=lambda fact: _month_for_fact(fact, period=period),
            fx_rates=fx_rates,
        ),
        "accounts": _breakdown(ordered_facts, key_name="account", key_fn=lambda fact: fact.account, fx_rates=fx_rates),
        "symbols": _breakdown(ordered_facts, key_name="symbol", key_fn=lambda fact: fact.symbol, fx_rates=fx_rates),
    }
    assigned_scope_rows = [
        *_assigned_stock_rows(opening_assigned_stock or {}, "assigned_stock_lots"),
        *_assigned_stock_rows(ending_assigned_stock or {}, "assigned_stock_lots"),
    ]
    accounts = sorted(
        {
            *[event.contract_key.account for event in period_events],
            *[allocation.contract_key.account for allocation in scoped_allocations],
            *[position.account for position in scoped_opening_positions],
            *[position.account for position in scoped_ending_positions],
            *[str(row.get("account") or "") for row in assigned_scope_rows if str(row.get("account") or "")],
        }
    )
    brokers = sorted(
        {
            *[event.contract_key.broker for event in period_events],
            *[allocation.contract_key.broker for allocation in scoped_allocations],
            *[position.broker for position in scoped_opening_positions],
            *[position.broker for position in scoped_ending_positions],
            *[str(row.get("broker") or "") for row in assigned_scope_rows if str(row.get("broker") or "")],
        }
    )
    symbols = sorted(
        {
            *[event.contract_key.underlying_symbol for event in period_events],
            *[allocation.contract_key.underlying_symbol for allocation in scoped_allocations],
            *[position.symbol for position in scoped_opening_positions],
            *[position.symbol for position in scoped_ending_positions],
            *[str(row.get("symbol") or "") for row in assigned_scope_rows if str(row.get("symbol") or "")],
        }
    )
    relevant_event_ids = {
        *[event.event_id for event in period_events],
        *[allocation.open_event_id for allocation in scoped_allocations],
        *[allocation.close_event_id for allocation in scoped_allocations],
    }
    combined_diagnostics = [
        *diagnostics,
        *_assigned_stock_diagnostics(opening_assigned_stock or {}),
        *_assigned_stock_diagnostics(ending_assigned_stock or {}),
    ]
    relevant_diagnostics = [
        item
        for item in combined_diagnostics
        if _diagnostic_is_relevant(
            item,
            event_ids=relevant_event_ids,
            period=period,
            account_filter=account_filter,
            broker_filter=broker_filter,
        )
    ]
    warnings = tuple(
        dict.fromkeys(
            [
                _diagnostic_text(item)
                for item in relevant_diagnostics
                if _diagnostic_text(item)
            ]
            + [
                f"capital:{item}"
                for item in capital.get("coverage", {}).get("warnings", [])
                if str(item)
            ]
        )
    )
    fact_missing = {fact.fact_id for fact in ordered_facts if fact.amount is None and fact.missing_reason}
    fx_missing, selected_fx_fact_ids = _fx_quality_for_facts(ordered_facts, fx_rates=fx_rates)
    capital_missing = {
        f"capital:{item}" for item in capital.get("coverage", {}).get("missing", []) if str(item)
    }
    missing = tuple(sorted({*fact_missing, *fx_missing, *capital_missing}))
    if missing or warnings:
        quality_status = MetricStatus.PARTIAL
    elif ordered_facts:
        quality_status = MetricStatus.OBSERVED
    else:
        quality_status = MetricStatus.NOT_OBSERVED
    evidence_fact_ids = tuple(
        dict.fromkeys(
            [fact_id for fact in ordered_facts for fact_id in fact.evidence_fact_ids] + list(selected_fx_fact_ids)
        )
    )
    quality = MetricQuality(
        status=quality_status,
        missing=missing,
        warnings=warnings,
        evidence_fact_ids=evidence_fact_ids,
    )
    return PeriodPerformance(
        period=period,
        scope={
            "account": account_filter or None,
            "broker": broker_filter or None,
            "accounts": accounts,
            "brokers": brokers,
            "symbols": symbols,
        },
        activity=summary["activity"],
        cash=summary["cash"],
        pnl=summary["pnl"],
        capital=capital,
        assigned_stock=_assigned_stock_report_summary(
            opening_assigned_stock or {},
            ending_assigned_stock or {},
            facts=ordered_facts,
            fx_rates=fx_rates,
        ),
        breakdowns=breakdowns,
        quality=quality,
        facts=ordered_facts,
    )


def _capital_report(
    *,
    events: Sequence[TradeEvent],
    allocations: Sequence[OptionEconomicAllocation],
    period: PeriodWindow,
    ending_assigned_stock: Mapping[str, Any],
    pnl: Mapping[str, Any],
) -> dict[str, Any]:
    segments: list[CapitalExposureSegment] = []
    missing: set[str] = set()
    warnings: set[str] = set()
    covered_call_rows = _assigned_stock_rows(ending_assigned_stock, "covered_call_allocations")
    covered_call_ids = {
        str(row.get("open_event_id") or "").strip()
        for row in covered_call_rows
        if str(row.get("open_event_id") or "").strip()
    }
    for row in covered_call_rows:
        open_event_id = str(row.get("open_event_id") or "").strip()
        allocation_status = str(row.get("allocation_status") or "unknown").strip().lower()
        if open_event_id and allocation_status != "explicit":
            warnings.add(f"covered_call_allocation_{allocation_status}:{open_event_id}")
    allocations_by_open: dict[str, list[OptionEconomicAllocation]] = {}
    for allocation in allocations:
        allocations_by_open.setdefault(allocation.open_event_id, []).append(allocation)

    for event in events:
        if event.event_type != "open":
            continue
        remaining = int(event.contracts)
        if remaining <= 0:
            continue
        cursor = int(event.event_time_ms)
        transitions = sorted(
            allocations_by_open.get(event.event_id, ()),
            key=lambda item: (int(item.closed_at_ms), item.allocation_id),
        )
        for allocation in transitions:
            close_at_ms = int(allocation.closed_at_ms)
            if close_at_ms < cursor or int(allocation.contracts) > remaining:
                missing.add(f"invalid_option_quantity_timeline:{event.event_id}")
                continue
            _append_option_capital_segment(
                segments,
                missing,
                event=event,
                remaining_contracts=remaining,
                start_at_ms=cursor,
                end_at_ms=close_at_ms,
                period=period,
                covered_call_ids=covered_call_ids,
            )
            remaining -= int(allocation.contracts)
            cursor = close_at_ms
        if remaining > 0:
            _append_option_capital_segment(
                segments,
                missing,
                event=event,
                remaining_contracts=remaining,
                start_at_ms=cursor,
                end_at_ms=period.effective_end_exclusive_at_ms,
                period=period,
                covered_call_ids=covered_call_ids,
            )

    _append_assigned_stock_capital_segments(
        segments,
        missing,
        projection=ending_assigned_stock,
        period=period,
    )
    for row in _assigned_stock_rows(ending_assigned_stock, "unsupported_inventory_rows"):
        event_id = str(row.get("event_id") or row.get("stock_event_id") or "unknown")
        missing.add(f"incomplete_inventory_basis:{event_id}")

    sums: dict[str, Decimal] = {}
    relevant_segments: list[CapitalExposureSegment] = []
    for segment in segments:
        overlap_ms = segment.overlap_ms(
            period_start_at_ms=period.effective_start_at_ms,
            period_end_exclusive_at_ms=period.effective_end_exclusive_at_ms,
        )
        if overlap_ms <= 0:
            continue
        relevant_segments.append(segment)
        capital_days = segment.capital_days(
            period_start_at_ms=period.effective_start_at_ms,
            period_end_exclusive_at_ms=period.effective_end_exclusive_at_ms,
        )
        if capital_days > 0:
            sums[segment.currency] = sums.get(segment.currency, Decimal(0)) + capital_days
    rounded_sums = {
        currency: value.quantize(CAPITAL_DAYS_QUANTUM, rounding=ROUND_HALF_UP)
        for currency, value in sorted(sums.items())
    }
    coverage_status = (
        MetricStatus.PARTIAL
        if missing or warnings
        else MetricStatus.OBSERVED
        if relevant_segments
        else MetricStatus.NOT_OBSERVED
    )
    return {
        "capital_basis": "notional_days_v1",
        "capital_days_by_currency": {currency: float(value) for currency, value in rounded_sums.items()},
        "period_total_net_annualized_efficiency": _annualized_efficiency(
            pnl.get("period_total_net", {}), sums
        ),
        "period_realized_net_annualized_efficiency": _annualized_efficiency(
            pnl.get("realized_net", {}), sums
        ),
        "coverage": {
            "status": coverage_status.value,
            "missing": sorted(missing),
            "warnings": sorted(warnings),
            "segment_count": len(relevant_segments),
            "incremental_segment_count": sum(1 for item in relevant_segments if item.incremental),
            "zero_incremental_segment_count": sum(1 for item in relevant_segments if not item.incremental),
            "covered_call_open_event_ids": sorted(covered_call_ids),
        },
        "segments": [
            item.to_dict(
                period_start_at_ms=period.effective_start_at_ms,
                period_end_exclusive_at_ms=period.effective_end_exclusive_at_ms,
            )
            for item in relevant_segments
        ],
    }


def _append_option_capital_segment(
    segments: list[CapitalExposureSegment],
    missing: set[str],
    *,
    event: TradeEvent,
    remaining_contracts: int,
    start_at_ms: int,
    end_at_ms: int,
    period: PeriodWindow,
    covered_call_ids: set[str],
) -> None:
    if remaining_contracts <= 0 or end_at_ms <= start_at_ms:
        return
    if min(end_at_ms, period.effective_end_exclusive_at_ms) <= max(
        start_at_ms, period.effective_start_at_ms
    ):
        return
    key = event.contract_key
    side = str(key.position_side or "").strip().lower()
    option_type = str(key.option_type or "").strip().lower()
    try:
        multiplier = to_decimal(event.multiplier, field_name="multiplier")
        currency = normalize_currency(event.currency)
    except ValueError:
        missing.add(f"capital_basis_unavailable:{event.event_id}")
        return
    quantity = Decimal(remaining_contracts)
    incremental = True
    if side == "short" and option_type == "put":
        try:
            notional = to_decimal(key.strike, field_name="strike") * multiplier * quantity
        except ValueError:
            missing.add(f"capital_basis_unavailable:{event.event_id}")
            return
        exposure_kind = "short_put_strike_notional"
    elif side == "long":
        try:
            notional = to_decimal(event.price, field_name="open_price") * multiplier * quantity
        except ValueError:
            missing.add(f"capital_basis_unavailable:{event.event_id}")
            return
        exposure_kind = "long_option_premium_debit"
    elif side == "short" and option_type == "call" and event.event_id in covered_call_ids:
        notional = Decimal(0)
        incremental = False
        exposure_kind = "covered_call_zero_incremental"
    else:
        missing.add(f"capital_basis_unavailable:{event.event_id}")
        return
    if incremental and notional < 0:
        missing.add(f"capital_basis_unavailable:{event.event_id}")
        return
    attribution_resolution = resolve_event_attribution(
        event,
        lifecycle_source_id=lot_id_for_open_event(event),
    )
    segments.append(
        CapitalExposureSegment(
            account=key.account,
            broker=key.broker,
            symbol=key.underlying_symbol,
            currency=currency,
            exposure_kind=exposure_kind,
            source_id=lot_id_for_open_event(event),
            start_at_ms=start_at_ms,
            end_at_ms=end_at_ms,
            notional=notional,
            quantity=quantity,
            incremental=incremental,
            attribution=attribution_resolution.attribution,
            attribution_issues=attribution_resolution.issues,
        )
    )


def _append_assigned_stock_capital_segments(
    segments: list[CapitalExposureSegment],
    missing: set[str],
    *,
    projection: Mapping[str, Any],
    period: PeriodWindow,
) -> None:
    sales_by_lot: dict[str, list[dict[str, Any]]] = {}
    for sale in _assigned_stock_rows(projection, "assigned_stock_sale_rows"):
        sales_by_lot.setdefault(str(sale.get("stock_lot_id") or ""), []).append(sale)
    for lot in _assigned_stock_rows(projection, "assigned_stock_lots"):
        lot_id = str(lot.get("stock_lot_id") or "").strip()
        if not lot_id:
            continue
        try:
            cursor = int(lot.get("assigned_at_ms") or lot.get("opened_at_ms") or 0)
            shares_remaining = Decimal(int(lot.get("shares_opened") or 0))
            basis_remaining = to_decimal(lot.get("stock_cost_basis_total"), field_name="stock_cost_basis_total")
        except (TypeError, ValueError):
            missing.add(f"assigned_stock_basis_unavailable:{lot_id}")
            continue
        if cursor <= 0 or shares_remaining <= 0 or basis_remaining < 0:
            missing.add(f"assigned_stock_basis_unavailable:{lot_id}")
            continue
        sales = sorted(
            sales_by_lot.get(lot_id, ()),
            key=lambda row: (int(row.get("event_at") or 0), str(row.get("stock_event_id") or "")),
        )
        for sale in sales:
            sale_at_ms = int(sale.get("event_at") or 0)
            sold_shares = Decimal(int(sale.get("shares") or 0))
            try:
                sold_basis = to_decimal(sale.get("stock_cost_basis_sold"), field_name="stock_cost_basis_sold")
            except ValueError:
                missing.add(f"assigned_stock_sale_basis_unavailable:{sale.get('stock_event_id') or lot_id}")
                continue
            if sale_at_ms < cursor or sold_shares <= 0 or sold_shares > shares_remaining:
                missing.add(f"invalid_assigned_stock_quantity_timeline:{lot_id}")
                continue
            _append_stock_capital_segment(
                segments,
                missing,
                lot=lot,
                lot_id=lot_id,
                start_at_ms=cursor,
                end_at_ms=sale_at_ms,
                notional=basis_remaining,
                shares=shares_remaining,
                period=period,
            )
            cursor = sale_at_ms
            shares_remaining -= sold_shares
            basis_remaining -= sold_basis
            if basis_remaining < 0:
                missing.add(f"invalid_assigned_stock_basis_timeline:{lot_id}")
                basis_remaining = Decimal(0)
        if shares_remaining > 0 and basis_remaining >= 0:
            _append_stock_capital_segment(
                segments,
                missing,
                lot=lot,
                lot_id=lot_id,
                start_at_ms=cursor,
                end_at_ms=period.effective_end_exclusive_at_ms,
                notional=basis_remaining,
                shares=shares_remaining,
                period=period,
            )


def _append_stock_capital_segment(
    segments: list[CapitalExposureSegment],
    missing: set[str],
    *,
    lot: Mapping[str, Any],
    lot_id: str,
    start_at_ms: int,
    end_at_ms: int,
    notional: Decimal,
    shares: Decimal,
    period: PeriodWindow,
) -> None:
    if end_at_ms <= start_at_ms or min(end_at_ms, period.effective_end_exclusive_at_ms) <= max(
        start_at_ms, period.effective_start_at_ms
    ):
        return
    try:
        segment = CapitalExposureSegment(
            account=str(lot.get("account") or ""),
            broker=str(lot.get("broker") or ""),
            symbol=str(lot.get("symbol") or ""),
            currency=str(lot.get("currency") or ""),
            exposure_kind="assigned_stock_cost_basis",
            source_id=lot_id,
            start_at_ms=start_at_ms,
            end_at_ms=end_at_ms,
            notional=notional,
            quantity=shares,
        )
    except ValueError:
        missing.add(f"assigned_stock_basis_unavailable:{lot_id}")
        return
    segments.append(segment)


def _annualized_efficiency(
    pnl_metric: Mapping[str, Any],
    capital_days_by_currency: Mapping[str, Decimal],
) -> dict[str, Any]:
    raw_pnl = pnl_metric.get("by_currency") if isinstance(pnl_metric, Mapping) else {}
    pnl_by_currency = raw_pnl if isinstance(raw_pnl, Mapping) else {}
    currencies = sorted({*capital_days_by_currency, *(str(key) for key in pnl_by_currency)})
    values: dict[str, float] = {}
    missing: list[str] = []
    zero_denominator = False
    for currency in currencies:
        denominator = capital_days_by_currency.get(currency, Decimal(0))
        if denominator <= 0:
            if currency in pnl_by_currency:
                missing.append(f"zero_capital_days:{currency}")
                zero_denominator = True
            continue
        if currency not in pnl_by_currency:
            missing.append(f"pnl_unavailable:{currency}")
            continue
        pnl_amount = to_decimal(pnl_by_currency[currency], field_name=f"pnl[{currency}]")
        value = (pnl_amount / denominator * Decimal(365)).quantize(
            Decimal("0.000000000001"),
            rounding=ROUND_HALF_UP,
        )
        values[currency] = float(value)
    if values and missing:
        status = MetricStatus.PARTIAL
    elif values:
        status = MetricStatus.OBSERVED
    elif zero_denominator:
        status = MetricStatus.NOT_APPLICABLE
    elif missing:
        status = MetricStatus.PARTIAL
    else:
        status = MetricStatus.NOT_OBSERVED
    return {
        "by_currency": values,
        "status": status.value,
        "missing": missing,
    }


def _matches_scope(account: str, broker: str, account_filter: str, broker_filter: str) -> bool:
    return (not account_filter or normalize_account(account) == account_filter) and (
        not broker_filter or normalize_broker(broker) == broker_filter
    )


def _effective_events(events: Sequence[TradeEvent]) -> list[TradeEvent]:
    ordered = sorted(events, key=lambda item: (int(item.event_time_ms or 0), item.event_id))
    validated: list[tuple[TradeEvent, bool]] = []
    seen: set[str] = set()
    for event in ordered:
        has_error = any(item.severity == "error" for item in validate_trade_event(event))
        if event.event_id in seen:
            has_error = True
        seen.add(event.event_id)
        validated.append((event, has_error))
    voided = {
        event.target_event_id
        for event, has_error in validated
        if event.event_type == "void" and event.target_event_id and not has_error
    }
    return [
        event
        for event, has_error in validated
        if not has_error and event.event_type != "void" and event.event_id not in voided
    ]


def _open_event_facts(event: TradeEvent) -> list[PerformanceFact]:
    currency, currency_reason = _fact_currency(event.currency, context="option event currency")
    amount, reason = _event_option_amount(event)
    if currency_reason:
        amount = None
        reason = currency_reason
    common = _event_fact_kwargs(event, currency=currency)
    facts = [
        PerformanceFact(
            fact_kind="contracts_opened",
            effective_at_ms=event.event_time_ms,
            quantity=event.contracts,
            **common,
        ),
        PerformanceFact(
            fact_kind="option_trade_cash_gross",
            effective_at_ms=event.event_time_ms,
            amount=amount,
            missing_reason=reason,
            **common,
        ),
    ]
    premium_kind = "premium_collected_gross" if event.contract_key.position_side == "short" else "premium_paid_gross"
    premium_amount = None if amount is None else abs(amount)
    facts.append(
        PerformanceFact(
            fact_kind=premium_kind,
            effective_at_ms=event.event_time_ms,
            amount=premium_amount,
            missing_reason=reason,
            **common,
        )
    )
    facts.append(_option_fee_cash_fact(event, currency=currency, currency_reason=currency_reason))
    return facts


def _close_event_cash_facts(event: TradeEvent) -> list[PerformanceFact]:
    currency, currency_reason = _fact_currency(event.currency, context="option event currency")
    amount, reason = _event_option_amount(event)
    if currency_reason:
        amount = None
        reason = currency_reason
    common = _event_fact_kwargs(event, currency=currency)
    return [
        PerformanceFact(
            fact_kind="contracts_closed",
            effective_at_ms=event.event_time_ms,
            quantity=event.contracts,
            **common,
        ),
        PerformanceFact(
            fact_kind="option_trade_cash_gross",
            effective_at_ms=event.event_time_ms,
            amount=amount,
            missing_reason=reason,
            **common,
        ),
        _option_fee_cash_fact(event, currency=currency, currency_reason=currency_reason),
    ]


def _event_option_amount(event: TradeEvent) -> tuple[Decimal | None, str | None]:
    try:
        price = to_decimal(event.price, field_name="price")
        multiplier = to_decimal(event.multiplier, field_name="multiplier")
        if price < 0:
            raise ValueError("price cannot be negative")
        if multiplier <= 0:
            raise ValueError("multiplier must be positive")
        gross = quantize_money(price * multiplier * Decimal(int(event.contracts)))
    except (TypeError, ValueError) as exc:
        return None, f"option cash unavailable: {exc}"
    is_open = event.event_type == "open"
    positive = (event.contract_key.position_side == "short" and is_open) or (
        event.contract_key.position_side == "long" and not is_open
    )
    return (gross if positive else -gross), None


def _option_fee_cash_fact(
    event: TradeEvent,
    *,
    currency: str | None,
    currency_reason: str | None,
) -> PerformanceFact:
    fee = fee_fact_for_event(event)
    amount = -fee.amount if fee.basis == FeeBasis.ACTUAL and fee.amount is not None else None
    reason = (
        None if amount is not None else fee.reason or f"{fee.basis.value} option fee is not production cash evidence"
    )
    if currency_reason:
        amount = None
        reason = currency_reason
    return PerformanceFact(
        fact_kind="option_fee_cash",
        effective_at_ms=event.event_time_ms,
        amount=amount,
        missing_reason=reason,
        **_event_fact_kwargs(event, currency=currency),
    )


def _allocation_realized_facts(allocation: OptionEconomicAllocation) -> list[PerformanceFact]:
    currency, currency_reason = _fact_currency(allocation.currency, context="allocation currency")
    common = {
        "effective_at_ms": allocation.closed_at_ms,
        "account": allocation.contract_key.account,
        "broker": allocation.contract_key.broker,
        "symbol": allocation.contract_key.underlying_symbol,
        "currency": currency,
        "source_event_id": allocation.close_event_id,
        "allocation_id": allocation.allocation_id,
        "attribution": resolve_allocation_attribution(
            strategy=allocation.strategy,
            leg_role=allocation.leg_role,
            strategy_group_id=allocation.strategy_group_id,
            target_lot_id=allocation.target_lot_id,
        ),
    }
    gross_amount = None if currency_reason else allocation.realized_pnl_gross
    net_amount = None if currency_reason else allocation.realized_pnl_net
    net_reason = currency_reason
    if net_reason is None and allocation.realized_pnl_net is None:
        net_reason = f"fee quality is {allocation.fee_quality}"
    return [
        PerformanceFact(
            fact_kind="realized_gross",
            amount=gross_amount,
            missing_reason=currency_reason,
            **common,
        ),
        PerformanceFact(
            fact_kind="realized_net",
            amount=net_amount,
            missing_reason=net_reason,
            **common,
        ),
    ]


def _missing_realized_facts(event: TradeEvent) -> list[PerformanceFact]:
    currency, _ = _fact_currency(event.currency, context="option event currency")
    common = _event_fact_kwargs(event, currency=currency)
    return [
        PerformanceFact(
            fact_kind="realized_gross",
            effective_at_ms=event.event_time_ms,
            amount=None,
            missing_reason="effective close has no canonical economic allocation",
            **common,
        ),
        PerformanceFact(
            fact_kind="realized_net",
            effective_at_ms=event.event_time_ms,
            amount=None,
            missing_reason="effective close has no canonical economic allocation",
            **common,
        ),
    ]


def _stock_settlement_facts(event: TradeEvent) -> list[PerformanceFact]:
    if event.event_type not in {"assignment", "exercise"}:
        return []
    raw = event.raw_payload.get("stock_settlement") if isinstance(event.raw_payload, dict) else None
    event_currency, _ = _fact_currency(event.currency, context="option event currency")
    common = _event_fact_kwargs(event, currency=event_currency)
    if not isinstance(raw, dict):
        return [
            PerformanceFact(
                fact_kind="stock_settlement_cash_gross",
                effective_at_ms=event.event_time_ms,
                amount=None,
                missing_reason="assignment/exercise stock_settlement is missing",
                **common,
            ),
            PerformanceFact(
                fact_kind="stock_settlement_fee_cash",
                effective_at_ms=event.event_time_ms,
                amount=None,
                missing_reason="assignment/exercise stock settlement fee is missing",
                **common,
            ),
        ]
    settlement_currency, currency_reason = _fact_currency(
        raw.get("currency") or event.currency,
        context="stock settlement currency",
    )
    stock_common = {**common, "currency": settlement_currency}
    side = str(raw.get("side") or raw.get("stock_side") or "").strip().lower()
    shares_raw = raw.get("shares") if raw.get("shares") not in (None, "") else raw.get("stock_qty")
    price_raw = raw.get("price") if raw.get("price") not in (None, "") else raw.get("stock_price")
    try:
        shares_decimal = to_decimal(shares_raw, field_name="stock settlement shares")
        if shares_decimal != shares_decimal.to_integral_value():
            raise ValueError("stock settlement shares must be an integer")
        shares = int(shares_decimal)
        price = to_decimal(price_raw, field_name="stock settlement price")
        if shares <= 0 or price < 0 or side not in {"buy", "sell"}:
            raise ValueError("stock settlement requires buy/sell, positive shares, and non-negative price")
        principal = quantize_money(price * Decimal(shares))
        cash_amount = principal if side == "sell" else -principal
        cash_reason = currency_reason
        if currency_reason:
            cash_amount = None
    except (TypeError, ValueError) as exc:
        cash_amount = None
        cash_reason = f"stock settlement cash unavailable: {exc}"
    fee_raw = raw.get("fees") if raw.get("fees") not in (None, "") else raw.get("fee")
    fee_provenance = raw.get("fee_provenance") if isinstance(raw.get("fee_provenance"), Mapping) else {}
    fee_basis = str(fee_provenance.get("basis") or "").strip().lower()
    if fee_basis != FeeBasis.ACTUAL.value:
        fee_amount = None
        fee_reason = str(
            fee_provenance.get("reason")
            or (
                f"stock settlement fee basis is {fee_basis}"
                if fee_basis in {FeeBasis.ESTIMATED.value, FeeBasis.MISSING.value}
                else "stock settlement fee lacks actual provenance"
            )
        )
    elif fee_raw in (None, ""):
        fee_amount = None
        fee_reason = "actual stock settlement fee amount is missing"
    else:
        try:
            fee = to_decimal(fee_raw, field_name="stock settlement fee")
            if fee < 0:
                raise ValueError("stock settlement fee cannot be negative")
            fee_amount = -quantize_money(fee)
            fee_reason = currency_reason
            if currency_reason:
                fee_amount = None
        except (TypeError, ValueError) as exc:
            fee_amount = None
            fee_reason = f"stock settlement fee unavailable: {exc}"
    return [
        PerformanceFact(
            fact_kind="stock_settlement_cash_gross",
            effective_at_ms=event.event_time_ms,
            amount=cash_amount,
            missing_reason=cash_reason,
            **stock_common,
        ),
        PerformanceFact(
            fact_kind="stock_settlement_fee_cash",
            effective_at_ms=event.event_time_ms,
            amount=fee_amount,
            missing_reason=fee_reason,
            **stock_common,
        ),
    ]


def _assigned_stock_period_facts(
    opening: Mapping[str, Any],
    ending: Mapping[str, Any],
    *,
    period: PeriodWindow,
) -> list[PerformanceFact]:
    facts: list[PerformanceFact] = []
    opening_lots = {
        str(row.get("stock_lot_id") or ""): row
        for row in _assigned_stock_rows(opening, "assigned_stock_lots")
        if str(row.get("stock_lot_id") or "")
    }
    ending_lots = {
        str(row.get("stock_lot_id") or ""): row
        for row in _assigned_stock_rows(ending, "assigned_stock_lots")
        if str(row.get("stock_lot_id") or "")
    }
    for row in ending_lots.values():
        assigned_at = _row_int(row, "assigned_at_ms", "opened_at_ms")
        if assigned_at is None or not period.contains(assigned_at):
            continue
        common = _assigned_stock_fact_kwargs(row, source_event_id=str(row.get("source_assignment_event_id") or ""))
        facts.append(
            PerformanceFact(
                fact_kind="assigned_stock_shares_opened",
                effective_at_ms=assigned_at,
                quantity=max(0, int(row.get("shares_opened") or 0)),
                **common,
            )
        )
        fee = _assigned_stock_fee(row, component="assignment_stock_fee")
        fee_amount = _actual_fee_amount(fee)
        facts.append(
            PerformanceFact(
                fact_kind="realized_net",
                effective_at_ms=assigned_at,
                amount=None if fee_amount is None else -fee_amount,
                missing_reason=None if fee_amount is not None else _fee_missing_reason(fee),
                **common,
            )
        )
    for row in _assigned_stock_rows(ending, "assigned_stock_sale_rows"):
        event_at = _row_int(row, "event_at", "trade_time_ms")
        if event_at is None or not period.contains(event_at):
            continue
        common = _assigned_stock_fact_kwargs(row, source_event_id=str(row.get("stock_event_id") or ""))
        proceeds = _decimal_or_none(row.get("cash_in_gross"))
        basis = _decimal_or_none(row.get("stock_principal_basis_sold"))
        gross = quantize_money(proceeds - basis) if proceeds is not None and basis is not None else None
        fee = {
            "basis": row.get("fee_basis"),
            "amount": row.get("fees"),
            "reason": row.get("fee_reason"),
        }
        fee_amount = _actual_fee_amount(fee)
        net = quantize_money(gross - fee_amount) if gross is not None and fee_amount is not None else None
        gross_reason = None if gross is not None else "assigned-stock sale proceeds or cost basis is unavailable"
        net_reason = None if net is not None else gross_reason or _fee_missing_reason(fee)
        evidence_ids = tuple(str(row.get("evidence_fact_id") or "").split())
        facts.extend(
            [
                PerformanceFact(
                    fact_kind="assigned_stock_shares_sold",
                    effective_at_ms=event_at,
                    quantity=max(0, int(row.get("shares") or 0)),
                    **common,
                ),
                PerformanceFact(
                    fact_kind="assigned_stock_sale_cash_gross",
                    effective_at_ms=event_at,
                    amount=proceeds,
                    missing_reason=None if proceeds is not None else "assigned-stock sale proceeds are unavailable",
                    evidence_fact_ids=evidence_ids,
                    **common,
                ),
                PerformanceFact(
                    fact_kind="assigned_stock_sale_fee_cash",
                    effective_at_ms=event_at,
                    amount=None if fee_amount is None else -fee_amount,
                    missing_reason=None if fee_amount is not None else _fee_missing_reason(fee),
                    **common,
                ),
                PerformanceFact(
                    fact_kind="realized_gross",
                    effective_at_ms=event_at,
                    amount=gross,
                    missing_reason=gross_reason,
                    **common,
                ),
                PerformanceFact(
                    fact_kind="realized_net",
                    effective_at_ms=event_at,
                    amount=net,
                    missing_reason=net_reason,
                    **common,
                ),
            ]
        )
    facts.extend(
        _assigned_stock_unrealized_facts(
            opening_lots.values(),
            at_ms=period.valuation_open_at_ms,
            prefix="opening",
        )
    )
    facts.extend(
        _assigned_stock_unrealized_facts(
            ending_lots.values(),
            at_ms=period.valuation_end_at_ms,
            prefix="ending",
        )
    )
    return facts


def _assigned_stock_unrealized_facts(
    rows: Any,
    *,
    at_ms: int,
    prefix: str,
) -> list[PerformanceFact]:
    facts: list[PerformanceFact] = []
    for row in rows:
        if not isinstance(row, Mapping) or int(row.get("shares_remaining") or 0) <= 0:
            continue
        amount = _decimal_or_none(row.get("assigned_stock_unrealized_pnl_gross"))
        reason = None if amount is not None else "assigned-stock valuation mark is unavailable"
        fact_id = str(row.get("quote_evidence_fact_id") or "")
        common = _assigned_stock_fact_kwargs(row, source_event_id=str(row.get("stock_lot_id") or ""))
        for suffix in ("gross", "net"):
            facts.append(
                PerformanceFact(
                    fact_kind=f"{prefix}_unrealized_{suffix}",
                    effective_at_ms=at_ms,
                    amount=amount,
                    missing_reason=reason,
                    evidence_fact_ids=(fact_id,) if fact_id else (),
                    **common,
                )
            )
    return facts


def _assigned_stock_fact_kwargs(row: Mapping[str, Any], *, source_event_id: str) -> dict[str, Any]:
    return {
        "account": str(row.get("account") or ""),
        "broker": str(row.get("broker") or ""),
        "symbol": str(row.get("symbol") or ""),
        "currency": str(row.get("currency") or "") or None,
        "source_event_id": source_event_id,
    }


def _assigned_stock_fee(row: Mapping[str, Any], *, component: str) -> Mapping[str, Any]:
    for item in row.get("fee_evidence") or []:
        if isinstance(item, Mapping) and str(item.get("component") or "") == component:
            return item
    return {"basis": "missing", "reason": f"{component} is missing"}


def _actual_fee_amount(fee: Mapping[str, Any]) -> Decimal | None:
    if str(fee.get("basis") or "").strip().lower() != FeeBasis.ACTUAL.value:
        return None
    amount = _decimal_or_none(fee.get("amount"))
    return amount if amount is not None and amount >= 0 else None


def _fee_missing_reason(fee: Mapping[str, Any]) -> str:
    return str(fee.get("reason") or f"fee basis is {fee.get('basis') or 'missing'}")


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        return quantize_money(value)
    except (TypeError, ValueError):
        return None


def _row_int(row: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        try:
            value = int(row.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _assigned_stock_rows(projection: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = projection.get(key)
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _assigned_stock_diagnostics(projection: Mapping[str, Any]) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    lots = {
        str(row.get("stock_lot_id") or ""): row
        for row in _assigned_stock_rows(projection, "assigned_stock_lots")
    }
    for row in _assigned_stock_rows(projection, "assigned_stock_review_rows"):
        lot = lots.get(str(row.get("stock_lot_id") or ""), {})
        payload = {
            "context": "assigned_stock",
            "code": str(row.get("status") or "assigned_stock_review_required"),
            "message": str(row.get("message") or "assigned-stock lifecycle is incomplete"),
            "event_id": str(row.get("event_id") or row.get("stock_event_id") or ""),
            "event_time_ms": _row_int(lot, "assigned_at_ms", "opened_at_ms") or 0,
            "account": str(row.get("account") or lot.get("account") or ""),
            "broker": str(row.get("broker") or lot.get("broker") or ""),
        }
        diagnostics.append(payload)
    return diagnostics


def _assigned_stock_report_summary(
    opening: Mapping[str, Any],
    ending: Mapping[str, Any],
    *,
    facts: Sequence[PerformanceFact],
    fx_rates: Sequence[FXRateFact],
) -> dict[str, Any]:
    stock_source_ids = {
        str(row.get("stock_event_id") or row.get("source_assignment_event_id") or row.get("stock_lot_id") or "")
        for row in [
            *_assigned_stock_rows(ending, "assigned_stock_lots"),
            *_assigned_stock_rows(ending, "assigned_stock_sale_rows"),
        ]
    }
    stock_facts = [
        fact
        for fact in facts
        if fact.fact_kind.startswith("assigned_stock_")
        or (
            fact.source_event_id in stock_source_ids
            and fact.fact_kind
            in {
                "realized_gross",
                "realized_net",
                "opening_unrealized_gross",
                "opening_unrealized_net",
                "ending_unrealized_gross",
                "ending_unrealized_net",
            }
        )
    ]
    return {
        "opening_lots": _assigned_stock_rows(opening, "assigned_stock_lots"),
        "ending_lots": _assigned_stock_rows(ending, "assigned_stock_lots"),
        "sales": _assigned_stock_rows(ending, "assigned_stock_sale_rows"),
        "review": _assigned_stock_rows(ending, "assigned_stock_review_rows"),
        "unsupported_inventory": _assigned_stock_rows(ending, "unsupported_inventory_rows"),
        "period": _summarize(stock_facts, fx_rates=fx_rates),
    }


def _option_valuation_facts(
    positions: Sequence[OptionValuationPosition],
    *,
    valuation_marks: Sequence[ValuationMarkFact],
    at_ms: int,
    prefix: str,
) -> list[PerformanceFact]:
    facts: list[PerformanceFact] = []
    for position in positions:
        selection = select_valuation_mark(
            list(valuation_marks),
            instrument_key=position.instrument.instrument_key,
            at_ms=at_ms,
        )
        common = {
            "effective_at_ms": int(at_ms),
            "account": position.account,
            "broker": position.broker,
            "symbol": position.symbol,
            "currency": position.currency,
            "source_event_id": position.lot_id,
            "attribution": position.attribution,
            "attribution_issues": position.attribution_issues,
        }
        if selection.fact is None:
            reason = f"{prefix} option mark unavailable: {selection.reason or selection.status}"
            facts.extend(
                [
                    PerformanceFact(
                        fact_kind=f"{prefix}_unrealized_gross",
                        amount=None,
                        missing_reason=reason,
                        **common,
                    ),
                    PerformanceFact(
                        fact_kind=f"{prefix}_unrealized_net",
                        amount=None,
                        missing_reason=reason,
                        **common,
                    ),
                ]
            )
            continue
        mark = selection.fact
        assert isinstance(mark, ValuationMarkFact)
        quantity = Decimal(position.contracts_open) * position.instrument.multiplier
        if position.position_side == "short":
            gross = quantize_money((position.open_price - mark.price) * quantity)
        else:
            gross = quantize_money((mark.price - position.open_price) * quantity)
        evidence_ids = (str(mark.fact_id),)
        facts.append(
            PerformanceFact(
                fact_kind=f"{prefix}_unrealized_gross",
                amount=gross,
                evidence_fact_ids=evidence_ids,
                **common,
            )
        )
        if position.open_fee_quality == FeeBasis.ACTUAL.value and position.open_fee_remaining is not None:
            net = quantize_money(gross - position.open_fee_remaining)
            net_reason = None
        else:
            net = None
            net_reason = f"opening fee quality is {position.open_fee_quality}"
        facts.append(
            PerformanceFact(
                fact_kind=f"{prefix}_unrealized_net",
                amount=net,
                missing_reason=net_reason,
                evidence_fact_ids=evidence_ids,
                **common,
            )
        )
    return facts


def _fact_currency(value: Any, *, context: str) -> tuple[str | None, str | None]:
    try:
        return normalize_currency(value), None
    except ValueError as exc:
        return None, f"{context} unavailable: {exc}"


def _event_fact_kwargs(event: TradeEvent, *, currency: str | None) -> dict[str, Any]:
    resolution = resolve_event_attribution(
        event,
        lifecycle_source_id=lot_id_for_open_event(event) if event.event_type == "open" else None,
    )
    return {
        "account": event.contract_key.account,
        "broker": event.contract_key.broker,
        "symbol": event.contract_key.underlying_symbol,
        "currency": currency,
        "source_event_id": event.event_id,
        "attribution": resolution.attribution,
        "attribution_issues": resolution.issues,
    }


def _summarize(
    facts: Sequence[PerformanceFact],
    *,
    fx_rates: Sequence[FXRateFact] = (),
) -> dict[str, Any]:
    return {
        "activity": {
            "premium_collected_gross": _metric(facts, {"premium_collected_gross"}, fx_rates=fx_rates).to_dict(),
            "premium_paid_gross": _metric(facts, {"premium_paid_gross"}, fx_rates=fx_rates).to_dict(),
            "contracts_opened": sum(fact.quantity or 0 for fact in facts if fact.fact_kind == "contracts_opened"),
            "contracts_closed": sum(fact.quantity or 0 for fact in facts if fact.fact_kind == "contracts_closed"),
            "assigned_stock_shares_opened": sum(
                fact.quantity or 0 for fact in facts if fact.fact_kind == "assigned_stock_shares_opened"
            ),
            "assigned_stock_shares_sold": sum(
                fact.quantity or 0 for fact in facts if fact.fact_kind == "assigned_stock_shares_sold"
            ),
        },
        "cash": {
            "option_trade_cash_gross": _metric(facts, {"option_trade_cash_gross"}, fx_rates=fx_rates).to_dict(),
            "option_fee_cash": _metric(facts, {"option_fee_cash"}, fx_rates=fx_rates).to_dict(),
            "stock_settlement_cash_gross": _metric(facts, {"stock_settlement_cash_gross"}, fx_rates=fx_rates).to_dict(),
            "stock_settlement_fee_cash": _metric(
                facts, {"stock_settlement_fee_cash"}, fx_rates=fx_rates
            ).to_dict(),
            "assigned_stock_sale_cash_gross": _metric(
                facts, {"assigned_stock_sale_cash_gross"}, fx_rates=fx_rates
            ).to_dict(),
            "assigned_stock_sale_fee_cash": _metric(
                facts, {"assigned_stock_sale_fee_cash"}, fx_rates=fx_rates
            ).to_dict(),
            "total_cash_change_net": _metric(facts, _CASH_NET_KINDS, fx_rates=fx_rates).to_dict(),
        },
        "pnl": {
            "realized_gross": _metric(facts, {"realized_gross"}, fx_rates=fx_rates).to_dict(),
            "realized_net": _metric(facts, {"realized_net"}, fx_rates=fx_rates).to_dict(),
            "opening_unrealized_gross": _metric(facts, {"opening_unrealized_gross"}, fx_rates=fx_rates).to_dict(),
            "opening_unrealized_net": _metric(facts, {"opening_unrealized_net"}, fx_rates=fx_rates).to_dict(),
            "ending_unrealized_gross": _metric(facts, {"ending_unrealized_gross"}, fx_rates=fx_rates).to_dict(),
            "ending_unrealized_net": _metric(facts, {"ending_unrealized_net"}, fx_rates=fx_rates).to_dict(),
            "period_total_gross": _period_total_metric(facts, net=False, fx_rates=fx_rates).to_dict(),
            "period_total_net": _period_total_metric(facts, net=True, fx_rates=fx_rates).to_dict(),
        },
    }


def _metric(
    facts: Sequence[PerformanceFact],
    kinds: set[str] | frozenset[str],
    *,
    fx_rates: Sequence[FXRateFact] = (),
) -> DecimalAmountEnvelope:
    selected = [fact for fact in facts if fact.fact_kind in kinds]
    if not selected:
        return DecimalAmountEnvelope(quality=MetricQuality(MetricStatus.NOT_OBSERVED))
    sums: dict[str, Decimal] = {}
    incomplete_currencies: set[str] = set()
    missing: list[str] = []
    evidence_fact_ids: list[str] = []
    fx_fact_ids: list[str] = []
    cny_sum = Decimal(0)
    cny_complete = True
    for fact in selected:
        currency = str(fact.currency or "")
        evidence_fact_ids.extend(fact.evidence_fact_ids)
        if fact.amount is None:
            incomplete_currencies.add(currency)
            missing.append(fact.fact_id)
            cny_complete = False
            continue
        sums[currency] = quantize_money(sums.get(currency, Decimal(0)) + fact.amount)
        if currency == "CNY":
            cny_sum = quantize_money(cny_sum + fact.amount)
            continue
        selection = select_fx_rate(list(fx_rates), base_currency=currency, at_ms=fact.effective_at_ms)
        if selection.fact is None:
            cny_complete = False
            missing.append(f"fx:{currency}:{fact.fact_id}")
            continue
        rate = selection.fact
        assert isinstance(rate, FXRateFact)
        cny_sum = quantize_money(cny_sum + fact.amount * rate.rate)
        fx_fact_ids.append(str(rate.fact_id))
    for currency in incomplete_currencies:
        sums.pop(currency, None)
    status = MetricStatus.PARTIAL if missing else MetricStatus.OBSERVED
    return DecimalAmountEnvelope(
        by_currency=sums,
        cny=cny_sum if cny_complete else None,
        quality=MetricQuality(
            status=status,
            missing=tuple(sorted(set(missing))),
            evidence_fact_ids=tuple(dict.fromkeys(evidence_fact_ids)),
        ),
        fx_fact_ids=tuple(dict.fromkeys(fx_fact_ids)),
    )


def _period_total_metric(
    facts: Sequence[PerformanceFact],
    *,
    net: bool,
    fx_rates: Sequence[FXRateFact],
) -> DecimalAmountEnvelope:
    realized = "realized_net" if net else "realized_gross"
    opening = "opening_unrealized_net" if net else "opening_unrealized_gross"
    ending = "ending_unrealized_net" if net else "ending_unrealized_gross"
    components: list[PerformanceFact] = []
    for fact in facts:
        if fact.fact_kind == realized or fact.fact_kind == ending:
            components.append(fact)
        elif fact.fact_kind == opening:
            components.append(replace(fact, amount=None if fact.amount is None else -fact.amount))
    return _metric(components, {realized, opening, ending}, fx_rates=fx_rates)


def _breakdown(
    facts: Sequence[PerformanceFact],
    *,
    key_name: str,
    key_fn: Any,
    fx_rates: Sequence[FXRateFact] = (),
) -> list[dict[str, Any]]:
    groups: dict[str, list[PerformanceFact]] = {}
    for fact in facts:
        key = str(key_fn(fact) or "").strip()
        if key:
            groups.setdefault(key, []).append(fact)
    out: list[dict[str, Any]] = []
    for key in sorted(groups):
        item = {key_name: key}
        item.update(_summarize(groups[key], fx_rates=fx_rates))
        out.append(item)
    return out


def _month_for_fact(fact: PerformanceFact, *, period: PeriodWindow | None = None) -> str:
    tz = ZoneInfo(REPORTING_TIMEZONE)
    timestamp_ms = fact.effective_at_ms
    if period is not None:
        timestamp_ms = min(
            max(timestamp_ms, period.effective_start_at_ms),
            period.effective_end_exclusive_at_ms - 1,
        )
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=tz).strftime("%Y-%m")


def _fx_quality_for_facts(
    facts: Sequence[PerformanceFact],
    *,
    fx_rates: Sequence[FXRateFact],
) -> tuple[set[str], tuple[str, ...]]:
    missing: set[str] = set()
    selected_ids: list[str] = []
    for fact in facts:
        if fact.fact_kind not in _MONETARY_KINDS or fact.amount is None or not fact.currency:
            continue
        if fact.currency == "CNY":
            continue
        selection = select_fx_rate(list(fx_rates), base_currency=fact.currency, at_ms=fact.effective_at_ms)
        if selection.fact is None:
            missing.add(f"fx:{fact.currency}:{fact.fact_id}")
            continue
        selected_ids.append(str(selection.fact.fact_id))
    return missing, tuple(dict.fromkeys(selected_ids))


def _diagnostic_text(item: Mapping[str, Any] | str) -> str:
    if isinstance(item, str):
        return item.strip()
    code = str(item.get("code") or "").strip()
    event_id = str(item.get("event_id") or "").strip()
    if code and event_id:
        return f"{code}:{event_id}"
    return code or str(item.get("message") or "").strip()


def _diagnostic_is_relevant(
    item: Mapping[str, Any] | str,
    *,
    event_ids: set[str],
    period: PeriodWindow,
    account_filter: str,
    broker_filter: str,
) -> bool:
    if isinstance(item, str):
        return True
    if str(item.get("context") or "").strip() in {"valuation", "assigned_stock"}:
        diagnostic_account = normalize_account(item.get("account"))
        diagnostic_broker = normalize_broker(str(item.get("broker") or ""))
        return (not account_filter or diagnostic_account == account_filter) and (
            not broker_filter or diagnostic_broker == broker_filter
        )
    event_id = str(item.get("event_id") or "").strip()
    if event_id and event_id in event_ids:
        return True
    try:
        event_time_ms = int(item.get("event_time_ms") or 0)
    except (TypeError, ValueError):
        event_time_ms = 0
    if event_time_ms <= 0:
        return not event_id
    if not period.contains(event_time_ms):
        return False
    diagnostic_account = normalize_account(item.get("account"))
    diagnostic_broker = normalize_broker(str(item.get("broker") or ""))
    return (not account_filter or diagnostic_account == account_filter) and (
        not broker_filter or diagnostic_broker == broker_filter
    )


__all__ = ["PerformanceFact", "PeriodPerformance", "build_period_performance"]
