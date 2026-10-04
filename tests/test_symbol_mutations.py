from __future__ import annotations

import pytest

from src.application.account_config import normalize_accounts
from src.application.symbol_calibration import calibrate_symbol, require_calibrated_symbol
from src.application.symbol_mutations import add_symbol_entry, find_symbol_entry, normalize_symbol


def test_symbol_calibration_accepts_common_user_inputs() -> None:
    cases = [
        ("700", "0700.HK"),
        ("HK.00700", "0700.HK"),
        ("腾讯", "0700.HK"),
        ("POP", "9992.HK"),
        ("nvda", "NVDA"),
    ]

    for raw, canonical in cases:
        result = calibrate_symbol(raw)
        assert result.status == "ok"
        assert result.canonical_symbol == canonical
    assert calibrate_symbol("700").source_kind == "hk_numeric"


def test_symbol_calibration_rejects_unknown_display_symbol() -> None:
    with pytest.raises(ValueError):
        require_calibrated_symbol("不存在的标的")


def test_normalize_symbol_canonicalizes_alias() -> None:
    assert normalize_symbol("POP") == "9992.HK"


def test_find_symbol_entry_matches_alias_against_canonical_symbol() -> None:
    cfg = {"symbols": [{"symbol": "9992.HK"}]}

    idx, found = find_symbol_entry(
        cfg,
        "POP",
        resolve_watchlist_config=lambda data: data.get("symbols") or [],
    )

    assert idx == 0
    assert found == {"symbol": "9992.HK"}


def test_symbol_add_normalizes_accounts_as_labels() -> None:
    cfg = {"symbols": []}

    add_symbol_entry(
        cfg, symbol="NVDA", use="put_base", limit_expirations=8,
        sell_put_enabled=True, sell_call_enabled=False,
        accounts=[" LX ", "sy", "lx"],
        normalize_accounts=lambda value: normalize_accounts(value, fallback=()),
    )

    assert cfg["symbols"][0]["symbol"] == "NVDA"
    assert cfg["symbols"][0]["accounts"] == ["lx", "sy"]


def test_add_symbol_entry_defaults_use_from_enabled_sides() -> None:
    cfg = {"symbols": []}

    add_symbol_entry(cfg, symbol="NVDA", sell_put_enabled=True, sell_call_enabled=True)

    assert cfg["symbols"][0]["use"] == ["put_base", "call_base"]
