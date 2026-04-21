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
        "_default_pm_rate_cache_path",
        lambda: tmp_path / "portfolio-management" / ".data" / "rate_cache.json",
    )

    monkeypatch.setattr(
        fx_rates,
        "_fetch_latest_rates_from_portfolio_management",
        lambda log=None: {"rates": {"USDCNY": 7.3, "HKDCNY": 0.93}, "timestamp": "2026-04-21T00:00:00+00:00"},
    )

    out = fx_rates.get_rates_or_fetch_latest(cache_path=cache_path)

    assert out == {"rates": {"USDCNY": 7.3, "HKDCNY": 0.93}, "timestamp": "2026-04-21T00:00:00+00:00"}
    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    assert saved["rates"] == {"USDCNY": 7.3, "HKDCNY": 0.93}


def test_get_rates_or_fetch_latest_reads_pm_shared_cache_when_local_cache_missing(tmp_path: Path, monkeypatch) -> None:
    from scripts import fx_rates

    cache_path = tmp_path / "state" / "rate_cache.json"
    pm_cache_path = tmp_path / "portfolio-management" / ".data" / "rate_cache.json"
    pm_cache_path.parent.mkdir(parents=True, exist_ok=True)
    pm_cache_path.write_text(
        json.dumps({"rates": {"USDCNY": 7.25, "HKDCNY": 0.94}}, ensure_ascii=False),
        encoding="utf-8",
    )

    monkeypatch.setattr(fx_rates, "_default_pm_rate_cache_path", lambda: pm_cache_path)
    monkeypatch.setattr(
        fx_rates,
        "_fetch_latest_rates_from_portfolio_management",
        lambda log=None: (_ for _ in ()).throw(AssertionError("should not fetch when pm cache exists")),
    )

    out = fx_rates.get_rates_or_fetch_latest(cache_path=cache_path)

    assert out == {"rates": {"USDCNY": 7.25, "HKDCNY": 0.94}}


def test_get_rates_or_fetch_latest_logs_when_external_repo_missing(tmp_path: Path, monkeypatch) -> None:
    from scripts import fx_rates

    cache_path = tmp_path / "state" / "rate_cache.json"
    messages: list[str] = []
    monkeypatch.setattr(
        fx_rates,
        "_default_pm_rate_cache_path",
        lambda: tmp_path / "portfolio-management" / ".data" / "rate_cache.json",
    )

    monkeypatch.setattr(fx_rates, "_fetch_latest_rates_from_portfolio_management", lambda log=None: None)

    out = fx_rates.get_rates_or_fetch_latest(cache_path=cache_path, log=messages.append)

    assert out is None
    assert any("fx cache miss" in msg for msg in messages)
    assert any("fx latest fetch unavailable" in msg for msg in messages)


def test_fetch_latest_rates_from_portfolio_management_logs_interface_change(monkeypatch) -> None:
    from scripts import fx_rates

    messages: list[str] = []

    monkeypatch.setattr(fx_rates.Path, "exists", lambda self: True)
    monkeypatch.setattr("importlib.import_module", lambda _name: type("FxModule", (), {"NotPriceFetcher": object})())

    out = fx_rates._fetch_latest_rates_from_portfolio_management(log=messages.append)

    assert out is None
    assert any("PriceFetcher missing" in msg for msg in messages)


def test_fetch_latest_rates_from_portfolio_management_imports_package_module(monkeypatch) -> None:
    from scripts import fx_rates

    seen: list[str] = []

    monkeypatch.setattr(fx_rates.Path, "exists", lambda self: True)

    class _Fetcher:
        def __init__(self, storage=None, use_cache=False):
            assert storage is None
            assert use_cache is False

        @staticmethod
        def _fetch_exchange_rates():
            return {"USDCNY": 7.31, "HKDCNY": 0.93}

    def _import_module(name: str):
        seen.append(name)
        return type("FxModule", (), {"PriceFetcher": _Fetcher})()

    monkeypatch.setattr("importlib.import_module", _import_module)

    out = fx_rates._fetch_latest_rates_from_portfolio_management()

    assert seen == ["src.price_fetcher"]
    assert out is not None
    assert out["rates"] == {"USDCNY": 7.31, "HKDCNY": 0.93}


def test_load_fx_info_can_read_cache_without_fetch(tmp_path: Path) -> None:
    from scripts.fx_rates import load_fx_info

    cache_path = tmp_path / "rate_cache.json"
    cache_path.write_text(
        json.dumps({"rates": {"USDCNY": 7.21}, "timestamp": "2026-04-21T00:00:00+00:00"}, ensure_ascii=False),
        encoding="utf-8",
    )

    out = load_fx_info(cache_path=cache_path, fetch_latest_on_miss=False)

    assert out == {"rates": {"USDCNY": 7.21}, "timestamp": "2026-04-21T00:00:00+00:00"}
