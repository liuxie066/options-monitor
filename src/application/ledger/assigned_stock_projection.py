from __future__ import annotations

from typing import Any, Mapping

from domain.domain.assigned_stock import (
    assigned_stock_allocation_row,
    assigned_stock_event_time_ms,
    assigned_stock_position_lot_row,
    assigned_stock_trade_event_row,
    project_assigned_stock_lifecycle,
)
from domain.domain.performance.models import (
    StockInstrumentKey,
    ValuationMarkFact,
    select_valuation_mark,
)
from src.application.ledger.event_codec import (
    stored_trade_event_to_ledger_event,
    valid_void_target_event_id,
)
from src.application.ledger.queries import project_trade_event_log


def _event_time_ms(row: Mapping[str, Any]) -> int:
    try:
        return int(row.get("event_time_ms") or row.get("trade_time_ms") or 0)
    except (TypeError, ValueError):
        return 0


def _quote_rows(
    valuation_marks: tuple[ValuationMarkFact, ...] | list[ValuationMarkFact],
    *,
    at_ms: int,
) -> list[dict[str, Any]]:
    instruments = {
        item.instrument_key: item.instrument
        for item in valuation_marks
        if isinstance(item.instrument, StockInstrumentKey)
    }
    rows: list[dict[str, Any]] = []
    for key in sorted(instruments):
        instrument = instruments[key]
        selection = select_valuation_mark(
            list(valuation_marks),
            instrument_key=instrument.instrument_key,
            at_ms=at_ms,
        )
        fact = selection.fact
        if not isinstance(fact, ValuationMarkFact):
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


def _raw_quote_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        value = value.get("rows") or value.get("quote_snapshots") or [value]
    return [dict(item) for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def project_assigned_stock_lifecycle_from_rows(
    rows: Mapping[str, Any],
    *,
    as_of_ms: int,
    valuation_marks: tuple[ValuationMarkFact, ...] | list[ValuationMarkFact] = (),
    quote_snapshots: Any = None,
    account: str | None = None,
    broker: str | None = None,
    stock_holdings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    instant = int(as_of_ms)
    selected_rows = [
        dict(row)
        for row in rows.get("trade_events") or []
        if isinstance(row, Mapping)
        and (_event_time_ms(row) <= instant or valid_void_target_event_id(row) is not None)
    ]
    published = project_trade_event_log(selected_rows)
    projection = published.ledger_projection
    current_fields = {item.record_id: item.fields for item in published.lots}
    selected_ids = {
        str(row.get("event_id") or "").strip()
        for row in selected_rows
        if str(row.get("event_id") or "").strip()
    }
    events = [stored_trade_event_to_ledger_event(row)[0] for row in selected_rows]
    return project_assigned_stock_lifecycle(
        [
            assigned_stock_trade_event_row(event)
            for event in events
            if event is not None and event.event_id in selected_ids
        ],
        assignment_option_rows=[assigned_stock_allocation_row(item) for item in projection.allocations],
        option_open_lots=[
            assigned_stock_position_lot_row(
                item,
                current_fields=current_fields.get(item.lot_id),
                valuation_marks=valuation_marks,
                at_ms=instant,
            )
            for item in projection.lots
        ],
        assigned_stock_events=[
            dict(item)
            for item in rows.get("account_assigned_stock_events") or []
            if isinstance(item, Mapping) and assigned_stock_event_time_ms(item) <= instant
        ],
        quote_snapshots=[*_quote_rows(valuation_marks, at_ms=instant), *_raw_quote_rows(quote_snapshots)],
        stock_holdings=stock_holdings,
        account_norm=str(account or "").strip().lower() or None,
        broker_norm=str(broker or "").strip() or None,
        month=None,
        as_of_ms=instant,
    )


__all__ = ["project_assigned_stock_lifecycle_from_rows"]
