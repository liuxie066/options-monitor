from __future__ import annotations

from pathlib import Path

import pandas as pd


def _candidate(**overrides):
    row = {
        "symbol": "NVDA",
        "contract_symbol": "NVDA260619C00140000",
        "expiration": "2026-06-19",
        "strike": 140.0,
        "spot": 120.0,
        "avg_cost": 100.0,
        "shares_total": 100,
        "shares_locked": 0,
        "shares_available_for_cover": 100,
        "covered_contracts_available": 1,
        "is_fully_covered_available": True,
        "multiplier": 100.0,
        "currency": "USD",
        "implied_volatility": 0.36,
        "realized_volatility_estimate": 0.24,
        "delta": 0.20,
        "annualized_net_premium_return": 0.12,
        "net_income": 200.0,
        "spread_ratio": 0.08,
        "open_interest": 100,
        "volume": 20,
        "dte": 30,
        "strike_above_spot_pct": 0.166667,
        "event_source_status": "ok",
    }
    row.update(overrides)
    return row


def _portfolio_ctx():
    return {
        "_global_portfolio_ctx": {
            "cash_by_currency": {"CNY": 1_100_000.0},
            "stocks_by_symbol": {
                "NVDA": {
                    "symbol": "NVDA",
                    "shares": 100,
                    "market_value_cny": 100_000.0,
                    "currency": "USD",
                }
            },
        },
        "_global_option_ctx": {
            "cash_secured_by_symbol_by_ccy": {},
            "cash_secured_total_cny": 0.0,
        },
    }


def test_covered_call_short_vol_enrichment_accepts_and_adds_risk_fields(tmp_path: Path) -> None:
    from src.application.covered_call_strategy_risk import enrich_and_filter_covered_call_short_vol
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_call_candidates.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame([_candidate()])

    filtered = enrich_and_filter_covered_call_short_vol(
        df_labeled=df,
        symbol="NVDA",
        sell_call_cfg={"strategy": "short_vol"},
        portfolio_ctx=_portfolio_ctx(),
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        out_path=out_path,
    )

    assert len(filtered) == 1
    top = filtered.iloc[0]
    assert top["iv_rv_ratio"] == 1.5
    assert top["iv_minus_rv"] == 0.12
    assert top["short_gamma_profile"] == "short_gamma"
    assert top["short_vega_profile"] == "short_vega"
    assert top["covered_notional_cny"] > 0
    assert top["single_trade_concentration"] > 0
    assert bool(top["path_stress_evaluable"]) is True
    assert top["call_gap_up_price"] == 132.0
    assert top["call_gap_up_opportunity_cost_cny"] == 0.0


def test_covered_call_short_vol_enrichment_writes_reject_trace(tmp_path: Path) -> None:
    from src.application.covered_call_strategy_risk import enrich_and_filter_covered_call_short_vol
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_call_candidates.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame([_candidate(implied_volatility=0.25, realized_volatility_estimate=0.24)])

    filtered = enrich_and_filter_covered_call_short_vol(
        df_labeled=df,
        symbol="NVDA",
        sell_call_cfg={"strategy": "short_vol"},
        portfolio_ctx=_portfolio_ctx(),
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        out_path=out_path,
    )

    assert filtered.empty
    trace = (out_path.parent / "candidate_filter_trace.jsonl").read_text(encoding="utf-8")
    assert "vol_edge_ratio_below_min" in trace
    assert "\"function\": \"sell_call\"" in trace


def test_covered_call_short_vol_enrichment_rejects_concentration(tmp_path: Path) -> None:
    from src.application.covered_call_strategy_risk import enrich_and_filter_covered_call_short_vol
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_call_candidates.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame([_candidate()])

    filtered = enrich_and_filter_covered_call_short_vol(
        df_labeled=df,
        symbol="NVDA",
        sell_call_cfg={"strategy": "short_vol", "concentration": {"max_single_trade_nav_pct": 0.05}},
        portfolio_ctx=_portfolio_ctx(),
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        out_path=out_path,
    )

    assert filtered.empty
    trace = (out_path.parent / "candidate_filter_trace.jsonl").read_text(encoding="utf-8")
    assert "single_trade_concentration_exceeded" in trace


def test_covered_call_short_vol_enrichment_rejects_gap_up_budget(tmp_path: Path) -> None:
    from src.application.covered_call_strategy_risk import enrich_and_filter_covered_call_short_vol
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_call_candidates.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame([_candidate(strike=100.0)])

    filtered = enrich_and_filter_covered_call_short_vol(
        df_labeled=df,
        symbol="NVDA",
        sell_call_cfg={
            "strategy": "short_vol",
            "short_vol": {
                "max_call_gap_up_opportunity_cost_nav_pct": 0.01,
                "max_call_gap_up_opportunity_cost_to_premium": 20.0,
            },
        },
        portfolio_ctx=_portfolio_ctx(),
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        out_path=out_path,
    )

    assert filtered.empty
    trace = (out_path.parent / "candidate_filter_trace.jsonl").read_text(encoding="utf-8")
    assert "call_gap_up_opportunity_cost_nav_exceeded" in trace
    assert "max_call_gap_up_opportunity_cost_nav_pct" in trace


def test_covered_call_short_vol_ranking_penalizes_gap_up_opportunity_cost(tmp_path: Path) -> None:
    from src.application.covered_call_strategy_risk import enrich_and_filter_covered_call_short_vol
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_call_candidates.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame(
        [
            _candidate(contract_symbol="HIGH_RIGHT_TAIL_COST", strike=125.0),
            _candidate(contract_symbol="LOW_RIGHT_TAIL_COST", strike=140.0),
        ]
    )

    filtered = enrich_and_filter_covered_call_short_vol(
        df_labeled=df,
        symbol="NVDA",
        sell_call_cfg={"strategy": "short_vol"},
        portfolio_ctx=_portfolio_ctx(),
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        out_path=out_path,
    )

    assert list(filtered["contract_symbol"]) == ["LOW_RIGHT_TAIL_COST", "HIGH_RIGHT_TAIL_COST"]
    assert filtered.iloc[0]["call_gap_up_opportunity_cost_nav_pct"] < filtered.iloc[1]["call_gap_up_opportunity_cost_nav_pct"]


def test_covered_call_short_vol_enrichment_raises_when_filtered_csv_cannot_be_written(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import pytest
    from src.application.covered_call_strategy_risk import enrich_and_filter_covered_call_short_vol
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_call_candidates.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame([_candidate()])

    def _boom(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pd.DataFrame, "to_csv", _boom)

    with pytest.raises(RuntimeError, match="failed to persist short-vol filtered covered-call candidates"):
        enrich_and_filter_covered_call_short_vol(
            df_labeled=df,
            symbol="NVDA",
            sell_call_cfg={"strategy": "short_vol"},
            portfolio_ctx=_portfolio_ctx(),
            exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
            out_path=out_path,
        )
