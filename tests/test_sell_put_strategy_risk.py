from __future__ import annotations

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


def test_sell_put_short_vol_accepts_iv_edge_delta_and_concentration() -> None:
    from src.application.sell_put_strategy_risk import SellPutShortVolConfig, evaluate_sell_put_short_vol_row

    decision = evaluate_sell_put_short_vol_row(
        _candidate(),
        cfg=SellPutShortVolConfig(strategy="short_vol"),
        risk_ctx=_risk_context(),
    )

    assert decision["accepted"] is True
    fields = decision["fields"]
    assert fields["iv_rv_ratio"] == 1.5
    assert fields["iv_minus_rv"] == 0.12
    assert fields["single_trade_concentration"] == 0.07
    assert fields["symbol_concentration_after"] == 0.17
    assert fields["total_short_put_concentration_after"] == 0.17


def test_sell_put_short_vol_rejects_when_iv_rv_edge_is_too_low() -> None:
    from src.application.sell_put_strategy_risk import SellPutShortVolConfig, evaluate_sell_put_short_vol_row

    decision = evaluate_sell_put_short_vol_row(
        _candidate(implied_volatility=0.25, realized_volatility_estimate=0.24),
        cfg=SellPutShortVolConfig(strategy="short_vol"),
        risk_ctx=_risk_context(),
    )

    assert decision["accepted"] is False
    assert decision["rule"] == "vol_edge_ratio_below_min"


def test_sell_put_short_vol_rejects_when_concentration_is_not_evaluable() -> None:
    from src.application.sell_put_strategy_risk import PortfolioRiskContext, SellPutShortVolConfig, evaluate_sell_put_short_vol_row

    decision = evaluate_sell_put_short_vol_row(
        _candidate(),
        cfg=SellPutShortVolConfig(strategy="short_vol"),
        risk_ctx=PortfolioRiskContext(
            nav_cny=None,
            stock_value_cny_by_symbol={},
            short_put_assignment_cny_by_symbol={},
            short_put_assignment_total_cny=None,
            unavailable_reasons=("holdings_context_missing",),
        ),
    )

    assert decision["accepted"] is False
    assert decision["rule"] == "concentration_not_evaluable"
    assert "holdings_context_missing" in str(decision["message"])


def test_sell_put_short_vol_rejects_symbol_concentration_after_assignment() -> None:
    from src.application.sell_put_strategy_risk import SellPutShortVolConfig, evaluate_sell_put_short_vol_row

    decision = evaluate_sell_put_short_vol_row(
        _candidate(),
        cfg=SellPutShortVolConfig(strategy="short_vol"),
        risk_ctx=_risk_context(nvda_stock=160_000.0, nvda_short_put=30_000.0),
    )

    assert decision["accepted"] is False
    assert decision["rule"] == "symbol_concentration_exceeded"


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


def test_enrich_and_filter_sell_put_short_vol_writes_reject_trace(tmp_path: Path) -> None:
    from src.application.sell_put_strategy_risk import enrich_and_filter_sell_put_short_vol
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_put_candidates_labeled.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame([_candidate(implied_volatility=0.25, realized_volatility_estimate=0.24)])

    filtered = enrich_and_filter_sell_put_short_vol(
        df_labeled=df,
        symbol="NVDA",
        sell_put_cfg={"strategy": "short_vol"},
        portfolio_ctx={
            "_global_portfolio_ctx": {"cash_by_currency": {"CNY": 1_000_000.0}, "stocks_by_symbol": {}},
            "_global_option_ctx": {"cash_secured_by_symbol_by_ccy": {}, "cash_secured_total_cny": 0.0},
        },
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        out_path=out_path,
    )

    assert filtered.empty
    trace = (out_path.parent / "candidate_filter_trace.jsonl").read_text(encoding="utf-8")
    assert "vol_edge_ratio_below_min" in trace


def test_enrich_and_filter_sell_put_short_vol_rejects_event_risk(tmp_path: Path) -> None:
    from src.application.sell_put_strategy_risk import enrich_and_filter_sell_put_short_vol
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_put_candidates_labeled.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame([_candidate(event_flag=True, event_types="earnings", event_dates="2026-06-01")])

    filtered = enrich_and_filter_sell_put_short_vol(
        df_labeled=df,
        symbol="NVDA",
        sell_put_cfg={"strategy": "short_vol"},
        portfolio_ctx={
            "_global_portfolio_ctx": {"cash_by_currency": {"CNY": 1_000_000.0}, "stocks_by_symbol": {}},
            "_global_option_ctx": {"cash_secured_by_symbol_by_ccy": {}, "cash_secured_total_cny": 0.0},
        },
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        out_path=out_path,
    )

    assert filtered.empty
    trace = (out_path.parent / "candidate_filter_trace.jsonl").read_text(encoding="utf-8")
    assert "event_risk_within_expiry" in trace


def test_enrich_and_filter_sell_put_short_vol_computes_cny_stress_inputs(tmp_path: Path) -> None:
    from src.application.sell_put_strategy_risk import enrich_and_filter_sell_put_short_vol
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_put_candidates_labeled.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame(
        [
            _candidate(
                spot=102.0,
                net_income=20.0,
                net_income_cny=None,
                option_contract_point_value_cny=None,
                implied_volatility=0.70,
                realized_volatility_estimate=0.50,
                dte=60,
            )
        ]
    )

    filtered = enrich_and_filter_sell_put_short_vol(
        df_labeled=df,
        symbol="NVDA",
        sell_put_cfg={"strategy": "short_vol"},
        portfolio_ctx={
            "_global_portfolio_ctx": {"cash_by_currency": {"CNY": 1_000_000.0}, "stocks_by_symbol": {}},
            "_global_option_ctx": {"cash_secured_by_symbol_by_ccy": {}, "cash_secured_total_cny": 0.0},
        },
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        out_path=out_path,
    )

    assert filtered.empty
    persisted = pd.read_csv(out_path)
    assert persisted.empty
    trace = (out_path.parent / "candidate_filter_trace.jsonl").read_text(encoding="utf-8")
    assert "put_sigma_stress_loss_exceeded" in trace


def test_enrich_and_filter_sell_put_short_vol_raises_when_filtered_csv_cannot_be_written(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import pytest
    from src.application.sell_put_strategy_risk import enrich_and_filter_sell_put_short_vol
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_put_candidates_labeled.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame([_candidate()])

    def _boom(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pd.DataFrame, "to_csv", _boom)

    with pytest.raises(RuntimeError, match="failed to persist short-vol filtered sell-put candidates"):
        enrich_and_filter_sell_put_short_vol(
            df_labeled=df,
            symbol="NVDA",
            sell_put_cfg={"strategy": "short_vol"},
            portfolio_ctx={
                "_global_portfolio_ctx": {"cash_by_currency": {"CNY": 1_000_000.0}, "stocks_by_symbol": {}},
                "_global_option_ctx": {"cash_secured_by_symbol_by_ccy": {}, "cash_secured_total_cny": 0.0},
            },
            exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
            out_path=out_path,
        )
