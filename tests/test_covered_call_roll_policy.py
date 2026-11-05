"""Covered-call policy: higher strikes, stock coverage and strictly positive net cash."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from om_position_advice_plugin import (
    evaluate_covered_call_roll, old_covered_call_in_roll_scope, rank_covered_call_roll_candidates,
)

TODAY = date(2026, 11, 4)


def evaluate(*, strike=105, bid=4, old_ask=5, old_buy_fee=2, new_sell_fee=3,
             covered_shares=100, **candidate_changes):
    position = {"symbol": "NVDA", "side": "short", "option_type": "call",
        "expiration": (TODAY + timedelta(days=10)).isoformat(), "strike": 100,
        "contracts_open": 1, "multiplier": 100, "currency": "USD", "broker": "futu"}
    candidate = {"symbol": "NVDA", "option_type": "call", "expiration": (TODAY + timedelta(days=40)).isoformat(),
        "strike": strike, "contracts": 1, "multiplier": 100, "currency": "USD", "bid": bid, "ask": float(bid) + 0.1,
        **candidate_changes}
    return evaluate_covered_call_roll(position, candidate, old_quote={"ask": old_ask}, spot=102,
        old_dte=10, new_dte=(date.fromisoformat(candidate["expiration"]) - TODAY).days,
        old_buy_fee=old_buy_fee, new_sell_fee=new_sell_fee, covered_shares=covered_shares)


@pytest.mark.parametrize("strike", [99, 100])
def test_call_rejects_lower_or_equal_strike_even_with_large_credit(strike):
    result = evaluate(strike=strike, bid=10)
    assert result["recommendation"] == "excluded"
    assert "new_strike_not_higher" in result["reason_codes"]
    assert "accepted" not in result


@pytest.mark.parametrize("days", [5, 9])
def test_call_rejects_expiration_before_old(days):
    result = evaluate(bid=6, expiration=(TODAY + timedelta(days=days)).isoformat())
    assert result["recommendation"] == "excluded"
    assert result["reason_codes"] == ["new_expiration_before_old"]


def test_call_allows_same_expiration_with_positive_dte_and_credit():
    result = evaluate(bid=6, old_buy_fee=0, new_sell_fee=0,
        expiration=(TODAY + timedelta(days=10)).isoformat())
    assert result["recommendation"] == "credit_roll"
    assert result["reason_codes"] == []
    metrics = result["metrics"]
    assert metrics["old_dte"] == metrics["new_dte"] == 10
    assert metrics["extension_days"] == 0
    assert metrics["assignment_improvement_annualized_return"] == pytest.approx(
        metrics["assignment_net_improvement"] / 10200 * 365 / 10)


@pytest.mark.parametrize("days,error", [(0, "new_dte must be > 0"), (-1, "new_dte must be finite and >= 0")])
def test_call_requires_positive_new_dte(days, error):
    with pytest.raises(ValueError, match=error):
        evaluate(expiration=(TODAY + timedelta(days=days)).isoformat())


def test_debit_is_not_recommended_even_when_conditional_assignment_improves():
    result = evaluate()
    assert result["recommendation"] == "not_recommended"
    assert result["cash_flow_type"] == "debit"
    assert result["reason_codes"] == ["non_positive_net_credit"]
    assert "accepted" not in result
    metrics = result["metrics"]
    assert metrics["old_close_cost"] == 502
    assert metrics["new_net_premium"] == 397
    assert metrics["net_credit"] == -105
    assert metrics["net_debit"] == 105
    assert metrics["strike_increase"] == 5
    assert metrics["assignment_proceeds_uplift"] == 500
    assert metrics["assignment_net_improvement"] == 395
    assert metrics["debit_to_uplift_ratio"] == pytest.approx(105 / 500)
    assert metrics["capital_basis_kind"] == "covered_stock_market_value"
    assert metrics["old_capital_basis"] == metrics["new_capital_basis"] == 10200
    assert metrics["incremental_annualized_return"] < 0
    assert metrics["assignment_improvement_annualized_return"] == pytest.approx(395 / 10200 * 365 / 40)


@pytest.mark.parametrize("bid,expected,flow", [(6, "credit_roll", "credit"), (5, "not_recommended", "cash_neutral")])
def test_only_credit_is_recommended_and_neutral_keeps_explicit_cash_flow(bid, expected, flow):
    result = evaluate(bid=bid, old_buy_fee=0, new_sell_fee=0)
    assert result["recommendation"] == expected
    assert result["cash_flow_type"] == flow
    assert result["metrics"]["net_debit"] == 0
    assert result["reason_codes"] == (["non_positive_net_credit"] if bid == 5 else [])


@pytest.mark.parametrize("bid", [Decimal("1"), Decimal("1.01")])
def test_debit_at_or_above_uplift_is_not_recommended(bid):
    result = evaluate(strike=104, bid=bid, old_ask=5.01, old_buy_fee=0, new_sell_fee=0)
    assert result["recommendation"] == "not_recommended"
    assert result["reason_codes"] == ["non_positive_net_credit"]
    assert result["metrics"]["assignment_net_improvement"] <= 0
    assert result["metrics"]["net_debit"] >= result["metrics"]["assignment_proceeds_uplift"]


@pytest.mark.parametrize("buy_fee,sell_fee", [(Decimal("1.01"), 0), (0, Decimal("1.01"))])
def test_either_leg_fee_can_change_credit_to_forbidden_debit(buy_fee, sell_fee):
    without_fees = evaluate(bid=Decimal("5.01"), old_ask=5, old_buy_fee=0, new_sell_fee=0)
    with_fees = evaluate(bid=Decimal("5.01"), old_ask=5,
        old_buy_fee=buy_fee, new_sell_fee=sell_fee)
    assert without_fees["recommendation"] == "credit_roll"
    assert without_fees["metrics"]["net_credit"] == 1
    assert with_fees["recommendation"] == "not_recommended"
    assert with_fees["reason_codes"] == ["non_positive_net_credit"]
    assert with_fees["metrics"]["net_credit"] == pytest.approx(-0.01)
    assert with_fees["metrics"]["assignment_net_improvement"] > 0


@pytest.mark.parametrize("bid,expected", [
    (Decimal("5.0099"), "not_recommended"),
    (Decimal("5.01"), "not_recommended"),
    (Decimal("5.0101"), "credit_roll"),
])
def test_cash_sign_boundary_is_after_fees_without_rounding(bid, expected):
    result = evaluate(bid=bid, old_ask=5, old_buy_fee=Decimal("0.4"), new_sell_fee=Decimal("0.6"))
    assert result["recommendation"] == expected
    assert result["reason_codes"] == (["non_positive_net_credit"] if expected == "not_recommended" else [])
    assert result["metrics"]["net_credit"] == pytest.approx(float((bid - 5) * 100 - 1))


@pytest.mark.parametrize("shares", [0, 99])
def test_naked_or_undercovered_call_is_excluded(shares):
    result = evaluate(covered_shares=shares)
    assert result["recommendation"] == "excluded"
    assert "insufficient_stock_coverage" in result["reason_codes"]


@pytest.mark.parametrize("strike,expected", [(90, True), (100, True), (105, True), (105.01, False)])
def test_call_scope_is_mirrored_about_spot_with_inclusive_boundary(strike, expected):
    assert old_covered_call_in_roll_scope(strike=strike, spot=100) is expected


def test_new_call_can_remain_itm_while_raising_the_sale_price():
    result = evaluate(strike=101, bid=6)
    assert result["recommendation"] == "credit_roll"
    assert result["metrics"]["new_otm_pct"] < 0


def test_call_rejects_wrong_option_type_and_quantity():
    assert "unsupported_candidate" in evaluate(option_type="put")["reason_codes"]
    assert "contracts_mismatch" in evaluate(contracts=2)["reason_codes"]


def test_call_ranking_prefers_cash_annualized_return_over_conditional_assignment_improvement():
    uplift = {**evaluate(strike=110, bid=5.1)["metrics"],
        "expiration": "2026-12-14", "contract": "uplift"}
    cash = {**evaluate(strike=101, bid=6)["metrics"],
        "expiration": "2026-12-14", "contract": "cash"}
    assert uplift["assignment_improvement_annualized_return"] > cash["assignment_improvement_annualized_return"]
    assert cash["incremental_annualized_return"] > uplift["incremental_annualized_return"]
    assert rank_covered_call_roll_candidates([uplift, cash])[0]["contract"] == "cash"


@pytest.mark.parametrize("field,first_value,second_value", [
    ("strike_increase_pct", 0.10, 0.05),
    ("extension_days", 10, 20),
    ("net_credit", 20, 10),
    ("contract", "a", "b"),
    ("expiration", "2026-12-13", "2026-12-14"),
])
def test_call_ranking_tiebreaks_match_put_direction_and_stable_identity(field, first_value, second_value):
    base = {**evaluate(bid=6)["metrics"], "expiration": "2026-12-14", "contract": "same"}
    first, second = {**base, field: first_value}, {**base, field: second_value}
    assert rank_covered_call_roll_candidates([second, first]) == [first, second]
