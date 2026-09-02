from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

from domain.domain.ledger.events import (
    TradeEvent,
    lot_id_for_open_event,
    validate_trade_event,
)
from domain.domain.ledger.economics import fee_fact_for_event
from domain.domain.ledger.identity import ContractKey
from domain.domain.ledger.lots import PositionLot
from domain.domain.ledger.position_fields import (
    POSITION_LOT_STRATEGY_PATCH_FIELDS,
    strategy_metadata_fields_from_payload,
)
from domain.domain.money import canonical_decimal_text, to_decimal


RESUMABLE_PROJECTION_STATE_SCHEMA = "resumable_projection_state.v1"
EMPTY_PROJECTION_DIAGNOSTIC_SHA256 = hashlib.sha256(
    b"position_projection_diagnostics.empty.v1"
).hexdigest()

_STATE_KEYS = {
    "schema_version",
    "diagnostic_count",
    "diagnostic_sha256",
    "active_lots",
}
_LOT_KEYS = {
    "lot_id",
    "open_event_id",
    "contract_key",
    "opened_at_ms",
    "contracts_opened",
    "contracts_open",
    "contracts_closed",
    "status",
    "premium_open",
    "multiplier",
    "currency",
    "realized_pnl",
    "last_event_id",
    "last_close_event_id",
    "open_event",
    "allocated_open_fee",
}
_CONTRACT_KEY_KEYS = {
    "broker",
    "account",
    "underlying_symbol",
    "option_type",
    "position_side",
    "strike",
    "expiration_ymd",
    "position_key",
}
_EVENT_KEYS = {
    "event_id",
    "event_type",
    "event_time_ms",
    "contract_key",
    "contracts",
    "price",
    "currency",
    "source",
    "multiplier",
    "fees",
    "target_lot_id",
    "target_event_id",
    "lot_id",
    "raw_payload",
}
_ECONOMIC_STRATEGY_KEYS = tuple(
    key
    for key in POSITION_LOT_STRATEGY_PATCH_FIELDS
    if key != "strategy_snapshot"
)


def _contract_key_from_dict(payload: Any) -> ContractKey:
    if not isinstance(payload, dict) or set(payload) != _CONTRACT_KEY_KEYS:
        raise ValueError("resumable lot contract_key fields differ from v1 schema")
    contract_key = ContractKey.from_values(
        broker=payload.get("broker"),
        account=payload.get("account"),
        underlying_symbol=(
            payload.get("underlying_symbol") or payload.get("symbol")
        ),
        option_type=payload.get("option_type"),
        position_side=(payload.get("position_side") or payload.get("side")),
        strike=payload.get("strike"),
        expiration_ymd=(
            payload.get("expiration_ymd") or payload.get("expiration")
        ),
    )
    if payload.get("position_key") != contract_key.position_key:
        raise ValueError("resumable lot contract_key position_key mismatch")
    return contract_key


def _decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("allocated_open_fee must be a finite Decimal")
    return canonical_decimal_text(value, field_name="allocated_open_fee")


def _parse_decimal(value: Any) -> Decimal:
    try:
        parsed = to_decimal(value, field_name="allocated_open_fee")
    except (TypeError, ValueError) as exc:
        raise ValueError("allocated_open_fee must be decimal text") from exc
    return parsed


def _finite_float(
    value: Any,
    *,
    field_name: str,
    positive: bool = False,
    nonnegative: bool = False,
) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be numeric")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not math.isfinite(numeric):
        raise ValueError(f"{field_name} must be finite")
    if positive and numeric <= 0:
        raise ValueError(f"{field_name} must be > 0")
    if nonnegative and numeric < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return numeric


