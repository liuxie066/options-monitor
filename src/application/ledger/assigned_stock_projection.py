from __future__ import annotations

from copy import deepcopy
from domain.domain.trade_contract_identity import contract_share_quantity
from domain.domain.symbol_identity import symbol_market
from domain.domain.wheel import project_wheel_branches, project_wheel_coverage, project_wheel_linkage_candidates

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
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    if not account_value:
        raise ValueError("wheel read model requires account")
    instant = int(as_of_ms)
    if instant <= 0:
        raise ValueError("wheel read model requires as_of_ms > 0")
    historical_event_ids = {
        item.get("event_id") for item in rows.get("trade_events") or []
        if isinstance(item, Mapping) and item.get("event_id")
        and _wheel_event_time_ms(item, "event_time_ms") <= instant
    }
    trade_events = [
        dict(item)
        for item in rows.get("trade_events") or []
        if isinstance(item, Mapping)
        # Later corrections still void historical facts; future economics stay out.
        and (_wheel_event_time_ms(item, "event_time_ms") <= instant
             or valid_void_target_event_id(item) in historical_event_ids)
    ]
    wheel_events = [
        dict(item)
        for item in rows.get("account_wheel_events") or []
        if isinstance(item, Mapping)
        and _wheel_event_time_ms(item, "occurred_at_ms") <= instant
    ]
    scoped_rows = {
        **dict(rows),
        "trade_events": trade_events,
        "account_wheel_events": wheel_events,
    }
    position_lots, assigned_stock = project_position_lots_and_assigned_stock_from_rows(
        scoped_rows, account=account_value, as_of_ms=instant,
    )
    scoped_rows["account_position_lots"] = position_lots
    wheel_branches = project_wheel_branches(
        wheel_events, trade_events, position_lots, assigned_stock, instant,
    )
    batches = _legacy_call_batches(wheel_branches)
    return {
        "account": account_value, "as_of_ms": instant,
        "position_lots": position_lots, "assigned_stock": assigned_stock,
        "wheel_events": wheel_events, "wheel_branches": wheel_branches,
        "batches": batches,
    }


def _render_wheel_read_model(
    base: dict[str, Any], *, candidate_snapshot: Mapping[str, Any] | None = None,
    monitoring_readiness: Mapping[str, Any] | None = None, market: str | None = None,
) -> dict[str, Any]:
    account_value, instant = base["account"], base["as_of_ms"]
    position_lots, assigned_stock = base["position_lots"], base["assigned_stock"]
    wheel_events, wheel_branches = base["wheel_events"], base["wheel_branches"]
    batches = base["batches"]
    market_value = str(market or "").strip().upper()
    if market_value:
        if market_value not in {"US", "HK"}:
            raise ValueError("wheel read model market must be us or hk")
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
    linkage_candidates = project_wheel_linkage_candidates(
        wheel_branches,
        position_lots,
        wheel_events,
    )
    unresolved_branch_ids = {
        str(item.get("wheel_branch_id") or "").strip()
        for item in linkage_candidates
        if str(item.get("wheel_branch_id") or "").strip()
    }
    for projection in [*batches, *wheel_branches]:
        if (
            str(projection.get("wheel_branch_id") or "").strip()
            in unresolved_branch_ids
            and projection.get("lifecycle_status") == "active"
        ):
            projection["phase"] = "linkage_unresolved"
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
) -> dict[str, Any]:
    return _render_wheel_read_model(
        _build_wheel_read_model_base(rows, account=account, as_of_ms=as_of_ms),
        candidate_snapshot=candidate_snapshot, monitoring_readiness=monitoring_readiness,
        market=market,
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
