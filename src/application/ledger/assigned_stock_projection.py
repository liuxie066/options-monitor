from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from domain.domain.trade_contract_identity import contract_share_quantity
from domain.domain.symbol_identity import symbol_market
from domain.domain.wheel import project_wheel_branches, project_wheel_coverage, project_wheel_linkage_candidates

from typing import Any, Mapping

from domain.domain.assigned_stock import (
    _AssignedStockInventoryMemo,
    assigned_stock_allocation_row,
    assigned_stock_event_time_ms,
    assigned_stock_position_lot_row,
    assigned_stock_trade_event_row,
    project_assigned_stock_lifecycle,
)
from domain.domain.ledger.events import TARGET_EVENT_TYPES, LedgerDiagnostic, TradeEvent, lot_id_for_open_event
from domain.domain.ledger.economics import OptionEconomicAllocation
from domain.domain.ledger.lots import PositionLot
from domain.domain.ledger.projection import (
    _append_target_event_graph_diagnostics,
    _canonical_combo_member,
    _validated_events,
    build_risk_position_views,
)
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
from domain.domain.wheel.projection import _WheelEventNormalizationMemo
from src.application.ledger.event_codec import (
    iter_import_stored_trade_events,
    stored_trade_event_to_ledger_event,
    valid_void_target_event_id,
)
from src.application.ledger.publisher import (
    PublishedPositionLotProjection,
    _project_decoded_trade_events,
    _project_stored_trade_events_with_decoded_events,
)


def _event_time_ms(row: Mapping[str, Any]) -> int:
    try:
        return int(row.get("event_time_ms") or row.get("trade_time_ms") or 0)
    except (TypeError, ValueError):
        return 0