def _exact_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _resumable_open_event(event: TradeEvent) -> TradeEvent:
    """Keep only open-event facts consumed by future economic allocations."""

    raw_payload = event.raw_payload if isinstance(event.raw_payload, dict) else {}
    fee_fact = fee_fact_for_event(event)
    fee_provenance: dict[str, Any] = {
        "basis": fee_fact.basis.value,
        "source": fee_fact.source,
        "reason": fee_fact.reason,
    }
    if fee_fact.amount is not None:
        fee_provenance["amount"] = _decimal_text(fee_fact.amount)
    resumable_payload: dict[str, Any] = {
        "fee_provenance": fee_provenance,
    }
    strategy_metadata = strategy_metadata_fields_from_payload(raw_payload)
    for key in _ECONOMIC_STRATEGY_KEYS:
        text = str(strategy_metadata.get(key) or "").strip()
        if text:
            resumable_payload[key] = text
    return TradeEvent(
        event_id=event.event_id,
        event_type=event.event_type,
        event_time_ms=event.event_time_ms,
        contract_key=event.contract_key,
        contracts=event.contracts,
        price=event.price,
        currency=event.currency,
        source=event.source,
        multiplier=event.multiplier,
        fees=event.fees,
        target_lot_id=event.target_lot_id,
        target_event_id=event.target_event_id,
        lot_id=event.lot_id,
        raw_payload=resumable_payload,
    )


