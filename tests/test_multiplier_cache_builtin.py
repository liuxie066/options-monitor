from __future__ import annotations

from pathlib import Path

from scripts.multiplier_cache import (
    get_cached_multiplier,
    normalize_symbol,
    resolve_multiplier,
    resolve_multiplier_with_source,
    save_cache,
)


def test_normalize_hk_symbol_to_four_digit_suffix() -> None:
    assert normalize_symbol("00700.HK") == "0700.HK"
    assert normalize_symbol("700.HK") == "0700.HK"
    assert normalize_symbol("NVDA") == "NVDA"


def test_resolve_multiplier_returns_none_when_cache_missing_and_refresh_disabled(tmp_path: Path) -> None:
    assert resolve_multiplier(
        repo_base=tmp_path,
        symbol="0700.HK",
        allow_opend_refresh=False,
    ) is None


def test_resolve_multiplier_uses_cached_value(tmp_path: Path) -> None:
    cache = {
        "0700.HK": {
            "multiplier": 500,
            "source": "test",
        }
    }
    cache_path = tmp_path / "output_shared" / "state" / "multiplier_cache.json"
    save_cache(cache_path, cache)

    assert get_cached_multiplier(cache, "00700.HK") == 500
    assert resolve_multiplier(
        repo_base=tmp_path,
        symbol="00700.HK",
        allow_opend_refresh=False,
    ) == 500
    assert resolve_multiplier_with_source(
        repo_base=tmp_path,
        symbol="00700.HK",
        allow_opend_refresh=False,
    ) == (500, "test")
