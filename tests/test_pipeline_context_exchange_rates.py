from __future__ import annotations

from pathlib import Path


def test_load_exchange_rates_fetches_latest_when_cache_missing(monkeypatch, tmp_path: Path) -> None:
    from src.application import pipeline_context as ctx
    from src.infrastructure import exchange_rates

    base = Path(__file__).resolve().parents[1]
    account_state = tmp_path / "account_state"
    account_state.mkdir()

    monkeypatch.setattr(
        exchange_rates,
        "get_exchange_rates_or_fetch_latest",
        lambda *, cache_path, max_age_hours=None, log=None: {"rates": {"USDCNY": 7.25, "HKDCNY": 0.93}},
    )

    usd_per_cny_exchange_rate, cny_per_hkd_exchange_rate = ctx.load_exchange_rates(
        base=base,
        state_dir=account_state,
        log=lambda _msg: None,
    )

    assert round(usd_per_cny_exchange_rate or 0.0, 8) == round(1.0 / 7.25, 8)
    assert cny_per_hkd_exchange_rate == 0.93
