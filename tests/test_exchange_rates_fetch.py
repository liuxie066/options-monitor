from __future__ import annotations

import json
from pathlib import Path


def test_get_rates_or_fetch_latest_prefers_cache(tmp_path: Path) -> None:
    from scripts.exchange_rates import get_exchange_rates_or_fetch_latest

    cache_path = tmp_path / "rate_cache.json"
    cache_path.write_text(
        json.dumps({"rates": {"USDCNY": 7.2, "HKDCNY": 0.92}}, ensure_ascii=False),
        encoding="utf-8",
    )

    out = get_exchange_rates_or_fetch_latest(cache_path=cache_path)

    assert out == {"rates": {"USDCNY": 7.2, "HKDCNY": 0.92}}


def test_get_rates_or_fetch_latest_fetches_sina_when_cache_missing(tmp_path: Path, monkeypatch) -> None:
    from scripts import exchange_rates
    cache_path = tmp_path / "state" / "rate_cache.json"
    monkeypatch.setattr(
        exchange_rates,
        "fetch_latest_exchange_rates",
        lambda log=None: {"rates": {"USDCNY": 7.3, "HKDCNY": 0.93}, "timestamp": "2026-04-24T00:00:00+00:00", "source": "sina_fx"},
    )
    out = exchange_rates.get_exchange_rates_or_fetch_latest(cache_path=cache_path)

    assert out is not None
    assert out["rates"] == {"USDCNY": 7.3, "HKDCNY": 0.93}
    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    assert saved["rates"] == {"USDCNY": 7.3, "HKDCNY": 0.93}


def test_get_rates_or_fetch_latest_falls_back_to_stale_cache_when_live_fetch_fails(tmp_path: Path, monkeypatch) -> None:
    from scripts import exchange_rates

    cache_path = tmp_path / "state" / "rate_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"rates": {"USDCNY": 7.28, "HKDCNY": 0.94}, "timestamp": "2026-04-20T00:00:00+00:00"}, ensure_ascii=False),
        encoding="utf-8",
    )
    messages: list[str] = []
    monkeypatch.setattr(exchange_rates, "fetch_latest_exchange_rates", lambda log=None: None)

    out = exchange_rates.get_exchange_rates_or_fetch_latest(cache_path=cache_path, max_age_hours=24, log=messages.append)

    assert out is not None
    assert out["rates"] == {"USDCNY": 7.28, "HKDCNY": 0.94}
    assert any("fallback to stale cache" in msg for msg in messages)


def test_load_exchange_rate_info_can_read_cache_without_fetch(tmp_path: Path) -> None:
    from scripts.exchange_rates import load_exchange_rate_info

    cache_path = tmp_path / "rate_cache.json"
    cache_path.write_text(
        json.dumps({"rates": {"USDCNY": 7.21}, "timestamp": "2026-04-21T00:00:00+00:00"}, ensure_ascii=False),
        encoding="utf-8",
    )

    out = load_exchange_rate_info(cache_path=cache_path, fetch_latest_on_miss=False)

    assert out == {"rates": {"USDCNY": 7.21}, "timestamp": "2026-04-21T00:00:00+00:00"}
