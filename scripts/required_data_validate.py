"""Required-data validation.

Validation policy:
- Validate at system boundaries (fetch output -> persisted required_data).
- Do NOT validate at every intermediate CSV read/write.

This module provides a small, reusable validator that:
- Drops bad rows (missing critical fields)
- Records counters for observability
- Never raises (best-effort)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ValidationStats:
    total_rows: int = 0
    kept_rows: int = 0
    dropped_rows: int = 0
    missing_strike: int = 0
    missing_expiration: int = 0
    missing_dte: int = 0
    missing_option_type: int = 0


def validate_required_rows(rows: list[dict]) -> tuple[list[dict], ValidationStats]:
    stats = ValidationStats(total_rows=len(rows or []))
    out: list[dict] = []
    for r in (rows or []):
        if not isinstance(r, dict):
            stats.dropped_rows += 1
            continue

        strike = r.get('strike')
        exp = r.get('expiration')
        dte = r.get('dte')
        opt_type = r.get('option_type')

        missing = False
        if strike is None:
            stats.missing_strike += 1
            missing = True
        if not exp:
            stats.missing_expiration += 1
            missing = True
        if dte is None:
            stats.missing_dte += 1
            missing = True
        if not opt_type:
            stats.missing_option_type += 1
            missing = True

        if missing:
            stats.dropped_rows += 1
            continue

        out.append(r)

    stats.kept_rows = len(out)
    return out, stats
