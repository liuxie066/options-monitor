from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _build_context(tmp_path: Path, **overrides):
    from src.application import pipeline_context as ctx

    kwargs = {
        "py": "python",
        "base": tmp_path,
        "cfg": {"portfolio": {"account": "paper"}},
        "report_dir": tmp_path / "reports",
        "portfolio_timeout_sec": 1,
        "runtime": {},
        "is_scheduled": True,
        "state_dir": tmp_path / "state",
        "log": lambda _message: None,
        "no_context": False,
        "want_scan": True,
    }
    kwargs.update(overrides)
    return ctx.build_pipeline_context(**kwargs)


def test_load_exchange_rates_fetches_latest_when_cache_missing(monkeypatch, tmp_path: Path) -> None:
    from src.application import pipeline_context as ctx

    base = Path(__file__).resolve().parents[1]
    account_state = tmp_path / "account_state"
    account_state.mkdir()

    monkeypatch.setattr(
        ctx,
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


def test_load_exchange_rates_uses_shared_run_cache_when_supplied(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import pipeline_context as ctx

    observed: list[Path] = []
    account_state = tmp_path / "account-state"
    shared_state = tmp_path / "run-state"
    account_state.mkdir()
    shared_state.mkdir()

    def _load(*, cache_path, **_kwargs):
        observed.append(Path(cache_path))
        return {"rates": {"USDCNY": 7.2}}

    monkeypatch.setattr(
        ctx,
        "get_exchange_rates_or_fetch_latest",
        _load,
    )

    ctx.load_exchange_rates(
        base=tmp_path,
        state_dir=account_state,
        shared_state_dir=shared_state,
        log=lambda _message: None,
    )

    assert observed == [(shared_state / "rate_cache.json").resolve()]


def test_load_exchange_rates_rejects_unverified_stale_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.application import pipeline_context as ctx
    from src.infrastructure import exchange_rates

    cache_path = tmp_path / "rate_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "rates": {"USDCNY": 7.25, "HKDCNY": 0.93},
                "timestamp": (
                    datetime.now(timezone.utc) - timedelta(hours=25)
                ).isoformat(),
                "source": "tencent_quote",
            }
        ),
        encoding="utf-8",
    )
    # An aggregate timestamp cannot prove either pair's quote time.
    monkeypatch.setattr(exchange_rates, "fetch_market_exchange_rates", lambda: None)

    usd, hkd = ctx.load_exchange_rates(
        base=tmp_path,
        state_dir=tmp_path,
        log=lambda _msg: None,
    )

    assert usd is None
    assert hkd is None


