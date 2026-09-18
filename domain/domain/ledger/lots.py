from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from domain.domain.ledger.events import TradeEvent
from domain.domain.ledger.identity import ContractKey, position_key_for
from domain.domain.ledger.position_fields import PositionLotPatch, decode_position_lot_patch
from domain.domain.money import canonical_decimal_text, to_decimal
from domain.domain.option_position_identity import exp_ms_to_ymd, normalize_side
from domain.domain.trade_contract_identity import derive_position_side


@dataclass(frozen=True)
class PositionLot:
    lot_id: str
    open_event_id: str
    contract_key: ContractKey
    position_side: str
    opened_at_ms: int
    contracts_opened: int
    contracts_open: int
    contracts_closed: int
    status: str
    premium_open: Decimal
    multiplier: int
    currency: str
    realized_pnl: Decimal
    last_event_id: str
    close_event_ids: tuple[str, ...] = ()
    asset_type: str = "option"
    # §7.3/§7.4: stock quantity and authority-layer amounts are Decimal, not
    # float. ``shares_*`` are the stock counterpart of ``contracts_*``.
    shares_opened: Decimal | None = None
    shares_open: Decimal | None = None
    shares_closed: Decimal | None = None
    cost_basis_total: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "premium_open",
            to_decimal(self.premium_open or 0, field_name="premium_open"),
        )
        object.__setattr__(
            self,
            "realized_pnl",
            to_decimal(self.realized_pnl or 0, field_name="realized_pnl"),
        )
        for name in ("shares_opened", "shares_open", "shares_closed", "cost_basis_total"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, to_decimal(value, field_name=name))

    @property
    def position_key(self) -> str:
        """Aggregation/display key: contract identity + derived side (§9.2 step 3)."""
        return position_key_for(self.contract_key, self.position_side)

    @classmethod
    def from_open_event(cls, event: TradeEvent, *, lot_id: str) -> "PositionLot":
        position_side = derive_position_side("open", event.side) or ""
        if event.asset_type == "stock":
            quantity = to_decimal(event.contracts, field_name="contracts")
            return cls(
                lot_id=lot_id,
                open_event_id=event.event_id,
                contract_key=event.contract_key,
                position_side=position_side,
                opened_at_ms=event.event_time_ms,
                contracts_opened=0,
                contracts_open=0,
                contracts_closed=0,
                status="open",
                premium_open=Decimal("0"),
                multiplier=0,
                currency=event.currency,
                realized_pnl=Decimal("0"),
                last_event_id=event.event_id,
                close_event_ids=(),
                asset_type="stock",
                shares_opened=quantity,
                shares_open=quantity,
                shares_closed=Decimal("0"),
                cost_basis_total=to_decimal(event.price, field_name="price") * quantity,
            )
        return cls(
            lot_id=lot_id,
            open_event_id=event.event_id,
            contract_key=event.contract_key,
            position_side=position_side,
            opened_at_ms=event.event_time_ms,
            contracts_opened=int(event.contracts),
            contracts_open=int(event.contracts),
            contracts_closed=0,
            status="open",
            premium_open=event.price,
            multiplier=int(event.multiplier),
            currency=event.currency,
            realized_pnl=Decimal("0"),
            last_event_id=event.event_id,
            close_event_ids=(),
            asset_type=event.asset_type,
        )

    @classmethod
    def from_stock_settlement(
        cls,
        *,
        lot_id: str,
        open_event_id: str,
        broker: str,
        account: str,
        symbol: str,
        position_side: str,
        currency: str,
        opened_at_ms: int,
        shares_opened: Any,
        cost_basis_total: Any,
        status: str = "open",
        shares_open: Any = None,
        shares_closed: Any = None,
        realized_pnl: Any = Decimal("0"),
        last_event_id: str | None = None,
    ) -> "PositionLot":
        contract_key = ContractKey.from_values(
            broker=broker,
            account=account,
            underlying_symbol=symbol,
            option_type="",
            strike=0.0,
            expiration_ymd="",
            asset_type="stock",
        )
        opened = to_decimal(shares_opened, field_name="shares_opened")
        return cls(
            lot_id=lot_id,
            open_event_id=open_event_id,
            contract_key=contract_key,
            position_side=normalize_side(position_side, strict=True),
            opened_at_ms=int(opened_at_ms),
            contracts_opened=0,
            contracts_open=0,
            contracts_closed=0,
            status=str(status or "open").strip().lower(),
            premium_open=Decimal("0"),
            multiplier=0,
            currency=currency,
            realized_pnl=to_decimal(realized_pnl, field_name="realized_pnl"),
            last_event_id=last_event_id or open_event_id,
            close_event_ids=(),
            asset_type="stock",
            shares_opened=opened,
            shares_open=to_decimal(
                shares_open if shares_open is not None else opened,
                field_name="shares_open",
            ),
            shares_closed=to_decimal(shares_closed or 0, field_name="shares_closed"),
            cost_basis_total=to_decimal(
                cost_basis_total,
                field_name="cost_basis_total",
            ),
        )

    def apply_close(
        self,
        event: TradeEvent,
        *,
        actual_fee_amount: float,
        retain_close_event_ids: bool = True,
    ) -> "PositionLot":
        if lot_is_stock(self):
            quantity = to_decimal(event.contracts, field_name="contracts")
            next_open = (self.shares_open or Decimal("0")) - quantity
            next_closed = (self.shares_closed or Decimal("0")) + quantity
            return replace(
                self,
                shares_open=next_open,
                shares_closed=next_closed,
                status="close" if next_open <= 0 else "open",
                realized_pnl=self.realized_pnl
                + _stock_realized_pnl_delta(
                    self,
                    event,
                    actual_fee_amount=actual_fee_amount,
                ),
                last_event_id=event.event_id,
                close_event_ids=(
                    (*self.close_event_ids, event.event_id)
                    if retain_close_event_ids
                    else ()
                ),
            )
        next_open = int(self.contracts_open) - int(event.contracts)
        next_closed = int(self.contracts_closed) + int(event.contracts)
        return replace(
            self,
            contracts_open=next_open,
            contracts_closed=next_closed,
            status="close" if next_open == 0 else "open",
            realized_pnl=self.realized_pnl
            + _realized_pnl_delta(
                self,
                event,
                actual_fee_amount=actual_fee_amount,
            ),
            last_event_id=event.event_id,
            close_event_ids=(
                (*self.close_event_ids, event.event_id)
                if retain_close_event_ids
                else ()
            ),
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
                strike=_patch_decimal(patch, "strike", contract_key.strike),
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
            premium_open=_patch_decimal(patch, "premium", self.premium_open),
            multiplier=_patch_int(patch, "multiplier", self.multiplier),
            currency=str(_patch_value(patch, "currency", self.currency)).strip() or self.currency,
            last_event_id=event.event_id,
        )

    def mark_adjusted(self, event: TradeEvent) -> "PositionLot":
        return self.apply_adjust(event)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "lot_id": self.lot_id,
            "open_event_id": self.open_event_id,
            "contract_key": self.contract_key.to_dict(),
            "position_side": self.position_side,
            "position_key": self.position_key,
            "opened_at_ms": self.opened_at_ms,
            "contracts_opened": self.contracts_opened,
            "contracts_open": self.contracts_open,
            "contracts_closed": self.contracts_closed,
            "status": self.status,
            "premium_open": canonical_decimal_text(self.premium_open),
            "multiplier": self.multiplier,
            "currency": self.currency,
            "realized_pnl": canonical_decimal_text(self.realized_pnl),
            "last_event_id": self.last_event_id,
            "close_event_ids": list(self.close_event_ids),
            "asset_type": self.asset_type,
        }
        if self.asset_type == "stock":
            payload["shares_opened"] = _optional_decimal_text(self.shares_opened)
            payload["shares_open"] = _optional_decimal_text(self.shares_open)
            payload["shares_closed"] = _optional_decimal_text(self.shares_closed)
            payload["cost_basis_total"] = _optional_decimal_text(self.cost_basis_total)
        return payload


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else canonical_decimal_text(value)


