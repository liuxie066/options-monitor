from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PositionLotRecord:
    record_id: str
    fields: dict[str, Any]

    def __post_init__(self) -> None:
        record_id = str(self.record_id or "").strip()
        if not record_id:
            raise ValueError("position lot record_id is required")
        if not isinstance(self.fields, dict):
            raise TypeError("position lot fields must be a dict")
        object.__setattr__(self, "record_id", record_id)
        object.__setattr__(self, "fields", dict(self.fields))

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "fields": dict(self.fields),
        }

    def with_fields(self, fields: dict[str, Any]) -> "PositionLotRecord":
        return PositionLotRecord(record_id=self.record_id, fields=fields)


__all__ = [
    "PositionLotRecord",
]
