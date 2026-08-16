from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from domain.domain.assigned_stock import (
    assigned_stock_allocation_row,
    assigned_stock_event_time_ms,
    assigned_stock_position_lot_row,
    assigned_stock_trade_event_row,
    project_assigned_stock_lifecycle,
)
from domain.domain.ledger import ContractKey, OptionEconomicAllocation, PositionLot, TradeEvent, fee_fact_for_event
from domain.domain.performance.attribution import resolve_event_attribution
from domain.domain.performance.models import (
    FeeBasis,
    OptionInstrumentKey,
    StockInstrumentKey,
    ValuationMarkFact,
    select_valuation_mark,
    OptionValuationPosition,
    quantize_money,
)
from src.application.ledger import api as ledger_api


@dataclass(frozen=True)
class LedgerPerformanceInputs:
    rows: tuple[dict[str, Any], ...]
    events: tuple[TradeEvent, ...]
    allocations: tuple[OptionEconomicAllocation, ...]
    position_lots: tuple[PositionLot, ...]
    assigned_stock_events: tuple[dict[str, Any], ...]
    diagnostics: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class OptionValuationInputs:
    positions: tuple[OptionValuationPosition, ...]
    diagnostics: tuple[dict[str, Any], ...]


def load_ledger_performance_inputs(repo: Any) -> LedgerPerformanceInputs:
    rows = ledger_api.trade_event_log(repo)
    projection = ledger_api.project_trade_event_log(rows)
    metadata_by_event_id = {
        str(row.get("event_id") or "").strip(): _diagnostic_metadata(row)
        for row in rows
        if str(row.get("event_id") or "").strip()
    }
    events: list[TradeEvent] = []
    adapter_diagnostics: list[dict[str, Any]] = []
    for row in rows:
        try:
            events.append(_trade_event_from_application_payload(row))
        except (TypeError, ValueError) as exc:
            adapter_diagnostics.append(
                {
                    "event_id": str(row.get("event_id") or "").strip(),
                    "severity": "error",
                    "code": "performance_event_decode_failed",
                    "message": str(exc),
                    **_diagnostic_metadata(row),
                }
            )
    diagnostics = []
    for item in projection.diagnostics:
        payload = item.to_dict()
        payload.update(metadata_by_event_id.get(item.event_id, {}))
        diagnostics.append(payload)
    diagnostics.extend(adapter_diagnostics)
    assigned_stock_log = ledger_api.assigned_stock_event_log(repo)
    diagnostics.extend(dict(item) for item in assigned_stock_log.diagnostics)
    return LedgerPerformanceInputs(
        rows=tuple(dict(row) for row in rows),
        events=tuple(events),
        allocations=tuple(projection.ledger_projection.allocations),
        position_lots=tuple(projection.ledger_projection.lots),
        assigned_stock_events=tuple(dict(item) for item in assigned_stock_log.events),
        diagnostics=tuple(_dedupe_diagnostics(diagnostics)),
    )


