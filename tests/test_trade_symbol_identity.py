from __future__ import annotations

from domain.domain.symbol_identity import (
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


def test_symbol_identity_accepts_explicit_aliases_without_runtime_config_io() -> None:
    aliases = {"MELIHK": "3690.HK"}

    identity = resolve_symbol_identity("MELIHK", symbol_aliases=aliases)

    assert identity is not None
    assert identity.canonical == "3690.HK"
    assert normalize_symbol_candidate("MELIHK", symbol_aliases=aliases) == "3690.HK"


def test_hk_option_root_does_not_become_same_named_us_stock() -> None:
    hk = resolve_symbol_identity("HK.CNC260330C30000")
    us = resolve_symbol_identity("US.CNC260320C30000")
    assert (hk.canonical, hk.market, hk.currency, hk.futu_code) == (
        "0883.HK", "HK", "HKD", "HK.00883",
    )
    assert (us.canonical, us.market, us.currency) == ("CNC", "US", "USD")
    assert resolve_symbol_identity("CNC").canonical == "CNC"
    assert resolve_symbol_identity("US.MET260320C30000").canonical == "MET.US"
    assert resolve_symbol_identity("HK.ABCD260330C30000") is None


def test_explicit_us_alias_collisions_preserve_identity_on_reparse() -> None:
    from src.application.opend_utils import normalize_underlier
    for code in ("US.MET", "MET.US", "US.MET260320C30000", "US.TCH", "US.POP"):
        identity = resolve_symbol_identity(code)
        assert identity.market == "US"
        assert identity.currency == "USD"
        again = resolve_symbol_identity(identity.canonical)
        assert (again.canonical, again.market, again.currency, again.futu_code) == (
            identity.canonical, "US", "USD", identity.futu_code,
        )
        assert normalize_underlier(identity.canonical).code == identity.futu_code
    assert normalize_symbol_candidate("MET") == "3690.HK"
    assert normalize_symbol_candidate("US.NVDA") == "NVDA"


def test_explicit_market_rejects_cross_market_alias_and_malformed_hk() -> None:
    aliases = {"MET": "3690.HK", "00700BAD": "NVDA"}
    assert resolve_symbol_identity("US.MET", symbol_aliases=aliases).market == "US"
    for code in ("HK.00700BAD", "HK.BAD00700", "HK.00-700", "HK.ABCD"):
        assert resolve_symbol_identity(code, symbol_aliases=aliases) is None
    assert normalize_symbol_candidate("HK.CNC") == "0883.HK"
    assert normalize_symbol_candidate("HK.TCH") == "0700.HK"


def test_explicit_us_alias_key_keeps_matching_market_precedence():
    aliases = {"US.CNC": "NVDA", "CNC": "0700.HK"}
    for code in ("US.CNC", "CNC.US", "US.CNC260320C30000"):
        identity = resolve_symbol_identity(code, symbol_aliases=aliases)
        assert (identity.canonical, identity.market, identity.futu_code) == ("NVDA", "US", "US.NVDA")