@dataclass(frozen=True)
class ResumableLotState:
    lot_id: str
    open_event_id: str
    contract_key: ContractKey
    opened_at_ms: int
    contracts_opened: int
    contracts_open: int
    contracts_closed: int
    status: str
    premium_open: float
    multiplier: float
    currency: str
    realized_pnl: float
    last_event_id: str
    last_close_event_id: str | None
    open_event: TradeEvent
    allocated_open_fee: Decimal

    def __post_init__(self) -> None:
        lot_id = str(self.lot_id or "").strip()
        open_event_id = str(self.open_event_id or "").strip()
        last_event_id = str(self.last_event_id or "").strip()
        last_close_event_id = str(self.last_close_event_id or "").strip() or None
        if not lot_id or not open_event_id or not last_event_id:
            raise ValueError("resumable lot ids must be non-empty")
        opened_at_ms = _exact_int(self.opened_at_ms, field_name="opened_at_ms")
        contracts_opened = _exact_int(
            self.contracts_opened,
            field_name="contracts_opened",
        )
        contracts_open = _exact_int(
            self.contracts_open,
            field_name="contracts_open",
        )
        contracts_closed = _exact_int(
            self.contracts_closed,
            field_name="contracts_closed",
        )
        if opened_at_ms <= 0:
            raise ValueError("opened_at_ms must be > 0")
        if contracts_opened <= 0:
            raise ValueError("contracts_opened must be > 0")
        if contracts_open <= 0 or str(self.status) != "open":
            raise ValueError("resumable projection stores active lots only")
        if contracts_closed < 0:
            raise ValueError("contracts_closed must be >= 0")
        if contracts_open + contracts_closed != contracts_opened:
            raise ValueError("resumable lot contract balance is invalid")
        premium_open = _finite_float(
            self.premium_open,
            field_name="premium_open",
            nonnegative=True,
        )
        multiplier = _finite_float(
            self.multiplier,
            field_name="multiplier",
            positive=True,
        )
        realized_pnl = _finite_float(
            self.realized_pnl,
            field_name="realized_pnl",
        )
        currency = str(self.currency or "").strip().upper()
        if not currency:
            raise ValueError("resumable lot currency must be non-empty")
        open_event = _resumable_open_event(self.open_event)
        if open_event.event_type != "open":
            raise ValueError("resumable lot open_event must be an open event")
        if open_event.event_id != open_event_id:
            raise ValueError("resumable lot open_event_id mismatch")
        if lot_id_for_open_event(open_event) != lot_id:
            raise ValueError("resumable lot_id does not match open_event")
        immutable_contract_fields = (
            "broker",
            "account",
            "underlying_symbol",
            "option_type",
            "position_side",
        )
        if any(
            getattr(open_event.contract_key, field_name)
            != getattr(self.contract_key, field_name)
            for field_name in immutable_contract_fields
        ):
            raise ValueError("resumable lot immutable contract identity changed")
        if any(item.severity == "error" for item in validate_trade_event(open_event)):
            raise ValueError("resumable lot open_event is invalid")
        if not isinstance(self.allocated_open_fee, Decimal):
            raise TypeError("allocated_open_fee must be Decimal")
        _decimal_text(self.allocated_open_fee)
        if self.allocated_open_fee < 0:
            raise ValueError("allocated_open_fee must be >= 0")
        object.__setattr__(self, "lot_id", lot_id)
        object.__setattr__(self, "open_event_id", open_event_id)
        object.__setattr__(self, "opened_at_ms", opened_at_ms)
        object.__setattr__(self, "contracts_opened", contracts_opened)
        object.__setattr__(self, "contracts_open", contracts_open)
        object.__setattr__(self, "contracts_closed", contracts_closed)
        object.__setattr__(self, "status", "open")
        object.__setattr__(self, "premium_open", premium_open)
        object.__setattr__(self, "multiplier", multiplier)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "realized_pnl", realized_pnl)
        object.__setattr__(self, "last_event_id", last_event_id)
        object.__setattr__(self, "last_close_event_id", last_close_event_id)
        object.__setattr__(self, "open_event", open_event)

    @classmethod
    def from_position_lot(
        cls,
        lot: PositionLot,
        *,
        open_event: TradeEvent,
        allocated_open_fee: Decimal,
        last_close_event_id: str | None,
    ) -> "ResumableLotState":
        return cls(
            lot_id=lot.lot_id,
            open_event_id=lot.open_event_id,
            contract_key=lot.contract_key,
            opened_at_ms=lot.opened_at_ms,
            contracts_opened=lot.contracts_opened,
            contracts_open=lot.contracts_open,
            contracts_closed=lot.contracts_closed,
            status=lot.status,
            premium_open=lot.premium_open,
            multiplier=lot.multiplier,
            currency=lot.currency,
            realized_pnl=lot.realized_pnl,
            last_event_id=lot.last_event_id,
            last_close_event_id=last_close_event_id,
            open_event=open_event,
            allocated_open_fee=allocated_open_fee,
        )

    def to_position_lot(self) -> PositionLot:
        return PositionLot(
            lot_id=self.lot_id,
            open_event_id=self.open_event_id,
            contract_key=self.contract_key,
            opened_at_ms=self.opened_at_ms,
            contracts_opened=self.contracts_opened,
            contracts_open=self.contracts_open,
            contracts_closed=self.contracts_closed,
            status=self.status,
            premium_open=self.premium_open,
            multiplier=self.multiplier,
            currency=self.currency,
            realized_pnl=self.realized_pnl,
            last_event_id=self.last_event_id,
            close_event_ids=(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "lot_id": self.lot_id,
            "open_event_id": self.open_event_id,
            "contract_key": self.contract_key.to_dict(),
            "opened_at_ms": self.opened_at_ms,
            "contracts_opened": self.contracts_opened,
            "contracts_open": self.contracts_open,
            "contracts_closed": self.contracts_closed,
            "status": self.status,
            "premium_open": self.premium_open,
            "multiplier": self.multiplier,
            "currency": self.currency,
            "realized_pnl": self.realized_pnl,
            "last_event_id": self.last_event_id,
            "last_close_event_id": self.last_close_event_id,
            "open_event": self.open_event.to_dict(),
            "allocated_open_fee": _decimal_text(self.allocated_open_fee),
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "ResumableLotState":
        if not isinstance(payload, dict) or set(payload) != _LOT_KEYS:
            raise ValueError("resumable lot fields differ from v1 schema")
        open_event = payload["open_event"]
        if not isinstance(open_event, dict) or set(open_event) != _EVENT_KEYS:
            raise ValueError("resumable lot open_event fields differ from v1 schema")
        return cls(
            lot_id=payload["lot_id"],
            open_event_id=payload["open_event_id"],
            contract_key=_contract_key_from_dict(payload["contract_key"]),
            opened_at_ms=payload["opened_at_ms"],
            contracts_opened=payload["contracts_opened"],
            contracts_open=payload["contracts_open"],
            contracts_closed=payload["contracts_closed"],
            status=str(payload["status"]),
            premium_open=payload["premium_open"],
            multiplier=payload["multiplier"],
            currency=str(payload["currency"]),
            realized_pnl=payload["realized_pnl"],
            last_event_id=payload["last_event_id"],
            last_close_event_id=payload["last_close_event_id"],
            open_event=TradeEvent.from_dict(open_event),
            allocated_open_fee=_parse_decimal(payload["allocated_open_fee"]),
        )


@dataclass(frozen=True)
class ResumableProjectionState:
    active_lots: tuple[ResumableLotState, ...] = ()
    schema_version: str = RESUMABLE_PROJECTION_STATE_SCHEMA
    diagnostic_count: int = 0
    diagnostic_sha256: str = EMPTY_PROJECTION_DIAGNOSTIC_SHA256

    def __post_init__(self) -> None:
        lots = tuple(self.active_lots)
        lot_ids = tuple(item.lot_id for item in lots)
        if lot_ids != tuple(sorted(lot_ids)) or len(set(lot_ids)) != len(lot_ids):
            raise ValueError("resumable active lots must have unique sorted lot_id values")
        if self.schema_version != RESUMABLE_PROJECTION_STATE_SCHEMA:
            raise ValueError("resumable projection state schema is unsupported")
        if (
            isinstance(self.diagnostic_count, bool)
            or not isinstance(self.diagnostic_count, int)
            or self.diagnostic_count != 0
        ):
            raise ValueError("resumable projection state requires zero diagnostics")
        if self.diagnostic_sha256 != EMPTY_PROJECTION_DIAGNOSTIC_SHA256:
            raise ValueError("resumable projection diagnostic sentinel is invalid")
        object.__setattr__(self, "active_lots", lots)

    @classmethod
    def from_lots(
        cls,
        lots: Iterable[ResumableLotState],
    ) -> "ResumableProjectionState":
        return cls(active_lots=tuple(sorted(lots, key=lambda item: item.lot_id)))

    @classmethod
    def empty(cls) -> "ResumableProjectionState":
        return cls()

    @property
    def accounts(self) -> tuple[str, ...]:
        return tuple(
            sorted({item.contract_key.account for item in self.active_lots})
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "diagnostic_count": self.diagnostic_count,
            "diagnostic_sha256": self.diagnostic_sha256,
            "active_lots": [item.to_dict() for item in self.active_lots],
        }

    def to_json_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    @classmethod
    def from_dict(cls, payload: Any) -> "ResumableProjectionState":
        if not isinstance(payload, dict) or set(payload) != _STATE_KEYS:
            raise ValueError("resumable projection state fields differ from v1 schema")
        active_lots = payload["active_lots"]
        if not isinstance(active_lots, list):
            raise ValueError("resumable active_lots must be an array")
        return cls(
            schema_version=str(payload["schema_version"]),
            diagnostic_count=payload["diagnostic_count"],
            diagnostic_sha256=str(payload["diagnostic_sha256"]),
            active_lots=tuple(
                ResumableLotState.from_dict(item) for item in active_lots
            ),
        )

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> "ResumableProjectionState":
        raw = bytes(payload)

        def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            out: dict[str, Any] = {}
            for key, value in items:
                if key in out:
                    raise ValueError(f"duplicate resumable state key: {key}")
                out[key] = value
            return out

        def _constant(value: str) -> Any:
            raise ValueError(f"non-finite resumable state number: {value}")

        try:
            decoded = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_pairs,
                parse_constant=_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("resumable projection state is not valid UTF-8 JSON") from exc
        state = cls.from_dict(decoded)
        if state.to_json_bytes() != raw:
            raise ValueError("resumable projection state JSON is not canonical")
        return state


__all__ = [
    "EMPTY_PROJECTION_DIAGNOSTIC_SHA256",
    "RESUMABLE_PROJECTION_STATE_SCHEMA",
    "ResumableLotState",
    "ResumableProjectionState",
]
