from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from domain.domain.money import quantize_money


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


__all__ = ["FeeBasis", "FeeComponent", "FeeFact"]
