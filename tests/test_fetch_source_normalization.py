from __future__ import annotations

from domain.domain.fetch_source import is_futu_fetch_source, normalize_fetch_source, resolve_symbol_fetch_source


def test_normalize_fetch_source_accepts_futu_aliases() -> None:
    assert normalize_fetch_source("futu") == "opend"
    assert normalize_fetch_source("futu_api") == "opend"
    assert normalize_fetch_source("futu-opend") == "opend"
    assert normalize_fetch_source("opend") == "opend"
    assert is_futu_fetch_source("futu") is True


def test_normalize_fetch_source_collapses_legacy_aliases_to_opend() -> None:
    assert normalize_fetch_source("yfinance") == "opend"
    assert normalize_fetch_source("yahoo") == "opend"
    assert is_futu_fetch_source("yahoo") is True


def test_normalize_fetch_source_defaults_to_futu_opend() -> None:
    assert normalize_fetch_source(None) == "opend"


def test_resolve_symbol_fetch_source_always_uses_opend() -> None:
    assert resolve_symbol_fetch_source({}) == ("opend", "default_opend")
    assert resolve_symbol_fetch_source({"source": "yahoo"}) == ("opend", "configured_opend")
    assert resolve_symbol_fetch_source({"source": "futu"}) == ("opend", "configured_opend")
    assert resolve_symbol_fetch_source({"source": "futu", "_source_resolution": "degraded_to_yahoo"}) == (
        "opend",
        "configured_opend",
    )
