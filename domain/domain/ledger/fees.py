from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from domain.domain.ledger.events import TradeEvent
from domain.domain.money import quantize_money, to_decimal


class FeeBasis(str, Enum):
    ACTUAL = "actual"
    ESTIMATED = "estimated"
    MISSING = "missing"


class FeeComponent(str, Enum):
    OPTION_OPEN = "option_open"
    OPTION_CLOSE = "option_close"
    ASSIGNMENT_OPTION = "assignment_option"
    STOCK_SETTLEMENT = "stock_settlement"
    STOCK_SALE = "stock_sale"


@dataclass(frozen=True)
class FeeFact:
    amount: Decimal | None
    basis: FeeBasis
    component: FeeComponent
    source_event_id: str
    source: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        basis = FeeBasis(self.basis)
        component = FeeComponent(self.component)
        source_event_id = str(self.source_event_id or "").strip()
        if not source_event_id:
            raise ValueError("source_event_id is required")
        if basis == FeeBasis.MISSING:
            if self.amount is not None:
                raise ValueError("missing fee must not have an amount")
            amount = None
        else:
            amount = quantize_money(self.amount)
            if amount < 0:
                raise ValueError("fee amount cannot be negative")
        object.__setattr__(self, "basis", basis)
        object.__setattr__(self, "component", component)
        object.__setattr__(self, "source_event_id", source_event_id)
        object.__setattr__(self, "amount", amount)
        object.__setattr__(
            self,
            "source",
            str(self.source).strip() if self.source not in (None, "") else None,
        )
        object.__setattr__(
            self,
            "reason",
            str(self.reason).strip() if self.reason not in (None, "") else None,
        )

    @property
    def is_complete(self) -> bool:
        return self.basis in {FeeBasis.ACTUAL, FeeBasis.ESTIMATED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "amount": None if self.amount is None else float(self.amount),
            "basis": self.basis.value,
            "component": self.component.value,
            "source": self.source,
            "reason": self.reason,
            "source_event_id": self.source_event_id,
        }


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


__all__ = ["FeeBasis", "FeeComponent", "FeeFact", "fee_component_for_event",
           "fee_fact_for_event", "fee_fact_from_persisted_evidence"]