def load_option_valuation_inputs(
    inputs: LedgerPerformanceInputs,
    *,
    as_of_ms: int,
    account: str | None = None,
    broker: str | None = None,
) -> OptionValuationInputs:
    instant = int(as_of_ms)
    rows = [
        row
        for row in inputs.rows
        if _row_event_time_ms(row) <= instant or ledger_api.valid_void_target_event_id(row) is not None
    ]
    projection = ledger_api.project_trade_event_log(rows)
    metadata_by_event_id = {
        str(row.get("event_id") or "").strip(): _diagnostic_metadata(row)
        for row in rows
        if str(row.get("event_id") or "").strip()
    }
    events: list[TradeEvent] = []
    diagnostics: list[dict[str, Any]] = []
    for row in rows:
        try:
            events.append(_trade_event_from_application_payload(row))
        except (TypeError, ValueError) as exc:
            diagnostics.append(
                {
                    "event_id": str(row.get("event_id") or "").strip(),
                    "code": "valuation_event_decode_failed",
                    "message": str(exc),
                    "context": "valuation",
                    **_diagnostic_metadata(row),
                }
            )
    events_by_id = {event.event_id: event for event in events}
    allocations_by_open: dict[str, list[OptionEconomicAllocation]] = {}
    for allocation in projection.ledger_projection.allocations:
        allocations_by_open.setdefault(allocation.open_event_id, []).append(allocation)
    account_filter = str(account or "").strip().lower()
    broker_filter = str(broker or "").strip()
    positions: list[OptionValuationPosition] = []
    for lot in projection.ledger_projection.lots:
        if int(lot.contracts_open) <= 0:
            continue
        if account_filter and lot.contract_key.account != account_filter:
            continue
        from domain.domain.option_position_identity import normalize_broker

        if broker_filter and normalize_broker(lot.contract_key.broker) != normalize_broker(broker_filter):
            continue
        open_event = events_by_id.get(lot.open_event_id)
        if open_event is None:
            diagnostics.append(
                {
                    "event_id": lot.open_event_id,
                    "code": "valuation_open_event_missing",
                    "message": f"open event missing for lot {lot.lot_id}",
                    "context": "valuation",
                    "account": lot.contract_key.account,
                    "broker": lot.contract_key.broker,
                }
            )
            continue
        try:
            instrument = OptionInstrumentKey.from_contract_key(
                lot.contract_key,
                currency=lot.currency,
                multiplier=lot.multiplier,
            )
            fee = fee_fact_for_event(open_event)
            allocated = allocations_by_open.get(lot.open_event_id, [])
            if (
                fee.basis == FeeBasis.ACTUAL
                and fee.amount is not None
                and all(
                    item.allocated_open_fee.basis == FeeBasis.ACTUAL and item.allocated_open_fee.amount is not None
                    for item in allocated
                )
            ):
                allocated_amount = sum(
                    (
                        item.allocated_open_fee.amount
                        for item in allocated
                        if item.allocated_open_fee.amount is not None
                    ),
                    start=quantize_money(0),
                )
                remaining_fee = quantize_money(fee.amount - allocated_amount)
                fee_quality = FeeBasis.ACTUAL.value
            else:
                remaining_fee = None
                fee_quality = fee.basis.value
            attribution_resolution = resolve_event_attribution(
                open_event,
                lifecycle_source_id=lot.lot_id,
            )
            positions.append(
                OptionValuationPosition(
                    lot_id=lot.lot_id,
                    account=lot.contract_key.account,
                    broker=lot.contract_key.broker,
                    instrument=instrument,
                    position_side=lot.contract_key.position_side,
                    contracts_open=lot.contracts_open,
                    open_price=lot.premium_open,
                    open_fee_remaining=remaining_fee,
                    open_fee_quality=fee_quality,
                    opened_at_ms=lot.opened_at_ms,
                    market_code=_event_market_code(open_event),
                    attribution=attribution_resolution.attribution,
                    attribution_issues=attribution_resolution.issues,
                )
            )
        except (TypeError, ValueError) as exc:
            diagnostics.append(
                {
                    "event_id": lot.open_event_id,
                    "code": "valuation_position_decode_failed",
                    "message": str(exc),
                    "context": "valuation",
                    "event_time_ms": lot.opened_at_ms,
                    "account": lot.contract_key.account,
                    "broker": lot.contract_key.broker,
                }
            )
    for item in projection.diagnostics:
        payload = item.to_dict()
        payload["context"] = "valuation"
        payload.update(metadata_by_event_id.get(item.event_id, {}))
        diagnostics.append(payload)
    return OptionValuationInputs(
        positions=tuple(sorted(positions, key=lambda item: (item.account, item.lot_id))),
        diagnostics=tuple(_dedupe_diagnostics(diagnostics)),
    )


def _row_event_time_ms(payload: dict[str, Any]) -> int:
    try:
        return int(payload.get("event_time_ms") or payload.get("trade_time_ms") or 0)
    except (TypeError, ValueError):
        return 0


def _event_market_code(event: TradeEvent) -> str | None:
    payload = event.raw_payload if isinstance(event.raw_payload, dict) else {}
    fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
    for key in ("contract_symbol", "option_code", "contract_code", "code"):
        raw = str(payload.get(key) or fields.get(key) or "").strip()
        if raw:
            return raw
    return None


def _trade_event_from_application_payload(payload: dict[str, Any]) -> TradeEvent:
    raw_key = payload.get("contract_key")
    if not isinstance(raw_key, dict):
        raise ValueError("contract_key must be an object")
    raw_payload = dict(payload.get("raw_payload") or {})
    if isinstance(payload.get("fee_provenance"), dict) and "fee_provenance" not in raw_payload:
        raw_payload["fee_provenance"] = dict(payload["fee_provenance"])
    return TradeEvent(
        event_id=str(payload.get("event_id") or "").strip(),
        event_type=str(payload.get("event_type") or "").strip(),
        event_time_ms=int(payload.get("event_time_ms") or payload.get("trade_time_ms") or 0),
        contract_key=ContractKey.from_values(
            broker=raw_key.get("broker"),
            account=raw_key.get("account"),
            underlying_symbol=raw_key.get("underlying_symbol") or raw_key.get("symbol"),
            option_type=raw_key.get("option_type"),
            position_side=raw_key.get("position_side") or raw_key.get("side"),
            strike=raw_key.get("strike"),
            expiration_ymd=raw_key.get("expiration_ymd") or raw_key.get("expiration"),
        ),
        contracts=int(payload.get("contracts") or 0),
        price=float(payload.get("price") or 0),
        currency=str(payload.get("currency") or ""),
        source=str(payload.get("source") or payload.get("source_name") or ""),
        multiplier=float(payload.get("multiplier") or 0),
        fees=float(payload.get("fees") or 0),
        target_lot_id=_optional_id(payload.get("target_lot_id")),
        target_event_id=_optional_id(payload.get("target_event_id")),
        lot_id=_optional_id(payload.get("lot_id")),
        raw_payload=raw_payload,
    )


