from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.position_records import PositionLotRecord
from src.application.ledger.publisher import PublishedPositionLotProjection, project_stored_trade_events_to_position_lots


@dataclass(frozen=True)
class LegacyTradeEvent:
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
    multiplier: float | None
    expiration_ymd: str | None
    currency: str
    trade_time_ms: int
    order_id: str | None = None
    multiplier_source: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        if self.position_effect == "open":
            event_type = "open"
            position_side = "short" if self.side == "sell" else "long"
        elif self.position_effect == "close":
            event_type = (
                "expire_close"
                if self.raw_payload.get("close_type") == "expire_auto_close"
                else "close"
            )
            position_side = "short" if self.side == "buy" else "long"
        else:
            raise AssertionError(f"unsupported test position_effect: {self.position_effect}")
        target_lot_id = str(
            self.raw_payload.get("target_lot_id")
            or self.raw_payload.get("record_id")
            or ""
        ).strip() or None
        lot_id = str(
            self.raw_payload.get("lot_record_id")
            or self.raw_payload.get("lot_id")
            or f"lot_{self.event_id}"
        ).strip()
        return TradeEvent(
            event_id=self.event_id,
            event_type=event_type,
            event_time_ms=self.trade_time_ms,
            contract_key=ContractKey.from_values(
                broker=self.broker,
                account=self.account,
                underlying_symbol=self.symbol,
                option_type=self.option_type,
                position_side=position_side,
                strike=self.strike,
                expiration_ymd=self.expiration_ymd,
            ),
            contracts=self.contracts,
            price=self.price,
            currency=self.currency,
            source=self.source_name,
            multiplier=float(self.multiplier or 100),
            fees=float(self.raw_payload.get("fees") or 0.0),
            target_lot_id=target_lot_id,
            target_event_id=str(
                self.raw_payload.get("target_event_id")
                or self.raw_payload.get("void_target_event_id")
                or ""
            ).strip() or None,
            lot_id=lot_id,
            raw_payload=dict(self.raw_payload),
        ).to_dict()

    def to_legacy_dict(self) -> dict[str, Any]:
        return asdict(self)


def project_position_lot_records_with_diagnostics(events: list[Any]) -> PublishedPositionLotProjection:
    return project_stored_trade_events_to_position_lots(events)


def project_position_lot_records(events: list[Any]) -> list[PositionLotRecord]:
    return project_position_lot_records_with_diagnostics(events).lots
