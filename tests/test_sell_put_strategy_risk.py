from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def _risk_context(*, nav: float = 1_000_000.0, nvda_stock: float = 50_000.0, nvda_short_put: float = 50_000.0):
    from src.application.sell_put_strategy_risk import PortfolioRiskContext

    return PortfolioRiskContext(
        nav_cny=nav,
        stock_value_cny_by_symbol={"NVDA": nvda_stock},
        short_put_assignment_cny_by_symbol={"NVDA": nvda_short_put},
        short_put_assignment_total_cny=100_000.0,
    )


def _candidate(**overrides):
    row = {
        "symbol": "NVDA",
        "contract_symbol": "NVDA260619P00100000",
        "expiration": "2026-06-19",
        "strike": 100.0,
        "spot": 110.0,
        "multiplier": 100.0,
        "currency": "USD",
        "cash_required_cny": 70_000.0,
        "net_income_cny": 1_400.0,
        "option_contract_point_value_cny": 700.0,
        "implied_volatility": 0.36,
        "realized_volatility_estimate": 0.24,
        "delta": -0.20,
        "annualized_net_return_on_cash_basis": 0.12,
        "net_income": 200.0,
        "spread_ratio": 0.08,
        "open_interest": 100,
        "volume": 20,
        "dte": 30,
        "otm_pct": 0.10,
        "event_source_status": "ok",
    }
    row.update(overrides)
    return row


def test_sell_put_underwriting_accepts_priced_candidate_without_concentration_gate() -> None:
    from src.application.sell_put_strategy_risk import evaluate_sell_put_underwriting_row, resolve_sell_put_underwriting_config

    decision = evaluate_sell_put_underwriting_row(
        _candidate(),
        cfg=resolve_sell_put_underwriting_config({"strategy": "insurance_underwriting", "max_strike": 110.0}),
    )

    assert decision["accepted"] is True
    fields = decision["fields"]
    assert fields["strategy_profile"] == "insurance_underwriting"
    assert fields["iv_rv_ratio"] == 1.5
    assert fields["iv_minus_rv"] == 0.12
    assert fields["strike_safety_margin_pct"] == 0.090909
    assert fields["premium_edge_score"] > 1.0
    assert "single_trade_concentration" not in fields


def test_sell_put_close_thesis_config_prefers_top_level_underwriting_fields() -> None:
    from src.application.sell_put_strategy_risk import resolve_sell_put_short_vol_config

    cfg = resolve_sell_put_short_vol_config(
        {
            "strategy": "insurance_underwriting",
            "min_iv_rv_ratio": 1.10,
            "min_iv_minus_rv": 0.05,
            "reject_event_risk": False,
            "event_source_fail_closed": False,
            "short_vol": {
                "min_iv_rv_ratio": 1.25,
                "min_iv_minus_rv": 0.10,
                "reject_event_risk": True,
                "event_source_fail_closed": True,
            },
        }
    )

    assert cfg.min_iv_rv_ratio == 1.10
    assert cfg.min_iv_minus_rv == 0.05
    assert cfg.reject_event_risk is False
    assert cfg.event_source_fail_closed is False


def test_sell_put_underwriting_rejects_when_iv_rv_edge_is_too_low() -> None:
    from src.application.sell_put_strategy_risk import evaluate_sell_put_underwriting_row, resolve_sell_put_underwriting_config

    decision = evaluate_sell_put_underwriting_row(
        _candidate(implied_volatility=0.25, realized_volatility_estimate=0.24),
        cfg=resolve_sell_put_underwriting_config({"strategy": "insurance_underwriting"}),
    )

    assert decision["accepted"] is False
    assert decision["rule"] == "vol_edge_ratio_below_min"


def test_sell_put_underwriting_rejects_when_return_is_too_low() -> None:
    from src.application.sell_put_strategy_risk import evaluate_sell_put_underwriting_row, resolve_sell_put_underwriting_config

    decision = evaluate_sell_put_underwriting_row(
        _candidate(annualized_net_return_on_cash_basis=0.08),
        cfg=resolve_sell_put_underwriting_config(
            {"strategy": "insurance_underwriting", "min_annualized_net_return": 0.10}
        ),
    )

    assert decision["accepted"] is False
    assert decision["rule"] == "annualized_return_below_min"


def test_build_portfolio_risk_context_uses_global_holdings_and_option_context() -> None:
    from src.application.sell_put_strategy_risk import build_portfolio_risk_context
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    risk = build_portfolio_risk_context(
        portfolio_ctx={
            "cash_by_currency": {"CNY": 1.0},
            "stocks_by_symbol": {},
            "_global_portfolio_ctx": {
                "cash_by_currency": {"CNY": 800_000.0},
                "stocks_by_symbol": {
                    "NVDA": {
                        "symbol": "NVDA",
                        "shares": 10,
                        "market_value_cny": 50_000.0,
                        "currency": "USD",
                    }
                },
            },
            "_global_option_ctx": {
                "cash_secured_by_symbol_by_ccy": {"NVDA": {"USD": 7_000.0}},
                "cash_secured_total_cny": 50_000.0,
            },
        },
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
    )

    assert risk.nav_cny == 850_000.0
    assert risk.stock_value_cny_by_symbol == {"NVDA": 50_000.0}
    assert abs(risk.short_put_assignment_cny_by_symbol["NVDA"] - 50_000.0) < 0.000001
    assert risk.short_put_assignment_total_cny == 50_000.0
    assert risk.unavailable_reasons == ()


