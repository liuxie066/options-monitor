from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from zoneinfo import ZoneInfo

from src.infrastructure import exchange_rates as fx


_CN = ZoneInfo("Asia/Shanghai")


def _at(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=_CN).astimezone(timezone.utc)


def _row(rate: float, quoted: datetime, *, source: str = "tencent_quote") -> dict:
    return {
        "rate": rate,
        "source": source,
        "quote_at_utc": quoted.isoformat(),
        "observed_at_utc": quoted.isoformat(),
    }


def _cache(path: Path, **pairs: dict) -> None:
    path.write_text(json.dumps({"schema_version": 2, "pairs": pairs}), encoding="utf-8")


def _sina(pair: str, price: str, quote: datetime) -> str:
    local = quote.astimezone(_CN)
    fields = [local.strftime("%H:%M:%S"), price, *(["0"] * 15), local.strftime("%Y-%m-%d")]
    return f'var hq_str_fx_s{pair.lower()}="{",".join(fields)}";'


def test_provider_selection_keeps_newest_verified_quote_per_pair(monkeypatch) -> None:
    monkeypatch.setattr(fx, "_utc_now", lambda: _at(9, 30, 12))
    tencent = (
        'v_whUSDCNY="310~名称~USDCNY~7.20~0~20260930110000~";\n'
        'v_whHKDCNY="310~名称~HKDCNY~0.90~0~20260930103000~";'
    )
    sina = "\n".join((
        _sina("USDCNY", "7.19", _at(9, 30, 10)),
        _sina("HKDCNY", "0.92", _at(9, 30, 11, 15)),
    ))
    monkeypatch.setattr(fx, "_http_get", lambda url, **_kwargs: tencent if "gtimg" in url else sina)

    result = fx.fetch_market_exchange_rates()

    assert result is not None
    assert result["source"] == "mixed"
    assert result["pairs"]["USDCNY"]["rate"] == 7.2
    assert result["pairs"]["USDCNY"]["source"] == "tencent_quote"
    assert result["pairs"]["HKDCNY"]["rate"] == 0.92
    assert result["pairs"]["HKDCNY"]["source"] == "sina_quote"


def test_provider_rejects_malformed_and_future_pair_without_losing_other_pair(monkeypatch) -> None:
    monkeypatch.setattr(fx, "_utc_now", lambda: _at(9, 30, 12))
    tencent = (
        'v_whUSDCNY="310~名称~USDCNY~nan~0~20260930110000~";\n'
        'v_whHKDCNY="310~名称~HKDCNY~0.91~0~20260930110000~";'
    )
    sina = "\n".join((
        _sina("USDCNY", "7.20", _at(9, 30, 11)),
        _sina("HKDCNY", "0.92", _at(9, 30, 13)),
    ))
    monkeypatch.setattr(fx, "_http_get", lambda url, **_kwargs: tencent if "gtimg" in url else sina)

    result = fx.fetch_market_exchange_rates()

    assert result is not None
    assert result["pairs"]["USDCNY"]["source"] == "sina_quote"
    assert result["pairs"]["HKDCNY"]["source"] == "tencent_quote"


