from __future__ import annotations

from typing import Any

from domain.domain.option_position_identity import normalize_account, normalize_broker
from domain.domain.performance.engine import build_period_performance
from domain.domain.performance.period import PeriodRequest, PeriodWindow, normalize_period
from src.application.performance.adapters import load_ledger_performance_inputs


def build_option_period_performance(
    repo: Any,
    *,
    period: PeriodWindow | PeriodRequest | dict[str, Any],
    account: str | None = None,
    broker: str | None = None,
    now_ms: int | None = None,
    include_rows: bool = True,
) -> dict[str, Any]:
    window = period if isinstance(period, PeriodWindow) else normalize_period(period, now_ms=now_ms)
    inputs = load_ledger_performance_inputs(repo)
    result = build_period_performance(
        events=inputs.events,
        allocations=inputs.allocations,
        period=window,
        account=normalize_account(account) if account else None,
        broker=normalize_broker(broker) if broker else None,
        diagnostics=inputs.diagnostics,
    )
    return result.to_dict(include_rows=include_rows)


__all__ = ["build_option_period_performance"]
