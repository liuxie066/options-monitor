from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from domain.domain.ledger.events import TradeEvent
from domain.domain.ledger.fees import FeeBasis, FeeComponent, FeeFact
from domain.domain.ledger.identity import ContractKey
from domain.domain.ledger.lots import PositionLot
from domain.domain.ledger.position_fields import (
    strategy_metadata_fields_from_payload,
)
from domain.domain.money import quantize_money, to_decimal


def _missing_fee_fact(
    source_event_id: str,
    *,
    component: FeeComponent,
    reason: str,
    source: Any = None,
) -> FeeFact:
    return FeeFact(
        amount=None,
        basis=FeeBasis.MISSING,
        component=component,
        source_event_id=source_event_id,
        source=source,
        reason=reason,
    )


def fee_component_for_event(event: TradeEvent) -> FeeComponent:
    if event.event_type == "open":
        return FeeComponent.OPTION_OPEN
    if event.event_type in {"assignment", "exercise"}:
        return FeeComponent.ASSIGNMENT_OPTION
    return FeeComponent.OPTION_CLOSE


def fee_fact_from_persisted_evidence(
    *,
    event_id: str,
    component: FeeComponent,
    provenance: Any,
    compatibility_amount: Any,
) -> FeeFact:
    """Resolve persisted fee evidence without calling a fee formula."""

    source_event_id = str(event_id or "").strip()
    if isinstance(provenance, dict):
        try:
            compatibility_fee = quantize_money(
                to_decimal(
                    0 if compatibility_amount in (None, "") else compatibility_amount,
                    field_name="fees",
                )
            )
        except (TypeError, ValueError) as exc:
            return _missing_fee_fact(
                source_event_id,
                component=component,
                source=provenance.get("source"),
                reason=f"invalid compatibility fee amount: {exc}",
            )
        basis_raw = str(provenance.get("basis") or "").strip().lower()
        if basis_raw not in {item.value for item in FeeBasis}:
            return _missing_fee_fact(
                source_event_id,
                component=component,
                source=provenance.get("source"),
                reason=f"invalid fee provenance basis: {basis_raw or '<empty>'}",
            )
        basis = FeeBasis(basis_raw)
        if basis == FeeBasis.MISSING:
            if compatibility_fee != 0:
                return _missing_fee_fact(
                    source_event_id,
                    component=component,
                    source=provenance.get("source"),
                    reason="fee provenance basis conflicts with non-zero compatibility amount",
                )
            return FeeFact(
                amount=None,
                basis=basis,
                component=component,
                source_event_id=source_event_id,
                source=provenance.get("source"),
                reason=provenance.get("reason") or "fee provenance explicitly missing",
            )
        amount_raw = provenance.get("amount")
        if amount_raw in (None, "") and basis == FeeBasis.ACTUAL:
            amount_raw = compatibility_fee
        if amount_raw in (None, ""):
            return _missing_fee_fact(
                source_event_id,
                component=component,
                source=provenance.get("source"),
                reason=f"{basis.value} fee provenance amount is missing",
            )
        try:
            amount = quantize_money(to_decimal(amount_raw, field_name="fee provenance amount"))
            if basis == FeeBasis.ACTUAL and amount != compatibility_fee:
                return _missing_fee_fact(
                    source_event_id,
                    component=component,
                    source=provenance.get("source"),
                    reason="fee provenance amount conflicts with compatibility amount",
                )
            if basis == FeeBasis.ESTIMATED and compatibility_fee != 0:
                return _missing_fee_fact(
                    source_event_id,
                    component=component,
                    source=provenance.get("source"),
                    reason="fee provenance basis conflicts with non-zero compatibility amount",
                )
            return FeeFact(
                amount=amount,
                basis=basis,
                component=component,
                source_event_id=source_event_id,
                source=provenance.get("source"),
                reason=provenance.get("reason"),
            )
        except (TypeError, ValueError) as exc:
            return _missing_fee_fact(
                source_event_id,
                component=component,
                source=provenance.get("source"),
                reason=f"invalid fee provenance amount: {exc}",
            )

    try:
        numeric_fee = to_decimal(compatibility_amount, field_name="fees")
    except (TypeError, ValueError) as exc:
        return _missing_fee_fact(
            source_event_id,
            component=component,
            reason=f"invalid legacy fee amount: {exc}",
        )
    if numeric_fee != 0:
        try:
            return FeeFact(
                amount=numeric_fee,
                basis=FeeBasis.ACTUAL,
                component=component,
                source_event_id=source_event_id,
                source="legacy_nonzero_fees",
                reason="non-zero canonical fee predates explicit provenance",
            )
        except ValueError as exc:
            return _missing_fee_fact(
                source_event_id,
                component=component,
                source="legacy_nonzero_fees",
                reason=f"invalid legacy fee amount: {exc}",
            )
    return _missing_fee_fact(
        source_event_id,
        component=component,
        reason="zero/absent fee has no provenance",
    )


