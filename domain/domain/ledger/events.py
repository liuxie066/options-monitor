from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
import math
from typing import Any

from domain.domain.ledger.identity import ContractKey, position_key_for
from domain.domain.money import canonical_decimal_text, coerce_decimal, to_decimal
from domain.domain.option_position_identity import normalize_currency
from domain.domain.trade_contract_identity import (
    derive_position_side,
    derive_trade_side,
    normalize_asset_type,
    normalize_quantity_unit,
    normalize_trade_side,
)


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
    account: str | None = None
    broker: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
            "account": self.account,
            "broker": self.broker,
        }


@dataclass(frozen=True)
class TradeEvent:
    event_id: str
    event_type: str
    event_time_ms: int
    contract_key: ContractKey
    contracts: int | Decimal
    price: Decimal
    currency: str
    source: str
    multiplier: int = 100
    fees: Decimal = Decimal("0")
    target_lot_id: str | None = None
    target_event_id: str | None = None
    lot_id: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)
    asset_type: str = "option"
    quantity_unit: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", str(self.event_id or "").strip())
        object.__setattr__(self, "event_type", str(self.event_type or "").strip().lower())
        object.__setattr__(self, "event_time_ms", int(self.event_time_ms or 0))
        object.__setattr__(self, "price", coerce_decimal(self.price))
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        object.__setattr__(self, "source", str(self.source or "").strip())
        object.__setattr__(self, "fees", coerce_decimal(self.fees))
        object.__setattr__(self, "target_lot_id", _clean_optional_id(self.target_lot_id))
        object.__setattr__(self, "target_event_id", _clean_optional_id(self.target_event_id))
        object.__setattr__(self, "lot_id", _clean_optional_id(self.lot_id))
        object.__setattr__(self, "raw_payload", dict(self.raw_payload or {}))
        asset_type = normalize_asset_type(self.asset_type) or self.contract_key.asset_type or "option"
        object.__setattr__(self, "asset_type", asset_type)
        quantity = to_decimal(0 if self.contracts in (None, "") else self.contracts, field_name="contracts")
        if asset_type != "stock" and quantity != quantity.to_integral_value():
            raise ValueError("option contracts must be a whole number")
        object.__setattr__(self, "contracts", quantity if asset_type == "stock" else int(quantity))
        if asset_type == "stock":
            object.__setattr__(self, "multiplier", 0)
        else:
            raw_multiplier = float(self.multiplier or 0)
            if not math.isfinite(raw_multiplier):
                object.__setattr__(self, "multiplier", 0)
            elif raw_multiplier != int(raw_multiplier):
                raise ValueError("multiplier must be a whole number")
            else:
                object.__setattr__(self, "multiplier", int(raw_multiplier))
        object.__setattr__(
            self,
            "quantity_unit",
            normalize_quantity_unit(self.quantity_unit)
            or ({"stock": "share", "option": "contract"}.get(asset_type)),
        )

    @property
    def side(self) -> str | None:
        """Trade side (buy/sell) carried by the source payload, if any."""
        declared = normalize_trade_side(self.raw_payload.get("side"))
        if declared:
            return declared
        # §9.2 step 3: events written before the migration stored the *position*
        # side under the legacy position-record payload (``raw_payload["fields"]``)
        # instead of a trade side, so re-derive it for those rows.
        legacy_fields = self.raw_payload.get("fields")
        if isinstance(legacy_fields, Mapping):
            return derive_trade_side(self.event_type, legacy_fields.get("side"))
        return None

    @property
    def position_effect(self) -> str | None:
        """Position effect derived from the event type (open/close)."""
        if self.event_type in OPEN_EVENT_TYPES:
            return "open"
        if self.event_type in CLOSE_EVENT_TYPES:
            return "close"
        return None

    @property
    def position_side(self) -> str | None:
        """Derived position side (long/short) from side + position_effect.

        §9.2 step 3: ``ContractKey`` no longer carries ``position_side``; it is
        derived via ``derive_position_side(position_effect, side)``. Void/adjust
        events do not change direction and yield ``None``.
        """
        return derive_position_side(self.position_effect, self.side)

    @property
    def position_key(self) -> str:
        return position_key_for(self.contract_key, self.position_side or "")

    @property
    def is_open(self) -> bool:
        return self.event_type in OPEN_EVENT_TYPES

    @property
    def is_close(self) -> bool:
        return self.event_type in CLOSE_EVENT_TYPES

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TradeEvent":
        if not isinstance(payload, dict):
            raise TypeError("trade event payload must be an object")
        contract_payload = payload.get("contract_key")
        if not isinstance(contract_payload, dict):
            raise ValueError("trade event contract_key must be an object")
        event_type = str(payload.get("event_type") or "").strip().lower()
        raw_payload = dict(payload.get("raw_payload") or {})
        if "side" not in raw_payload:
            # Legacy payloads stored position_side on the contract key rather than
            # the trade side in raw_payload; backfill the trade side so position_side
            # can be re-derived uniformly.
            stored_position_side = (
                contract_payload.get("position_side") or contract_payload.get("side")
            )
            if stored_position_side:
                trade_side = derive_trade_side(event_type, stored_position_side)
                if trade_side:
                    raw_payload["side"] = trade_side
        return cls(
            event_id=payload.get("event_id"),
            event_type=payload.get("event_type"),
            event_time_ms=payload.get("event_time_ms"),
            contract_key=ContractKey.from_values(
                broker=contract_payload.get("broker"),
                account=contract_payload.get("account"),
                underlying_symbol=(
                    contract_payload.get("underlying_symbol")
                    or contract_payload.get("symbol")
                ),
                option_type=contract_payload.get("option_type"),
                strike=contract_payload.get("strike"),
                expiration_ymd=(
                    contract_payload.get("expiration_ymd")
                    or contract_payload.get("expiration")
                ),
                asset_type=contract_payload.get("asset_type") or payload.get("asset_type"),
            ),
            contracts=payload.get("contracts"),
            price=payload.get("price"),
            currency=payload.get("currency"),
            source=payload.get("source") or payload.get("source_name"),
            multiplier=payload.get("multiplier", 100),
            fees=payload.get("fees", 0.0),
            target_lot_id=payload.get("target_lot_id"),
            target_event_id=payload.get("target_event_id"),
            lot_id=payload.get("lot_id"),
            raw_payload=raw_payload,
            asset_type=payload.get("asset_type") or contract_payload.get("asset_type"),
            quantity_unit=payload.get("quantity_unit") or contract_payload.get("quantity_unit"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "event_time_ms": self.event_time_ms,
            "contract_key": self.contract_key.to_dict(),
            # Preserve the historical JSON key; stock quantities use decimal text.
            "contracts": canonical_decimal_text(self.contracts) if self.asset_type == "stock" else self.contracts,
            "price": _canonical_decimal_text_or_raw(self.price),
            "currency": self.currency,
            "source": self.source,
            "multiplier": self.multiplier,
            "fees": _canonical_decimal_text_or_raw(self.fees),
            "target_lot_id": self.target_lot_id,
            "target_event_id": self.target_event_id,
            "lot_id": self.lot_id,
            "raw_payload": dict(self.raw_payload),
            "asset_type": self.asset_type,
            "quantity_unit": self.quantity_unit,
        }


def _canonical_decimal_text_or_raw(value: Decimal) -> str:
    """Serialize a decimal for the canonical payload without rejecting non-finite.

    Unvalidated events may still carry NaN/Infinity (validation is deferred to
    ``validate_trade_event``); round-tripping them through the codec requires a
    JSON-serializable text form instead of a ``ValueError``.
    """
    if not value.is_finite():
        return str(value)
    return canonical_decimal_text(value)


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
    if event.event_time_ms <= 0:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event.event_id,
                severity="error",
                code="event_time_must_be_positive",
                message="event_time_ms must be > 0",
                details={"event_time_ms": event.event_time_ms},
            )
        )
    if not event.source:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event.event_id,
                severity="error",
                code="event_source_required",
                message="source is required",
            )
        )
    if event.currency not in {"CNY", "HKD", "USD"}:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event.event_id,
                severity="error",
                code="event_currency_invalid",
                message="currency must be one of CNY, HKD, USD",
                details={"currency": event.currency},
            )
        )
    if event.asset_type != "stock" and (not math.isfinite(event.multiplier) or event.multiplier <= 0):
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event.event_id,
                severity="error",
                code="event_multiplier_invalid",
                message="multiplier must be finite and > 0",
                details={"multiplier": event.multiplier},
            )
        )
    if not event.price.is_finite() or event.price < 0:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event.event_id,
                severity="error",
                code="event_price_invalid",
                message="price must be finite and >= 0",
                details={"price": event.price},
            )
        )
    if not event.fees.is_finite():
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event.event_id,
                severity="error",
                code="event_fees_invalid",
                message="fees must be finite",
                details={"fees": event.fees},
            )
        )
    if event.event_type in OPEN_EVENT_TYPES | CLOSE_EVENT_TYPES and event.contracts <= 0:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event.event_id,
                severity="error",
                code="contracts_must_be_positive",
                message="contracts must be > 0",
                details={"contracts": str(event.contracts) if event.asset_type == "stock" else event.contracts},
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


def persisted_stock_settlement(raw: Any) -> dict[str, Any]:
    """Read historical settlement aliases without changing persisted event bytes."""
    if not isinstance(raw, Mapping):
        return {}
    result = dict(raw)
    for field, alias in (("side", "stock_side"), ("shares", "stock_qty"), ("price", "stock_price"), ("fees", "fee")):
        if result.get(field) in (None, "") and alias in result:
            result[field] = result[alias]
    return result
