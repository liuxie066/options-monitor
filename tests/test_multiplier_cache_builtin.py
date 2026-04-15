from __future__ import annotations

from scripts.multiplier_cache import get_builtin_multiplier, get_cached_multiplier, get_multiplier, normalize_symbol


def test_normalize_hk_symbol_to_four_digit_suffix() -> None:
    assert normalize_symbol("00700.HK") == "0700.HK"
    assert normalize_symbol("700.HK") == "0700.HK"
    assert normalize_symbol("NVDA") == "NVDA"


def test_builtin_multiplier_for_common_hk_symbols() -> None:
    assert get_builtin_multiplier("0700.HK") == 100
    assert get_builtin_multiplier("00700.HK") == 100
    assert get_builtin_multiplier("0883.HK") == 1000
    assert get_builtin_multiplier("9992.HK") == 200


def test_builtin_multiplier_for_us_symbols_defaults_to_100() -> None:
    assert get_builtin_multiplier("NVDA") == 100


def test_cached_multiplier_precedes_builtin() -> None:
    cache = {
        "0700.HK": {
            "multiplier": 500,
            "source": "test",
        }
    }

    assert get_cached_multiplier(cache, "00700.HK") == 500
    assert get_multiplier(cache, "00700.HK") == 500