def fee_fact_for_event(event: TradeEvent) -> FeeFact:
    payload = event.raw_payload if isinstance(event.raw_payload, dict) else {}
    return fee_fact_from_persisted_evidence(
        event_id=event.event_id,
        component=fee_component_for_event(event),
        provenance=payload.get("fee_provenance"),
        compatibility_amount=event.fees,
    )


def _allocated_fee_fact(total: FeeFact, *, amount: Decimal | None) -> FeeFact:
    return FeeFact(
        amount=amount,
        basis=total.basis if amount is not None else FeeBasis.MISSING,
        component=FeeComponent.OPTION_OPEN,
        source_event_id=total.source_event_id,
        source=total.source,
        reason=total.reason,
    )


def _allocation_id(open_event_id: str, close_event_id: str, sequence: int) -> str:
    seed = f"{open_event_id}\x1f{close_event_id}\x1f{int(sequence)}".encode()
    return f"alloc_{hashlib.sha256(seed).hexdigest()[:24]}"


def _strategy_value(open_event: TradeEvent, close_event: TradeEvent, key: str) -> str | None:
    for event in (close_event, open_event):
        payload = event.raw_payload if isinstance(event.raw_payload, dict) else {}
        value = strategy_metadata_fields_from_payload(payload).get(key)
        raw = str(value or "").strip()
        if raw:
            return raw
    return None


def _settlement_ref(event: TradeEvent) -> str | None:
    payload = event.raw_payload if isinstance(event.raw_payload, dict) else {}
    for key in ("settlement_ref", "assignment_ref", "exercise_ref", "lifecycle_case_id"):
        raw = str(payload.get(key) or "").strip()
        if raw:
            return raw
    return None


def _fee_quality(open_fee: FeeFact, close_fee: FeeFact) -> str:
    bases = {open_fee.basis, close_fee.basis}
    if FeeBasis.MISSING in bases:
        return FeeBasis.MISSING.value
    if FeeBasis.ESTIMATED in bases:
        return FeeBasis.ESTIMATED.value
    return FeeBasis.ACTUAL.value


@dataclass(frozen=True)
class OptionEconomicAllocation:
    allocation_id: str
    contract_key: ContractKey
    open_event_id: str
    close_event_id: str
    target_lot_id: str
    opened_at_ms: int
    closed_at_ms: int
    contracts: int
    multiplier: Decimal
    currency: str
    position_side: str
    close_type: str
    open_price: Decimal
    close_price: Decimal
    open_amount_gross: Decimal
    close_amount_gross: Decimal
    realized_pnl_gross: Decimal
    allocated_open_fee: FeeFact
    close_fee: FeeFact
    realized_pnl_net: Decimal | None
    fee_quality: str
    strategy: str | None = None
    leg_role: str | None = None
    strategy_group_id: str | None = None
    settlement_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        def amount(value: Decimal | None) -> float | None:
            return None if value is None else float(value)

        return {
            "allocation_id": self.allocation_id,
            "contract_key": self.contract_key.to_dict(),
            "open_event_id": self.open_event_id,
            "close_event_id": self.close_event_id,
            "target_lot_id": self.target_lot_id,
            "opened_at_ms": self.opened_at_ms,
            "closed_at_ms": self.closed_at_ms,
            "contracts": self.contracts,
            "multiplier": float(self.multiplier),
            "currency": self.currency,
            "position_side": self.position_side,
            "close_type": self.close_type,
            "open_price": float(self.open_price),
            "close_price": float(self.close_price),
            "open_amount_gross": amount(self.open_amount_gross),
            "close_amount_gross": amount(self.close_amount_gross),
            "realized_pnl_gross": amount(self.realized_pnl_gross),
            "allocated_open_fee": self.allocated_open_fee.to_dict(),
            "close_fee": self.close_fee.to_dict(),
            "realized_pnl_net": amount(self.realized_pnl_net),
            "fee_quality": self.fee_quality,
            "strategy": self.strategy,
            "leg_role": self.leg_role,
            "strategy_group_id": self.strategy_group_id,
            "settlement_ref": self.settlement_ref,
        }


