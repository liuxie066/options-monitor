import pytest

from src.application.cash_totals import sum_by_currency_to_cny


@pytest.mark.parametrize("amount", [None, "invalid", True, float("nan"), float("inf"), float("-inf")])
def test_invalid_balance_cannot_be_reported_as_complete_total(amount):
    assert sum_by_currency_to_cny(
        {"CNY": 100, "USD": amount}, usdcny_exchange_rate=7, cny_per_hkd_exchange_rate=None,
    ) is None


@pytest.mark.parametrize("rate", [None, 0, -1, True, float("nan"), float("inf")])
@pytest.mark.parametrize("currency", ["USD", "HKD"])
def test_nonzero_balance_requires_positive_finite_fx(currency, rate):
    assert sum_by_currency_to_cny(
        {currency: 100}, usdcny_exchange_rate=rate, cny_per_hkd_exchange_rate=rate,
    ) is None


def test_signed_cash_currency_aliases_and_valid_zero():
    assert sum_by_currency_to_cny(
        {"RMB": -100, "美元": 10, "港币": 50}, usdcny_exchange_rate=7, cny_per_hkd_exchange_rate=.9,
    ) == 15
    assert sum_by_currency_to_cny(
        {"USD": 0, "HKD": 0}, usdcny_exchange_rate=None, cny_per_hkd_exchange_rate=None,
    ) == 0
