from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class _FakeRunlog:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def safe_event(self, step: str, status: str, **kwargs) -> None:
        event = {"step": step, "status": status}
        event.update(kwargs)
        self.events.append(event)


def test_resolve_market_run_reports_trading_day_block_without_exiting() -> None:
    from src.application.multi_tick_scheduler import resolve_market_run

    runlog = _FakeRunlog()

    out = resolve_market_run(
        now_utc=datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc),
        base_cfg={"symbols": [{"market": "US"}]},
        market_config="auto",
        force_mode=False,
        runlog=runlog,
        safe_data_fn=lambda data: data,
        domain_select_markets_to_run=lambda *_args: [],
        domain_markets_for_trading_day_guard=lambda *_args: ["US"],
        decide_trading_day_guard=lambda **_kwargs: {
            "guard_results": [{"market": "US", "is_trading_day": False}],
            "markets_to_run": [],
            "should_skip": True,
            "skip_message": "non-trading day: US",
        },
        reduce_trading_day_guard=lambda **_kwargs: {},
        check_trading_day_for_market=lambda market: (False, market),
    )

    assert out.trading_day_blocked is True
    assert out.markets_to_run == []
    assert out.scheduler_markets == ["US"]
    assert out.skip_message == "non-trading day: US"
    assert any(evt["step"] == "trading_day_guard" and evt["status"] == "skip" for evt in runlog.events)
