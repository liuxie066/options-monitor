"""Closed-market identity and candidate selection through the public facade."""

from copy import deepcopy
from decimal import Decimal

import pytest

from om_strategy_core.evaluation import eligible_stock_marks, legacy_market, value_portfolio
from tests.test_closed_market_valuation import market, ms


AT = ms("2026-10-24T07:56:00Z")


def portfolio(raw):
    contract = raw["options"][0]["contract_id"]
    return value_portfolio(
        point_id="synthetic-weekend",
        at_ms=AT,
        base_currency="HKD",
        positions=dict(
            cash_by_currency={"HKD": "100000"},
            stock_positions=[dict(symbol="0700.HK", currency="HKD", quantity=100)],
            short_option_positions=[dict(
                contract_id=contract, currency="HKD", strike="390", contracts=1,
                multiplier=100, underlying_symbol="0700.HK", option_type="put",
            )],
        ),
        receivables=[],
        market=raw,
        assignment_fees={contract: "0"},
    )


def test_unknown_hk_option_root_uses_contract_market_with_explicit_underlier():
    raw = market()
    raw["options"][0]["contract_id"] = "HK.ABCD261230P390000"
    before = deepcopy(raw)
    result = legacy_market(market=raw, base_currency="HKD", at_ms=AT)
    assert result["status"] == "complete"
    assert result["option_marks"] == [dict(
        contract_id="HK.ABCD261230P390000", currency="HKD", price=Decimal("11"),
    )]
    valuation = portfolio(raw)
    assert valuation["status"] == "complete"
    assert valuation["nav"] == Decimal("138900")
    assert raw == before


@pytest.mark.parametrize("contract", [
    "HK.ABCD", "HK.ABCD261230X390000", "HK.ABCD261230P390000tail",
    "HK.ABCD261230P390000\ninvalid", "ZZ.ABCD261230P390000",
    "US.ABCD261230P390000",
])
def test_invalid_or_uncovered_option_market_is_rejected(contract):
    raw = market()
    raw["options"][0]["contract_id"] = contract
    with pytest.raises(ValueError):
        legacy_market(market=raw, base_currency="HKD", at_ms=AT)


def test_us_option_uses_its_own_close_calendar():
    raw = market()
    observed = ms("2026-10-23T19:56:00Z")
    raw["stocks"][0].update(symbol="US.ABCD", currency="USD")
    raw["options"][0].update(contract_id="US.ABCD261230P390000", currency="USD")
    for kind in ("stocks", "options"):
        raw[kind][0].update(available_at_ms=observed, valid_until_ms=observed + 300_000)
    carry = raw["closed_market_carry"]
    carry["observation_at_ms"] = observed
    carry["calendars"]["US"] = carry["calendars"].pop("HK")
    result = legacy_market(market=raw, base_currency="USD", at_ms=ms("2026-10-24T19:56:00Z"))
    assert result["status"] == "complete"
    assert result["option_marks"][0]["price"] == Decimal("11")


def test_unsupported_contract_market_is_rejected_even_with_calendar():
    raw = market()
    raw["options"][0]["contract_id"] = "ZZ.ABCD261230P390000"
    raw["closed_market_carry"]["calendars"]["ZZ"] = raw["closed_market_carry"]["calendars"]["HK"]
    with pytest.raises(ValueError, match="calendar missing for asset"):
        legacy_market(market=raw, base_currency="HKD", at_ms=AT)


@pytest.mark.parametrize("kind", ["stocks", "options"])
@pytest.mark.parametrize("offset", [-360_000, 1])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("approximate", [False, True])
def test_ineligible_history_does_not_poison_eligible_close(kind, offset, reverse, approximate):
    raw = market()
    valid = raw[kind][0]
    if not approximate:
        valid.pop("time_basis")
        valid["effective_at_ms"] = valid["available_at_ms"]
    ineligible = deepcopy(valid)
    ineligible["available_at_ms"] += offset
    ineligible["valid_until_ms"] += offset
    if not approximate:
        ineligible["effective_at_ms"] += offset
    ineligible.update({"price": "999"} if kind == "stocks" else {"bid": "98", "ask": "100"})
    raw[kind] = [valid, ineligible] if reverse else [ineligible, valid]
    before = deepcopy(raw)
    result = legacy_market(market=raw, base_currency="HKD", at_ms=AT)
    assert result["status"] == "complete"
    assert result["stock_marks"][0]["price"] == Decimal("400")
    assert result["option_marks"][0]["price"] == Decimal("11")
    stock_marks = eligible_stock_marks(market=raw, at_ms=AT)
    assert stock_marks[0]["available_at_ms"] == raw["closed_market_carry"]["observation_at_ms"]
    assert portfolio(raw)["nav"] == Decimal("138900")
    assert raw == before


@pytest.mark.parametrize("kind", ["stocks", "options"])
@pytest.mark.parametrize("offset", [-300_001, 1])
def test_only_ineligible_originals_still_fail_closed(kind, offset):
    raw = market()
    raw[kind][0]["available_at_ms"] += offset
    raw[kind][0]["valid_until_ms"] += offset
    with pytest.raises(ValueError, match="original mark was not eligible"):
        legacy_market(market=raw, base_currency="HKD", at_ms=AT)


@pytest.mark.parametrize("kind", ["stocks", "options"])
def test_eligible_quote_cannot_cover_another_currency(kind):
    raw = market()
    stale = deepcopy(raw[kind][0])
    stale.update(currency="USD", available_at_ms=stale["available_at_ms"] - 360_000,
                 valid_until_ms=stale["valid_until_ms"] - 360_000)
    raw[kind].append(stale)
    with pytest.raises(ValueError, match="original mark was not eligible"):
        legacy_market(market=raw, base_currency="HKD", at_ms=AT)


@pytest.mark.parametrize("kind", ["stocks", "options"])
@pytest.mark.parametrize("offset", [0, -360_000, 1])
def test_conflicting_prices_are_rejected_even_outside_observation(kind, offset):
    raw = market()
    first = deepcopy(raw[kind][0])
    first["available_at_ms"] += offset
    first["valid_until_ms"] += offset
    conflict = deepcopy(first)
    conflict.update({"price": "999"} if kind == "stocks" else {"bid": "98", "ask": "100"})
    raw[kind].extend([first, conflict])
    with pytest.raises(ValueError, match=f"conflicting {kind} mark"):
        legacy_market(market=raw, base_currency="HKD", at_ms=AT)


@pytest.mark.parametrize("kind", ["stocks", "options"])
@pytest.mark.parametrize("reverse", [False, True])
def test_latest_eligible_original_wins_with_inclusive_expiry(kind, reverse):
    raw = market()
    newest = raw[kind][0]
    older = deepcopy(newest)
    older["available_at_ms"] -= 300_000
    older["valid_until_ms"] -= 300_000
    older.update({"price": "999"} if kind == "stocks" else {"bid": "98", "ask": "100"})
    raw[kind] = [newest, older] if reverse else [older, newest]
    result = legacy_market(market=raw, base_currency="HKD", at_ms=AT)
    assert result["stock_marks"][0]["price"] == Decimal("400")
    assert result["option_marks"][0]["price"] == Decimal("11")