def test_enrich_and_filter_sell_put_underwriting_writes_reject_trace(tmp_path: Path) -> None:
    from src.application.sell_put_strategy_risk import enrich_and_filter_sell_put_underwriting
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_put_candidates_labeled.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame([_candidate(implied_volatility=0.25, realized_volatility_estimate=0.24)])

    filtered = enrich_and_filter_sell_put_underwriting(
        df_labeled=df,
        symbol="NVDA",
        sell_put_cfg={"strategy": "insurance_underwriting"},
        portfolio_ctx=None,
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        out_path=out_path,
    )

    assert filtered.empty
    trace = (out_path.parent / "candidate_filter_trace.jsonl").read_text(encoding="utf-8")
    assert "vol_edge_ratio_below_min" in trace
    assert '"strategy_family": "sell_put"' in trace
    assert '"strategy_profile": "insurance_underwriting"' in trace
    trace_row = json.loads(trace)
    assert trace_row["dte"] == 30
    assert trace_row["abs_delta"] == 0.2
    assert trace_row["iv_rv_ratio"] == 1.041667
    assert trace_row["iv_minus_rv"] == 0.01


def test_enrich_and_filter_sell_put_underwriting_rejects_event_risk(tmp_path: Path) -> None:
    from src.application.sell_put_strategy_risk import enrich_and_filter_sell_put_underwriting
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_put_candidates_labeled.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame([_candidate(event_flag=True, event_types="earnings", event_dates="2026-06-01")])

    filtered = enrich_and_filter_sell_put_underwriting(
        df_labeled=df,
        symbol="NVDA",
        sell_put_cfg={"strategy": "insurance_underwriting"},
        portfolio_ctx=None,
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        out_path=out_path,
    )

    assert filtered.empty
    trace = (out_path.parent / "candidate_filter_trace.jsonl").read_text(encoding="utf-8")
    assert "event_risk_within_expiry" in trace


def test_enrich_and_filter_sell_put_underwriting_does_not_reject_stress_or_concentration(tmp_path: Path) -> None:
    from src.application.sell_put_strategy_risk import enrich_and_filter_sell_put_underwriting
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_put_candidates_labeled.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame(
        [
            _candidate(
                spot=102.0,
                implied_volatility=0.70,
                realized_volatility_estimate=0.50,
                dte=60,
            )
        ]
    )

    filtered = enrich_and_filter_sell_put_underwriting(
        df_labeled=df,
        symbol="NVDA",
        sell_put_cfg={
            "strategy": "insurance_underwriting",
            "max_strike": 110.0,
            "concentration": {"max_single_trade_nav_pct": 0.0001},
        },
        portfolio_ctx=None,
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        out_path=out_path,
    )

    assert len(filtered) == 1
    assert filtered.iloc[0]["contract_symbol"] == "NVDA260619P00100000"


def test_sell_put_underwriting_ranking_prefers_premium_edge_then_strike_safety(tmp_path: Path) -> None:
    from src.application.sell_put_strategy_risk import enrich_and_filter_sell_put_underwriting
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_put_candidates_labeled.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame(
        [
            _candidate(contract_symbol="NEAR", strike=105.0, net_income=210.0, net_income_cny=1470.0),
            _candidate(contract_symbol="FAR", strike=95.0, net_income=210.0, net_income_cny=1470.0),
            _candidate(contract_symbol="RICH", strike=104.0, net_income=280.0, net_income_cny=1960.0),
        ]
    )

    filtered = enrich_and_filter_sell_put_underwriting(
        df_labeled=df,
        symbol="NVDA",
        sell_put_cfg={"strategy": "insurance_underwriting", "max_strike": 110.0, "min_net_income": 1000.0},
        portfolio_ctx=None,
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        out_path=out_path,
    )

    assert list(filtered["contract_symbol"]) == ["RICH", "FAR", "NEAR"]
    assert filtered.iloc[1]["strike_safety_margin_pct"] > filtered.iloc[2]["strike_safety_margin_pct"]


def test_enrich_and_filter_sell_put_underwriting_raises_when_filtered_csv_cannot_be_written(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import pytest
    from src.application.sell_put_strategy_risk import enrich_and_filter_sell_put_underwriting
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_put_candidates_labeled.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame([_candidate()])

    def _boom(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pd.DataFrame, "to_csv", _boom)

    with pytest.raises(RuntimeError, match="failed to persist insurance-underwriting filtered sell-put candidates"):
        enrich_and_filter_sell_put_underwriting(
            df_labeled=df,
            symbol="NVDA",
            sell_put_cfg={"strategy": "insurance_underwriting"},
            portfolio_ctx=None,
            exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
            out_path=out_path,
        )
