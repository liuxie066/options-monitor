from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from domain.domain.ledger import ProjectionResult, TradeEvent, project_trade_events
from domain.domain.ledger.events import LedgerDiagnostic
from domain.domain.ledger.lots import PositionLot
from domain.domain.ledger.position_fields import (
    BUY_TO_CLOSE,
    EXPIRE_AUTO_CLOSE,
    SELL_TO_CLOSE,
    OpenPositionCommand,
    build_position_id,
    build_position_lot_fields,
    normalize_currency,
    parse_exp_to_ms,
)
from domain.domain.trade_contract_identity import normalize_trade_side
from src.application.ledger.event_codec import import_stored_trade_events, trade_event_payload_dict
from src.application.ledger.position_records import PositionLotRecord


@dataclass(frozen=True)
class PublishedPositionLotProjection:
    lots: list[PositionLotRecord]
    diagnostics: list[LedgerDiagnostic]
    ledger_projection: ProjectionResult

    @property
    def has_errors(self) -> bool:
        return any(item.severity == "error" for item in self.diagnostics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lots": [lot.to_dict() for lot in self.lots],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "has_errors": self.has_errors,
            "ledger_projection": self.ledger_projection.to_dict(),
        }


def project_stored_trade_events_to_position_lots(events: list[Any]) -> PublishedPositionLotProjection:
    stored_events = [trade_event_payload_dict(item) for item in events]
    stored_by_event_id = {
        str(item.get("event_id") or "").strip(): item
        for item in stored_events
        if str(item.get("event_id") or "").strip()
    }
    ledger_events, import_diagnostics = import_stored_trade_events(stored_events)
    ledger_events, target_diagnostics = _resolve_legacy_close_targets(ledger_events)
    ledger_projection = project_trade_events(ledger_events)
    lots = [
        _position_lot_to_legacy_record(
            lot,
            ledger_events=ledger_events,
            legacy_by_event_id=stored_by_event_id,
        )
        for lot in ledger_projection.lots
    ]
    lots.extend(_bootstrap_passthrough_records(stored_events, imported_event_ids={event.event_id for event in ledger_events}))
    return PublishedPositionLotProjection(
        lots=lots,
        diagnostics=[*import_diagnostics, *target_diagnostics, *ledger_projection.diagnostics],
        ledger_projection=ledger_projection,
    )


def _resolve_legacy_close_targets(events: list[TradeEvent]) -> tuple[list[TradeEvent], list[LedgerDiagnostic]]:
    resolved: list[TradeEvent] = []
    diagnostics: list[LedgerDiagnostic] = []
    for event in sorted(events, key=lambda item: (int(item.event_time_ms or 0), item.event_id)):
        if not event.is_close or event.target_lot_id:
            resolved.append(event)
            continue
        current = project_trade_events(resolved)
        candidates = [
            lot
            for lot in sorted(current.lots, key=lambda item: (item.opened_at_ms, item.lot_id))
            if lot.contract_key == event.contract_key and int(lot.contracts_open) > 0
        ]
        remaining = int(event.contracts)
        matched = 0
        for lot in candidates:
            if remaining <= 0:
                break
            take = min(int(lot.contracts_open), remaining)
            if take <= 0:
                continue
            close_event_id = event.event_id if matched == 0 else f"{event.event_id}:target:{lot.lot_id}"
            resolved.append(
                replace(
                    event,
                    event_id=close_event_id,
                    contracts=take,
                    target_lot_id=lot.lot_id,
                    raw_payload={
                        **event.raw_payload,
                        "legacy_heuristic_target": True,
                        "legacy_source_event_id": event.event_id,
                    },
                )
            )
            remaining -= take
            matched += take
        if remaining > 0:
            diagnostics.append(
                LedgerDiagnostic(
                    event_id=event.event_id,
                    severity="warn",
                    code="close_unmatched_contracts",
                    message="legacy close event contracts could not be fully matched to open lots",
                    details={
                        "contracts_requested": int(event.contracts),
                        "contracts_matched": int(matched),
                        "contracts_unmatched": int(remaining),
                    },
                )
            )
    return resolved, diagnostics


