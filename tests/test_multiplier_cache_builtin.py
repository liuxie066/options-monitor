from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType

from scripts.multiplier_cache import (
    get_cached_multiplier,
    load_cache,
    merge_cache_updates,
    normalize_symbol,
    refresh_via_opend,
    resolve_multiplier,
    resolve_multiplier_with_source_and_diagnostics,
    resolve_multiplier_with_source,
    save_cache,
)


def test_normalize_hk_symbol_to_four_digit_suffix() -> None:
    assert normalize_symbol("00700.HK") == "0700.HK"
    assert normalize_symbol("700.HK") == "0700.HK"
    assert normalize_symbol("NVDA") == "NVDA"
    assert normalize_symbol("POP") == "9992.HK"


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


def test_resolve_multiplier_uses_repo_market_config_when_active_config_lacks_intake(tmp_path: Path) -> None:
    (tmp_path / "config.hk.json").write_text(
        json.dumps(
            {
                "intake": {
                    "default_multiplier_hk": 1000,
                }
            }
        ),
        encoding="utf-8",
    )

    value, source, diagnostics = resolve_multiplier_with_source_and_diagnostics(
        repo_base=tmp_path,
        symbol="9992.HK",
        config={"trade_intake": {"mode": "dry-run"}},
        allow_opend_refresh=False,
    )

    assert value == 1000
    assert source == "config-file:config.hk.json:intake.default_multiplier_hk"
    assert diagnostics["selected_source"] == source
    assert any(
        item["source"] == "config:intake.default_multiplier_hk" and item["status"] == "missing_or_invalid"
        for item in diagnostics["attempted_sources"]
    )


def test_merge_cache_updates_preserves_existing_entries(tmp_path: Path) -> None:
    cache_path = tmp_path / "output_shared" / "state" / "multiplier_cache.json"
    save_cache(
        cache_path,
        {
            "0700.HK": {
                "multiplier": 500,
                "source": "existing",
            }
        },
    )

    merge_cache_updates(
        cache_path,
        {
            "3690.HK": {
                "multiplier": 500,
                "source": "opend",
            }
        },
    )

    cache = load_cache(cache_path)
    assert cache["0700.HK"]["source"] == "existing"
    assert cache["3690.HK"]["source"] == "opend"
    assert cache_path.with_suffix(".json.lock").exists()


def test_refresh_via_opend_forwards_opend_fetch_config(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    fake_module = ModuleType("src.application.opend_symbol_fetching")

    class _FetchSymbolRequest:
        def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
            self.__dict__.update(kwargs)

    def _fake_fetch_symbol_request(request):  # type: ignore[no-untyped-def]
        captured["symbol"] = request.symbol
        captured.update(request.__dict__)
        return {"rows": [{"multiplier": 500}]}

    fake_module.FetchSymbolRequest = _FetchSymbolRequest  # type: ignore[attr-defined]
    fake_module.fetch_symbol_request = _fake_fetch_symbol_request  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.application.opend_symbol_fetching", fake_module)

    result = refresh_via_opend(
        repo_base=tmp_path,
        symbol="00700.HK",
        host="127.0.0.1",
        port=11111,
        limit_expirations=1,
        opend_fetch_config={
            "max_wait_sec": 11,
            "option_chain_window_sec": 12,
            "option_chain_max_calls": 13,
            "snapshot_max_wait_sec": 21,
            "snapshot_window_sec": 22,
            "snapshot_max_calls": 23,
            "expiration_max_wait_sec": 31,
            "expiration_window_sec": 32,
            "expiration_max_calls": 33,
        },
    )

    assert result.ok is True
    assert captured["symbol"] == "0700.HK"
    assert captured["base_dir"] == tmp_path
    assert captured["max_wait_sec"] == 11
    assert captured["option_chain_window_sec"] == 12
    assert captured["option_chain_max_calls"] == 13
    assert captured["snapshot_max_wait_sec"] == 21
    assert captured["snapshot_window_sec"] == 22
    assert captured["snapshot_max_calls"] == 23
    assert captured["expiration_max_wait_sec"] == 31
    assert captured["expiration_window_sec"] == 32
    assert captured["expiration_max_calls"] == 33
