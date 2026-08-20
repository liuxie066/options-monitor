from __future__ import annotations

from domain.domain.risk_capacity import (
    allocate_opening_share_capacity,
    allocate_portfolio_capacity_shadow,
    compute_sell_call_share_capacity,
    compute_sell_put_cash_capacity,
    compute_sell_put_effective_cash,
    compute_short_call_locked_shares,
    compute_short_put_cash_secured,
)


def test_opening_share_capacity_prioritizes_wheel_and_grants_whole_contracts() -> None:
    allocations = allocate_opening_share_capacity(
        [
            {
                "account": "lx",
                "symbol": "NVDA",
                "status": "available",
                "shares_eligible": 300,
                "shares_locked": 100,
                "shares_reserved": 0,
            }
        ],
        [
            {
                "claim_id": "cc",
                "strategy_family": "covered_call",
                "account": "lx",
                "symbol": "NVDA",
                "requested_contracts": 2,
                "multiplier": 100,
            },
            {
                "claim_id": "wheel",
                "strategy_family": "wheel",
                "account": "lx",
                "symbol": "NVDA",
                "stock_lot_id": "stock-1",
                "assignment_at_ms": 1,
                "requested_contracts": 1,
                "multiplier": 100,
            },
        ],
    )

    assert allocations[0]["granted_contracts"] == 1
    assert allocations[0]["allocation_reason"] == "share_capacity_partially_supported"
    assert allocations[1]["granted_contracts"] == 1
    assert allocations[1]["capacity_before"] == 200


def test_opening_share_capacity_fails_closed_when_existing_coverage_is_excessive() -> None:
    result = allocate_opening_share_capacity(
        [
            {
                "account": "lx",
                "symbol": "NVDA",
                "status": "available",
                "shares_eligible": 100,
                "shares_locked": 200,
                "shares_reserved": 0,
            }
        ],
        [
            {
                "claim_id": "wheel",
                "strategy_family": "wheel",
                "account": "lx",
                "symbol": "NVDA",
                "requested_contracts": 1,
                "multiplier": 100,
            }
        ],
    )[0]

    assert result["granted_contracts"] == 0
    assert result["allocation_reason"] == "share_capacity_oversubscribed"
    assert result["risk_level"] == "high"


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


def test_sell_put_effective_cash_uses_positive_foreign_cash_without_haircut() -> None:
    rates = {("CNY", "USD"): 1.0 / 7.0}
    result = compute_sell_put_effective_cash(
        cash_by_currency={"USD": 10_000, "CNY": 70_000},
        cash_secured_by_currency={"USD": 2_000, "CNY": 7_000},
        native_currency="USD",
        convert_currency=lambda amount, source, target: amount * rates[(source, target)],
    )

    assert result.available
    assert result.cash_free == 17_000
    assert result.reason == "cash_supported_by_same_currency_then_fx"


def test_sell_put_effective_cash_keeps_native_cash_when_foreign_fx_is_missing() -> None:
    result = compute_sell_put_effective_cash(
        cash_by_currency={"USD": 20_000, "CNY": 70_000},
        cash_secured_by_currency={"USD": 2_000},
        native_currency="USD",
        convert_currency=lambda _amount, _source, _target: None,
    )

    assert result.available
    assert result.cash_free == 18_000
    assert result.reason == "known_cash_only_cross_currency_fx_unavailable:CNY"


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
    assert result.reason == "known_cash_only_cross_currency_fx_stale:CNY"


def test_sell_put_effective_cash_blocks_when_missing_fx_is_needed() -> None:
    result = compute_sell_put_effective_cash(
        cash_by_currency={"USD": 20_000, "CNY": 70_000},
        cash_secured_by_currency={"USD": 2_000},
        native_currency="USD",
        cash_required_native=20_000,
        convert_currency=lambda _amount, _source, _target: None,
    )

    assert not result.available
    assert result.cash_free is None
    assert result.reason == "cross_currency_cash_fx_unavailable:CNY"


