from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


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


def test_load_exchange_rates_uses_shared_run_cache_when_supplied(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import pipeline_context as ctx
    from src.infrastructure import exchange_rates

    observed: list[Path] = []
    account_state = tmp_path / "account-state"
    shared_state = tmp_path / "run-state"
    account_state.mkdir()
    shared_state.mkdir()

    def _load(*, cache_path, **_kwargs):
        observed.append(Path(cache_path))
        return {"rates": {"USDCNY": 7.2}}

    monkeypatch.setattr(
        exchange_rates,
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


def test_load_exchange_rates_rejects_stale_cache_without_fallback(
    tmp_path: Path,
) -> None:
    from src.application import pipeline_context as ctx

    cache_path = tmp_path / "rate_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "rates": {"USDCNY": 7.25, "HKDCNY": 0.93},
                "timestamp": (
                    datetime.now(timezone.utc) - timedelta(hours=25)
                ).isoformat(),
                "source": "opend_fx_market_snapshot",
            }
        ),
        encoding="utf-8",
    )
    messages: list[str] = []
    status: dict[str, str] = {}

    assert ctx.load_exchange_rates(
        base=tmp_path,
        state_dir=tmp_path,
        log=messages.append,
        status_out=status,
    ) == (None, None)
    assert status == {"status": "unavailable"}
    assert any("OpenD exchange_rate observation missing/stale" in message for message in messages)


def test_fetch_opend_exchange_rate_observation_uses_canonical_quote_route(
    monkeypatch,
) -> None:
    from src.application import exchange_rate_loader as loader
    from src.infrastructure.exchange_rates import exchange_rate_observation_status

    observed_codes: list[tuple[str, ...]] = []
    closed: list[bool] = []

    class _Gateway:
        def get_snapshot(self, codes):
            observed_codes.append(tuple(codes))
            now = datetime.now(timezone.utc).timestamp()
            return [
                {
                    "code": "FX.USDCNH",
                    "bid_price": 7.20,
                    "ask_price": 7.22,
                    "update_timestamp": now,
                },
                {
                    "code": "FX.USDHKD",
                    "bid_price": 7.80,
                    "ask_price": 7.82,
                    "update_timestamp": now,
                },
            ]

        def close(self):
            closed.append(True)

    monkeypatch.setattr(
        loader,
        "resolve_shared_futu_quote_route",
        lambda _configs: SimpleNamespace(
            ok=True,
            host="127.0.0.1",
            port=11111,
        ),
    )
    monkeypatch.setattr(
        loader,
        "build_ready_futu_quote_gateway",
        lambda **_kwargs: _Gateway(),
    )

    observation = loader.fetch_opend_exchange_rate_observation(
        (("us", {"symbols": []}),)
    )

    assert observed_codes == [("FX.USDCNH", "FX.USDHKD")]
    assert closed == [True]
    assert exchange_rate_observation_status(observation, max_age_hours=24) == "ready"


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
        "load_global_option_positions_risk_context",
        _unexpected,
    )
    monkeypatch.setattr(
        ctx,
        "adapt_option_positions_context",
        lambda payload: dict(payload),
    )
    monkeypatch.setattr(ctx, "_persist_source_snapshot", lambda *_args: None)

    portfolio, option, usd_per_cny, cny_per_hkd = (
        ctx.build_pipeline_context(
            py="python",
            base=tmp_path,
            cfg={
                "portfolio": {
                    "account": "lx",
                    "broker": "富途",
                    "data_config": "portfolio.runtime.json",
                },
                "symbols": [],
            },
            report_dir=tmp_path / "reports",
            portfolio_timeout_sec=1,
            runtime={},
            is_scheduled=True,
            state_dir=tmp_path / "state",
            shared_state_dir=tmp_path / "shared",
            log=lambda _message: None,
            no_context=False,
            want_scan=True,
            prepared_portfolio_context_manifest=tmp_path
            / "prepared-portfolio.json",
            prepared_portfolio_context_run_id="run-1",
            prepared_portfolio_context_account_config_sha256="a" * 64,
            prepared_portfolio_context_manifest_sha256="b" * 64,
            prepared_option_positions_context_manifest=tmp_path
            / "prepared-options.json",
            prepared_option_positions_context_run_id="run-1",
            prepared_option_positions_context_account_config_sha256=(
                "a" * 64
            ),
            prepared_option_positions_context_manifest_sha256="c" * 64,
        )
    )

    assert portfolio == {"cash_by_currency": {"USD": 1000}}
    assert option is option_context
    assert round(usd_per_cny or 0.0, 8) == round(1.0 / 7.25, 8)
    assert cny_per_hkd == 0.93
