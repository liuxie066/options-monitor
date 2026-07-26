from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any, Sequence

from domain.domain.fee_calc import extract_actual_fees
from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.option_position_identity import normalize_currency
from domain.domain.performance.models import canonical_decimal_text, quantize_money, to_decimal
from domain.domain.trade_contract_identity import (
    canonical_contract_symbol,
    normalize_contract_expiration,
    normalize_position_effect,
    normalize_trade_side,
)
from src.application.ledger.lot_resolver import LotCloseResolutionError, LotCloseSelector, resolve_fifo_close_targets
from src.application.ledger.event_codec import encode_trade_event_for_storage
from src.application.ledger.external_event_key import broker_external_event_key
from src.application.ledger.publisher import project_stored_trade_events_to_position_lots
from src.application.ledger.repository import with_sqlite_repo_transaction
from src.application.ledger.results import LedgerWriteResult, ProjectionRefreshResult
from src.application.cash_conversion import (
    attach_trade_event_cash_conversions,
    load_cash_fx_payload,
    utc_now_ms,
)


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
        "unmatched_heuristic_close_count": int(
            sum(1 for item in diagnostics if item.code == "close_unmatched_contracts")
        ),
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
        storage_events = [
            _canonical_storage_event(item)
            for item in _events_for_storage(sqlite_repo, event)
        ]
        existing_events = sqlite_repo.list_trade_events(conn=conn) if conn is not None else sqlite_repo.list_trade_events()
        existing_by_id = {
            str(item.get("event_id") or ""): item
            for item in existing_events
            if isinstance(item, dict) and str(item.get("event_id") or "")
        }
        fx_payload = load_cash_fx_payload(sqlite_repo)
        observed_at_ms = utc_now_ms()
        storage_events = [
            _event_with_existing_cash_conversions(item, existing_by_id[item.event_id])
            if item.event_id in existing_by_id
            else attach_trade_event_cash_conversions(
                item,
                fx_payload=fx_payload,
                observed_at_ms=observed_at_ms,
            )
            for item in storage_events
        ]
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


def _canonical_storage_event(item: Any) -> TradeEvent:
    encoded = encode_trade_event_for_storage(item)
    if encoded.event is None:  # pragma: no cover - encoder raises before this branch
        raise ValueError("trade event could not be canonicalized for storage")
    return encoded.event


def _event_with_existing_cash_conversions(event: TradeEvent, existing: dict[str, Any]) -> TradeEvent:
    existing_raw_payload = existing.get("raw_payload")
    if not isinstance(existing_raw_payload, dict):
        return event
    conversions = existing_raw_payload.get("cash_conversions")
    if not isinstance(conversions, dict):
        return event
    raw_payload = dict(event.raw_payload or {})
    raw_payload["cash_conversions"] = dict(conversions)
    return replace(event, raw_payload=raw_payload)


def persist_trade_event(repo: Any, deal: Any) -> LedgerWriteResult:
    return persist_trade_event_object(repo, _trade_event_from_normalized_deal(deal))


def persist_normalized_trade_events_atomically(
    repo: Any,
    deals: Sequence[Any],
) -> list[LedgerWriteResult]:
    """Persist explicitly targeted broker-event splits in one transaction."""

    return persist_trade_event_objects_atomically(
        repo,
        [_trade_event_from_normalized_deal(deal) for deal in deals],
    )


