from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from domain.domain.ledger.identity import ContractKey
from domain.domain.option_position_identity import normalize_currency


OPEN_EVENT_TYPES = {"open"}
CLOSE_EVENT_TYPES = {"close", "expire_close", "assignment", "exercise"}
TARGET_LOT_EVENT_TYPES = CLOSE_EVENT_TYPES | {"adjust"}
TARGET_EVENT_TYPES = {"void", "repair"}
READONLY_EVENT_TYPES = {"verification"}
SUPPORTED_EVENT_TYPES = OPEN_EVENT_TYPES | TARGET_LOT_EVENT_TYPES | TARGET_EVENT_TYPES | READONLY_EVENT_TYPES


@dataclass(frozen=True)
class LedgerDiagnostic:
    event_id: str
    severity: str
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class TradeEvent:
    event_id: str
    event_type: str
    event_time_ms: int
    contract_key: ContractKey
    contracts: int
    price: float
    currency: str
    source: str
    multiplier: float = 100.0
    fees: float = 0.0
    target_lot_id: str | None = None
    target_event_id: str | None = None
    lot_id: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", str(self.event_id or "").strip())
        object.__setattr__(self, "event_type", str(self.event_type or "").strip().lower())
        object.__setattr__(self, "event_time_ms", int(self.event_time_ms or 0))
        object.__setattr__(self, "contracts", int(self.contracts or 0))
        object.__setattr__(self, "price", float(self.price or 0.0))
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        object.__setattr__(self, "source", str(self.source or "").strip())
        object.__setattr__(self, "multiplier", float(self.multiplier or 0.0))
        object.__setattr__(self, "fees", float(self.fees or 0.0))
        object.__setattr__(self, "target_lot_id", _clean_optional_id(self.target_lot_id))
        object.__setattr__(self, "target_event_id", _clean_optional_id(self.target_event_id))
        object.__setattr__(self, "lot_id", _clean_optional_id(self.lot_id))
        object.__setattr__(self, "raw_payload", dict(self.raw_payload or {}))

    @property
    def is_open(self) -> bool:
        return self.event_type in OPEN_EVENT_TYPES

    @property
    def is_close(self) -> bool:
        return self.event_type in CLOSE_EVENT_TYPES

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "event_time_ms": self.event_time_ms,
            "contract_key": self.contract_key.to_dict(),
            "contracts": self.contracts,
            "price": self.price,
            "currency": self.currency,
            "source": self.source,
            "multiplier": self.multiplier,
            "fees": self.fees,
            "target_lot_id": self.target_lot_id,
            "target_event_id": self.target_event_id,
            "lot_id": self.lot_id,
            "raw_payload": dict(self.raw_payload),
        }


def _clean_optional_id(value: Any) -> str | None:
    raw = str(value or "").strip()
    return raw or None


def lot_id_for_open_event(event: TradeEvent) -> str:
    return event.lot_id or f"lot_{event.event_id}"


def validate_trade_event(event: TradeEvent) -> list[LedgerDiagnostic]:
    diagnostics: list[LedgerDiagnostic] = []
    if not event.event_id:
        diagnostics.append(
            LedgerDiagnostic(
                event_id="",
                severity="error",
                code="event_id_required",
                message="event_id is required",
            )
        )
    if event.event_type not in SUPPORTED_EVENT_TYPES:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event.event_id,
                severity="error",
                code="unsupported_event_type",
                message="event_type is not supported",
                details={"event_type": event.event_type},
            )
        )
    if event.event_type in OPEN_EVENT_TYPES | CLOSE_EVENT_TYPES and event.contracts <= 0:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event.event_id,
                severity="error",
                code="contracts_must_be_positive",
                message="contracts must be > 0",
                details={"contracts": event.contracts},
            )
        )
    if event.event_type in TARGET_LOT_EVENT_TYPES and not event.target_lot_id:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event.event_id,
                severity="error",
                code="target_lot_id_required",
                message="event requires target_lot_id",
                details={"event_type": event.event_type},
            )
        )
    if event.event_type in TARGET_EVENT_TYPES and not event.target_event_id:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event.event_id,
                severity="error",
                code="target_event_id_required",
                message="event requires target_event_id",
                details={"event_type": event.event_type},
            )
        )
    return diagnostics