class _HistoricalTradeRows:
    """Share prepared trade evidence within one view of ordinary history."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = [(_event_time_ms(row), row) for row in rows]
        self._decoded: dict[int, tuple[TradeEvent | None, list[LedgerDiagnostic]]] = {}
        self._wheel_compatible: dict[int, bool] = {}
        self._opening_lots: dict[int, tuple[TradeEvent, PositionLot]] = {}
        self._opening_projections: dict[int, PublishedPositionLotProjection | None] = {}
        self._prefix_key: tuple[int, ...] = ()
        self._prefix: PublishedPositionLotProjection | None = None
        self._assigned_stock_rows: dict[int, dict[str, Any]] = {}
        self.wheel_event_normalizations: _WheelEventNormalizationMemo = {}
        self.validated_call_inputs: set[tuple[Any, ...]] = set()
        self.settlement_option_fees: dict[tuple[int, str], dict[str, Any]] = {}
        self.stock_inventory = (
            None if any(str(row.get("event_type") or "").strip().lower() == "adjust" for row in rows)
            else _AssignedStockInventoryMemo()
        )

    @classmethod
    def from_rows(cls, rows: Mapping[str, Any]) -> _HistoricalTradeRows | None:
        trade_rows = [dict(row) for row in rows.get("trade_events") or [] if isinstance(row, Mapping)]
        if any(str(row.get("event_type") or "").strip().lower() in TARGET_EVENT_TYPES for row in trade_rows):
            return None
        return cls(trade_rows)

    def select(self, instant: int) -> list[dict[str, Any]]:
        return [row for at_ms, row in self.rows if at_ms <= instant]

    def assigned_stock_trade_row(self, event: TradeEvent) -> dict[str, Any]:
        key = id(event)
        if key not in self._assigned_stock_rows:
            self._assigned_stock_rows[key] = assigned_stock_trade_event_row(event)
        return self._assigned_stock_rows[key]

    def project(self, selected_rows: list[dict[str, Any]]) -> tuple[PublishedPositionLotProjection, list[TradeEvent]]:
        events: list[TradeEvent] = []
        diagnostics: list[LedgerDiagnostic] = []
        for row in selected_rows:
            key = id(row)
            if key not in self._decoded:
                self._decoded[key] = stored_trade_event_to_ledger_event(row)
            event, import_diagnostics = self._decoded[key]
            if event is not None:
                events.append(event)
            diagnostics.extend(import_diagnostics)
        published = None if diagnostics else self._project_opening_suffix(events)
        if published is None:
            published = _project_decoded_trade_events(
                events, diagnostics, with_publication_state=False, opening_lots=self._opening_lots,
            )
        return published, events

    def _project_opening_suffix(self, events: list[TradeEvent]) -> PublishedPositionLotProjection | None:
        # Only independent option openings can be added without replaying prior
        # economics, settlement allocations, or formal Combo membership.
        cutoff = 0
        for event in events:
            if event.event_type == "adjust" or event.event_type in TARGET_EVENT_TYPES:
                return None
            if event.event_type != "open":
                cutoff = max(cutoff, event.event_time_ms)
        prefix_events = [event for event in events if event.event_time_ms <= cutoff]
        openings = [event for event in events if event.event_time_ms > cutoff]
        if not openings or len({event.event_id for event in events}) != len(events):
            return None
        prefix_key = tuple(id(event) for event in prefix_events)
        if self._prefix is None or self._prefix_key != prefix_key:
            self._prefix = _project_decoded_trade_events(
                prefix_events, [], with_publication_state=False, opening_lots=self._opening_lots,
            )
            self._prefix_key = prefix_key
        prefix = self._prefix
        if prefix.diagnostics:
            return None

        published_lots = list(prefix.lots)
        lot_ids = {record.lot_id for record in published_lots}
        ledger = prefix.ledger_projection
        lots = list(ledger.lots)
        effective_openings = [(lot_id_for_open_event(event), event) for event in ledger.effective_open_events]
        cash_events = list(ledger.effective_cash_events)
        for event in sorted(openings, key=lambda item: (item.event_time_ms, item.event_id)):
            key = id(event)
            if key not in self._opening_projections:
                opening = None
                if event.asset_type == "option" and _canonical_combo_member(event) is None:
                    isolated = _project_decoded_trade_events(
                        [event], [], with_publication_state=False, opening_lots=self._opening_lots,
                    )
                    if not isolated.diagnostics and len(isolated.lots) == 1:
                        opening = isolated
                self._opening_projections[key] = opening
            opening = self._opening_projections[key]
            if opening is None or opening.lots[0].lot_id in lot_ids:
                return None
            record = opening.lots[0]
            lot_ids.add(record.lot_id)
            published_lots.append(record)
            lots.extend(opening.ledger_projection.lots)
            effective_openings.extend((record.lot_id, item) for item in opening.ledger_projection.effective_open_events)
            cash_events.extend(opening.ledger_projection.effective_cash_events)
        return replace(
            prefix,
            lots=published_lots,
            ledger_projection=replace(
                ledger, lots=lots, views=build_risk_position_views(lots),
                effective_open_events=tuple(event for _, event in sorted(effective_openings)),
                effective_cash_events=tuple(cash_events),
            ),
        )

    def wheel_allocations(
        self, selected_rows: list[dict[str, Any]], published: PublishedPositionLotProjection,
    ) -> list[OptionEconomicAllocation] | None:
        for row in selected_rows:
            key = id(row)
            if key not in self._wheel_compatible:
                event, _ = self._decoded[key]
                try:
                    # The storage codec and Wheel's direct decoder can interpret
                    # legacy aliases differently. Reuse only equivalent events.
                    direct = TradeEvent.from_dict(row) if event is not None else None
                    self._wheel_compatible[key] = (
                        direct is not None and event == direct
                        and event.fees.as_tuple() == direct.fees.as_tuple()
                    )
                except (TypeError, ValueError, ArithmeticError):
                    self._wheel_compatible[key] = False
            if not self._wheel_compatible[key]:
                return None
        return published.ledger_projection.allocations


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
    if not diagnostics:
        return
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
    selected_rows = _select_assigned_stock_trade_rows(rows, instant, account=account, broker=broker)
    published, events = _project_stored_trade_events_with_decoded_events(
        selected_rows, with_publication_state=False,
    )
    return _project_assigned_stock_from_prepared_rows(
        rows, selected_rows=selected_rows, published=published, events=events, instant=instant,
        valuation_marks=valuation_marks, quote_snapshots=quote_snapshots,
        account=account, broker=broker, stock_holdings=stock_holdings,
    )


def qualify_assigned_stock_sell_observations(
    rows: Mapping[str, Any], observations: list[dict[str, Any]], *, as_of_ms: int,
) -> list[dict[str, Any]]:
    """Qualify source observations against accepted effects and historical inventory.

    Source status alone cannot resolve a fill, and a currently closed batch may
    still have been a candidate at an earlier uncertain disposal. Reuse the same
    canonical projection at the disposal's instant instead of deriving inventory
    with a separate quantity algorithm.
    """
    from domain.domain.assigned_stock import assigned_stock_sale_allocations
    from domain.domain.option_position_identity import normalize_broker
    from domain.domain.trade_execution import structured_deal_keys_from_ledger_event

    def keys(item: Mapping[str, Any]) -> set[str]:
        # A native namespace identity must not fall back to an unnamespaced ID.
        native = str(item.get("execution_id") or "")
        durable = str(item.get("broker_deal_key") or "")
        if native.startswith("execution:v1:"):
            return {native}
        if durable.startswith("execution:v1:"):
            return {durable}
        return structured_deal_keys_from_ledger_event(
            {"account": item.get("account"), "raw_payload": dict(item)},
        ) | ({durable} if durable else set())

    def economic(item: Mapping[str, Any]) -> tuple[Any, ...] | None:
        if any(item.get(k) is None for k in (
            "account", "futu_account_id", "symbol", "currency", "shares", "price", "trade_time_ms",
        )):
            return None
        try:
            amount = Decimal(str(item["price"]))
            quantity = Decimal(str(item["shares"]))
            if not amount.is_finite() or amount < 0 or not quantity.is_finite() or quantity <= 0:
                return None
            return (item["account"], str(item["futu_account_id"]), normalize_broker(item.get("broker")),
                    item["symbol"], item["currency"], quantity, amount, int(item["trade_time_ms"]))
        except (ValueError, TypeError, InvalidOperation):
            return None

    current = project_assigned_stock_lifecycle_from_rows(rows, as_of_ms=as_of_ms, quote_snapshots=[])
    accepted = {r["stock_event_id"] for r in current.get("assigned_stock_sale_rows") or []}
    accepted_sales = []
    for sale in rows.get("account_assigned_stock_events") or []:
        try:
            children = assigned_stock_sale_allocations(sale)
            if (children and all(child.get("stock_event_id") in accepted for child in children)
                    and sum(int(c.get("shares") or 0) for c in children) == int(sale.get("shares") or 0)):
                accepted_sales.append(sale)
        except (ValueError, TypeError):
            continue
    qualified = []
    for observation in observations:
        item = dict(observation)
        proof = economic(item)
        identity = keys(item)
        item["resolved"] = not item.get("identity_errors") and proof is not None and bool(identity) and any(
            identity & keys(sale) and economic(sale) == proof for sale in accepted_sales
        )
        qualified.append(item)
    # Sparse state is not another economic trade. Link it only to an exact
    # scoped source identity whose complete peer is proved by accepted effects.
    for item in qualified:
        if item.get("source_kind") != "state" or economic(item) is not None:
            continue
        identity = keys(item)
        peers = [p for p in qualified if p.get("source_kind") != "state" and economic(p) is not None
                 and identity and identity & keys(p)
                 and p.get("account") == item.get("account")
                 and str(p.get("futu_account_id") or "") == str(item.get("futu_account_id") or "")]
        if item.get("economic_payload_hash") and any(
            p.get("economic_payload_hash") != item["economic_payload_hash"] for p in peers
        ):
            continue
        if peers and all(p["resolved"] for p in peers) and len({economic(p) for p in peers}) == 1:
            item["resolved"] = True
            item["resolution_source_ids"] = [p.get("source_id") for p in peers]
    historical: dict[int, list[dict[str, Any]]] = {}

    def inventory_at(instant: int) -> list[dict[str, Any]]:
        if instant not in historical:
            projection = project_assigned_stock_lifecycle_from_rows(rows, as_of_ms=instant, quote_snapshots=[])
            historical[instant] = projection.get("assigned_stock_lots") or []
        return historical[instant]

    for item in qualified:
        instant = item.get("trade_time_ms")
        if item["resolved"]:
            continue
        # A presently observed revision is evidence against an already accepted
        # disposal even when the revised economic timestamp is unknown/future.
        # This does not apply the revised disposal or any future cash flow.
        identity = keys(item)
        conflict_targets: set[str] = set()
        for sale in accepted_sales:
            if (identity and identity & keys(sale)
                    and item.get("account") == sale.get("account")
                    and item.get("futu_account_id")
                    and str(item["futu_account_id"]) == str(sale.get("futu_account_id") or "")
                    and normalize_broker(item.get("broker")) == normalize_broker(sale.get("broker"))):
                conflict_targets.update(c["target_stock_lot_id"] for c in assigned_stock_sale_allocations(sale))
        item["accepted_sale_conflict"] = bool(conflict_targets)
        item["accepted_sale_conflict_lot_ids"] = sorted(conflict_targets)
        if instant is None or int(instant) > as_of_ms:
            continue
        instant = int(instant)
        # Independent fills sharing one millisecond have no proven ordering.
        # Include inventory just before and at that instant, including a batch
        # first assigned there even if a confirmed fill also closes it there.
        inventory = [*inventory_at(max(0, instant - 1)), *inventory_at(instant)]
        # An explicitly invalidated currency is evidence, not a trusted scope
        # discriminator. Keep the original value for provenance/economic proof,
        # but never use it to prove a same-account/symbol sale affects no lot.
        scope = dict(item)
        if "invalid:currency:instrument_mismatch" in (item.get("identity_errors") or []):
            scope["currency"] = None
        candidate_ids = {r["stock_lot_id"] for r in inventory
            if (int(r.get("shares_remaining") or 0) > 0 or int(r.get("assigned_at_ms") or 0) == instant)
            and all(not scope.get(k) or scope[k] == r.get(k) for k in ("account", "symbol", "currency"))
            and (not item.get("broker") or normalize_broker(item["broker"]) == normalize_broker(r.get("broker")))}
        # A revision conflicting with an accepted disposal is evidence against
        # that disposal's targets, even after it has consumed all inventory.
        # Do not let post-sale zero erase a known same-execution contradiction.
        candidate_ids.update(conflict_targets)
        item["candidate_stock_lot_ids"] = sorted(candidate_ids)
    return qualified


def _select_assigned_stock_trade_rows(
    rows: Mapping[str, Any], instant: int, *, account: str | None = None, broker: str | None = None,
) -> list[dict[str, Any]]:
    trade_rows = [dict(row) for row in rows.get("trade_events") or [] if isinstance(row, Mapping)]
    if not any(str(row.get("event_type") or "").strip().lower() in TARGET_EVENT_TYPES for row in trade_rows):
        # Only voids and repairs need targets outside the selected history.
        # The publisher still validates every selected economic event.
        return [row for row in trade_rows if _event_time_ms(row) <= instant]
    decoded = list(iter_import_stored_trade_events(trade_rows))
    events = [event for _row, event, _diagnostics in decoded if event is not None]
    validated = _validated_events(events)
    _append_target_event_graph_diagnostics(validated)
    # Validate controls against the full graph without replaying future economics.
    control_diagnostics = [
        diagnostic for row, _event, diagnostics in decoded
        if str(row.get("event_type") or "").strip() in TARGET_EVENT_TYPES
        for diagnostic in diagnostics
    ]
    control_diagnostics.extend(
        diagnostic for event, diagnostics in validated if event.event_type in TARGET_EVENT_TYPES
        for diagnostic in diagnostics
    )
    _require_trusted_assigned_stock_projection(control_diagnostics, events, account=account, broker=broker)
    historical_ids = {
        event.event_id for row, event, _diagnostics in decoded
        if event is not None and _event_time_ms(row) <= instant
    }
    future_ids = {
        event.event_id for row, event, _diagnostics in decoded
        if event is not None and _event_time_ms(row) > instant
    } - historical_ids
    return [
        row
        for row in trade_rows
        if _event_time_ms(row) <= instant
        or ((target := valid_void_target_event_id(row)) is not None and target not in future_ids)
    ]


def project_position_lots_and_assigned_stock_from_rows(
    rows: Mapping[str, Any], *, account: str, as_of_ms: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Compose Wheel's scoped source rows without accepting prepared projections."""
    lots, stock, _, _ = _project_position_lots_and_assigned_stock(rows, account=account, as_of_ms=as_of_ms)
    return lots, stock


