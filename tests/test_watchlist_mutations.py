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


def test_watchlist_cli_add_normalizes_accounts_as_labels() -> None:
    from scripts.watchlist import cmd_add

    cfg = {"symbols": []}

    cmd_add(cfg, "NVDA", "put_base", 8, True, False, accounts=[" LX ", "sy", "lx"])

    assert cfg["symbols"][0]["symbol"] == "NVDA"
    assert cfg["symbols"][0]["accounts"] == ["lx", "sy"]
