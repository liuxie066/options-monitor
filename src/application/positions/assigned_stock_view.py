from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from src.application.performance.adapters import (
    load_assigned_stock_projection,
    load_ledger_performance_inputs,
)


def build_assigned_stock_view(
    repo: Any,
    *,
    account: str | None = None,
    broker: str | None = None,
    quote_snapshots: Any = None,
    assigned_stock_events: list[dict[str, Any]] | None = None,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    """Project assigned-stock state through the canonical performance adapters."""

    instant = int(as_of_ms) if as_of_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    inputs = load_ledger_performance_inputs(repo)
    if assigned_stock_events is not None:
        inputs = replace(
            inputs,
            assigned_stock_events=tuple(
                dict(item) for item in assigned_stock_events if isinstance(item, dict)
            ),
        )
    report = load_assigned_stock_projection(
        inputs,
        as_of_ms=instant,
        quote_snapshots=quote_snapshots,
        account=account,
        broker=broker,
    )
    diagnostics = [dict(item) for item in inputs.diagnostics if isinstance(item, dict)]
    if diagnostics:
        report["input_diagnostics"] = diagnostics
        report["warnings"] = [
            *[str(item) for item in report.get("warnings") or [] if str(item).strip()],
            *[
                str(item.get("message") or item.get("code") or "assigned-stock input warning")
                for item in diagnostics
            ],
        ]
    return report


__all__ = ["build_assigned_stock_view"]