def _position_lot_to_legacy_record(
    lot: PositionLot,
    *,
    ledger_events: list[TradeEvent],
    legacy_by_event_id: dict[str, dict[str, Any]],
) -> PositionLotRecord:
    ledger_by_event_id = {event.event_id: event for event in ledger_events}
    open_event = ledger_by_event_id.get(lot.open_event_id)
    legacy_open_event = legacy_by_event_id.get(lot.open_event_id, {})
    base_fields = _base_fields_for_lot(lot, open_event=open_event, legacy_open_event=legacy_open_event)
    fields = _apply_lot_state_fields(base_fields, lot, ledger_by_event_id=ledger_by_event_id)
    close_event = ledger_by_event_id.get(lot.close_event_ids[-1]) if lot.close_event_ids else None
    if close_event is not None:
        fields.update(_close_fields(close_event, legacy_by_event_id=legacy_by_event_id, lot=lot))
    return PositionLotRecord(record_id=lot.lot_id, fields=fields)


def _bootstrap_passthrough_records(
    legacy_events: list[dict[str, Any]],
    *,
    imported_event_ids: set[str],
) -> list[PositionLotRecord]:
    records: list[PositionLotRecord] = []
    passthrough_sources = {"feishu_bootstrap", "legacy_option_positions"}
    for event in legacy_events:
        event_id = str(event.get("event_id") or "").strip()
        if not event_id or event_id in imported_event_ids:
            continue
        if str(event.get("source_type") or "").strip().lower() != "bootstrap_snapshot":
            continue
        if str(event.get("source_name") or "").strip() not in passthrough_sources:
            continue
        payload = _event_payload(event)
        record_id = str(payload.get("lot_record_id") or "").strip()
        fields = payload.get("fields")
        if not record_id or not isinstance(fields, dict):
            continue
        patched_fields = dict(fields)
        patched_fields["source_event_id"] = event_id
        patched_fields["event_source_type"] = str(event.get("source_type") or "").strip()
        patched_fields["event_source_name"] = str(event.get("source_name") or "").strip()
        records.append(PositionLotRecord(record_id=record_id, fields=patched_fields))
    return records


def _base_fields_for_lot(
    lot: PositionLot,
    *,
    open_event: TradeEvent | None,
    legacy_open_event: dict[str, Any],
) -> dict[str, Any]:
    raw_payload = _event_payload(legacy_open_event)
    snapshot_fields = raw_payload.get("fields")
    if isinstance(snapshot_fields, dict):
        fields = dict(snapshot_fields)
    else:
        source_name = str(legacy_open_event.get("source_name") or (open_event.source if open_event else "")).strip()
        order_id = str(legacy_open_event.get("order_id") or "").strip()
        multiplier_source = str(legacy_open_event.get("multiplier_source") or "").strip()
        note = (
            f"source={source_name} "
            f"event_id={lot.open_event_id} "
            f"order_id={order_id} "
            f"multiplier_source={multiplier_source}"
        ).strip()
        fields = build_position_lot_fields(
            OpenPositionCommand(
                broker=lot.contract_key.broker,
                account=lot.contract_key.account,
                symbol=lot.contract_key.underlying_symbol,
                option_type=lot.contract_key.option_type,
                side=lot.contract_key.position_side,
                contracts=int(lot.contracts_opened),
                currency=normalize_currency(lot.currency),
                strike=float(lot.contract_key.strike),
                multiplier=float(lot.multiplier),
                expiration_ymd=lot.contract_key.expiration_ymd,
                premium_per_share=float(lot.premium_open),
                note=note,
                opened_at_ms=int(lot.opened_at_ms),
            )
        ).to_dict()
    fields["source_event_id"] = lot.open_event_id
    fields["event_source_type"] = str(legacy_open_event.get("source_type") or "").strip()
    fields["event_source_name"] = str(legacy_open_event.get("source_name") or (open_event.source if open_event else "")).strip()
    return fields


