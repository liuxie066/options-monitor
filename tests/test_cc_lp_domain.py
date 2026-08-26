from __future__ import annotations

import pytest

from domain.domain.engine.cc_lp import (
    CC_LP_DEFAULT_MAX_PUT_DELTA,
    CC_LP_DEFAULT_MIN_PUT_DELTA,
    cc_lp_rank_key,
    compute_cc_lp_metrics,
    rank_cc_lp_rows,
    validate_cc_lp_pair,
)
from domain.domain.engine.combo_yield import ComboYieldLeg


def _call_leg(strike: float = 110.0, **overrides):
    base = dict(
        symbol="NVDA",
        option_type="CALL",
        expiration="2026-08-28",
        contract_symbol="NVDA260828C110000",
        currency="USD",
        dte=20,
        strike=strike,
        spot=100.0,
        bid=4.0,
        ask=4.2,
        mid=4.1,
        multiplier=100.0,
        open_interest=500.0,
        volume=20.0,
        implied_volatility=0.35,
        delta=0.30,
        spread=0.2,
        spread_ratio=0.05,
    )
    base.update(overrides)
    return ComboYieldLeg(**base)


def _put_leg(strike: float = 90.0, **overrides):
    base = dict(
        symbol="NVDA",
        option_type="PUT",
        expiration="2026-08-28",
        contract_symbol="NVDA260828P90000",
        currency="USD",
        dte=20,
        strike=strike,
        spot=100.0,
        bid=2.0,
        ask=2.2,
        mid=2.1,
        multiplier=100.0,
        open_interest=600.0,
        volume=15.0,
        implied_volatility=0.40,
        delta=-0.15,
        spread=0.2,
        spread_ratio=0.10,
    )
    base.update(overrides)
    return ComboYieldLeg(**base)


def test_validate_cc_lp_pair_accepts_call_above_put() -> None:
    call = _call_leg(strike=110.0)
    put = _put_leg(strike=90.0)
    assert validate_cc_lp_pair(call, put) == []


def test_validate_cc_lp_pair_rejects_call_below_put() -> None:
    call = _call_leg(strike=90.0)
    put = _put_leg(strike=110.0)
    rejects = validate_cc_lp_pair(call, put)
    assert "strike_order" in rejects


def test_validate_cc_lp_pair_rejects_delta_outside_window() -> None:
    call = _call_leg()
    shallow = _put_leg(delta=-0.05)
    assert "put_delta_below_min" in validate_cc_lp_pair(call, shallow)
    deep = _put_leg(delta=-0.35)
    assert "put_delta_above_max" in validate_cc_lp_pair(call, deep)


def test_validate_cc_lp_pair_rejects_expiration_mismatch() -> None:
    call = _call_leg(expiration="2026-08-28")
    put = _put_leg(expiration="2026-09-29")
    assert "expiration_mismatch" in validate_cc_lp_pair(call, put)


def test_compute_cc_lp_metrics_uses_call_sell_put_buy() -> None:
    call = _call_leg(bid=5.0)
    put = _put_leg(ask=2.0)
    metrics = compute_cc_lp_metrics(
        call_leg=call,
        put_leg=put,
        call_sell_fee=1.0,
        put_buy_fee=1.0,
        covered_notional=10_000.0,
        dte=20,
    )
    # call net = 5*100 - 1 = 499; put cost = 2*100 + 1 = 201; net = 298
    assert metrics.call_net_credit == 499.0
    assert metrics.put_total_cost == 201.0
    assert metrics.net_credit == 298.0
    assert metrics.net_debit == 0.0
    assert metrics.retention == pytest.approx(298.0 / 499.0)
    assert metrics.net_return == pytest.approx(298.0 / 10_000.0)
    assert metrics.annualized_net_return is not None
    assert metrics.gap_width_pct == 0.20
    assert metrics.call_otm_pct == 0.10
    assert metrics.put_otm_pct == 0.10


def test_compute_cc_lp_metrics_rejects_covered_notional_non_positive() -> None:
    call = _call_leg()
    put = _put_leg()
    try:
        compute_cc_lp_metrics(
            call_leg=call,
            put_leg=put,
            call_sell_fee=0.0,
            put_buy_fee=0.0,
            covered_notional=0.0,
        )
    except ValueError:
        return
    raise AssertionError("expected ValueError for non-positive covered_notional")


def test_compute_cc_lp_metrics_rejects_non_positive_call_net_credit() -> None:
    call = _call_leg(bid=0.01)  # 0.01*100 - 2 fee = -1
    put = _put_leg()
    try:
        compute_cc_lp_metrics(
            call_leg=call,
            put_leg=put,
            call_sell_fee=2.0,
            put_buy_fee=0.0,
            covered_notional=10_000.0,
        )
    except ValueError:
        return
    raise AssertionError("expected ValueError for non-positive call_net_credit")


def test_compute_cc_lp_metrics_retention_is_positive_for_net_credit() -> None:
    call = _call_leg(bid=5.0)
    put = _put_leg(ask=2.0)
    metrics = compute_cc_lp_metrics(
        call_leg=call,
        put_leg=put,
        call_sell_fee=1.0,
        put_buy_fee=1.0,
        covered_notional=10_000.0,
        dte=20,
    )
    assert metrics.retention > 0.0
    assert metrics.net_debit == 0.0


def test_rank_cc_lp_retention_primary_delta_secondary() -> None:
    rows = [
        {"net_credit_retention": 0.30, "put_delta": -0.15, "call_spread_ratio": 0.1, "put_spread_ratio": 0.1, "call_open_interest": 100.0, "put_open_interest": 200.0, "call_contract_symbol": "C1", "put_contract_symbol": "P1"},
        {"net_credit_retention": 0.50, "put_delta": -0.10, "call_spread_ratio": 0.1, "put_spread_ratio": 0.1, "call_open_interest": 100.0, "put_open_interest": 200.0, "call_contract_symbol": "C2", "put_contract_symbol": "P2"},
        {"net_credit_retention": 0.50, "put_delta": -0.20, "call_spread_ratio": 0.1, "put_spread_ratio": 0.1, "call_open_interest": 100.0, "put_open_interest": 200.0, "call_contract_symbol": "C3", "put_contract_symbol": "P3"},
    ]
    ranked = rank_cc_lp_rows(rows)
    # retention 0.50 first; among them, delta closer to 0.12 (-0.10) before -0.20
    assert [r["put_delta"] for r in ranked] == [-0.10, -0.20, -0.15]


def test_cc_lp_defaults_are_sane() -> None:
    assert CC_LP_DEFAULT_MIN_PUT_DELTA < CC_LP_DEFAULT_MAX_PUT_DELTA
    assert cc_lp_rank_key({})[0] == 0.0
