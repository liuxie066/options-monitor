from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


OPEND_SOURCE = "opend_account_funds_conversion"
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _local_utc(hour: int, minute: int = 0, *, day: int = 30) -> str:
    return datetime(2026, 9, day, hour, minute, tzinfo=_SHANGHAI).astimezone(timezone.utc).isoformat()


def _pair(rate: float, *, quote_hour: int = 11, day: int = 30, source: str = "tencent_quote") -> dict:
    return {
        "rate": rate, "source": source,
        "quote_at_utc": _local_utc(quote_hour, day=day),
        "observed_at_utc": _local_utc(quote_hour, 1, day=day),
    }


def _write_cache(path: Path, rates: dict, source: str, *, timestamp: str | None = None) -> Path:
    path.write_text(
        json.dumps(
            {
                "rates": rates,
                "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
                "source": source,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_get_rates_or_fetch_latest_uses_verified_cache_when_provider_fails(tmp_path: Path, monkeypatch) -> None:
    from src.infrastructure import exchange_rates

    monkeypatch.setattr(exchange_rates, "_utc_now", lambda: datetime.fromisoformat(_local_utc(12)))
    monkeypatch.setattr(exchange_rates, "fetch_market_exchange_rates", lambda: None)
    cache_path = tmp_path / "rate_cache.json"
    cache_path.write_text(json.dumps({"schema_version": 2, "pairs": {
        "USDCNY": _pair(7.2), "HKDCNY": _pair(0.92),
    }}), encoding="utf-8")

    out = exchange_rates.get_exchange_rates_or_fetch_latest(
        cache_path=cache_path,
        max_age_hours=24,
    )

    assert out is not None
    assert out["rates"] == {"USDCNY": 7.2, "HKDCNY": 0.92}
    assert out["source"] == "tencent_quote"


def test_get_rates_or_fetch_latest_fetches_when_cache_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.infrastructure import exchange_rates

    cache_path = tmp_path / "state" / "rate_cache.json"
    messages: list[str] = []

    monkeypatch.setattr(exchange_rates, "_utc_now", lambda: datetime.fromisoformat(_local_utc(12)))

    def _fake_fetch():
        return {"pairs": {"USDCNY": _pair(6.74), "HKDCNY": _pair(0.86)}}

    monkeypatch.setattr(exchange_rates, "fetch_market_exchange_rates", _fake_fetch)

    out = exchange_rates.get_exchange_rates_or_fetch_latest(
        cache_path=cache_path,
        max_age_hours=24,
        log=messages.append,
    )

    assert out is not None
    assert out["rates"] == {"USDCNY": 6.74, "HKDCNY": 0.86}
    assert json.loads(cache_path.read_text(encoding="utf-8"))["schema_version"] == 2


def test_get_rates_or_fetch_latest_rejects_unverified_legacy_cache(tmp_path: Path, monkeypatch) -> None:
    from src.infrastructure import exchange_rates

    cache_path = tmp_path / "state" / "rate_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path = _write_cache(
        cache_path,
        {"USDCNY": 7.28, "HKDCNY": 0.94},
        "tencent_quote",
        timestamp=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(),
    )
    messages: list[str] = []
    monkeypatch.setattr(exchange_rates, "fetch_market_exchange_rates", lambda: None)

    out = exchange_rates.get_exchange_rates_or_fetch_latest(cache_path=cache_path, max_age_hours=24, log=messages.append)

    assert out is None


def test_exchange_rate_observation_without_timestamp_is_stale() -> None:
    from src.infrastructure.exchange_rates import exchange_rate_observation_status

    assert (
        exchange_rate_observation_status(
            {
                "rates": {"USDCNY": 7.2, "HKDCNY": 0.92},
                "source": "opend_account_funds_conversion",
            },
            max_age_hours=24,
        )
        == "unavailable"
    )


def test_load_exchange_rate_info_rejects_cache_without_pair_times(tmp_path: Path) -> None:
    from src.infrastructure.exchange_rates import load_exchange_rate_info

    cache_path = _write_cache(tmp_path / "rate_cache.json", {"USDCNY": 7.21}, OPEND_SOURCE)

    out = load_exchange_rate_info(cache_path=cache_path, fetch_latest_on_miss=False)

    assert out is None


def test_exchange_rate_cache_rejects_non_opend_source(tmp_path: Path) -> None:
    from src.infrastructure.exchange_rates import get_cached_exchange_rates

    cache_path = _write_cache(tmp_path / "rate_cache.json", {"USDCNY": 7.21, "HKDCNY": 0.92}, "legacy_provider")

    assert (
        get_cached_exchange_rates(cache_path=cache_path, max_age_hours=24)
        is None
    )


@pytest.mark.parametrize(
    "invalid_rate",
    [-7.0, float("nan"), float("inf"), "bad", True, False],
)
def test_exchange_rate_boundaries_reject_invalid_present_rate(
    tmp_path: Path, invalid_rate: object
) -> None:
    from src.infrastructure.exchange_rates import (
        exchange_rate_observation_status,
        get_cached_exchange_rates,
    )

    payload = {
        "rates": {"USDCNY": invalid_rate},
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": OPEND_SOURCE,
    }
    cache_path = tmp_path / "rate_cache.json"
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    assert exchange_rate_observation_status(payload, max_age_hours=24) == "unavailable"
    assert get_cached_exchange_rates(cache_path=cache_path, max_age_hours=24) is None

def test_get_usd_per_cny_uses_shared_state_cache(tmp_path: Path, monkeypatch) -> None:
    from src.infrastructure import exchange_rates

    calls: list[Path] = []

    def _fake_rates(*, cache_path: Path, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(Path(cache_path))
        return {"rates": {"USDCNY": 7.25}}

    monkeypatch.setattr(exchange_rates, "get_exchange_rates_or_fetch_latest", _fake_rates)

    out = exchange_rates.get_usd_per_cny_exchange_rate(tmp_path)

    assert out == 1.0 / 7.25
    assert calls == [(tmp_path / "output_shared" / "state" / "rate_cache.json").resolve()]


def test_parse_tencent_response() -> None:
    from src.infrastructure.exchange_rates import _parse_tencent

    text = (
        'v_whUSDCNY="310~名称~USDCNY~6.7390~0~20260817151006~6.7421~";\n'
        'v_whHKDCNY="310~名称~HKDCNY~0.8586~0~20260817151012~0.8589~";\n'
    )
    rates = _parse_tencent(text)
    assert rates == {"USDCNY": 6.739, "HKDCNY": 0.8586}


def test_parse_sina_response() -> None:
    from src.infrastructure.exchange_rates import _parse_sina

    usd = ["15:10:06", "6.7382", *(["0"] * 15), "2026-08-17"]
    hkd = ["15:10:12", "0.8586", *(["0"] * 15), "2026-08-17"]
    text = (
        f'var hq_str_fx_susdcny="{",".join(usd)}";\n'
        f'var hq_str_fx_shkdcny="{",".join(hkd)}";\n'
    )
    rates = _parse_sina(text)
    assert rates["USDCNY"] == 6.7382
    assert rates["HKDCNY"] == 0.8586


def test_parse_rejects_missing_currency() -> None:
    from src.infrastructure.exchange_rates import _parse_tencent

    text = 'v_whUSDCNY="310~名称~USDCNY~6.7390~0";\n'
    assert _parse_tencent(text) is None
