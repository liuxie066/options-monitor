"""Covered-call policy: higher strikes, stock coverage and explicit debit economics."""
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


@pytest.mark.parametrize("days", [5, 10])
def test_call_requires_strictly_later_expiration(days):
    result = evaluate(expiration=(TODAY + timedelta(days=days)).isoformat())
    assert result["recommendation"] == "excluded"
    assert "new_expiration_not_later" in result["reason_codes"]


def test_debit_roll_keeps_cost_uplift_and_common_stock_capital():
    result = evaluate()
    assert result["recommendation"] == "debit_roll"
    assert result["cash_flow_type"] == "debit"
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


@pytest.mark.parametrize("bid,expected,flow", [(6, "credit_roll", "credit"), (5, "cash_neutral_roll", "cash_neutral")])
def test_credit_and_neutral_are_distinct_conclusions(bid, expected, flow):
    result = evaluate(bid=bid, old_buy_fee=0, new_sell_fee=0)
    assert result["recommendation"] == expected
    assert result["cash_flow_type"] == flow
    assert result["metrics"]["net_debit"] == 0


@pytest.mark.parametrize("bid", [Decimal("1"), Decimal("1.01")])
def test_debit_at_or_above_uplift_is_not_recommended(bid):
    result = evaluate(strike=104, bid=bid, old_ask=5.01, old_buy_fee=0, new_sell_fee=0)
    assert result["recommendation"] == "not_recommended"
    assert result["reason_codes"] == ["debit_not_below_assignment_uplift"]
    assert result["metrics"]["assignment_net_improvement"] <= 0
    assert result["metrics"]["net_debit"] >= result["metrics"]["assignment_proceeds_uplift"]


def test_fees_can_change_positive_conditional_improvement_to_nonrecommendation():
    without_fees = evaluate(strike=104, bid=1.01, old_ask=5, old_buy_fee=0, new_sell_fee=0)
    with_fees = evaluate(strike=104, bid=1.01, old_ask=5, old_buy_fee=1, new_sell_fee=0)
    assert without_fees["recommendation"] == "debit_roll"
    assert with_fees["recommendation"] == "not_recommended"


@pytest.mark.parametrize("shares", [0, 99])
def test_naked_or_undercovered_call_is_excluded(shares):
    result = evaluate(covered_shares=shares)
    assert result["recommendation"] == "excluded"
    assert "insufficient_stock_coverage" in result["reason_codes"]


@pytest.mark.parametrize("strike,expected", [(90, True), (100, True), (105, True), (105.01, False)])
def test_call_scope_is_mirrored_about_spot_with_inclusive_boundary(strike, expected):
    assert old_covered_call_in_roll_scope(strike=strike, spot=100) is expected


def test_new_call_can_remain_itm_while_raising_the_sale_price():
    result = evaluate(strike=101, bid=4.5)
    assert result["recommendation"] == "debit_roll"
    assert result["metrics"]["new_otm_pct"] < 0


def test_call_rejects_wrong_option_type_and_quantity():
    assert "unsupported_candidate" in evaluate(option_type="put")["reason_codes"]
    assert "contracts_mismatch" in evaluate(contracts=2)["reason_codes"]


def test_call_ranking_uses_conditional_improvement_instead_of_cash_sign():
    debit = {**evaluate()["metrics"], "expiration": "2026-12-14", "contract": "debit"}
    credit = {**evaluate(strike=101, bid=5.1)["metrics"], "expiration": "2026-12-14", "contract": "credit"}
    assert rank_covered_call_roll_candidates([credit, debit])[0]["contract"] == "debit"