def test_sell_put_effective_cash_keeps_native_pool_when_stale_fx_is_not_needed() -> None:
    result = compute_sell_put_effective_cash(
        cash_by_currency={"USD": 20_000, "CNY": 70_000},
        cash_secured_by_currency={"USD": 2_000},
        native_currency="USD",
        cash_required_native=10_000,
        convert_currency=lambda _amount, _source, _target: None,
        fx_status="unavailable_stale",
    )

    assert result.available
    assert result.cash_free == 18_000
    assert result.reason == "known_cash_only_cross_currency_fx_stale:CNY"


def test_sell_put_effective_cash_deducts_foreign_short_put_deficit() -> None:
    rates = {("HKD", "USD"): 1.0 / 7.8}
    result = compute_sell_put_effective_cash(
        cash_by_currency={"USD": 20_000, "HKD": 1_000},
        cash_secured_by_currency={"HKD": 8_800},
        native_currency="USD",
        cash_required_native=10_000,
        convert_currency=lambda amount, source, target: amount * rates[(source, target)],
    )

    assert result.available
    assert result.cash_free == 19_000


def test_sell_put_effective_cash_fails_when_foreign_lock_fx_is_missing() -> None:
    result = compute_sell_put_effective_cash(
        cash_by_currency={"USD": 20_000, "HKD": 1_000},
        cash_secured_by_currency={"HKD": 8_800},
        native_currency="USD",
        cash_required_native=10_000,
        convert_currency=lambda _amount, _source, _target: None,
    )

    assert not result.available
    assert result.cash_free is None
    assert result.reason == "cross_currency_secured_cash_fx_unavailable:HKD->USD"


def test_sell_put_capacity_uses_same_currency_then_fx_algorithm_for_us_and_hk() -> None:
    scenarios = (
        (
            "USD",
            {"USD": 10_000, "CNY": 70_000},
            lambda amount, source, target: (
                amount / 7.0 if (source, target) == ("CNY", "USD") else None
            ),
        ),
        (
            "HKD",
            {"HKD": 10_000, "CNY": 9_200},
            lambda amount, source, target: (
                amount / 0.92 if (source, target) == ("CNY", "HKD") else None
            ),
        ),
    )

    for native_currency, cash, converter in scenarios:
        effective = compute_sell_put_effective_cash(
            cash_by_currency=cash,
            cash_secured_by_currency={},
            native_currency=native_currency,
            cash_required_native=10_000,
            convert_currency=converter,
        )
        capacity = compute_sell_put_cash_capacity(
            cash_required_native=10_000,
            cash_free_effective_native=effective.cash_free,
            cash_native_currency=native_currency,
        )
        assert effective.available
        assert effective.cash_free == 20_000
        assert capacity.max_new_contracts == 2


def test_sell_call_share_capacity_uses_actual_multiplier() -> None:
    capacity = compute_sell_call_share_capacity(
        shares_total=600,
        shares_can_sell=600,
        shares_locked=100,
        multiplier=500,
    )

    assert capacity.accepted
    assert capacity.shares_available_for_cover == 500
    assert capacity.covered_contracts_available == 1
    assert capacity.is_fully_covered_available is True


def test_sell_call_share_capacity_uses_lower_of_total_and_can_sell() -> None:
    capacity = compute_sell_call_share_capacity(
        shares_total=600,
        shares_can_sell=520,
        shares_locked=20,
        multiplier=500,
    )

    assert capacity.accepted
    assert capacity.shares_eligible == 520
    assert capacity.shares_available_for_cover == 500
    assert capacity.covered_contracts_available == 1


def test_sell_call_share_capacity_fails_when_lock_exceeds_eligible_shares() -> None:
    capacity = compute_sell_call_share_capacity(
        shares_total=600,
        shares_can_sell=100,
        shares_locked=200,
        multiplier=100,
    )

    assert not capacity.accepted
    assert capacity.reason == "locked_shares_exceed_eligible_underlying"


def test_sell_call_share_capacity_requires_integral_multiplier() -> None:
    capacity = compute_sell_call_share_capacity(
        shares_total=600,
        shares_can_sell=600,
        multiplier=99.5,
    )

    assert not capacity.accepted
    assert capacity.reason == "invalid_multiplier"


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
