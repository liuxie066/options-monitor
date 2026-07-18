from __future__ import annotations

from decimal import Decimal

import pytest

from domain.domain.ledger.identity import ContractKey
from domain.domain.performance.models import OptionInstrumentKey, StockInstrumentKey, canonical_decimal_text


def test_option_instrument_key_round_trip_and_decimal_stability() -> None:
    key = OptionInstrumentKey(
        symbol="nvda",
        option_type="P",
        strike=Decimal("100.500000"),
        expiration_ymd="2026-08-21",
        currency="usd",
        multiplier=Decimal("100.00"),
    )

    assert key.instrument_key == "option:v1|NVDA|put|100.5|2026-08-21|USD|100"
    assert OptionInstrumentKey.decode(key.instrument_key) == key
    assert key.to_dict()["strike"] == "100.5"


def test_stock_instrument_key_round_trip_with_hk_symbol() -> None:
    key = StockInstrumentKey(symbol="0700.hk", currency="hkd")

    assert key.instrument_key == "stock:v1|0700.HK|HKD"
    assert StockInstrumentKey.decode(key.instrument_key) == key


def test_contract_key_conversion_excludes_account_broker_and_side() -> None:
    short_lx = ContractKey.from_values(
        broker="futu",
        account="lx",
        underlying_symbol="NVDA",
        option_type="put",
        position_side="short",
        strike=100,
        expiration_ymd="2026-08-21",
    )
    long_sy = ContractKey.from_values(
        broker="futu",
        account="sy",
        underlying_symbol="NVDA",
        option_type="put",
        position_side="long",
        strike=100,
        expiration_ymd="2026-08-21",
    )

    first = OptionInstrumentKey.from_contract_key(short_lx, currency="USD", multiplier=100)
    second = OptionInstrumentKey.from_contract_key(long_sy, currency="USD", multiplier=100)

    assert first.instrument_key == second.instrument_key
    assert short_lx.position_key != long_sy.position_key
    assert "lx" not in first.instrument_key
    assert "short" not in first.instrument_key


@pytest.mark.parametrize(
    "value",
    [
        "option:v2|NVDA|put|100|2026-08-21|USD|100",
        "option:v1|NVDA|put|100|2026-08-21|USD",
        "option:v1|NVDA|bad|100|2026-08-21|USD|100",
        "option:v1|NVDA|put|0|2026-08-21|USD|100",
        "option:v1|NVDA|put|100|bad|USD|100",
        "option:v1|NVDA|put|100|2026-08-21|usd|100",
        "option:v1|NVDA|put|100|2026-08-21|USD|0",
    ],
)
def test_invalid_option_key_decode_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        OptionInstrumentKey.decode(value)


@pytest.mark.parametrize("value", ["stock:v2|NVDA|USD", "stock:v1|NVDA", "stock:v1|NVDA|usd"])
def test_invalid_stock_key_decode_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        StockInstrumentKey.decode(value)


def test_canonical_decimal_text_rejects_non_finite_and_normalizes_negative_zero() -> None:
    assert canonical_decimal_text(Decimal("-0.000")) == "0"
    assert canonical_decimal_text(Decimal("1E+3")) == "1000"
    with pytest.raises(ValueError, match="finite"):
        canonical_decimal_text("NaN")