def _project_position_lots_and_assigned_stock(
    rows: Mapping[str, Any], *, account: str, as_of_ms: int,
    historical_rows: _HistoricalTradeRows | None = None,
    inventory_only: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, dict[str, Any]], PublishedPositionLotProjection]:
    instant = int(as_of_ms)
    trade_events = rows.get("trade_events") or []
    if historical_rows is None:
        published, events = _project_stored_trade_events_with_decoded_events(
            trade_events, with_publication_state=False,
        )
    else:
        published, events = historical_rows.project(trade_events)
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
    selected_rows = (trade_events if historical_rows is not None
                     else _select_assigned_stock_trade_rows(rows, instant, account=account))
    # Its trade_time fallback and late void rules can select different rows.
    if selected_rows != trade_events:
        published, events = _project_stored_trade_events_with_decoded_events(
            selected_rows, with_publication_state=False,
        )
    assigned_stock = _project_assigned_stock_from_prepared_rows(
        rows, selected_rows=selected_rows, published=published, events=events,
        instant=instant, account=account,
        strategy_by_lot_id=strategy_by_lot_id if historical_rows is not None else None,
        historical_rows=historical_rows,
        inventory_only=inventory_only,
    )
    return position_lots, assigned_stock, strategy_by_lot_id, published


def _project_assigned_stock_from_prepared_rows(
    rows: Mapping[str, Any], *, selected_rows: list[dict[str, Any]],
    published: PublishedPositionLotProjection, events: list[TradeEvent], instant: int,
    valuation_marks: tuple[ValuationMarkFact, ...] | list[ValuationMarkFact] = (),
    quote_snapshots: Any = None, account: str | None = None,
    broker: str | None = None, stock_holdings: list[dict[str, Any]] | None = None,
    strategy_by_lot_id: dict[str, dict[str, Any]] | None = None,
    historical_rows: _HistoricalTradeRows | None = None,
    inventory_only: bool = False,
) -> dict[str, Any]:
    _require_trusted_assigned_stock_projection(
        published.diagnostics, events,
        account=account, broker=broker,
    )
    projection = published.ledger_projection
    allocations = projection.allocations
    settlement_lot_ids = None
    if inventory_only:
        allocations = tuple(item for item in allocations if item.close_type in {"assignment", "exercise"})
        settlement_lot_ids = {item.target_lot_id for item in allocations}
    current_fields = {item.lot_id: item.fields for item in published.lots}
    # The strategy-metadata family is read from the event layer now (design
    # §7.5), so each lot is handed the metadata its own events declare.
    if strategy_by_lot_id is None:
        # Selection already excludes future economics and retains validated
        # late voids that restate this boundary. Metadata must use that same
        # effective history rather than revive a voided opening or adjustment.
        strategy_by_lot_id = lot_strategy_metadata_from_trade_events(selected_rows)
    selected_ids = {
        str(row.get("event_id") or "").strip()
        for row in selected_rows
        if str(row.get("event_id") or "").strip()
    }
    trade_row = (historical_rows.assigned_stock_trade_row if historical_rows is not None
                 else assigned_stock_trade_event_row)
    return project_assigned_stock_lifecycle(
        [
            trade_row(event)
            for event in events
            if event.event_id in selected_ids
        ],
        assignment_option_rows=[assigned_stock_allocation_row(item) for item in allocations],
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
            if settlement_lot_ids is None or item.lot_id in settlement_lot_ids
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
        _settlement_option_fees=(historical_rows.settlement_option_fees
                                 if historical_rows is not None else None),
        _inventory_only=inventory_only,
        _inventory_memo=historical_rows.stock_inventory if historical_rows is not None else None,
    )


