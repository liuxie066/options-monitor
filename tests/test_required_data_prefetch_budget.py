from __future__ import annotations

from src.application.required_data_prefetch_planning import (
    build_prefetch_budget_plan,
    estimate_prefetch_option_chain_calls,
)


def test_prefetch_budget_plan_splits_symbols_by_safe_option_chain_budget() -> None:
    cfgs = [
        {"symbol": "AAPL", "fetch": {"source": "futu", "limit_expirations": 4}},
        {"symbol": "MSFT", "fetch": {"source": "futu", "limit_expirations": 4}},
        {"symbol": "NVDA", "fetch": {"source": "futu", "limit_expirations": 4}},
    ]

    plan = build_prefetch_budget_plan(
        cfgs,
        option_chain_cfg={"max_calls": 10, "window_sec": 30.0},
    )

    assert plan.safe_option_chain_calls_per_window == 8
    assert plan.estimated_option_chain_calls == 12
    assert [wave.symbols for wave in plan.waves] == [["AAPL", "MSFT"], ["NVDA"]]
    assert [wave.estimated_option_chain_calls for wave in plan.waves] == [8, 4]
    assert plan.summary()["waves_count"] == 2


def test_prefetch_budget_plan_tracks_oversized_symbol_without_reordering() -> None:
    cfgs = [
        {"symbol": "AAPL", "fetch": {"source": "futu", "limit_expirations": 3}},
        {"symbol": "TSLA", "fetch": {"source": "futu", "limit_expirations": 9}},
        {"symbol": "MSFT", "fetch": {"source": "futu", "limit_expirations": 3}},
    ]

    plan = build_prefetch_budget_plan(
        cfgs,
        option_chain_cfg={"max_calls": 10, "window_sec": 30.0},
    )

    assert [wave.symbols for wave in plan.waves] == [["AAPL"], ["TSLA"], ["MSFT"]]
    assert plan.oversized_symbols == [
        {
            "symbol": "TSLA",
            "estimated_option_chain_calls": 9,
            "safe_option_chain_calls_per_window": 8,
        }
    ]


def test_prefetch_budget_estimate_ignores_non_futu_sources() -> None:
    assert estimate_prefetch_option_chain_calls({"symbol": "AAPL", "fetch": {"source": "yahoo"}}) == 0
