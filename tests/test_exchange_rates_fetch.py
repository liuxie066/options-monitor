from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def test_get_rates_or_fetch_latest_prefers_cache(tmp_path: Path) -> None:
    from src.infrastructure.exchange_rates import get_exchange_rates_or_fetch_latest

    cache_path = tmp_path / "rate_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "rates": {"USDCNY": 7.2, "HKDCNY": 0.92},
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "opend_account_funds_conversion",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    out = get_exchange_rates_or_fetch_latest(
        cache_path=cache_path,
        max_age_hours=24,
    )

    assert out is not None
    assert out["rates"] == {"USDCNY": 7.2, "HKDCNY": 0.92}
    assert out["source"] == "opend_account_funds_conversion"


def test_get_rates_or_fetch_latest_fetches_when_cache_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.infrastructure import exchange_rates

    cache_path = tmp_path / "state" / "rate_cache.json"
    messages: list[str] = []

    def _fake_fetch():
        return {
            "source": "tencent_quote",
            "rates": {"USDCNY": 6.74, "HKDCNY": 0.86},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    monkeypatch.setattr(exchange_rates, "fetch_market_exchange_rates", _fake_fetch)

    out = exchange_rates.get_exchange_rates_or_fetch_latest(
        cache_path=cache_path,
        max_age_hours=24,
        log=messages.append,
    )

    assert out is not None
    assert out["rates"] == {"USDCNY": 6.74, "HKDCNY": 0.86}
    assert cache_path.exists()


def test_get_rates_or_fetch_latest_falls_back_to_stale_cache(tmp_path: Path, monkeypatch) -> None:
    from src.infrastructure import exchange_rates

    cache_path = tmp_path / "state" / "rate_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "rates": {"USDCNY": 7.28, "HKDCNY": 0.94},
                "timestamp": (
                    datetime.now(timezone.utc) - timedelta(hours=25)
                ).isoformat(),
                "source": "tencent_quote",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    messages: list[str] = []
    monkeypatch.setattr(exchange_rates, "fetch_market_exchange_rates", lambda: None)

    out = exchange_rates.get_exchange_rates_or_fetch_latest(cache_path=cache_path, max_age_hours=24, log=messages.append)

    assert out is not None
    assert out["rates"] == {"USDCNY": 7.28, "HKDCNY": 0.94}
    assert any("stale cache" in msg for msg in messages)


def test_save_exchange_rate_observation_preserves_provider_timestamp(
    tmp_path: Path,
) -> None:
    from src.infrastructure.exchange_rates import save_exchange_rate_observation

    cache_path = tmp_path / "rate_cache.json"
    observed_at = "2026-08-06T01:02:03+00:00"
    save_exchange_rate_observation(
        cache_path,
        {
            "rates": {"USDCNY": 7.2, "HKDCNY": 0.92},
            "timestamp": observed_at,
            "source": "opend_account_funds_conversion",
        },
    )

    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    assert saved["timestamp"] == observed_at
    assert saved["source"] == "opend_account_funds_conversion"


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
        == "unavailable_stale"
    )


def test_load_exchange_rate_info_can_read_cache_without_fetch(tmp_path: Path) -> None:
    from src.infrastructure.exchange_rates import load_exchange_rate_info

    cache_path = tmp_path / "rate_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "rates": {"USDCNY": 7.21},
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "opend_account_funds_conversion",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    out = load_exchange_rate_info(cache_path=cache_path, fetch_latest_on_miss=False)

    assert out is not None
    assert out["rates"] == {"USDCNY": 7.21}
    assert out["source"] == "opend_account_funds_conversion"


def test_exchange_rate_cache_rejects_non_opend_source(tmp_path: Path) -> None:
    from src.infrastructure.exchange_rates import get_cached_exchange_rates

    cache_path = tmp_path / "rate_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "rates": {"USDCNY": 7.21, "HKDCNY": 0.92},
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "legacy_provider",
            }
        ),
        encoding="utf-8",
    )

    assert (
        get_cached_exchange_rates(cache_path=cache_path, max_age_hours=24)
        is None
    )


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

    text = (
        'var hq_str_fx_susdcny="6.7382,6.7392,6.7322,150";\n'
        'var hq_str_fx_shkdcny="0.8586,0.8593,0.8593,10";\n'
    )
    rates = _parse_sina(text)
    assert rates["USDCNY"] == 6.7382
    assert rates["HKDCNY"] == 0.8586


def test_parse_rejects_missing_currency() -> None:
    from src.infrastructure.exchange_rates import _parse_tencent

    text = 'v_whUSDCNY="310~名称~USDCNY~6.7390~0";\n'
    assert _parse_tencent(text) is None