def persist_trade_event_objects_atomically(
    repo: Any,
    events: Sequence[Any],
) -> list[LedgerWriteResult]:
    """Persist explicitly targeted canonical events in one transaction."""

    events = list(events)
    if not events:
        raise ValueError("atomic trade persistence requires at least one event")

    def _run(sqlite_repo: Any, conn: Any | None) -> list[LedgerWriteResult]:
        storage_events: list[TradeEvent] = []
        for event in events:
            expanded = [
                _canonical_storage_event(item)
                for item in _events_for_storage(sqlite_repo, event)
            ]
            if len(expanded) != 1:
                raise ValueError(
                    "atomic trade persistence requires explicitly targeted events"
                )
            storage_events.append(expanded[0])

        event_ids = [event.event_id for event in storage_events]
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("atomic trade persistence contains duplicate event_id")

        existing_events = (
            sqlite_repo.list_trade_events(conn=conn)
            if conn is not None
            else sqlite_repo.list_trade_events()
        )
        existing_by_id = {
            str(item.get("event_id") or ""): item
            for item in existing_events
            if isinstance(item, dict) and str(item.get("event_id") or "")
        }
        fx_payload = load_cash_fx_payload(sqlite_repo)
        observed_at_ms = utc_now_ms()
        storage_events = [
            _event_with_existing_cash_conversions(event, existing_by_id[event.event_id])
            if event.event_id in existing_by_id
            else attach_trade_event_cash_conversions(
                event,
                fx_payload=fx_payload,
                observed_at_ms=observed_at_ms,
            )
            for event in storage_events
        ]
        created_flags = [
            (
                sqlite_repo.upsert_trade_event(event, conn=conn)
                if conn is not None
                else sqlite_repo.upsert_trade_event(event)
            )
            for event in storage_events
        ]
        stored = (
            sqlite_repo.list_trade_events(conn=conn)
            if conn is not None
            else sqlite_repo.list_trade_events()
        )
        projection = project_stored_trade_events_to_position_lots(stored)
        lot_count = (
            sqlite_repo.replace_position_lots(projection.lots, conn=conn)
            if conn is not None
            else sqlite_repo.replace_position_lots(projection.lots)
        )
        diagnostics = projection_diagnostics_summary(projection.diagnostics)
        return [
            LedgerWriteResult.from_payload(
                {
                    "event_id": event.event_id,
                    "record_id": (
                        str(
                            (event.raw_payload or {}).get("record_id")
                            or (event.raw_payload or {}).get("target_lot_id")
                            or event.target_lot_id
                            or ""
                        ).strip()
                        or None
                    ),
                    "created": bool(created),
                    "position_lot_count": int(lot_count),
                    **diagnostics,
                }
            )
            for event, created in zip(storage_events, created_flags, strict=True)
        ]

    return with_sqlite_repo_transaction(repo, _run)


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
    fee_splits = _close_fee_splits(event, resolution.matches)
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
        allocated_fee = fee_splits[index]
        match_payload = _payload_with_allocated_fee(match_payload, allocated_fee)
        out.append(
            replace(
                event,
                event_id=event_id,
                contracts=int(match.contracts_to_close),
                fees=float(allocated_fee),
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
    fee_splits = _close_fee_splits(event, resolution.matches)
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
        allocated_fee = fee_splits[index]
        raw_payload = _payload_with_allocated_fee(raw_payload, allocated_fee)
        out.append(
            replace(
                event,
                event_id=event_id,
                contracts=int(match.contracts_to_close),
                fees=float(allocated_fee),
                target_lot_id=match.record_id,
                raw_payload=raw_payload,
            )
        )
    return out


def _close_fee_splits(event: Any, matches: Sequence[Any]) -> list[Decimal]:
    ordered = list(matches)
    if not ordered:
        return []
    total_contracts = sum(int(match.contracts_to_close) for match in ordered)
    if total_contracts <= 0:
        raise ValueError("close fee allocation requires positive matched contracts")

    payload = dict(getattr(event, "raw_payload", {}) or {})
    provenance = payload.get("fee_provenance")
    amount_raw: Any = getattr(event, "fees", 0.0)
    if isinstance(provenance, dict):
        basis = str(provenance.get("basis") or "").strip().lower()
        if basis in {"actual", "estimated"} and provenance.get("amount") not in (None, ""):
            amount_raw = provenance["amount"]
    try:
        total_fee = quantize_money(to_decimal(amount_raw, field_name="close fee"))
    except (TypeError, ValueError):
        try:
            total_fee = quantize_money(to_decimal(getattr(event, "fees", 0.0), field_name="close fee"))
        except (TypeError, ValueError):
            total_fee = Decimal(0)

    allocated_before = Decimal(0)
    out: list[Decimal] = []
    for index, match in enumerate(ordered):
        if index == len(ordered) - 1:
            allocated = quantize_money(total_fee - allocated_before)
        else:
            allocated = quantize_money(total_fee * Decimal(int(match.contracts_to_close)) / Decimal(total_contracts))
        out.append(allocated)
        allocated_before = quantize_money(allocated_before + allocated)
    return out


def _payload_with_allocated_fee(payload: dict[str, Any], amount: Decimal) -> dict[str, Any]:
    out = dict(payload)
    provenance = out.get("fee_provenance")
    if not isinstance(provenance, dict):
        return out
    updated = dict(provenance)
    basis = str(updated.get("basis") or "").strip().lower()
    if basis in {"actual", "estimated"}:
        existing_amount = updated.get("amount")
        if existing_amount not in (None, ""):
            try:
                to_decimal(existing_amount, field_name="fee provenance amount")
            except (TypeError, ValueError):
                return out
        updated["amount"] = canonical_decimal_text(amount, field_name="allocated close fee")
    elif basis == "missing":
        updated.pop("amount", None)
    out["fee_provenance"] = updated
    return out


def _trade_event_from_normalized_deal(deal: Any) -> TradeEvent:
    trade_side = normalize_trade_side(getattr(deal, "side", None)) or ""
    position_effect = normalize_position_effect(getattr(deal, "position_effect", None)) or ""
    raw_payload = dict(getattr(deal, "raw_payload", {}) or {})
    source_deal_id = str(getattr(deal, "deal_id", "") or "").strip()
    event_id = broker_external_event_key(deal)
    event_type = _event_type_from_position_effect(position_effect, raw_payload=raw_payload)
    position_side = _position_side_from_trade(effect=position_effect, trade_side=trade_side)
    raw_payload.setdefault("source_type", "broker_trade_event")
    raw_payload.setdefault("source", "api")
    if source_deal_id:
        raw_payload.setdefault("source_deal_id", source_deal_id)
    futu_account_id = str(getattr(deal, "futu_account_id", "") or "").strip()
    if futu_account_id:
        raw_payload.setdefault("futu_account_id", futu_account_id)
    if event_id:
        raw_payload.setdefault("external_event_key", event_id)
    raw_payload.setdefault("side", trade_side)
    order_id = str(getattr(deal, "order_id", "") or "").strip()
    if order_id:
        raw_payload.setdefault("order_id", order_id)
    multiplier_source = str(getattr(deal, "multiplier_source", "") or "").strip()
    if multiplier_source:
        raw_payload.setdefault("multiplier_source", multiplier_source)
    actual_fees = extract_actual_fees(raw_payload)
    if actual_fees is not None:
        raw_payload["fee_provenance"] = {
            "basis": "actual",
            "source": actual_fees["source"],
            "components": actual_fees["components"],
        }
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
        event_id=event_id,
        event_type=event_type,
        event_time_ms=event_time_ms,
        contract_key=contract_key,
        contracts=int(getattr(deal, "contracts", 0) or 0),
        price=float(getattr(deal, "price", 0.0) or 0.0),
        currency=normalize_currency(getattr(deal, "currency", None)),
        source="opend_push",
        multiplier=float(getattr(deal, "multiplier", None) or 100),
        fees=float(actual_fees["amount"]) if actual_fees is not None else 0.0,
        target_lot_id=str(raw_payload.get("target_lot_id") or raw_payload.get("record_id") or "").strip() or None,
        raw_payload=raw_payload,
    )


def _event_type_from_position_effect(position_effect: str, *, raw_payload: dict[str, Any] | None = None) -> str:
    if position_effect == "open":
        return "open"
    if position_effect == "close":
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        close_type = str(payload.get("close_type") or payload.get("broker_close_type") or "").strip().lower()
        if close_type in {"expire_auto_close", "expire_close", "expiration_close", "expiration_zero_close"}:
            return "expire_close"
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
