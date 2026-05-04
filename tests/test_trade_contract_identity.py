from __future__ import annotations

from scripts.trade_contract_identity import (
    canonical_contract_symbol,
    contract_key,
    contract_strike_key,
    normalize_contract_expiration,
    normalize_contract_option_type,
    normalize_position_effect,
    normalize_trade_side,
)


def test_trade_side_and_position_effect_aliases_are_centralized() -> None:
    assert normalize_trade_side("SELL_SHORT") == "sell"
    assert normalize_trade_side("sell short") == "sell"
    assert normalize_trade_side("short sell") == "sell"
    assert normalize_trade_side("buy to close") == "buy"
    assert normalize_trade_side("buy back") == "buy"
    assert normalize_trade_side("买平") == "buy"
    assert normalize_trade_side("买 平") == "buy"
    assert normalize_position_effect("SELL_SHORT") == "open"
    assert normalize_position_effect("sell short") == "open"
    assert normalize_position_effect("short sell") == "open"
    assert normalize_position_effect("buy to close") == "close"
    assert normalize_position_effect("buy back") == "close"
    assert normalize_position_effect("买 平") == "close"
    assert normalize_position_effect("voided") == "void"
    assert normalize_position_effect("adjustment") == "adjust"


def test_contract_expiration_accepts_common_option_date_shapes() -> None:
    assert normalize_contract_expiration("260618") == "2026-06-18"
    assert normalize_contract_expiration("20260618") == "2026-06-18"
    assert normalize_contract_expiration("2026-06-18T09:30:00") == "2026-06-18"
    assert normalize_contract_expiration("1781712000000") == "2026-06-18"
    assert normalize_contract_expiration("bad", fallback_raw=True) == "bad"
    assert normalize_contract_expiration("bad") is None


def test_contract_key_uses_canonical_symbol_option_type_expiration_and_strike() -> None:
    assert canonical_contract_symbol("HK.00700") == "0700.HK"
    assert normalize_contract_option_type("认沽") == "put"
    assert contract_strike_key("100") == "100.000000"
    assert contract_strike_key(float("nan")) == ""
    assert contract_key("HK.00700", "认沽", "260618", "100") == (
        "0700.HK",
        "put",
        "2026-06-18",
        "100.000000",
    )
