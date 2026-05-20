from __future__ import annotations

from dataclasses import replace
from typing import Any, Sequence

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.ledger.position_fields import normalize_currency
from domain.domain.trade_contract_identity import (
    canonical_contract_symbol,
    normalize_contract_expiration,
    normalize_position_effect,
    normalize_trade_side,
)
from src.application.ledger.lot_resolver import LotCloseResolutionError, LotCloseSelector, resolve_fifo_close_targets
from src.application.ledger.publisher import project_stored_trade_events_to_position_lots
from src.application.ledger.repository import with_sqlite_repo_transaction
from src.application.ledger.results import LedgerWriteResult, ProjectionRefreshResult


def projection_diagnostics_summary(diagnostics: Sequence[Any]) -> dict[str, Any]:
    explicit_close_codes = {
        "close_explicit_target_not_found",
        "close_explicit_target_conflict",
        "close_explicit_target_already_closed",
        "close_explicit_target_mismatch",
        "close_explicit_target_oversized",
        "close_explicit_source_event_target_not_found",
        "close_explicit_source_event_target_already_closed",
        "close_explicit_source_event_target_mismatch",
        "close_explicit_source_event_target_oversized",
        "target_lot_id_required",
        "target_lot_not_found",
        "target_contract_mismatch",
        "target_lot_already_closed",
        "close_contracts_exceed_open",
    }
    return {
        "projection_diagnostic_count": int(len(diagnostics)),
        "unmatched_explicit_close_count": int(sum(1 for item in diagnostics if item.code in explicit_close_codes)),
        "unmatched_heuristic_close_count": int(sum(1 for item in diagnostics if item.code == "close_unmatched_contracts")),
        "projection_diagnostics": [item.to_dict() for item in diagnostics],
    }


