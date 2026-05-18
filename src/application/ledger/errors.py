from __future__ import annotations

from typing import Any


class LedgerPreflightError(ValueError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = str(code or "ledger_preflight_failed")
        self.details = dict(details or {})