__all__ = [
    "qualify_assigned_stock_sell_observations",
    "project_assigned_stock_lifecycle_from_rows",
    "project_position_lots_and_assigned_stock_from_rows",
]


WHEEL_READ_MODEL_SCHEMA = "wheel_read_model.v2"


def _candidate_rows(snapshot: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if not isinstance(snapshot, Mapping):
        return []
    rows = snapshot.get("batches") or snapshot.get("rows")
    if isinstance(rows, list):
        return [item for item in rows if isinstance(item, Mapping)]
    return [snapshot]


def _wheel_event_time_ms(row: Mapping[str, Any], field: str) -> int:
    try:
        return int(row.get(field) or 0)
    except (TypeError, ValueError):
        return 0


def _legacy_call_batch(branch: Mapping[str, Any]) -> dict[str, Any]:
    batch = dict(branch)
    if not batch.get("legacy_call_adapter"):
        batch["phase"] = {
            "option_open": "call_open",
            "intent_pending": "call_pending",
            "residual_capacity": "residual_stock",
            "manual_ended": None,
        }.get(batch.get("phase"), batch.get("phase"))
    batch.setdefault("batch_generation_hash", batch.get("branch_generation_hash"))
    batch.setdefault("active_call_lot_ids", list(batch.get("active_option_lot_ids") or []))
    batch.setdefault("unresolved_call_lot_ids", [])
    if "active_intent_reserved_shares" not in batch:
        try:
            batch["active_intent_reserved_shares"] = contract_share_quantity(
                batch.get("active_intent_reserved_contracts") or 0, batch.get("multiplier"),
            )
        except ValueError:
            batch["active_intent_reserved_shares"] = None
    return batch


def _legacy_call_batches(branches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _legacy_call_batch(branch)
        for branch in branches
        if str(branch.get("direction") or "call").strip().lower() == "call"
        and str(branch.get("stock_lot_id") or "").strip()
    ]


def _attach_candidates(
    projections: list[dict[str, Any]],
    candidates: list[Mapping[str, Any]],
    *,
    account: str,
) -> None:
    for projection in projections:
        branch_id = str(projection.get("wheel_branch_id") or "").strip()
        lot_id = str(projection.get("stock_lot_id") or "").strip()
        matches = [
            item
            for item in candidates
            if str(item.get("account") or "").strip().lower() == account
            and str(item.get("projection_hash") or "").strip()
            == str(projection.get("projection_hash") or "").strip()
            and (
                (
                    branch_id
                    and str(item.get("wheel_branch_id") or "").strip()
                    == branch_id
                )
                or (
                    lot_id
                    and str(item.get("stock_lot_id") or "").strip()
                    == lot_id
                )
            )
        ]
        if len(matches) == 1:
            candidate = matches[0].get("final_candidate")
            if isinstance(candidate, Mapping):
                projection["candidate"] = dict(candidate)


def _build_wheel_read_model_base(
    rows: Mapping[str, Any], *, account: str, as_of_ms: int,
    historical_rows: _HistoricalTradeRows | None = None,
    inventory_only: bool = False,
    attribution_only: bool = False,
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    if not account_value:
        raise ValueError("wheel read model requires account")
    instant = int(as_of_ms)
    if instant <= 0:
        raise ValueError("wheel read model requires as_of_ms > 0")
    trade_events = (historical_rows.select(instant) if historical_rows is not None
                    else _select_assigned_stock_trade_rows(rows, instant, account=account_value))
    normalization_memo = historical_rows.wheel_event_normalizations if attribution_only and historical_rows else None
    wheel_events = [
        item if normalization_memo is not None and type(item) is dict else dict(item)
        for item in rows.get("account_wheel_events") or []
        if isinstance(item, Mapping)
        and _wheel_event_time_ms(item, "occurred_at_ms") <= instant
    ]
    scoped_rows = {
        **dict(rows),
        "trade_events": trade_events,
        "account_wheel_events": wheel_events,
    }
    position_lots, assigned_stock, strategy_by_lot_id, published = _project_position_lots_and_assigned_stock(
        scoped_rows, account=account_value, as_of_ms=instant,
        historical_rows=historical_rows,
        inventory_only=inventory_only,
    )
    scoped_rows["account_position_lots"] = position_lots
    # The historical scope excludes voids and repairs. With canonical times,
    # the Wheel projector's strategy slice is the same slice used by the lots.
    if historical_rows is not None and all(
        type(row.get("event_time_ms")) is int
        and row["event_time_ms"] <= instant
        and type(row.get("event_type")) is str
        and row["event_type"].strip()
        and type(row.get("event_id")) is str
        and row["event_id"].strip()
        and isinstance(row.get("raw_payload"), Mapping)
        for row in trade_events
    ):
        wheel_branches = project_wheel_branches(
            wheel_events, trade_events, position_lots, assigned_stock, instant,
            _strategy_by_lot_id=strategy_by_lot_id,
            _load_allocations=lambda: historical_rows.wheel_allocations(trade_events, published),
            _include_projection_hash=not attribution_only,
            _normalization_memo=normalization_memo,
        )
    else:
        wheel_branches = project_wheel_branches(
            wheel_events, trade_events, position_lots, assigned_stock, instant,
            _include_projection_hash=not attribution_only,
            _normalization_memo=normalization_memo,
        )
    return {
        "account": account_value, "as_of_ms": instant,
        "position_lots": position_lots, "assigned_stock": assigned_stock,
        "wheel_events": wheel_events, "wheel_branches": wheel_branches,
        "batches": [] if attribution_only else _legacy_call_batches(wheel_branches),
    }


def _historical_unresolved_branch_ids(
    branches: list[dict[str, Any]], base: Mapping[str, Any], historical_rows: _HistoricalTradeRows | None,
    *, normalization_memo: _WheelEventNormalizationMemo | None = None,
) -> set[str]:
    unresolved: set[str] = set()
    project_wheel_linkage_candidates(
        branches, base["position_lots"], base["wheel_events"], _unresolved_branch_ids=unresolved,
        _validated_call_inputs=historical_rows.validated_call_inputs if historical_rows is not None else None,
        _normalization_memo=normalization_memo,
    )
    return unresolved


def _mark_unresolved_linkage(branch: dict[str, Any], unresolved: set[str]) -> None:
    if branch.get("lifecycle_status") == "active" and str(branch.get("wheel_branch_id") or "").strip() in unresolved:
        branch["phase"] = "linkage_unresolved"


def _wheel_read_model_market(market: str | None) -> str:
    value = str(market or "").strip().upper()
    if value and value not in {"US", "HK"}:
        raise ValueError("wheel read model market must be us or hk")
    return value


def _historical_attribution_branches(
    rows: Mapping[str, Any], *, account: str, as_of_ms: int, market: str,
    historical_rows: _HistoricalTradeRows | None,
) -> dict[str, dict[str, Any]]:
    """Project the historical branch facts used by attribution competition."""
    base = _build_wheel_read_model_base(
        rows, account=account, as_of_ms=as_of_ms, historical_rows=historical_rows,
        inventory_only=True, attribution_only=True,
    )
    market_value = _wheel_read_model_market(market)
    branches = [branch for branch in base["wheel_branches"]
                if not market_value or symbol_market(branch.get("symbol")) == market_value]
    unresolved = _historical_unresolved_branch_ids(
        branches, base, historical_rows,
        normalization_memo=historical_rows.wheel_event_normalizations if historical_rows is not None else None,
    )
    evidence = {}
    for branch in branches:
        _mark_unresolved_linkage(branch, unresolved)
        evidence[branch["wheel_branch_id"]] = {
            "lifecycle_status": branch["lifecycle_status"],
            "integrity_status": branch["integrity_status"],
            "active_option_lot_ids": branch.get("active_option_lot_ids", []),
            "coverage": {"status": project_wheel_coverage(branch)["status"]},
        }
    return evidence


def _render_wheel_read_model(
    base: dict[str, Any], *, candidate_snapshot: Mapping[str, Any] | None = None,
    monitoring_readiness: Mapping[str, Any] | None = None, market: str | None = None,
    historical: bool = False, historical_rows: _HistoricalTradeRows | None = None,
) -> dict[str, Any]:
    account_value, instant = base["account"], base["as_of_ms"]
    position_lots, assigned_stock = base["position_lots"], base["assigned_stock"]
    wheel_events, wheel_branches = base["wheel_events"], base["wheel_branches"]
    batches = base["batches"]
    market_value = _wheel_read_model_market(market)
    if market_value:
        batches = [
            batch
            for batch in batches
            if symbol_market(batch.get("symbol")) == market_value
        ]
        wheel_branches = [
            branch
            for branch in wheel_branches
            if symbol_market(branch.get("symbol")) == market_value
        ]
    candidates = _candidate_rows(candidate_snapshot)
    _attach_candidates(batches, candidates, account=account_value)
    _attach_candidates(wheel_branches, candidates, account=account_value)
    readiness = dict(monitoring_readiness or {})
    monitoring_gate = str(readiness.get("monitoring_gate") or "disabled").strip().lower()
    if monitoring_gate not in {"enabled", "disabled", "config_mismatch"}:
        monitoring_gate = "disabled"
    for projection in [*batches, *wheel_branches]:
        projection["monitoring_gate"] = monitoring_gate
        if readiness.get("reason_code"):
            projection["monitoring_gate_reason"] = readiness["reason_code"]
    if historical:
        unresolved_branch_ids = _historical_unresolved_branch_ids(wheel_branches, base, historical_rows)
        linkage_candidates = []
    else:
        linkage_candidates = project_wheel_linkage_candidates(wheel_branches, position_lots, wheel_events)
        unresolved_branch_ids = {
            str(item.get("wheel_branch_id") or "").strip() for item in linkage_candidates
        }
        unresolved_branch_ids.discard("")
    for projection in [*batches, *wheel_branches]:
        _mark_unresolved_linkage(projection, unresolved_branch_ids)
    lots_by_id = {str(row["record_id"]): row["fields"] for row in position_lots}
    for projection in [*batches, *wheel_branches]:
        projection["coverage"] = project_wheel_coverage(projection)
        # Use only the lot identities already linked by the Wheel projection.
        active_ids = projection.get("active_option_lot_ids") or projection.get("active_call_lot_ids") or []
        projection["active_option_contracts"] = [
            {"lot_id": lot_id, **{
                key: (lots_by_id.get(lot_id, {}).get("contract_key") or {}).get(key)
                for key in ("underlying_symbol", "option_type", "strike", "expiration_ymd")
            }}
            for lot_id in active_ids
        ]
    return {
        "schema_version": WHEEL_READ_MODEL_SCHEMA,
        "account": account_value,
        "market": market_value or None,
        "as_of_ms": instant,
        "monitoring_gate": monitoring_gate,
        "monitoring_gate_reason": readiness.get("reason_code"),
        "batches": batches,
        "wheel_branches": wheel_branches,
        "linkage_candidates": linkage_candidates,
        "assigned_stock_projection": assigned_stock,
    }


def build_wheel_read_model_from_rows(
    rows: Mapping[str, Any],
    *,
    account: str,
    as_of_ms: int,
    candidate_snapshot: Mapping[str, Any] | None = None,
    monitoring_readiness: Mapping[str, Any] | None = None,
    market: str | None = None,
    _historical: bool = False,
    _historical_rows: _HistoricalTradeRows | None = None,
) -> dict[str, Any]:
    return _render_wheel_read_model(
        _build_wheel_read_model_base(
            rows, account=account, as_of_ms=as_of_ms, historical_rows=_historical_rows,
            inventory_only=_historical,
        ),
        candidate_snapshot=candidate_snapshot, monitoring_readiness=monitoring_readiness,
        market=market,
        historical=_historical,
        historical_rows=_historical_rows,
    )


def build_wheel_read_model_with_capacity_from_rows(
    rows: Mapping[str, Any], *, account: str, as_of_ms: int, market: str | None,
    monitoring_readiness: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive independent market and account-wide views from one observation."""
    base = _build_wheel_read_model_base(rows, account=account, as_of_ms=as_of_ms)
    market_model = _render_wheel_read_model(
        deepcopy(base), market=market, monitoring_readiness=monitoring_readiness,
    )
    capacity_model = _render_wheel_read_model(base)
    return market_model, capacity_model


def build_assigned_stock_projection_from_rows(
    rows: Mapping[str, Any],
    *,
    account: str,
    as_of_ms: int,
) -> dict[str, Any]:
    return project_assigned_stock_lifecycle_from_rows(
        rows,
        as_of_ms=int(as_of_ms),
        quote_snapshots=[],
        account=str(account or "").strip().lower(),
    )


def resolve_wheel_batch_from_rows(
    rows: Mapping[str, Any],
    *,
    account: str,
    lot_id: str,
    as_of_ms: int,
    market: str,
) -> dict[str, Any]:
    matches = [
        batch
        for batch in build_wheel_read_model_from_rows(
            rows,
            account=account,
            as_of_ms=as_of_ms,
            market=market,
        )["batches"]
        if batch["stock_lot_id"] == lot_id
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Wheel batch must resolve uniquely: stock_lot_id={lot_id}"
        )
    return matches[0]


def resolve_wheel_branch_from_rows(
    rows: Mapping[str, Any],
    *,
    account: str,
    wheel_branch_id: str,
    as_of_ms: int,
    market: str | None = None,
) -> dict[str, Any]:
    matches = [
        branch
        for branch in build_wheel_read_model_from_rows(
            rows,
            account=account,
            as_of_ms=as_of_ms,
        )["wheel_branches"]
        if branch["wheel_branch_id"] == wheel_branch_id
        and (
            not str(market or "").strip()
            or symbol_market(branch.get("symbol"))
            == str(market or "").strip().upper()
        )
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Wheel branch must resolve uniquely: wheel_branch_id={wheel_branch_id}"
        )
    return matches[0]
