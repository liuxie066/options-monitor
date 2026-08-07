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


def test_get_rates_or_fetch_latest_does_not_fetch_when_cache_missing(
    tmp_path: Path,
) -> None:
    from src.infrastructure import exchange_rates

    cache_path = tmp_path / "state" / "rate_cache.json"
    messages: list[str] = []

    out = exchange_rates.get_exchange_rates_or_fetch_latest(
        cache_path=cache_path,
        max_age_hours=24,
        log=messages.append,
    )

    assert out is None
    assert not cache_path.exists()
    assert any("OpenD exchange_rate observation missing/stale" in msg for msg in messages)


def test_get_rates_or_fetch_latest_rejects_stale_cache(tmp_path: Path) -> None:
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
                "source": "opend_account_funds_conversion",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    messages: list[str] = []

    out = exchange_rates.get_exchange_rates_or_fetch_latest(cache_path=cache_path, max_age_hours=24, log=messages.append)

    assert out is None
    assert any("OpenD exchange_rate observation missing/stale" in msg for msg in messages)


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
