from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PositionLotRecord:
    lot_id: str
    fields: dict[str, Any]

    def __post_init__(self) -> None:
        lot_id = str(self.lot_id or "").strip()
        if not lot_id:
            raise ValueError("position lot lot_id is required")
        if not isinstance(self.fields, dict):
            raise TypeError("position lot fields must be a dict")
        object.__setattr__(self, "lot_id", lot_id)
        object.__setattr__(self, "fields", dict(self.fields))

    def to_dict(self) -> dict[str, Any]:
        return {
            "lot_id": self.lot_id,
            "fields": dict(self.fields),
        }

    def with_fields(self, fields: dict[str, Any]) -> "PositionLotRecord":
        return PositionLotRecord(lot_id=self.lot_id, fields=fields)


__all__ = [
    "PositionLotRecord",
]
