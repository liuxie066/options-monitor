from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from domain.domain.ledger.events import TradeEvent
from domain.domain.ledger.identity import ContractKey
from domain.domain.ledger.position_fields import PositionLotPatch, decode_position_lot_patch
from domain.domain.option_position_identity import exp_ms_to_ymd


@dataclass(frozen=True)
class PositionLot:
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
    close_event_ids: tuple[str, ...] = ()

    @classmethod
    def from_open_event(cls, event: TradeEvent, *, lot_id: str) -> "PositionLot":
        return cls(
            lot_id=lot_id,
            open_event_id=event.event_id,
            contract_key=event.contract_key,
            opened_at_ms=event.event_time_ms,
            contracts_opened=int(event.contracts),
            contracts_open=int(event.contracts),
            contracts_closed=0,
            status="open",
            premium_open=float(event.price),
            multiplier=float(event.multiplier),
            currency=event.currency,
            realized_pnl=0.0,
            last_event_id=event.event_id,
            close_event_ids=(),
        )

    def apply_close(self, event: TradeEvent) -> "PositionLot":
        next_open = int(self.contracts_open) - int(event.contracts)
        next_closed = int(self.contracts_closed) + int(event.contracts)
        return replace(
            self,
            contracts_open=next_open,
            contracts_closed=next_closed,
            status="close" if next_open == 0 else "open",
            realized_pnl=self.realized_pnl + _realized_pnl_delta(self, event),
            last_event_id=event.event_id,
            close_event_ids=(*self.close_event_ids, event.event_id),
        )

    def apply_adjust(self, event: TradeEvent) -> "PositionLot":
        patch = decode_position_lot_patch(event.raw_payload.get("patch"))

        contract_key = self.contract_key
        if patch.has("strike") or patch.has("expiration") or patch.has("expiration_ymd"):
            contract_key = ContractKey.from_values(
                broker=contract_key.broker,
                account=contract_key.account,
                underlying_symbol=contract_key.underlying_symbol,
                option_type=contract_key.option_type,
                position_side=contract_key.position_side,
                strike=_patch_float(patch, "strike", contract_key.strike),
                expiration_ymd=_patch_expiration_ymd(patch, fallback=contract_key.expiration_ymd),
            )

        contracts_opened = _patch_int(patch, "contracts", self.contracts_opened)
        contracts_closed = _patch_int(patch, "contracts_closed", self.contracts_closed)
        contracts_open = _patch_int(patch, "contracts_open", max(0, contracts_opened - contracts_closed))
        if contracts_open < 0:
            raise ValueError("adjust patch contracts_open must be >= 0")
        if contracts_closed < 0:
            raise ValueError("adjust patch contracts_closed must be >= 0")
        if contracts_closed > contracts_opened:
            raise ValueError("adjust patch contracts_closed must be <= contracts")
        if contracts_open + contracts_closed != contracts_opened:
            raise ValueError("adjust patch contracts_open + contracts_closed must equal contracts")

        return replace(
            self,
            contract_key=contract_key,
            opened_at_ms=_patch_int(patch, "opened_at", self.opened_at_ms),
            contracts_opened=contracts_opened,
            contracts_open=contracts_open,
            contracts_closed=contracts_closed,
            status=str(_patch_value(patch, "status", "close" if contracts_open == 0 else "open")).strip().lower(),
            premium_open=_patch_float(patch, "premium", self.premium_open),
            multiplier=_patch_float(patch, "multiplier", self.multiplier),
            currency=str(_patch_value(patch, "currency", self.currency)).strip() or self.currency,
            last_event_id=event.event_id,
        )

    def mark_adjusted(self, event: TradeEvent) -> "PositionLot":
        return self.apply_adjust(event)

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
            "close_event_ids": list(self.close_event_ids),
        }


def _realized_pnl_delta(lot: PositionLot, event: TradeEvent) -> float:
    contracts = int(event.contracts)
    multiplier = float(lot.multiplier)
    if lot.contract_key.position_side == "short":
        gross = (float(lot.premium_open) - float(event.price)) * contracts * multiplier
    else:
        gross = (float(event.price) - float(lot.premium_open)) * contracts * multiplier
    return gross - float(event.fees or 0.0)


def _patch_value(patch: PositionLotPatch, key: str, fallback: Any) -> Any:
    if not patch.has(key):
        return fallback
    value = patch.value(key)
    if value in (None, ""):
        return fallback
    return value


def _patch_int(patch: PositionLotPatch, key: str, fallback: int) -> int:
    value = _patch_value(patch, key, fallback)
    if value in (None, ""):
        return int(fallback)
    return int(float(value))


def _patch_float(patch: PositionLotPatch, key: str, fallback: float) -> float:
    value = _patch_value(patch, key, fallback)
    if value in (None, ""):
        return float(fallback)
    return float(value)


def _patch_expiration_ymd(patch: PositionLotPatch, *, fallback: str) -> str:
    raw_ymd = str(_patch_value(patch, "expiration_ymd", "") or "").strip()
    if raw_ymd:
        return raw_ymd
    if not patch.has("expiration") or patch.value("expiration") in (None, ""):
        return fallback
    ymd = exp_ms_to_ymd(patch.value("expiration"))
    if not ymd:
        raise ValueError("adjust patch expiration must resolve to YYYY-MM-DD")
    return ymd