def build_option_economic_allocation(
    *,
    lot: PositionLot,
    open_event: TradeEvent,
    close_event: TradeEvent,
    sequence: int,
    open_fee_allocated_before: Decimal,
) -> tuple[OptionEconomicAllocation, Decimal]:
    contracts = int(close_event.contracts)
    multiplier = to_decimal(lot.multiplier, field_name="multiplier")
    open_price = to_decimal(lot.premium_open, field_name="open_price")
    close_price = to_decimal(close_event.price, field_name="close_price")
    quantity = Decimal(contracts)
    gross_open_abs = quantize_money(open_price * multiplier * quantity)
    gross_close_abs = quantize_money(close_price * multiplier * quantity)
    if lot.contract_key.position_side == "short":
        open_amount = gross_open_abs
        close_amount = -gross_close_abs
    else:
        open_amount = -gross_open_abs
        close_amount = gross_close_abs
    realized_gross = quantize_money(open_amount + close_amount)

    allocated_open_fee, allocated_after = allocate_open_fee_for_close(
        lot=lot,
        open_event=open_event,
        close_contracts=contracts,
        open_fee_allocated_before=open_fee_allocated_before,
    )

    close_fee = fee_fact_for_event(close_event)
    if allocated_open_fee.basis != FeeBasis.ACTUAL or close_fee.basis != FeeBasis.ACTUAL:
        realized_net = None
    else:
        assert allocated_open_fee.amount is not None
        assert close_fee.amount is not None
        realized_net = quantize_money(realized_gross - allocated_open_fee.amount - close_fee.amount)

    payload = close_event.raw_payload if isinstance(close_event.raw_payload, dict) else {}
    close_type = str(payload.get("close_type") or close_event.event_type).strip().lower()
    return (
        OptionEconomicAllocation(
            allocation_id=_allocation_id(open_event.event_id, close_event.event_id, sequence),
            contract_key=lot.contract_key,
            open_event_id=open_event.event_id,
            close_event_id=close_event.event_id,
            target_lot_id=lot.lot_id,
            opened_at_ms=int(lot.opened_at_ms),
            closed_at_ms=int(close_event.event_time_ms),
            contracts=contracts,
            multiplier=multiplier,
            currency=lot.currency,
            position_side=lot.contract_key.position_side,
            close_type=close_type,
            open_price=open_price,
            close_price=close_price,
            open_amount_gross=open_amount,
            close_amount_gross=close_amount,
            realized_pnl_gross=realized_gross,
            allocated_open_fee=allocated_open_fee,
            close_fee=close_fee,
            realized_pnl_net=realized_net,
            fee_quality=_fee_quality(allocated_open_fee, close_fee),
            strategy=_strategy_value(open_event, close_event, "strategy"),
            leg_role=_strategy_value(open_event, close_event, "leg_role"),
            strategy_group_id=_strategy_value(open_event, close_event, "strategy_group_id"),
            settlement_ref=_settlement_ref(close_event),
        ),
        allocated_after,
    )


def allocate_open_fee_for_close(
    *,
    lot: PositionLot,
    open_event: TradeEvent,
    close_contracts: int,
    open_fee_allocated_before: Decimal,
) -> tuple[FeeFact, Decimal]:
    contracts = int(close_contracts)
    total_open_fee = fee_fact_for_event(open_event)
    allocated_after = open_fee_allocated_before
    if total_open_fee.amount is None:
        return _allocated_fee_fact(total_open_fee, amount=None), allocated_after

    total_amount = total_open_fee.amount
    is_final_close = contracts == int(lot.contracts_open)
    if is_final_close:
        allocated_amount = quantize_money(total_amount - open_fee_allocated_before)
    else:
        allocated_amount = quantize_money(total_amount * Decimal(contracts) / Decimal(int(lot.contracts_opened)))
    if allocated_amount < 0:
        raise ValueError("allocated open fee cannot be negative")
    allocated_after = quantize_money(open_fee_allocated_before + allocated_amount)
    return _allocated_fee_fact(total_open_fee, amount=allocated_amount), allocated_after


__all__ = [
    "OptionEconomicAllocation",
    "allocate_open_fee_for_close",
    "build_option_economic_allocation",
    "fee_component_for_event",
    "fee_fact_for_event",
    "fee_fact_from_persisted_evidence",
]
