"""Public offline evaluation contract: synthetic inputs, no provider or ledger I/O."""

from copy import deepcopy
from decimal import Decimal

import pytest

from om_strategy_core.evaluation import (
    EvaluationBook,
    IncompleteCashError,
    decision_exchange_rates,
    legacy_market,
    value_portfolio,
)


def test_public_owner_values_put_liability_and_assignment_without_mutation():
    positions = {
        "cash_by_currency": {"USD": "10000"},
        "stock_positions": [],
        "short_option_positions": [
            dict(
                contract_id="test-put",
                currency="USD",
                strike="95",
                contracts=1,
                multiplier=100,
                underlying_symbol="TEST",
                option_type="put",
            )
        ],
    }
    basis = dict(source="synthetic", effective_at_ms=1000, available_at_ms=1000, valid_until_ms=2000)
    market = dict(
        stocks=[dict(symbol="TEST", currency="USD", price="100", **basis)],
        options=[dict(contract_id="test-put", currency="USD", bid="2", ask="2.2", **basis)],
        fx=[],
    )
    before = deepcopy((positions, market))
    result = value_portfolio(
        point_id="day",
        at_ms=1500,
        base_currency="USD",
        positions=positions,
        receivables=[],
        market=market,
        assignment_fees={"test-put": "5"},
    )
    assert result["status"] == "complete"
    assert result["nav"] == Decimal("9790")
    assert result["scenario"]["nav"] == Decimal("10495")
    assert result["scenario"]["cash_delta_by_currency"] == {"USD": Decimal("-9505")}
    assert result["current_stock_ratio"]["overall"] == 0
    assert (positions, market) == before
    market["options"] = []
    missing = value_portfolio(
        point_id="day",
        at_ms=1500,
        base_currency="USD",
        positions=positions,
        receivables=[],
        market=market,
        assignment_fees={"test-put": "5"},
    )
    assert missing["status"] == "incomplete"
    assert missing["nav"] is None
    assert "option_mark:test-put" in missing["gaps"]


def test_received_snapshot_preserves_unknown_source_time_and_expires():
    mark = dict(
        symbol="TEST",
        currency="USD",
        price="10",
        source="synthetic",
        time_basis="received_snapshot",
        effective_at_ms=None,
        available_at_ms=1000,
        valid_until_ms=301000,
    )
    market = dict(stocks=[mark], options=[], fx=[])
    assert legacy_market(market=market, base_currency="USD", at_ms=1000)["stock_marks"][0]["price"] == 10
    assert legacy_market(market=market, base_currency="USD", at_ms=301001)["stock_marks"] == []
    mark["valid_until_ms"] += 1
    with pytest.raises(ValueError, match="300s"):
        legacy_market(market=market, base_currency="USD", at_ms=1000)


def test_cash_interest_requires_explicit_rate_and_consecutive_days():
    book = EvaluationBook({"USD": None})
    with pytest.raises(IncompleteCashError, match="annual_rate"):
        book.accrue_interest(day="2026-10-26", at_ms=1000, cash={"USD": Decimal("1000")})
    book = EvaluationBook({"USD": "0.0365"})
    assert book.accrue_interest(day="2026-10-26", at_ms=1000, cash={"USD": Decimal("1000")}) == {"USD": Decimal("0.1")}
    with pytest.raises(ValueError, match="consecutive"):
        book.accrue_interest(day="2026-10-28", at_ms=3000, cash={"USD": Decimal("1000")})


def test_dividend_rights_use_ex_date_quantity_and_payment_is_idempotent():
    book = EvaluationBook({"USD": "0"})
    dividend = dict(
        dividend_id="test",
        symbol="TEST",
        currency="USD",
        net_per_share="0.5",
        ex_at_ms=1000,
        pay_at_ms=2000,
        source="synthetic",
        available_at_ms=500,
    )
    book.ex_dividend(dividend=dividend, stocks={("TEST", "USD"): 100}, at_ms=1000)
    assert book.receivables()[0]["amount"] == Decimal("50")
    with pytest.raises(ValueError, match="before"):
        book.pay_dividend(dividend_id="test", at_ms=1500)
    assert book.pay_dividend(dividend_id="test", at_ms=2000) == {"USD": Decimal("50")}
    assert book.pay_dividend(dividend_id="test", at_ms=2000) == {}
    assert book.receivables() == []
    assert len(book.cash_events) == 1


def test_decision_fx_public_scalar_owner_does_not_accept_missing_qualification():
    result = decision_exchange_rates(fx=[], at_ms=1000)
    assert result["status"] == "incomplete"
    assert result["exchange_rates"] == {}
    assert set(result["gaps"]) == {"fx:USD:1000", "fx:HKD:1000"}
