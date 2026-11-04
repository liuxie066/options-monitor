"""Pure roll cash economics; quote freshness/standard-contract evidence is OM's."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from domain.domain.fee_calc import estimate_futu_option_buy_fee, estimate_futu_option_sell_fee
from om_position_advice_plugin import (
    evaluate_short_put_roll,
    old_position_in_roll_scope,
    rank_short_put_roll_candidates,
    remaining_max_annualized_return,
)


_TODAY = date(2026, 11, 4)


def _position(**overrides):
    position = {
        "symbol": "NVDA",
        "side": "short",
        "option_type": "put",
        "expiration": (_TODAY + timedelta(days=10)).isoformat(),
        "strike": 100,
        "contracts_open": 1,
        "multiplier": 100,
        "currency": "USD",
        "broker": "futu",
    }
    position.update(overrides)
    return position


def _candidate(*, dte=40, **overrides):
    candidate = {
        "symbol": "NVDA",
        "option_type": "put",
        "expiration": (_TODAY + timedelta(days=dte)).isoformat(),
        "strike": 95,
        "contracts": 1,
        "multiplier": 100,
        "currency": "USD",
        "bid": 5.4,
        "ask": 5.5,
    }
    candidate.update(overrides)
    return candidate


def _evaluate(*, position=None, candidate=None, **overrides):
    kwargs = {
        "old_quote": {"ask": 5},
        "spot": 98,
        "old_dte": 10,
        "new_dte": 40,
        "old_buy_fee": 0,
        "new_sell_fee": 0,
    }
    kwargs.update(overrides)
    return evaluate_short_put_roll(
        position if position is not None else _position(),
        candidate if candidate is not None else _candidate(),
        **kwargs,
    )


def test_old_buyback_needs_positive_ask_and_allows_zero_bid():
    result = _evaluate(old_quote={"ask": 5, "bid": 0})
    assert result["accepted"] is True
    assert result["metrics"]["net_credit"] == pytest.approx(40)
    with pytest.raises(ValueError):
        _evaluate(old_quote={"ask": 5, "bid": -0.01})


@pytest.mark.parametrize(("strike", "spot", "expected"), [
    (101, 100, True),
    (100, 100, True),
    (96, 100, True),
    (95, 100, True),
    (94.9999999999, 100, False),
    (Decimal("0.95"), Decimal("1"), True),
    (Decimal("9.5"), Decimal("10"), True),
    (Decimal("9.499999999999999999"), Decimal("10"), False),
])
def test_old_scope_includes_exact_five_percent_without_display_rounding(strike, spot, expected):
    assert old_position_in_roll_scope(strike=strike, spot=spot) is expected


@pytest.mark.parametrize("value", [None, "", True, False, 0, -1, float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("field", ["strike", "spot"])
def test_old_scope_rejects_invalid_required_numbers(field, value):
    kwargs = {"strike": 95, "spot": 100, field: value}
    with pytest.raises(ValueError):
        old_position_in_roll_scope(**kwargs)


def test_design_example_prefers_nearer_a_despite_more_cash_in_b():
    a = _evaluate()
    b = _evaluate(candidate=_candidate(dte=100, bid=5.9, ask=6), new_dte=100)
    assert a["accepted"] is b["accepted"] is True
    assert a["reason_codes"] == b["reason_codes"] == []
    am, bm = a["metrics"], b["metrics"]
    assert am["old_close_cost"] == bm["old_close_cost"] == 500
    assert am["net_credit"] == 40
    assert bm["net_credit"] == 90
    assert am["incremental_annualized_return"] * 100 == pytest.approx(3.8421052631578947)
    assert bm["incremental_annualized_return"] * 100 == pytest.approx(3.457894736842105)
    assert am["old_remaining_max_annualized_return"] == pytest.approx(1.825)
    assert am["new_remaining_max_annualized_return"] == pytest.approx(540 / 9500 * 365 / 40)
    assert am["remaining_annualized_change"] < 0
    assert bm["remaining_annualized_change"] < 0
    assert am["extension_annualized_return"] == pytest.approx(40 / 9500 * 365 / 30)
    rows = [{"contract": "B", **bm}, {"contract": "A", **am}]
    assert [row["contract"] for row in rank_short_put_roll_candidates(rows)] == ["A", "B"]
    assert rows[0]["contract"] == "B"


def test_complete_unrounded_metrics_use_cash_and_strike_basis():
    result = _evaluate(old_buy_fee=2, new_sell_fee=3)
    assert result["accepted"] is True
    metrics = result["metrics"]
    assert metrics == pytest.approx({
        "old_otm_pct": -2 / 98,
        "new_otm_pct": 3 / 98,
        "strike_drop_pct": 0.05,
        "notional_obligation_reduction": 500,
        "contracts": 1,
        "multiplier": 100,
        "old_close_cost": 502,
        "new_net_premium": 537,
        "net_credit": 35,
        "old_capital_basis": 10000,
        "new_capital_basis": 9500,
        "old_dte": 10,
        "new_dte": 40,
        "extension_days": 30,
        "incremental_period_return": 35 / 9500,
        "incremental_annualized_return": 35 / 9500 * 365 / 40,
        "new_remaining_max_annualized_return": 537 / 9500 * 365 / 40,
        "old_remaining_max_annualized_return": 502 / 10000 * 365 / 10,
        "remaining_annualized_change": 537 / 9500 * 365 / 40 - 502 / 10000 * 365 / 10,
        "net_credit_per_extension_day": 35 / 30,
        "extension_annualized_return": 35 / 9500 * 365 / 30,
    })


def test_same_expiration_has_no_extension_denominator():
    result = _evaluate(candidate=_candidate(dte=10), new_dte=10)
    assert result["accepted"] is True
    metrics = result["metrics"]
    assert metrics["extension_days"] == 0
    assert metrics["net_credit_per_extension_day"] is None
    assert metrics["extension_annualized_return"] is None
    assert metrics["incremental_annualized_return"] == pytest.approx(40 / 9500 * 365 / 10)


@pytest.mark.parametrize(("candidate_values", "reason"), [
    ({"strike": 100}, "new_strike_not_lower"),
    ({"strike": 101}, "new_strike_not_lower"),
    ({"strike": 98}, "new_put_not_otm"),
    ({"strike": 99}, "new_put_not_otm"),
    ({"symbol": "MSFT"}, "symbol_mismatch"),
    ({"symbol": "0700.HK"}, "symbol_mismatch"),
    ({"currency": "HKD"}, "currency_mismatch"),
    ({"multiplier": 10}, "multiplier_mismatch"),
    ({"contracts": 2}, "contracts_mismatch"),
    ({"option_type": "call"}, "unsupported_candidate"),
])
def test_ineligible_candidate_has_explicit_rejection(candidate_values, reason):
    result = _evaluate(candidate=_candidate(**candidate_values))
    assert result["accepted"] is False
    assert reason in result["reason_codes"]


@pytest.mark.parametrize("position_values", [{"side": "long"}, {"option_type": "call"}])
def test_only_existing_short_puts_are_supported(position_values):
    result = _evaluate(position=_position(**position_values))
    assert result["accepted"] is False
    assert "unsupported_position" in result["reason_codes"]


def test_explicit_earlier_expiration_is_ineligible():
    result = _evaluate(candidate=_candidate(dte=9), new_dte=9)
    assert result["accepted"] is False
    assert result["reason_codes"] == ["new_expiration_before_old"]


def test_dte_must_bind_both_expirations_to_same_evaluation_date():
    with pytest.raises(ValueError, match="same market date"):
        _evaluate(new_dte=41)
    with pytest.raises(ValueError, match="same market date"):
        _evaluate(candidate=_candidate(dte=9), new_dte=40)


def test_old_scope_at_five_percent_is_included_and_just_beyond_is_rejected():
    included = _evaluate(position=_position(strike=95), candidate=_candidate(strike=90), spot=100)
    excluded = _evaluate(position=_position(strike=94.999999999), candidate=_candidate(strike=90), spot=100)
    assert included["accepted"] is True
    assert included["metrics"]["old_otm_pct"] == 0.05
    assert excluded["accepted"] is False
    assert excluded["reason_codes"] == ["old_position_outside_roll_scope"]


def test_missing_or_high_opening_strategy_metrics_do_not_block_roll():
    baseline = _evaluate()
    assert baseline["accepted"] is True
    assert baseline == _evaluate(candidate=_candidate(delta=-0.99, implied_volatility=None, term_matched_rv=None))
    assert baseline["metrics"]["incremental_annualized_return"] < 0.1


def test_historical_premium_and_fees_do_not_affect_incremental_cash_or_rank():
    baseline = _evaluate()
    changed = _evaluate(position=_position(
        premium=999, premium_open=999, estimated_open_fee=12345,
        net_premium=-999, realized_pnl=-99999,
    ))
    assert changed == baseline
    assert baseline["metrics"]["net_credit"] == 40
    assert baseline["metrics"]["notional_obligation_reduction"] == 500


@pytest.mark.parametrize(("bid", "buy_fee", "sell_fee", "accepted", "credit"), [
    (5.4, 0, 0, True, 40),
    (5.4, 20, 20, False, 0),
    (5.4, 21, 20, False, -1),
    (5, 0, 0, False, 0),
    (4.9, 0, 0, False, -10),
    (5.00000000001, 0, 0, True, 0.000000001),
])
def test_positive_net_credit_uses_full_precision_and_both_fees(bid, buy_fee, sell_fee, accepted, credit):
    result = _evaluate(candidate=_candidate(bid=bid, ask=6), old_buy_fee=buy_fee, new_sell_fee=sell_fee)
    assert result["accepted"] is accepted
    assert result["metrics"]["net_credit"] == pytest.approx(credit, abs=1e-14)
    assert result["reason_codes"] == ([] if accepted else ["non_positive_net_credit"])


def test_cash_gate_uses_new_bid_and_old_ask_instead_of_midpoints():
    result = _evaluate(candidate=_candidate(bid=4.99, ask=7), old_quote={"bid": 4, "ask": 5})
    assert result["accepted"] is False
    assert result["metrics"]["net_credit"] == -1


@pytest.mark.parametrize(("currency", "multiplier", "old_ask", "new_bid"), [
    ("USD", 10, 1, 1.5),
    ("HKD", 500, 1, 1.5),
])
def test_actual_quantity_and_observed_multiplier_bind_both_fee_legs(currency, multiplier, old_ask, new_bid):
    qty = 3
    buy_fee = estimate_futu_option_buy_fee(currency, old_ask, contracts=qty, multiplier=multiplier)
    sell_fee = estimate_futu_option_sell_fee(currency, new_bid, contracts=qty, multiplier=multiplier)
    result = _evaluate(
        position=_position(currency=currency, multiplier=multiplier, contracts_open=qty),
        candidate=_candidate(currency=currency, multiplier=multiplier, contracts=qty, bid=new_bid, ask=2),
        old_quote={"ask": old_ask}, old_buy_fee=buy_fee.amount, new_sell_fee=sell_fee.amount,
    )
    assert result["accepted"] is True
    metrics = result["metrics"]
    assert metrics["contracts"] == qty
    assert metrics["multiplier"] == multiplier
    assert metrics["old_close_cost"] == old_ask * qty * multiplier + buy_fee.amount
    assert metrics["net_credit"] == pytest.approx((new_bid - old_ask) * qty * multiplier - buy_fee.amount - sell_fee.amount)
    assert metrics["new_capital_basis"] == 95 * qty * multiplier


@pytest.mark.parametrize(("owner", "field", "value"), [
    ("position", "contracts_open", None),
    ("position", "contracts_open", 0),
    ("position", "contracts_open", 1.5),
    ("position", "contracts_open", True),
    ("candidate", "contracts", None),
    ("candidate", "contracts", 1.5),
    ("position", "multiplier", None),
    ("candidate", "multiplier", None),
    ("candidate", "multiplier", 1.5),
    ("position", "strike", 0),
    ("candidate", "strike", float("nan")),
    ("candidate", "bid", None),
    ("candidate", "bid", 0),
    ("candidate", "ask", float("inf")),
    ("candidate", "ask", 5),
    ("candidate", "currency", None),
    ("position", "broker", None),
    ("position", "symbol", ""),
    ("candidate", "symbol", None),
    ("position", "expiration", None),
    ("candidate", "expiration", "2026-02-30"),
])
def test_required_evidence_is_invalid_instead_of_defaulted(owner, field, value):
    position, candidate = _position(), _candidate()
    (position if owner == "position" else candidate)[field] = value
    with pytest.raises(ValueError):
        _evaluate(position=position, candidate=candidate)


@pytest.mark.parametrize(("field", "value"), [
    ("spot", None), ("spot", float("nan")), ("spot", True),
    ("old_dte", 0), ("new_dte", -1), ("old_dte", True), ("new_dte", 40.1),
    ("old_buy_fee", None), ("old_buy_fee", -1), ("old_buy_fee", float("inf")),
    ("new_sell_fee", float("nan")), ("new_sell_fee", True),
    ("old_quote", None), ("old_quote", {}), ("old_quote", {"ask": 0}),
    ("old_quote", {"bid": 6, "ask": 5}),
])
def test_invalid_context_or_fee_raises_for_application_unavailable_handling(field, value):
    with pytest.raises(ValueError):
        _evaluate(**{field: value})


def test_rank_uses_every_documented_tie_breaker_without_rounding_or_mutation():
    baseline = {
        "incremental_annualized_return": 0.04,
        "strike_drop_pct": 0.05,
        "extension_days": 30,
        "net_credit": 40,
        "expiration": "2026-12-14",
    }
    rows = [
        {**baseline, "contract": "last", "incremental_annualized_return": 0.039999999999},
        {**baseline, "contract": "D", "net_credit": 39},
        {**baseline, "contract": "C", "extension_days": 31},
        {**baseline, "contract": "B"},
        {**baseline, "contract": "A"},
        {**baseline, "contract": "drop", "strike_drop_pct": 0.06},
        {**baseline, "contract": "first", "incremental_annualized_return": 0.040000000001},
    ]
    original = [dict(row) for row in rows]
    result = rank_short_put_roll_candidates(rows)
    assert [row["contract"] for row in result] == ["first", "drop", "A", "B", "D", "C", "last"]
    assert rows == original
    assert rank_short_put_roll_candidates([]) == []


def test_shared_remaining_annualized_helper_keeps_original_arithmetic_order():
    cash, basis, dte = 502.3489, 19997.53, 11
    assert remaining_max_annualized_return(remaining_cash=cash, capital_basis=basis, dte=dte) == cash / basis * 365 / dte