def _apply_lot_state_fields(
    fields: dict[str, Any],
    lot: PositionLot,
    *,
    ledger_by_event_id: dict[str, TradeEvent],
) -> dict[str, Any]:
    out = dict(fields)
    expiration_ms = parse_exp_to_ms(lot.contract_key.expiration_ymd)
    out.update(
        {
            "broker": lot.contract_key.broker,
            "account": lot.contract_key.account,
            "symbol": lot.contract_key.underlying_symbol,
            "option_type": lot.contract_key.option_type,
            "side": lot.contract_key.position_side,
            "contracts": int(lot.contracts_opened),
            "contracts_open": int(lot.contracts_open),
            "contracts_closed": int(lot.contracts_closed),
            "currency": normalize_currency(lot.currency),
            "status": lot.status,
            "strike": float(lot.contract_key.strike),
            "expiration_ymd": lot.contract_key.expiration_ymd,
            "multiplier": _compact_number(lot.multiplier),
            "premium": float(lot.premium_open),
            "opened_at": int(lot.opened_at_ms),
            "last_action_at": int(_last_action_at(lot, ledger_by_event_id=ledger_by_event_id)),
            "position_id": build_position_id(
                symbol=lot.contract_key.underlying_symbol,
                expiration_ymd=lot.contract_key.expiration_ymd,
                strike=lot.contract_key.strike,
                option_type=lot.contract_key.option_type,
                side=lot.contract_key.position_side,
                contracts=int(lot.contracts_opened),
            ),
            "position_key": lot.contract_key.position_key,
        }
    )
    if expiration_ms is not None:
        out["expiration"] = int(expiration_ms)
    if lot.contract_key.position_side == "short" and lot.contract_key.option_type == "put":
        out["cash_secured_amount"] = float(lot.contract_key.strike) * float(lot.multiplier) * int(lot.contracts_opened)
    if lot.contract_key.position_side == "short" and lot.contract_key.option_type == "call":
        out["underlying_share_locked"] = int(float(lot.multiplier) * int(lot.contracts_opened))
    return out


def _close_fields(
    event: TradeEvent,
    *,
    legacy_by_event_id: dict[str, dict[str, Any]],
    lot: PositionLot,
) -> dict[str, Any]:
    legacy_event = legacy_by_event_id.get(event.event_id, {})
    payload = _event_payload(legacy_event)
    close_type = str(payload.get("close_type") or "").strip().lower()
    mode = str(payload.get("mode") or "").strip().lower()
    trade_side = normalize_trade_side(legacy_event.get("side"))
    if close_type != EXPIRE_AUTO_CLOSE and mode != EXPIRE_AUTO_CLOSE:
        if trade_side == "buy":
            close_type = BUY_TO_CLOSE
        elif trade_side == "sell":
            close_type = SELL_TO_CLOSE
        else:
            close_type = BUY_TO_CLOSE if event.contract_key.position_side == "short" else SELL_TO_CLOSE
    else:
        close_type = EXPIRE_AUTO_CLOSE

    reason = str(payload.get("close_reason") or "").strip()
    if not reason:
        if close_type == EXPIRE_AUTO_CLOSE:
            reason = "expired"
        elif close_type == BUY_TO_CLOSE:
            reason = "broker_trade_buy_to_close"
        else:
            reason = "broker_trade_sell_to_close"

    fields: dict[str, Any] = {
        "close_type": close_type,
        "close_reason": reason,
        "close_price": float(event.price),
        "last_close_event_id": event.event_id,
        "last_action_at": int(event.event_time_ms),
    }
    if lot.contracts_open <= 0:
        fields["closed_at"] = int(event.event_time_ms)
    if close_type == EXPIRE_AUTO_CLOSE:
        fields["auto_close_exp_src"] = str(payload.get("auto_close_exp_src") or payload.get("effective_exp_source") or "").strip()
        raw_grace_days = payload.get("auto_close_grace_days")
        if raw_grace_days not in (None, ""):
            fields["auto_close_grace_days"] = int(raw_grace_days)
    return fields


def _last_action_at(lot: PositionLot, *, ledger_by_event_id: dict[str, TradeEvent]) -> int:
    event = ledger_by_event_id.get(lot.last_event_id)
    if event is not None:
        return int(event.event_time_ms)
    return int(lot.opened_at_ms)


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("raw_payload") or {}
    return dict(payload) if isinstance(payload, dict) else {}


def _compact_number(value: Any) -> int | float:
    numeric = float(value or 0.0)
    return int(numeric) if numeric.is_integer() else numeric


__all__ = [
    "PublishedPositionLotProjection",
    "project_stored_trade_events_to_position_lots",
]
