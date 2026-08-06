from __future__ import annotations

import json
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
        "term_matched_rv": 0.24,
        "delta": 0.20,
        "annualized_net_premium_return": 0.12,
        "net_income": 200.0,
        "spread_ratio": 0.08,
        "open_interest": 100,
        "volume": 20,
        "dte": 30,
        "strike_above_spot_pct": 0.166667,
        "earnings_evidence_status": "ready",
        "earnings_has_event": False,
        "earnings_event_dates": "",
    }
    row.update(overrides)
    return row


def test_covered_call_underwriting_enrichment_accepts_and_adds_pricing_fields(tmp_path: Path) -> None:
    from src.application.covered_call_strategy_risk import enrich_and_filter_covered_call_underwriting
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_call_candidates.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame([_candidate()])

    filtered = enrich_and_filter_covered_call_underwriting(
        df_labeled=df,
        symbol="NVDA",
        sell_call_cfg={"strategy": "insurance_underwriting", "min_strike": 120.0},
        portfolio_ctx=None,
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        out_path=out_path,
    )

    assert len(filtered) == 1
    top = filtered.iloc[0]
    assert top["strategy_profile"] == "insurance_underwriting"
    assert top["iv_rv_ratio"] == 1.5
    assert top["iv_minus_rv"] == 0.12
    assert top["short_gamma_profile"] == "short_gamma"
    assert top["short_vega_profile"] == "short_vega"
    assert top["covered_notional_cny"] > 0
    assert top["strike_upside_margin_pct"] == 0.166667
    assert "call_gap_up_opportunity_cost_cny" not in top


def test_covered_call_underwriting_writes_reject_trace(tmp_path: Path) -> None:
    from src.application.covered_call_strategy_risk import enrich_and_filter_covered_call_underwriting
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_call_candidates.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame([_candidate(implied_volatility=0.25, term_matched_rv=0.24)])

    filtered = enrich_and_filter_covered_call_underwriting(
        df_labeled=df,
        symbol="NVDA",
        sell_call_cfg={"strategy": "insurance_underwriting"},
        portfolio_ctx=None,
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        out_path=out_path,
    )

    assert filtered.empty
    trace = (out_path.parent / "candidate_filter_trace.jsonl").read_text(encoding="utf-8")
    assert "risk_iv_rv_ratio" in trace
    assert "\"function\": \"sell_call\"" in trace
    assert '"strategy_family": "sell_call"' in trace
    assert '"strategy_profile": "insurance_underwriting"' in trace
    trace_row = json.loads(trace)
    assert trace_row["dte"] == 30
    assert trace_row["abs_delta"] == 0.2
    assert trace_row["iv_rv_ratio"] == 1.041667
    assert trace_row["iv_minus_rv"] == 0.01


def test_covered_call_underwriting_does_not_reject_concentration_or_gap_up_budget(tmp_path: Path) -> None:
    from src.application.covered_call_strategy_risk import enrich_and_filter_covered_call_underwriting
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_call_candidates.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame([_candidate(strike=100.0)])

    filtered = enrich_and_filter_covered_call_underwriting(
        df_labeled=df,
        symbol="NVDA",
        sell_call_cfg={
            "strategy": "insurance_underwriting",
            "min_strike": 100.0,
            "concentration": {"max_single_trade_nav_pct": 0.0001},
        },
        portfolio_ctx=None,
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        out_path=out_path,
    )

    assert len(filtered) == 1
    assert filtered.iloc[0]["contract_symbol"] == "NVDA260619C00140000"


def test_covered_call_underwriting_rejects_return_below_min(tmp_path: Path) -> None:
    from src.application.covered_call_strategy_risk import enrich_and_filter_covered_call_underwriting
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_call_candidates.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame([_candidate(annualized_net_premium_return=0.08)])

    filtered = enrich_and_filter_covered_call_underwriting(
        df_labeled=df,
        symbol="NVDA",
        sell_call_cfg={"strategy": "insurance_underwriting", "min_annualized_net_premium_return": 0.10},
        portfolio_ctx=None,
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        out_path=out_path,
    )

    assert filtered.empty
    trace = (out_path.parent / "candidate_filter_trace.jsonl").read_text(encoding="utf-8")
    assert "return_annualized" in trace


def test_covered_call_underwriting_rejects_when_income_fx_is_missing(tmp_path: Path) -> None:
    from src.application.covered_call_strategy_risk import enrich_and_filter_covered_call_underwriting
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_call_candidates.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame([_candidate()])

    filtered = enrich_and_filter_covered_call_underwriting(
        df_labeled=df,
        symbol="NVDA",
        sell_call_cfg={"strategy": "insurance_underwriting"},
        portfolio_ctx=None,
        exchange_rate_converter=CurrencyConverter(ExchangeRates()),
        out_path=out_path,
    )

    assert filtered.empty
    trace = (out_path.parent / "candidate_filter_trace.jsonl").read_text(encoding="utf-8")
    assert "return_net_premium_cny" in trace


def test_covered_call_underwriting_ranking_prefers_upside_margin_and_deduplicates_income(tmp_path: Path) -> None:
    from src.application.covered_call_strategy_risk import enrich_and_filter_covered_call_underwriting
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_call_candidates.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame(
        [
            _candidate(contract_symbol="LOW_UPSIDE", strike=125.0, spot=110.0, net_income=210.0),
            _candidate(contract_symbol="HIGH_UPSIDE", strike=140.0, spot=110.0, net_income=210.0),
            _candidate(contract_symbol="RICH", strike=126.0, spot=110.0, net_income=280.0),
        ]
    )

    filtered = enrich_and_filter_covered_call_underwriting(
        df_labeled=df,
        symbol="NVDA",
        sell_call_cfg={
            "strategy": "insurance_underwriting",
            "min_strike_cost_multiplier": 1.2,
            "min_net_income": 200.0,
        },
        portfolio_ctx=None,
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        out_path=out_path,
    )

    assert list(filtered["contract_symbol"]) == ["HIGH_UPSIDE", "RICH", "LOW_UPSIDE"]
    assert set(filtered["effective_min_strike"]) == {120.0}
    by_contract = filtered.set_index("contract_symbol")
    assert "premium_edge_score" not in by_contract.columns
    assert by_contract.loc["HIGH_UPSIDE", "strike_upside_margin_pct"] > by_contract.loc["RICH", "strike_upside_margin_pct"]


def test_covered_call_underwriting_raises_when_filtered_csv_cannot_be_written(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import pytest
    from src.application.covered_call_strategy_risk import enrich_and_filter_covered_call_underwriting
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_call_candidates.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame([_candidate()])

    def _boom(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pd.DataFrame, "to_csv", _boom)

    with pytest.raises(RuntimeError, match="failed to persist insurance-underwriting filtered covered-call candidates"):
        enrich_and_filter_covered_call_underwriting(
            df_labeled=df,
            symbol="NVDA",
            sell_call_cfg={"strategy": "insurance_underwriting"},
            portfolio_ctx=None,
            exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
            out_path=out_path,
        )
