from __future__ import annotations

from domain.domain.fetch_source import is_futu_fetch_source, normalize_fetch_source


def test_normalize_fetch_source_accepts_futu_aliases() -> None:
    assert normalize_fetch_source("futu") == "opend"
    assert normalize_fetch_source("futu_api") == "opend"
    assert normalize_fetch_source("futu-opend") == "opend"
    assert normalize_fetch_source("opend") == "opend"
    assert is_futu_fetch_source("futu") is True


def test_normalize_fetch_source_accepts_yahoo_aliases() -> None:
    assert normalize_fetch_source("yfinance") == "yahoo"
    assert normalize_fetch_source("yahoo") == "yahoo"
    assert is_futu_fetch_source("yahoo") is False
