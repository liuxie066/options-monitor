from __future__ import annotations

import json
from pathlib import Path

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
    from src.interfaces.cli.watchlist import cmd_add

    cfg = {"symbols": []}

    cmd_add(cfg, "NVDA", "put_base", 8, True, False, accounts=[" LX ", "sy", "lx"])

    assert cfg["symbols"][0]["symbol"] == "NVDA"
    assert cfg["symbols"][0]["accounts"] == ["lx", "sy"]


def test_watchlist_cli_list_reads_config_path(tmp_path: Path, capsys) -> None:
    from src.interfaces.cli import watchlist as watchlist_cli

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "symbols": [
                    {
                        "symbol": "NVDA",
                        "accounts": ["lx"],
                        "sell_put": {"enabled": True, "max_strike": 120},
                        "sell_call": {"enabled": False},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert watchlist_cli.main(["--config", str(cfg_path), "list"]) == 0

    out = capsys.readouterr().out
    assert "# options-monitor symbols" in out
    assert "NVDA" in out
