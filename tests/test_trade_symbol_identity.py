from __future__ import annotations

from scripts.trade_symbol_identity import normalize_symbol_candidate


def test_normalize_symbol_candidate_parses_futu_hk_option_display_name() -> None:
    assert normalize_symbol_candidate("泡泡玛特 260528 135.00 沽") == "9992.HK"


def test_normalize_symbol_candidate_rejects_unrecognized_display_name() -> None:
    assert normalize_symbol_candidate("未知标的 260528 135.00 沽") is None
