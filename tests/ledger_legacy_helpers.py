from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from src.application.ledger.position_records import PositionLotRecord
from src.application.ledger.publisher import PublishedPositionLotProjection, project_stored_trade_events_to_position_lots
from src.application.ledger.migration import legacy_trade_event_to_ledger_event


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
        event, diagnostics = legacy_trade_event_to_ledger_event(self)
        errors = [item.code for item in diagnostics if item.severity == "error"]
        if event is None or errors:
            raise AssertionError(f"legacy test event could not be converted: {', '.join(errors) or 'unknown'}")
        return event.to_dict()

    def to_legacy_dict(self) -> dict[str, Any]:
        return asdict(self)


def project_position_lot_records_with_diagnostics(events: list[Any]) -> PublishedPositionLotProjection:
    return project_stored_trade_events_to_position_lots(events)


def project_position_lot_records(events: list[Any]) -> list[PositionLotRecord]:
    return project_position_lot_records_with_diagnostics(events).lots