def safe_int_count(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def rebuild_position_lots_from_trade_events(repo: Any) -> ProjectionRefreshResult:
    def _run(sqlite_repo: Any, conn: Any | None) -> ProjectionRefreshResult:
        if conn is not None:
            events = sqlite_repo.list_trade_events(conn=conn)
            projection = project_stored_trade_events_to_position_lots(events)
            inserted = sqlite_repo.replace_position_lots(projection.lots, conn=conn)
        else:
            events = sqlite_repo.list_trade_events()
            projection = project_stored_trade_events_to_position_lots(events)
            inserted = sqlite_repo.replace_position_lots(projection.lots)
        result = {
            "trade_event_count": int(len(events)),
            "position_lot_count": int(inserted),
        }
        result.update(projection_diagnostics_summary(projection.diagnostics))
        return ProjectionRefreshResult.from_payload(result)

    return with_sqlite_repo_transaction(repo, _run)


def persist_trade_event_object(repo: Any, event: Any) -> LedgerWriteResult:
    def _run(sqlite_repo: Any, conn: Any | None) -> LedgerWriteResult:
        storage_events = _events_for_storage(sqlite_repo, event)
        if conn is not None:
            created_flags = [sqlite_repo.upsert_trade_event(item, conn=conn) for item in storage_events]
            projection = project_stored_trade_events_to_position_lots(sqlite_repo.list_trade_events(conn=conn))
            records = projection.lots
            lot_count = sqlite_repo.replace_position_lots(records, conn=conn)
        else:
            created_flags = [sqlite_repo.upsert_trade_event(item) for item in storage_events]
            projection = project_stored_trade_events_to_position_lots(sqlite_repo.list_trade_events())
            records = projection.lots
            lot_count = sqlite_repo.replace_position_lots(records)
        payload = storage_events[0].raw_payload or {}
        explicit_record_id = str(payload.get("record_id") or "").strip()
        record_id = explicit_record_id or next(
            (
                record.record_id
                for record in records
                if str(record.fields.get("source_event_id") or "").strip() == str(event.event_id).strip()
            ),
            "",
        )
        result = {
            "event_id": event.event_id,
            "record_id": record_id or None,
            "created": any(created_flags),
            "position_lot_count": int(lot_count),
        }
        result.update(projection_diagnostics_summary(projection.diagnostics))
        return LedgerWriteResult.from_payload(result)

    return with_sqlite_repo_transaction(repo, _run)


def persist_trade_event(repo: Any, deal: Any) -> LedgerWriteResult:
    return persist_trade_event_object(repo, _trade_event_from_normalized_deal(deal))


def _events_for_storage(repo: Any, event: Any) -> list[Any]:
    if hasattr(event, "event_type") and not hasattr(event, "position_effect"):
        if bool(getattr(event, "is_close", False)) and not getattr(event, "target_lot_id", None):
            return _canonical_close_events_for_storage(repo, event)
        return [event]
    if str(event.position_effect or "").strip().lower() != "close":
        return [event]
    payload = dict(event.raw_payload or {})
    if str(payload.get("record_id") or payload.get("target_lot_id") or "").strip():
        return [event]
    selector = LotCloseSelector.from_values(
        broker=event.broker,
        account=event.account,
        symbol=event.symbol,
        option_type=event.option_type,
        position_side=_close_position_side(event),
        strike=event.strike,
        expiration_ymd=event.expiration_ymd,
        contracts_to_close=event.contracts,
    )
    try:
        resolution = resolve_fifo_close_targets(repo, selector, source="stored_trade_close")
    except LotCloseResolutionError as exc:
        raise ValueError(f"close trade event target resolution failed: {exc.code}") from exc
    out: list[TradeEvent] = []
    resolution_payload = resolution.to_dict()
    for index, match in enumerate(resolution.matches):
        event_id = event.event_id if index == 0 else f"{event.event_id}:target:{match.record_id}"
        match_payload = {
            **payload,
            "record_id": match.record_id,
            "target_lot_id": match.record_id,
            "close_target_resolution": resolution_payload,
        }
        source_event_id = getattr(match.candidate, "source_event_id", None)
        if source_event_id not in (None, ""):
            match_payload["close_target_source_event_id"] = source_event_id
        out.append(
            replace(
                event,
                event_id=event_id,
                contracts=int(match.contracts_to_close),
                raw_payload=match_payload,
            )
        )
    return out


def _close_position_side(event: Any) -> str:
    trade_side = normalize_trade_side(event.side)
    if trade_side == "buy":
        return "short"
    if trade_side == "sell":
        return "long"
    return str(event.side or "").strip().lower()


def _canonical_close_events_for_storage(repo: Any, event: TradeEvent) -> list[TradeEvent]:
    selector = LotCloseSelector.from_values(
        broker=event.contract_key.broker,
        account=event.contract_key.account,
        symbol=event.contract_key.underlying_symbol,
        option_type=event.contract_key.option_type,
        position_side=event.contract_key.position_side,
        strike=event.contract_key.strike,
        expiration_ymd=event.contract_key.expiration_ymd,
        contracts_to_close=event.contracts,
    )
    try:
        resolution = resolve_fifo_close_targets(repo, selector, source="stored_canonical_trade_close")
    except LotCloseResolutionError as exc:
        raise ValueError(f"close trade event target resolution failed: {exc.code}") from exc
    out: list[TradeEvent] = []
    resolution_payload = resolution.to_dict()
    for index, match in enumerate(resolution.matches):
        event_id = event.event_id if index == 0 else f"{event.event_id}:target:{match.record_id}"
        raw_payload = {
            **dict(event.raw_payload or {}),
            "record_id": match.record_id,
            "target_lot_id": match.record_id,
            "close_target_resolution": resolution_payload,
        }
        source_event_id = getattr(match.candidate, "source_event_id", None)
        if source_event_id not in (None, ""):
            raw_payload["close_target_source_event_id"] = source_event_id
        out.append(
            replace(
                event,
                event_id=event_id,
                contracts=int(match.contracts_to_close),
                target_lot_id=match.record_id,
                raw_payload=raw_payload,
            )
        )
    return out


def _trade_event_from_normalized_deal(deal: Any) -> TradeEvent:
    trade_side = normalize_trade_side(getattr(deal, "side", None)) or ""
    position_effect = normalize_position_effect(getattr(deal, "position_effect", None)) or ""
    event_type = _event_type_from_position_effect(position_effect)
    position_side = _position_side_from_trade(effect=position_effect, trade_side=trade_side)
    raw_payload = dict(getattr(deal, "raw_payload", {}) or {})
    raw_payload.setdefault("source_type", "broker_trade_event")
    raw_payload.setdefault("side", trade_side)
    order_id = str(getattr(deal, "order_id", "") or "").strip()
    if order_id:
        raw_payload.setdefault("order_id", order_id)
    multiplier_source = str(getattr(deal, "multiplier_source", "") or "").strip()
    if multiplier_source:
        raw_payload.setdefault("multiplier_source", multiplier_source)
    event_time_ms = _required_broker_trade_time_ms(deal)
    contract_key = ContractKey.from_values(
        broker=getattr(deal, "broker", None) or "富途",
        account=getattr(deal, "internal_account", None) or "",
        underlying_symbol=canonical_contract_symbol(getattr(deal, "symbol", "")),
        option_type=getattr(deal, "option_type", None) or "",
        position_side=position_side,
        strike=getattr(deal, "strike", None),
        expiration_ymd=normalize_contract_expiration(getattr(deal, "expiration_ymd", None)),
    )
    return TradeEvent(
        event_id=str(getattr(deal, "deal_id", "") or "").strip(),
        event_type=event_type,
        event_time_ms=event_time_ms,
        contract_key=contract_key,
        contracts=int(getattr(deal, "contracts", 0) or 0),
        price=float(getattr(deal, "price", 0.0) or 0.0),
        currency=normalize_currency(getattr(deal, "currency", None)),
        source="opend_push",
        multiplier=float(getattr(deal, "multiplier", None) or 100),
        target_lot_id=str(raw_payload.get("target_lot_id") or raw_payload.get("record_id") or "").strip() or None,
        raw_payload=raw_payload,
    )


def _event_type_from_position_effect(position_effect: str) -> str:
    if position_effect == "open":
        return "open"
    if position_effect == "close":
        return "close"
    if position_effect in {"adjust", "void"}:
        return position_effect
    return position_effect


def _required_broker_trade_time_ms(deal: Any) -> int:
    raw = getattr(deal, "trade_time_ms", None)
    if raw in (None, ""):
        value = 0
    else:
        try:
            value = int(raw)
        except Exception:
            value = 0
    if value <= 0:
        deal_id = str(getattr(deal, "deal_id", "") or "").strip()
        suffix = f" deal_id={deal_id}" if deal_id else ""
        raise ValueError(f"broker trade event requires positive trade_time_ms; refusing event_time_ms=0{suffix}")
    return value


def _position_side_from_trade(*, effect: str, trade_side: str) -> str:
    if effect == "open":
        return "short" if trade_side == "sell" else "long"
    if effect == "close":
        return "short" if trade_side == "buy" else "long"
    return trade_side
