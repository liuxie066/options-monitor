from __future__ import annotations

from src.application.watchlist_mutations import find_symbol_entry, normalize_symbol


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
