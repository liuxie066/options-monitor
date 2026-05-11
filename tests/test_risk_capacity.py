from __future__ import annotations

from domain.domain.risk_capacity import (
    compute_covered_call_share_capacity,
    compute_sell_put_cash_capacity,
    compute_short_call_locked_shares,
    compute_short_put_cash_secured,
)


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


def test_covered_call_share_capacity_uses_actual_multiplier() -> None:
    capacity = compute_covered_call_share_capacity(
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
