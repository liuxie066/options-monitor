from __future__ import annotations

from domain.domain.risk_capacity import (
    allocate_portfolio_capacity_shadow,
    compute_sell_call_share_capacity,
    compute_sell_put_cash_capacity,
    compute_sell_put_effective_cash,
    compute_short_call_locked_shares,
    compute_short_put_cash_secured,
)


def test_portfolio_capacity_shadow_allocates_in_existing_order_without_optimizer() -> None:
    rows = [
        {"account": "lx", "symbol": "NVDA", "strategy_family": "sell_put", "cash_free_cny": 10_000, "cash_required_cny": 6_000},
        {"account": "lx", "symbol": "NVDA", "strategy_family": "sell_put", "cash_free_cny": 10_000, "cash_required_cny": 5_000},
        {"account": "lx", "symbol": "PDD", "strategy_family": "sell_put", "cash_free_cny": 10_000, "cash_required_cny": 5_000},
        {"account": "lx", "symbol": "NVDA", "strategy_family": "covered_call", "shares_available_for_cover": 200, "multiplier": 100},
        {"account": "lx", "symbol": "NVDA", "strategy_family": "covered_call", "shares_available_for_cover": 200, "multiplier": 100},
    ]

    allocated = allocate_portfolio_capacity_shadow(rows)

    assert [row["allocation_status"] for row in allocated] == [
        "allocated",
        "alternative_not_allocated",
        "capacity_blocked",
        "allocated",
        "alternative_not_allocated",
    ]
    assert allocated[0]["capacity_after"] == 4_000
    assert allocated[2]["capacity_before"] == 4_000
    assert allocated[3]["capacity_after"] == 100
    assert "allocation_status" not in rows[0]


def test_portfolio_capacity_shadow_fails_closed_on_inconsistent_pool() -> None:
    allocated = allocate_portfolio_capacity_shadow(
        [
            {"account": "lx", "symbol": "NVDA", "strategy_family": "sell_put", "cash_free_cny": 10_000, "cash_required_cny": 5_000},
            {"account": "lx", "symbol": "PDD", "strategy_family": "sell_put", "cash_free_cny": 9_000, "cash_required_cny": 5_000},
        ]
    )

    assert {row["allocation_status"] for row in allocated} == {"not_evaluable"}
    assert {row["allocation_reason"] for row in allocated} == {"capacity_pool_missing_or_inconsistent"}


def test_portfolio_capacity_shadow_fails_closed_when_pool_is_missing() -> None:
    allocated = allocate_portfolio_capacity_shadow(
        [
            {
                "account": "lx",
                "symbol": "NVDA",
                "strategy_family": "sell_put",
                "cash_required_cny": 5_000,
            }
        ]
    )

    assert allocated[0]["allocation_status"] == "not_evaluable"
    assert allocated[0]["allocation_reason"] == "capacity_pool_missing_or_inconsistent"


def test_sell_put_cash_capacity_prefers_base_cny_over_total_cny() -> None:
    capacity = compute_sell_put_cash_capacity(
        cash_required_cny=20_000,
        cash_free_cny=15_000,
        cash_free_total_cny=50_000,
    )

    assert not capacity.accepted
    assert capacity.basis == "base_cny"
    assert capacity.reason == "base_cny_cash_insufficient"


def test_sell_put_cash_capacity_uses_total_cny_fallback() -> None:
    capacity = compute_sell_put_cash_capacity(
        cash_required_cny=20_000,
        cash_free_cny=None,
        cash_free_total_cny=50_000,
    )

    assert capacity.accepted
    assert capacity.basis == "total_cny"
    assert capacity.cash_free == 50_000


def test_sell_put_cash_capacity_fails_closed_when_basis_missing() -> None:
    capacity = compute_sell_put_cash_capacity(cash_required_cny=20_000)

    assert not capacity.accepted
    assert capacity.basis is None
    assert capacity.reason == "cash_basis_missing"


def test_sell_put_effective_cash_haircuts_only_positive_non_native_cash() -> None:
    rates = {("CNY", "USD"): 1.0 / 7.0}
    result = compute_sell_put_effective_cash(
        cash_by_currency={"USD": 10_000, "CNY": 70_000},
        cash_secured_by_currency={"USD": 2_000, "CNY": 7_000},
        native_currency="USD",
        convert_currency=lambda amount, source, target: amount * rates[(source, target)],
    )

    assert result.available
    assert result.cash_free == 8_000 + 9_000 * 0.95


def test_sell_put_effective_cash_keeps_native_cash_when_foreign_fx_is_missing() -> None:
    result = compute_sell_put_effective_cash(
        cash_by_currency={"USD": 20_000, "CNY": 70_000},
        cash_secured_by_currency={"USD": 2_000},
        native_currency="USD",
        convert_currency=lambda _amount, _source, _target: None,
    )

    assert result.available
    assert result.cash_free == 18_000
    assert result.reason == "native_cash_only_cross_currency_fx_unavailable:CNY"


def test_sell_put_effective_cash_marks_stale_fx_while_keeping_native_cash() -> None:
    result = compute_sell_put_effective_cash(
        cash_by_currency={"USD": 20_000, "CNY": 70_000},
        cash_secured_by_currency={"USD": 2_000},
        native_currency="USD",
        convert_currency=lambda _amount, _source, _target: None,
        fx_status="unavailable_stale",
    )

    assert result.available
    assert result.cash_free == 18_000
    assert result.reason == "native_cash_only_cross_currency_fx_stale:CNY"


def test_sell_call_share_capacity_uses_actual_multiplier() -> None:
    capacity = compute_sell_call_share_capacity(
        shares_total=600,
        shares_locked=100,
        multiplier=500,
    )

    assert capacity.accepted
    assert capacity.shares_available_for_cover == 500
    assert capacity.covered_contracts_available == 1
    assert capacity.is_fully_covered_available is True


def test_short_call_locked_shares_derives_from_multiplier() -> None:
    assert compute_short_call_locked_shares(contracts_open=2, multiplier=500) == 1000


def test_short_call_locked_shares_does_not_guess_default_multiplier() -> None:
    assert compute_short_call_locked_shares(contracts_open=2) is None


def test_short_call_locked_shares_scales_partial_close() -> None:
    locked = compute_short_call_locked_shares(
        contracts_open=1,
        contracts_total=4,
        underlying_share_locked=2000,
    )

    assert locked == 500


def test_short_call_locked_shares_zero_open_contracts_release_explicit_lock() -> None:
    assert compute_short_call_locked_shares(
        contracts_open=0,
        underlying_share_locked=2000,
    ) == 0


def test_short_put_cash_secured_derives_from_strike_multiplier() -> None:
    cash_secured = compute_short_put_cash_secured(
        contracts_open=1,
        strike=480,
        multiplier=500,
    )

    assert cash_secured == 240_000.0


def test_short_put_cash_secured_does_not_guess_missing_multiplier() -> None:
    assert compute_short_put_cash_secured(contracts_open=1, strike=480) is None


def test_short_put_cash_secured_scales_partial_close_after_deriving() -> None:
    cash_secured = compute_short_put_cash_secured(
        contracts_open=1,
        contracts_total=4,
        strike=100,
        multiplier=100,
    )

    assert cash_secured == 10_000.0


def test_short_put_cash_secured_zero_open_contracts_release_explicit_cash() -> None:
    assert compute_short_put_cash_secured(
        contracts_open=0,
        cash_secured_amount=40_000,
    ) == 0.0
