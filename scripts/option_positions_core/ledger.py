from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from scripts.option_positions_core.domain import (
    OpenPositionCommand,
    build_buy_to_close_patch,
    build_open_fields,
    effective_contracts_open,
    normalize_account,
    normalize_broker,
    normalize_option_type,
    normalize_side,
)


@dataclass(frozen=True)
class TradeEvent:
    event_id: str
    source_type: str
    source_name: str
    broker: str
    account: str
    symbol: str
    option_type: str
    side: str
    position_effect: str
    contracts: int
    price: float
    strike: float | None
    multiplier: int | None
    expiration_ymd: str | None
    currency: str
    trade_time_ms: int | None
    order_id: str | None
    multiplier_source: str | None
    raw_payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def trade_event_from_normalized_deal(deal: Any) -> TradeEvent:
    return TradeEvent(
        event_id=str(getattr(deal, "deal_id", "") or "").strip(),
        source_type="broker_trade_event",
        source_name="opend_push",
        broker=normalize_broker(getattr(deal, "broker", None) or "富途"),
        account=normalize_account(getattr(deal, "internal_account", None) or ""),
        symbol=str(getattr(deal, "symbol", "") or "").strip().upper(),
        option_type=normalize_option_type(getattr(deal, "option_type", None) or ""),
        side=str(getattr(deal, "side", "") or "").strip().lower(),
        position_effect=str(getattr(deal, "position_effect", "") or "").strip().lower(),
        contracts=int(getattr(deal, "contracts", 0) or 0),
        price=float(getattr(deal, "price", 0.0) or 0.0),
        strike=(float(getattr(deal, "strike")) if getattr(deal, "strike", None) is not None else None),
        multiplier=(int(getattr(deal, "multiplier")) if getattr(deal, "multiplier", None) is not None else None),
        expiration_ymd=(str(getattr(deal, "expiration_ymd", "") or "").strip() or None),
        currency=str(getattr(deal, "currency", "") or "").strip().upper(),
        trade_time_ms=(int(getattr(deal, "trade_time_ms")) if getattr(deal, "trade_time_ms", None) is not None else None),
        order_id=(str(getattr(deal, "order_id", "") or "").strip() or None),
        multiplier_source=(str(getattr(deal, "multiplier_source", "") or "").strip() or None),
        raw_payload=(dict(getattr(deal, "raw_payload", {}) or {})),
    )


def _same_strike(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return abs(float(left) - float(right)) < 1e-9


def _open_lot_record(event: TradeEvent) -> dict[str, Any]:
    if str(event.source_type).strip().lower() == "bootstrap_snapshot":
        payload = dict(event.raw_payload or {})
        record_id = str(payload.get("lot_record_id") or event.event_id or "").strip()
        snapshot_fields = payload.get("fields") or {}
        if record_id and isinstance(snapshot_fields, dict):
            fields = dict(snapshot_fields)
            fields["source_event_id"] = event.event_id
            fields["event_source_type"] = event.source_type
            fields["event_source_name"] = event.source_name
            return {"record_id": record_id, "fields": fields}
    cmd = OpenPositionCommand(
        broker=event.broker,
        account=event.account,
        symbol=event.symbol,
        option_type=event.option_type,
        side="short",
        contracts=int(event.contracts),
        currency=event.currency,
        strike=event.strike,
        multiplier=event.multiplier,
        expiration_ymd=event.expiration_ymd,
        premium_per_share=event.price,
        note=(
            f"source={event.source_name} "
            f"event_id={event.event_id} "
            f"order_id={event.order_id or ''} "
            f"multiplier_source={event.multiplier_source or ''}"
        ).strip(),
        opened_at_ms=event.trade_time_ms,
    )
    fields = build_open_fields(cmd)
    fields["source_event_id"] = event.event_id
    fields["event_source_type"] = event.source_type
    fields["event_source_name"] = event.source_name
    return {"record_id": f"lot_{event.event_id}", "fields": fields}


def _matches_close(fields: dict[str, Any], event: TradeEvent) -> bool:
    if effective_contracts_open(fields) <= 0:
        return False
    return (
        normalize_broker(fields.get("broker")) == event.broker
        and normalize_account(fields.get("account")) == event.account
        and str(fields.get("symbol") or "").strip().upper() == event.symbol
        and normalize_option_type(fields.get("option_type")) == event.option_type
        and normalize_side(fields.get("side")) == "short"
        and _same_strike(fields.get("strike"), event.strike)
        and str(fields.get("source_event_id") or "") != event.event_id
        and str(fields.get("note") or "").find(f"exp={event.expiration_ymd}") >= 0
    )


def project_position_lot_records(events: list[dict[str, Any]] | list[TradeEvent]) -> list[dict[str, Any]]:
    normalized_events: list[TradeEvent] = []
    for item in events:
        if isinstance(item, TradeEvent):
            normalized_events.append(item)
            continue
        if not isinstance(item, dict):
            continue
        normalized_events.append(TradeEvent(**item))

    normalized_events.sort(key=lambda row: (int(row.trade_time_ms or 0), row.event_id))
    lots: list[dict[str, Any]] = []
    for event in normalized_events:
        if str(event.source_type).strip().lower() == "bootstrap_snapshot":
            seeded = _open_lot_record(event)
            if seeded.get("record_id") and isinstance(seeded.get("fields"), dict):
                lots.append(seeded)
            continue
        if not event.event_id or event.contracts <= 0:
            continue
        if event.position_effect == "open" and event.side == "sell":
            lots.append(_open_lot_record(event))
            continue
        if event.position_effect != "close" or event.side != "buy":
            continue

        remaining = int(event.contracts)
        for lot in lots:
            fields = lot.get("fields") or {}
            open_qty = int(fields.get("contracts_open") or 0)
            if open_qty <= 0 or not _matches_close(fields, event):
                continue
            take = min(open_qty, remaining)
            patch = build_buy_to_close_patch(
                fields,
                contracts_to_close=take,
                close_price=event.price,
                close_reason="broker_trade_buy_to_close",
                as_of_ms=event.trade_time_ms,
            )
            merged = dict(fields)
            merged.update(patch)
            merged["last_close_event_id"] = event.event_id
            lot["fields"] = merged
            remaining -= take
            if remaining <= 0:
                break
    return lots