def test_market_data_only_context_never_reads_account_authority(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import pipeline_context as ctx

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("experience context must not read account authority")

    for name in (
        "load_portfolio_context",
        "load_option_positions_context",
        "load_prepared_portfolio_context",
        "load_prepared_option_positions_context",
    ):
        monkeypatch.setattr(ctx, name, _forbidden)
    monkeypatch.setattr(
        ctx,
        "load_exchange_rates",
        lambda **_kwargs: (0.14, 0.93),
    )

    assert _build_context(tmp_path, market_data_only=True) == (None, None, 0.14, 0.93)


def test_direct_pipeline_uses_one_fx_observation_and_rejects_old_secured_total(
    monkeypatch, tmp_path: Path,
) -> None:
    from src.application import pipeline_context as ctx

    calls: list[Path] = []
    from src.infrastructure import exchange_rates as fx
    from test_unified_fx_consumers import quote, at
    observation = quote()
    observation["rates"] = {"USDCNY": 7.2}
    monkeypatch.setattr(fx, "_utc_now", lambda: at("2026-10-07T12:00:00"))

    def _fetch(*, cache_path, **_kwargs):
        calls.append(Path(cache_path))
        return observation

    monkeypatch.setattr(ctx, "get_exchange_rates_or_fetch_latest", _fetch)
    monkeypatch.setattr(
        ctx, "load_portfolio_context",
        lambda **kwargs: {"cash_by_currency": {"USD": 100}, "exchange_rates": kwargs["exchange_rate_observation"]},
    )
    monkeypatch.setattr(
        ctx, "load_option_positions_context",
        lambda **kwargs: ({
            "cash_secured_total_by_ccy": {"USD": 100},
            "cash_secured_total_cny": 725,
            "exchange_rates": {"rates": {"USDCNY": 7.25}},
        }, False),
    )

    portfolio, option, usd_per_cny, _ = _build_context(tmp_path)

    assert len(calls) == 1
    assert portfolio["exchange_rates"] is observation
    assert option["exchange_rates"] is observation
    assert option["cash_secured_total_cny"] is None
    assert usd_per_cny == pytest.approx(1 / 7.2)


def test_fetch_opend_exchange_rate_observation_uses_market_fetch(
    monkeypatch,
) -> None:
    from src.application import exchange_rate_loader as loader

    monkeypatch.setattr(
        loader,
        "get_exchange_rates_or_fetch_latest",
        lambda **_kwargs: {
            "rates": {"USDCNY": 7.21, "HKDCNY": 0.92},
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "tencent_quote",
        },
    )

    observation = loader.fetch_opend_exchange_rate_observation(
        (("lx", {"symbols": []}),)
    )

    assert observation["rates"] == {"USDCNY": 7.21, "HKDCNY": 0.92}


def test_prepared_option_context_disables_live_ledger_and_fx_fallbacks(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import pipeline_context as ctx

    option_context = {
        "filters": {"broker": "富途", "account": "lx"},
        "exchange_rates": {
            "timestamp": "2026-08-05T01:00:00+00:00",
            "source": "test",
            "rates": {"USDCNY": 7.25, "HKDCNY": 0.93},
        },
    }

    def _unexpected(**_kwargs):
        raise AssertionError("prepared context must not use a live fallback")

    monkeypatch.setattr(
        ctx,
        "load_prepared_portfolio_context",
        lambda **_kwargs: {"cash_by_currency": {"USD": 1000}},
    )
    monkeypatch.setattr(
        ctx,
        "load_prepared_option_positions_context",
        lambda **_kwargs: option_context,
    )
    monkeypatch.setattr(ctx, "load_option_positions_context", _unexpected)
    monkeypatch.setattr(ctx, "load_exchange_rates", _unexpected)
    monkeypatch.setattr(
        ctx,
        "adapt_option_positions_context",
        lambda payload: dict(payload),
    )
    monkeypatch.setattr(ctx, "_persist_source_snapshot", lambda *_args: None)

    portfolio, option, usd_per_cny, cny_per_hkd = _build_context(
        tmp_path,
        cfg={
            "portfolio": {
                "account": "lx",
                "broker": "富途",
                "data_config": "portfolio.runtime.json",
            },
            "symbols": [],
        },
        shared_state_dir=tmp_path / "shared",
        prepared_portfolio_context_manifest=tmp_path / "prepared-portfolio.json",
        prepared_portfolio_context_run_id="run-1",
        prepared_portfolio_context_account_config_sha256="a" * 64,
        prepared_portfolio_context_manifest_sha256="b" * 64,
        prepared_option_positions_context_manifest=tmp_path / "prepared-options.json",
        prepared_option_positions_context_run_id="run-1",
        prepared_option_positions_context_account_config_sha256="a" * 64,
        prepared_option_positions_context_manifest_sha256="c" * 64,
    )

    assert portfolio == {"cash_by_currency": {"USD": 1000}}
    assert option is option_context
    assert round(usd_per_cny or 0.0, 8) == round(1.0 / 7.25, 8)
    assert cny_per_hkd == 0.93


@pytest.mark.parametrize(
    ("secured", "expected_cny"),
    [({"USD": 1000}, 7200.0), ({"CNY": 1000}, 1000.0)],
)
@pytest.mark.parametrize("reopened", [False, True])
def test_prepared_fx_rechecks_capacity_at_scan_time(monkeypatch, tmp_path: Path, secured, expected_cny, reopened) -> None:
    from src.application import pipeline_context as ctx
    from src.infrastructure import exchange_rates as fx

    now = datetime.fromisoformat("2026-10-08T01:43:00+00:00" if reopened else "2026-10-02T01:43:00+00:00")
    monkeypatch.setattr(fx, "_utc_now", lambda: now)
    snapshot = {
        "schema_version": 2,
        "pairs": {
            pair: {
                "rate": rate,
                "source": "tencent_quote",
                "quote_at_utc": "2026-09-30T06:00:00+00:00",
                "observed_at_utc": "2026-09-30T06:00:00+00:00",
            }
            for pair, rate in (("USDCNY", 7.2), ("HKDCNY", 0.92))
        },
    }
    old_fx = {**snapshot, "rates": {"USDCNY": 7.2, "HKDCNY": 0.92}}
    portfolio = {"fx_snapshot_sha256": "f" * 64}
    option = {
        "prepared_authority": {"fx_status": "ready", "run_fx_snapshot_sha256": "f" * 64},
        "exchange_rates": old_fx,
        "cash_secured_total_by_ccy": secured,
        "cash_secured_total_cny": expected_cny,
    }
    monkeypatch.setattr(ctx, "load_prepared_portfolio_context", lambda **_kwargs: portfolio)
    monkeypatch.setattr(ctx, "load_prepared_option_positions_context", lambda **_kwargs: option)
    monkeypatch.setattr(ctx, "load_run_fx_snapshot", lambda **_kwargs: (snapshot, "f" * 64))
    monkeypatch.setattr(ctx, "adapt_option_positions_context", lambda value: value)
    monkeypatch.setattr(ctx, "_persist_source_snapshot", lambda *_args: None)

    current_portfolio, current_option, usd, hkd = _build_context(
        tmp_path,
        cfg={"portfolio": {"account": "lx", "broker": "富途", "data_config": "portfolio.runtime.json"}},
        prepared_portfolio_context_manifest=tmp_path / "portfolio.json",
        prepared_portfolio_context_run_id="run-1",
        prepared_portfolio_context_account_config_sha256="a" * 64,
        prepared_option_positions_context_manifest=tmp_path / "options.json",
        prepared_option_positions_context_run_id="run-1",
        prepared_option_positions_context_account_config_sha256="a" * 64,
    )

    if reopened:
        assert current_option["exchange_rates"]["rates"] == {}
        assert current_option["cash_secured_total_cny"] == (1000.0 if "CNY" in secured else None)
        assert current_portfolio["exchange_rate_status"] == "unavailable_stale"
        assert (usd, hkd) == (None, None)
    else:
        assert current_option["exchange_rates"]["rates"] == {"USDCNY": 7.2, "HKDCNY": 0.92}
        assert current_option["cash_secured_total_cny"] == expected_cny
        assert current_portfolio["exchange_rate_status"] == "ready"
        assert (usd, hkd) == (1 / 7.2, 0.92)
