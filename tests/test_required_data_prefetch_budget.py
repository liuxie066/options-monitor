from __future__ import annotations

from types import SimpleNamespace

import pytest

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


@pytest.mark.parametrize(
    "outcome",
    ["success_empty", "projection_empty", "provider_error", "parse_error"],
)
def test_typed_zero_call_outcomes_do_not_consume_or_split_budget(
    outcome: str,
) -> None:
    cfgs = [
        {"symbol": symbol, "fetch": {"source": "futu"}}
        for symbol in ("AAPL", "MSFT", "NVDA")
    ]
    plans = {
        id(cfg): SimpleNamespace(
            projection_outcome=outcome,
            side_plans=[],
            projected_expirations=[],
        )
        for cfg in cfgs
    }

    plan = build_prefetch_budget_plan(
        cfgs,
        option_chain_cfg={"max_calls": 2, "window_sec": 30.0},
        fetch_plans_by_config_id=plans,
    )

    assert plan.estimated_option_chain_calls == 0
    assert [wave.symbols for wave in plan.waves] == [
        ["AAPL", "MSFT", "NVDA"]
    ]
    assert plan.waves[0].estimated_option_chain_calls == 0
    assert plan.oversized_symbols == []


def test_success_rows_budget_uses_exact_frozen_targets() -> None:
    cfg = {"symbol": "NVDA", "fetch": {"source": "futu", "limit_expirations": 99}}
    fetch_plan = SimpleNamespace(
        projection_outcome="success_rows",
        side_plans=[],
        projected_expirations=["2026-08-21", "2026-09-18"],
    )

    assert estimate_prefetch_option_chain_calls(
        cfg,
        fetch_plan=fetch_plan,
    ) == 2


def test_success_rows_budget_rejects_missing_exact_targets() -> None:
    cfg = {"symbol": "NVDA", "fetch": {"source": "futu"}}
    fetch_plan = SimpleNamespace(
        projection_outcome="success_rows",
        side_plans=[],
        projected_expirations=[],
    )

    with pytest.raises(
        ValueError,
        match="lacks exact projected targets",
    ):
        estimate_prefetch_option_chain_calls(cfg, fetch_plan=fetch_plan)
