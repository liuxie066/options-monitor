from __future__ import annotations

import json
from pathlib import Path


def test_get_rates_or_fetch_latest_prefers_cache(tmp_path: Path) -> None:
    from scripts.fx_rates import get_rates_or_fetch_latest

    cache_path = tmp_path / "rate_cache.json"
    cache_path.write_text(
        json.dumps({"rates": {"USDCNY": 7.2, "HKDCNY": 0.92}}, ensure_ascii=False),
        encoding="utf-8",
    )

    out = get_rates_or_fetch_latest(cache_path=cache_path)

    assert out == {"rates": {"USDCNY": 7.2, "HKDCNY": 0.92}}


def test_get_rates_or_fetch_latest_fetches_and_writes_when_cache_missing(tmp_path: Path, monkeypatch) -> None:
    from scripts import fx_rates

    cache_path = tmp_path / "state" / "rate_cache.json"

    monkeypatch.setattr(
        fx_rates,
        "_fetch_latest_rates_from_portfolio_management",
        lambda: {"rates": {"USDCNY": 7.3, "HKDCNY": 0.93}, "timestamp": "2026-04-21T00:00:00+00:00"},
    )

    out = fx_rates.get_rates_or_fetch_latest(cache_path=cache_path)

    assert out == {"rates": {"USDCNY": 7.3, "HKDCNY": 0.93}, "timestamp": "2026-04-21T00:00:00+00:00"}
    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    assert saved["rates"] == {"USDCNY": 7.3, "HKDCNY": 0.93}