def test_holiday_carry_preserves_source_time_and_allows_capacity(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "rate_cache.json"
    quoted = _at(9, 30, 14)
    _cache(path, USDCNY=_row(7.2, quoted), HKDCNY=_row(0.92, quoted))
    monkeypatch.setattr(fx, "fetch_market_exchange_rates", lambda: None)
    now = _at(10, 2, 9, 43)

    result = fx.current_exchange_rate_snapshot(cache_path=path, now=now)

    assert {row["quality"] for row in result["pairs"].values()} == {"holiday_carried"}
    assert fx.rates_for_purpose(result, purpose="display", now=now) == {"USDCNY": 7.2, "HKDCNY": 0.92}
    assert fx.rates_for_purpose(result, purpose="capacity", now=now) == {"USDCNY": 7.2, "HKDCNY": 0.92}
    assert all(row["capacity_eligible"] for row in result["pairs"].values())
    monkeypatch.setattr(fx, "_utc_now", lambda: now)
    assert fx.exchange_rate_observation_status(result) == "ready"
    assert fx.project_exchange_rate_snapshot(result, purpose="capacity", now=now)["rates"] == result["rates"]
    assert result["pairs"]["USDCNY"]["quote_at_utc"] == quoted.isoformat()
    assert json.loads(path.read_text(encoding="utf-8"))["pairs"]["USDCNY"]["quote_at_utc"] == quoted.isoformat()


def test_regular_overnight_stays_fresh_until_next_session_opens(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "rate_cache.json"
    _cache(path, USDCNY=_row(7.2, _at(9, 28, 22)))
    monkeypatch.setattr(fx, "fetch_market_exchange_rates", lambda: None)

    for hour, minute in ((3, 30), (9, 29)):
        result = fx.current_exchange_rate_snapshot(cache_path=path, now=_at(9, 29, hour, minute), write_cache=False)
        assert result["pairs"]["USDCNY"]["quality"] == "fresh"
    opened = fx.current_exchange_rate_snapshot(cache_path=path, now=_at(9, 29, 9, 30), write_cache=False)
    assert opened["pairs"]["USDCNY"]["quality"] == "unavailable"
    assert opened["pairs"]["USDCNY"]["reason"] == "trading_session_gap"


def test_friday_night_session_and_weekend_boundary(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "rate_cache.json"
    _cache(path, USDCNY=_row(7.2, _at(9, 18, 23)))
    monkeypatch.setattr(fx, "fetch_market_exchange_rates", lambda: None)

    expected = (
        (_at(9, 19, 2, 30), "fresh"),
        (_at(9, 19, 3, 30), "holiday_carried"),
        (_at(9, 21, 9, 29), "holiday_carried"),
        (_at(9, 21, 9, 30), "unavailable"),
    )
    for now, quality in expected:
        result = fx.current_exchange_rate_snapshot(cache_path=path, now=now, write_cache=False)
        assert result["pairs"]["USDCNY"]["quality"] == quality


def test_missing_trading_session_or_unknown_calendar_cannot_carry(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "rate_cache.json"
    _cache(path, USDCNY=_row(7.2, _at(9, 28, 11)))
    monkeypatch.setattr(fx, "fetch_market_exchange_rates", lambda: None)

    gap = fx.current_exchange_rate_snapshot(cache_path=path, now=_at(9, 30, 9, 29), write_cache=False)
    unknown = fx.current_exchange_rate_snapshot(
        cache_path=path, now=datetime(2027, 1, 1, tzinfo=timezone.utc), write_cache=False,
    )
    assert gap["pairs"]["USDCNY"]["quality"] == "unavailable"
    assert unknown["pairs"]["USDCNY"]["quality"] == "unavailable"


def test_foreign_currency_holiday_does_not_close_cny_market(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "rate_cache.json"
    _cache(path, USDCNY=_row(7.2, _at(9, 4, 22)))
    monkeypatch.setattr(fx, "fetch_market_exchange_rates", lambda: None)

    # US Labor Day is 2026-09-07, but the RMB FX session opens as usual.
    result = fx.current_exchange_rate_snapshot(cache_path=path, now=_at(9, 7, 9, 43), write_cache=False)
    assert result["pairs"]["USDCNY"]["quality"] == "unavailable"
    assert result["pairs"]["USDCNY"]["reason"] == "trading_session_gap"


def test_legacy_cache_requires_pair_times_and_newer_quote_wins(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "rate_cache.json"
    original = _at(9, 30, 11)
    path.write_text(json.dumps({
        "source": "tencent_quote",
        "rates": {"USDCNY": 7.2},
        "timestamp": original.isoformat(),
        "quote_timestamps": {"USDCNY": original.isoformat()},
        "observed_at": original.isoformat(),
    }), encoding="utf-8")
    monkeypatch.setattr(fx, "fetch_market_exchange_rates", lambda: {
        "pairs": {
            "USDCNY": _row(7.1, _at(9, 30, 10)),
            "HKDCNY": _row(0.92, _at(9, 30, 11)),
        },
    })

    result = fx.current_exchange_rate_snapshot(cache_path=path, now=_at(9, 30, 12))

    assert result["pairs"]["USDCNY"]["rate"] == 7.2
    assert result["pairs"]["HKDCNY"]["rate"] == 0.92
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 2
    assert saved["pairs"]["USDCNY"]["quote_at_utc"] == original.isoformat()


def test_delayed_capacity_rechecks_sealed_quote_time(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "rate_cache.json"
    _cache(path, USDCNY=_row(7.2, _at(9, 30, 11)))
    monkeypatch.setattr(fx, "fetch_market_exchange_rates", lambda: None)
    snapshot = fx.current_exchange_rate_snapshot(cache_path=path, now=_at(9, 30, 12), write_cache=False)

    assert fx.rates_for_purpose(snapshot, purpose="capacity", now=_at(9, 30, 12)) == {"USDCNY": 7.2}
    assert fx.rates_for_purpose(snapshot, purpose="capacity", now=_at(10, 2, 12)) == {"USDCNY": 7.2}
    assert fx.rates_for_purpose(snapshot, purpose="capacity", now=_at(10, 8, 9, 30)) == {}
    assert fx.rates_for_purpose(snapshot, purpose="display", now=_at(10, 2, 12)) == {"USDCNY": 7.2}


def test_concurrent_pair_updates_do_not_erase_each_other(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "rate_cache.json"
    now = _at(9, 30, 12)
    local = threading.local()
    barrier = threading.Barrier(2)

    def fetch() -> dict:
        pair = local.pair
        barrier.wait()
        return {"pairs": {pair: _row(7.2 if pair == "USDCNY" else 0.92, _at(9, 30, 11))}}

    monkeypatch.setattr(fx, "fetch_market_exchange_rates", fetch)

    def run(pair: str) -> None:
        local.pair = pair
        fx.current_exchange_rate_snapshot(cache_path=path, now=now)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(run, ("USDCNY", "HKDCNY")))

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert set(saved["pairs"]) == {"USDCNY", "HKDCNY"}
