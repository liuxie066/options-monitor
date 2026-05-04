from __future__ import annotations

from scripts.trade_symbol_identity import (
    canonical_symbol_aliases,
    futu_underlier_code,
    normalize_symbol_candidate,
    resolve_symbol_identity,
    symbol_currency,
    symbol_market,
)


def test_normalize_symbol_candidate_parses_futu_hk_option_display_name() -> None:
    assert normalize_symbol_candidate("泡泡玛特 260528 135.00 沽") == "9992.HK"


def test_normalize_symbol_candidate_rejects_unrecognized_display_name() -> None:
    assert normalize_symbol_candidate("未知标的 260528 135.00 沽") is None


def test_resolve_symbol_identity_returns_canonical_market_currency_and_futu_code() -> None:
    identity = resolve_symbol_identity("HK.POP260528P135000")

    assert identity is not None
    assert identity.canonical == "9992.HK"
    assert identity.market == "HK"
    assert identity.currency == "HKD"
    assert identity.futu_code == "HK.09992"
    assert identity.source_kind == "option_code"


def test_symbol_identity_helpers_share_the_same_canonical_parser() -> None:
    assert normalize_symbol_candidate("00700.HK") == "0700.HK"
    assert normalize_symbol_candidate("HK.00700") == "0700.HK"
    assert futu_underlier_code("700") == "HK.00700"
    assert symbol_market("US.NVDA") == "US"
    assert symbol_currency("US.NVDA") == "USD"
    assert canonical_symbol_aliases("0700.HK") == ["0700.HK", "00700.HK"]