def _optional_id(value: Any) -> str | None:
    raw = str(value or "").strip()
    return raw or None


def _diagnostic_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    raw_key = payload.get("contract_key")
    contract_key = raw_key if isinstance(raw_key, dict) else {}
    try:
        event_time_ms = int(payload.get("event_time_ms") or payload.get("trade_time_ms") or 0)
    except (TypeError, ValueError):
        event_time_ms = 0
    return {
        "event_time_ms": event_time_ms,
        "account": str(contract_key.get("account") or payload.get("account") or "").strip().lower(),
        "broker": str(contract_key.get("broker") or payload.get("broker") or "").strip(),
    }


def _dedupe_diagnostics(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        key = (
            str(item.get("event_id") or ""),
            str(item.get("code") or ""),
            str(item.get("message") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(item))
    return out


def load_assigned_stock_projection(
    inputs: LedgerPerformanceInputs,
    *,
    as_of_ms: int,
    valuation_marks: tuple[ValuationMarkFact, ...] | list[ValuationMarkFact] = (),
    quote_snapshots: Any = None,
    account: str | None = None,
    broker: str | None = None,
    stock_holdings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    instant = int(as_of_ms)
    rows = [
        row
        for row in inputs.rows
        if _row_event_time_ms(row) <= instant or ledger_api.valid_void_target_event_id(row) is not None
    ]
    published = ledger_api.project_trade_event_log(rows)
    projection = published.ledger_projection
    current_fields_by_lot_id = {
        item.record_id: item.fields for item in published.lots
    }
    selected_event_ids = {
        str(row.get("event_id") or "").strip()
        for row in rows
        if str(row.get("event_id") or "").strip()
    }
    event_rows = [
        assigned_stock_trade_event_row(event)
        for event in inputs.events
        if event.event_id in selected_event_ids
    ]
    allocation_rows = [
        assigned_stock_allocation_row(item) for item in projection.allocations
    ]
    option_lot_rows = [
        assigned_stock_position_lot_row(
            item,
            current_fields=current_fields_by_lot_id.get(item.lot_id),
            valuation_marks=valuation_marks,
            at_ms=instant,
        )
        for item in projection.lots
    ]
    quote_rows = [
        *_stock_quote_rows(valuation_marks, at_ms=instant),
        *_raw_stock_quote_rows(quote_snapshots),
    ]
    return project_assigned_stock_lifecycle(
        event_rows,
        assignment_option_rows=allocation_rows,
        option_open_lots=option_lot_rows,
        assigned_stock_events=[
            dict(item)
            for item in inputs.assigned_stock_events
            if assigned_stock_event_time_ms(item) <= instant
        ],
        quote_snapshots=quote_rows,
        stock_holdings=stock_holdings,
        account_norm=str(account or "").strip().lower() or None,
        broker_norm=str(broker or "").strip() or None,
        month=None,
        as_of_ms=instant,
    )


def assigned_stock_instruments(projection: dict[str, Any]) -> tuple[StockInstrumentKey, ...]:
    instruments: dict[str, StockInstrumentKey] = {}
    for row in projection.get("assigned_stock_lots") or []:
        if not isinstance(row, dict) or int(row.get("shares_remaining") or 0) <= 0:
            continue
        try:
            instrument = StockInstrumentKey(symbol=row.get("symbol"), currency=row.get("currency"))
        except ValueError:
            continue
        instruments[instrument.instrument_key] = instrument
    return tuple(instruments[key] for key in sorted(instruments))


def _stock_quote_rows(valuation_marks: tuple[ValuationMarkFact, ...] | list[ValuationMarkFact], *, at_ms: int) -> list[dict[str, Any]]:
    stock_instruments = {
        item.instrument_key: item.instrument
        for item in valuation_marks
        if isinstance(item.instrument, StockInstrumentKey)
    }
    rows: list[dict[str, Any]] = []
    for key in sorted(stock_instruments):
        instrument = stock_instruments[key]
        selection = select_valuation_mark(
            list(valuation_marks),
            instrument_key=instrument.instrument_key,
            at_ms=at_ms,
        )
        fact = selection.fact
        if fact is None or not isinstance(fact, ValuationMarkFact):
            continue
        rows.append(
            {
                "symbol": instrument.symbol,
                "currency": instrument.currency,
                "spot": float(fact.price),
                "quote_time_ms": fact.effective_at_ms,
                "quote_source": fact.source,
                "quote_status": "stale" if selection.status == "stale" else "fresh",
                "evidence_fact_id": fact.fact_id,
            }
        )
    return rows


def _raw_stock_quote_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get("rows") or value.get("quote_snapshots") or [value]
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


__all__ = [
    "LedgerPerformanceInputs",
    "OptionValuationInputs",
    "assigned_stock_instruments",
    "load_assigned_stock_projection",
    "load_ledger_performance_inputs",
    "load_option_valuation_inputs",
]