def lot_is_stock(lot: PositionLot) -> bool:
    """Whether a lot tracks shares/cost-basis instead of contracts/premium."""
    return lot.asset_type == "stock"


def lot_open_quantity(lot: PositionLot) -> float | Decimal:
    """Open quantity in the lot's native unit (contracts or shares).

    §7.3: option quantity stays an exact int count of contracts; stock quantity
    is Decimal so fractional shares survive.
    """
    if lot_is_stock(lot):
        return lot.shares_open or Decimal("0")
    return float(lot.contracts_open)


def _realized_pnl_delta(
    lot: PositionLot,
    event: TradeEvent,
    *,
    actual_fee_amount: float,
) -> Decimal:
    contracts = int(event.contracts)
    multiplier = to_decimal(lot.multiplier, field_name="multiplier")
    if lot.position_side == "short":
        gross = (lot.premium_open - event.price) * contracts * multiplier
    else:
        gross = (event.price - lot.premium_open) * contracts * multiplier
    return gross - to_decimal(actual_fee_amount, field_name="actual_fee_amount")


def _stock_realized_pnl_delta(
    lot: PositionLot,
    event: TradeEvent,
    *,
    actual_fee_amount: float,
) -> Decimal:
    shares = to_decimal(event.contracts, field_name="contracts")
    shares_opened = lot.shares_opened or Decimal("0")
    cost_basis_total = lot.cost_basis_total or Decimal("0")
    cost_per_share = (
        cost_basis_total / shares_opened if shares_opened > 0 else Decimal("0")
    )
    proceeds = event.price * shares
    return (
        proceeds
        - cost_per_share * shares
        - to_decimal(actual_fee_amount, field_name="actual_fee_amount")
    )


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


def _patch_decimal(patch: PositionLotPatch, key: str, fallback: Decimal) -> Decimal:
    return to_decimal(_patch_value(patch, key, fallback), field_name=key)


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
