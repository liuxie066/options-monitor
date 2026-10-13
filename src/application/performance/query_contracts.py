"""Capability errors; public adapters retain their own exception envelopes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PerformanceQueryError(Exception):
    code: str
    message: str
    hint: str | None = None
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        result = {"code": self.code, "message": self.message}
        if self.hint:
            result["hint"] = self.hint
        if self.details:
            result["details"] = self.details
        return result
