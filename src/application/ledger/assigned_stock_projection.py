from __future__ import annotations

from typing import Any, Mapping

from domain.domain.assigned_stock import (
    assigned_stock_allocation_row,
    assigned_stock_event_time_ms,
    assigned_stock_position_lot_row,
    assigned_stock_trade_event_row,
    project_assigned_stock_lifecycle,
)
from domain.domain.ledger.events import LedgerDiagnostic, TradeEvent
from domain.domain.option_position_identity import normalize_broker
from domain.domain.performance.models import (
    StockInstrumentKey,
    ValuationMarkFact,
    select_valuation_mark,
)
from domain.domain.wheel import (
    attach_lot_strategy_metadata,
    lot_strategy_metadata_from_trade_events,
    merge_lot_strategy_metadata,
)
from src.application.ledger.event_codec import (
    stored_trade_event_to_ledger_event,
    valid_void_target_event_id,
)
from src.application.ledger.queries import project_trade_event_log
from src.application.ledger.publisher import PublishedPositionLotProjection


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


def _require_trusted_assigned_stock_projection(
    diagnostics: list[LedgerDiagnostic],
    events: list[TradeEvent],
    *,
    account: str | None,
    broker: str | None = None,
) -> None:
    """Keep scoped economics unavailable when the authoritative event graph is invalid."""
    events_by_id: dict[str, list[TradeEvent]] = {}
    for event in events:
        if event is not None:
            events_by_id.setdefault(event.event_id, []).append(event)
    account_filter = str(account or "").strip().lower()
    broker_filter = normalize_broker(str(broker or "").strip())
    errors = set()
    for diagnostic in diagnostics:
        if diagnostic.severity != "error":
            continue
        affected = list(events_by_id.get(diagnostic.event_id, ()))
        unknown = not affected
        for event in tuple(affected):
            if event.target_event_id:
                targets = events_by_id.get(event.target_event_id, ())
                unknown = unknown or not targets
                affected.extend(targets)
        diagnostic_in_scope = bool(diagnostic.account or diagnostic.broker) and (
            (not account_filter or not diagnostic.account or diagnostic.account == account_filter)
            and (not broker_filter or not diagnostic.broker or diagnostic.broker == broker_filter)
        )
        if unknown or diagnostic_in_scope or any(
            (not account_filter or event.contract_key.account == account_filter)
            and (not broker_filter or event.contract_key.broker == broker_filter)
            for event in affected
        ):
            errors.add(diagnostic.code)
    if errors:
        raise ValueError("assigned-stock ledger projection is untrusted: " + ", ".join(sorted(errors)))


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
    selected_rows = _select_assigned_stock_trade_rows(rows, instant)
    published = project_trade_event_log(selected_rows)
    return _project_assigned_stock_from_prepared_rows(
        rows, selected_rows=selected_rows, published=published, instant=instant,
        valuation_marks=valuation_marks, quote_snapshots=quote_snapshots,
        account=account, broker=broker, stock_holdings=stock_holdings,
    )


def _select_assigned_stock_trade_rows(
    rows: Mapping[str, Any], instant: int,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in rows.get("trade_events") or []
        if isinstance(row, Mapping)
        and (_event_time_ms(row) <= instant or valid_void_target_event_id(row) is not None)
    ]


def project_position_lots_and_assigned_stock_from_rows(
    rows: Mapping[str, Any], *, account: str, as_of_ms: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Compose Wheel's scoped source rows without accepting prepared projections."""
    instant = int(as_of_ms)
    trade_events = rows.get("trade_events") or []
    published = project_trade_event_log(trade_events)
    # Keep Wheel's original failure order: publish, metadata, attach, then
    # assigned-stock selection and trust checks.
    strategy_by_lot_id = lot_strategy_metadata_from_trade_events(trade_events)
    position_lots = [
        {
            "record_id": item.lot_id,
            "fields": attach_lot_strategy_metadata(
                {"record_id": item.lot_id, "fields": dict(item.fields)},
                strategy_by_lot_id,
            ),
        }
        for item in published.lots
    ]
    selected_rows = _select_assigned_stock_trade_rows(rows, instant)
    # Its trade_time fallback and late void rules can select different rows.
    assigned_projection = (
        published if selected_rows == trade_events else project_trade_event_log(selected_rows)
    )
    assigned_stock = _project_assigned_stock_from_prepared_rows(
        rows, selected_rows=selected_rows, published=assigned_projection,
        instant=instant, account=account,
    )
    return position_lots, assigned_stock


def _project_assigned_stock_from_prepared_rows(
    rows: Mapping[str, Any], *, selected_rows: list[dict[str, Any]],
    published: PublishedPositionLotProjection, instant: int,
    valuation_marks: tuple[ValuationMarkFact, ...] | list[ValuationMarkFact] = (),
    quote_snapshots: Any = None, account: str | None = None,
    broker: str | None = None, stock_holdings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    events = [stored_trade_event_to_ledger_event(row)[0] for row in selected_rows]
    _require_trusted_assigned_stock_projection(
        published.diagnostics, [event for event in events if event is not None],
        account=account, broker=broker,
    )
    projection = published.ledger_projection
    current_fields = {item.lot_id: item.fields for item in published.lots}
    # The strategy-metadata family is read from the event layer now (design
    # §7.5), so each lot is handed the metadata its own events declare.
    strategy_by_lot_id = lot_strategy_metadata_from_trade_events(
        [row for row in selected_rows if _event_time_ms(row) <= instant])
    selected_ids = {
        str(row.get("event_id") or "").strip()
        for row in selected_rows
        if str(row.get("event_id") or "").strip()
    }
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
                current_fields=merge_lot_strategy_metadata(
                    current_fields.get(item.lot_id) or {},
                    strategy_by_lot_id.get(item.lot_id, {}),
                ),
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


__all__ = [
    "project_assigned_stock_lifecycle_from_rows",
    "project_position_lots_and_assigned_stock_from_rows",
]
