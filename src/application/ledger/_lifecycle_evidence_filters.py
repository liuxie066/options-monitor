"""Private shared filters for lifecycle-evidence repository queries."""
from __future__ import annotations

from typing import Any


def _append_lifecycle_evidence_filters(
    clauses: list[str],
    params: list[Any],
    *,
    case_id: str | None = None,
    account: str | None = None,
    symbol: str | None = None,
) -> None:
    if case_id:
        clauses.append("case_id = ?")
        params.append(str(case_id).strip())
    if account:
        clauses.append("account = ?")
        params.append(str(account).strip().lower())
    if symbol:
        clauses.append("symbol = ?")
        params.append(str(symbol).strip().upper())
