from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from domain.domain.engine import (
    EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
    EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
)


def _risk_context(*, nav: float = 1_000_000.0, nvda_stock: float = 50_000.0, nvda_short_put: float = 50_000.0):
    from src.application.sell_put_strategy_risk import PortfolioRiskContext

    return PortfolioRiskContext(
        nav_cny=nav,
        stock_value_cny_by_symbol={"NVDA": nvda_stock},
        short_put_assignment_cny_by_symbol={"NVDA": nvda_short_put},
        short_put_assignment_total_cny=100_000.0,
    )


def _earnings_evidence(*, event_date: str | None = None) -> dict:
    event = None
    if event_date is not None:
        days_before_expiration = (
            date.fromisoformat("2026-06-19") - date.fromisoformat(event_date)
        ).days
        blocking = days_before_expiration <= EARNINGS_NEAR_EXPIRY_WINDOW_DAYS
        event = {
            "earnings_date": event_date,
            "days_before_expiration": days_before_expiration,
            "classification": "blocking" if blocking else "nonblocking",
            "blocking": blocking,
        }
    events = [] if event is None else [event]
    blocking_events = [item for item in events if item["blocking"]]
    nonblocking_events = [item for item in events if not item["blocking"]]
    return {
        "earnings_evidence_status": "ready",
        "earnings_reason_code": None,
        "earnings_policy_version": EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
        "earnings_window_days": EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
        "earnings_market_date": "2026-05-20",
        "earnings_hard_window_start": "2026-06-13",
        "earnings_hard_window_end": "2026-06-19",
        "earnings_hard_coverage_status": "complete",
        "earnings_soft_coverage_status": "complete",
        "earnings_has_event": bool(events),
        "earnings_blocking_has_event": bool(blocking_events),
        "earnings_events": events,
        "earnings_blocking_events": blocking_events,
        "earnings_nonblocking_events": nonblocking_events,
    }


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
        "max_new_contracts": 1,
        "net_income_cny": 1_400.0,
        "option_contract_point_value_cny": 700.0,
        "implied_volatility": 0.36,
        "realized_volatility_estimate": 0.24,
        "term_matched_rv": 0.24,
        "delta": -0.20,
        "annualized_net_return_on_cash_basis": 0.12,
        "net_income": 200.0,
        "spread_ratio": 0.08,
        "open_interest": 100,
        "volume": 20,
        "dte": 30,
        "otm_pct": 0.10,
    }
    row.update(_earnings_evidence())
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
    assert "premium_edge_score" not in fields
    assert "single_trade_concentration" not in fields


def test_sell_put_underwriting_rejects_when_iv_rv_edge_is_too_low() -> None:
    from src.application.sell_put_strategy_risk import evaluate_sell_put_underwriting_row, resolve_sell_put_underwriting_config

    decision = evaluate_sell_put_underwriting_row(
        _candidate(implied_volatility=0.25, term_matched_rv=0.24),
        cfg=resolve_sell_put_underwriting_config({"strategy": "insurance_underwriting"}),
    )

    assert decision["accepted"] is False
    assert decision["rule"] == "risk_iv_rv_ratio"


def test_sell_put_underwriting_rejects_when_return_is_too_low() -> None:
    from src.application.sell_put_strategy_risk import evaluate_sell_put_underwriting_row, resolve_sell_put_underwriting_config

    decision = evaluate_sell_put_underwriting_row(
        _candidate(annualized_net_return_on_cash_basis=0.08),
        cfg=resolve_sell_put_underwriting_config(
            {"strategy": "insurance_underwriting", "min_annualized_net_return": 0.10}
        ),
    )

    assert decision["accepted"] is False
    assert decision["rule"] == "return_annualized"


def test_build_portfolio_risk_context_uses_global_holdings_and_option_context() -> None:
    from src.application.short_vol_risk_context import build_portfolio_risk_context
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


def test_build_portfolio_risk_context_does_not_relabel_cost_price_as_avg_cost() -> None:
    from src.application.short_vol_risk_context import build_portfolio_risk_context
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    risk = build_portfolio_risk_context(
        portfolio_ctx={
            "cash_by_currency": {},
            "stocks_by_symbol": {
                "0883.HK": {
                    "symbol": "0883.HK",
                    "shares": 1000,
                    "cost_price": 6.6,
                    "currency": "HKD",
                }
            },
        },
        exchange_rate_converter=CurrencyConverter(
            ExchangeRates(cny_per_hkd=0.92)
        ),
    )

    assert risk.stock_value_cny_by_symbol == {}
    assert risk.unavailable_reasons == ("stock_value_missing:0883.HK",)
    assert risk.warnings == ()


def test_enrich_and_filter_sell_put_underwriting_rejects_event_risk(tmp_path: Path) -> None:
    from src.application.sell_put_strategy_risk import enrich_and_filter_sell_put_underwriting
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    df = pd.DataFrame([_candidate(**_earnings_evidence(event_date="2026-06-13"))])

    filtered = enrich_and_filter_sell_put_underwriting(
        df_labeled=df,
        symbol="NVDA",
        sell_put_cfg={"strategy": "insurance_underwriting"},
        portfolio_ctx=None,
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
    )

    assert filtered.empty


def test_sell_put_underwriting_retains_distant_pending_earnings_event() -> None:
    from src.application.sell_put_strategy_risk import (
        evaluate_sell_put_underwriting_row,
        resolve_sell_put_underwriting_config,
    )

    decision = evaluate_sell_put_underwriting_row(
        _candidate(**_earnings_evidence(event_date="2026-06-01")),
        cfg=resolve_sell_put_underwriting_config(
            {"strategy": "insurance_underwriting"}
        ),
    )

    assert decision["accepted"] is True


def test_sell_put_underwriting_emits_all_decisions_and_resolved_policy() -> None:
    from src.application.sell_put_strategy_risk import (
        enrich_and_filter_sell_put_underwriting,
    )
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    captured: list[dict] = []
    filtered = enrich_and_filter_sell_put_underwriting(
        df_labeled=pd.DataFrame(
            [
                _candidate(contract_symbol="ACCEPTED"),
                _candidate(
                    contract_symbol="REJECTED",
                    annualized_net_return_on_cash_basis=0.09,
                ),
            ]
        ),
        symbol="NVDA",
        sell_put_cfg={
            "strategy": "insurance_underwriting",
            "min_annualized_net_return": 0.10,
        },
        portfolio_ctx=None,
        exchange_rate_converter=CurrencyConverter(
            ExchangeRates(usd_per_cny=0.14)
        ),
        decision_sink_fn=captured.extend,
    )

    assert list(filtered["contract_symbol"]) == ["ACCEPTED"]
    assert [
        item["opening_decision"]["accepted"] for item in captured
    ] == [True, False]
    assert all(
        item["normalized_input"]["policy_min_annualized_return"] == 0.10
        for item in captured
    )
    rejected = captured[1]["opening_decision"]
    assert rejected["rejects"][0]["reason"] == "return_annualized"


def test_sell_put_underwriting_does_not_hard_reject_non_earnings_event() -> None:
    from src.application.sell_put_strategy_risk import (
        evaluate_sell_put_underwriting_row,
        resolve_sell_put_underwriting_config,
    )

    decision = evaluate_sell_put_underwriting_row(
        _candidate(event_flag=True, event_types="ex_dividend", event_dates="2026-06-01"),
        cfg=resolve_sell_put_underwriting_config({"strategy": "insurance_underwriting"}),
    )

    assert decision["accepted"] is True


def test_sell_put_underwriting_fails_closed_on_stale_event_source() -> None:
    from src.application.sell_put_strategy_risk import (
        evaluate_sell_put_underwriting_row,
        resolve_sell_put_underwriting_config,
    )

    decision = evaluate_sell_put_underwriting_row(
        _candidate(earnings_evidence_status="data_unavailable", earnings_reason_code="stale"),
        cfg=resolve_sell_put_underwriting_config({"strategy": "insurance_underwriting"}),
    )

    assert decision["accepted"] is False
    assert decision["rule"] == "risk_earnings_unavailable"


def test_sell_put_underwriting_requires_complete_earnings_coverage() -> None:
    from src.application.sell_put_strategy_risk import (
        evaluate_sell_put_underwriting_row,
        resolve_sell_put_underwriting_config,
    )

    decision = evaluate_sell_put_underwriting_row(
        _candidate(earnings_evidence_status="data_unavailable", earnings_reason_code="coverage_incomplete"),
        cfg=resolve_sell_put_underwriting_config({"strategy": "insurance_underwriting"}),
    )

    assert decision["accepted"] is False
    assert decision["rule"] == "risk_earnings_unavailable"


def test_enrich_and_filter_sell_put_underwriting_rejects_when_income_fx_is_missing(tmp_path: Path) -> None:
    from src.application.sell_put_strategy_risk import enrich_and_filter_sell_put_underwriting
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    df = pd.DataFrame([_candidate(net_income_cny=None)])

    filtered = enrich_and_filter_sell_put_underwriting(
        df_labeled=df,
        symbol="NVDA",
        sell_put_cfg={"strategy": "insurance_underwriting"},
        portfolio_ctx=None,
        exchange_rate_converter=CurrencyConverter(ExchangeRates()),
    )

    assert filtered.empty


def test_enrich_and_filter_sell_put_underwriting_does_not_reject_stress_or_concentration(tmp_path: Path) -> None:
    from src.application.sell_put_strategy_risk import enrich_and_filter_sell_put_underwriting
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    df = pd.DataFrame(
        [
            _candidate(
                spot=102.0,
                implied_volatility=0.70,
                term_matched_rv=0.50,
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
    )

    assert len(filtered) == 1
    assert filtered.iloc[0]["contract_symbol"] == "NVDA260619P00100000"
    assert not bool(filtered.iloc[0]["concentration_evaluable"])
    assert filtered.iloc[0]["concentration_unavailable_reason"] == "holdings_context_missing"


def test_enrich_and_filter_sell_put_underwriting_projects_assignment_concentration(tmp_path: Path) -> None:
    from src.application.sell_put_strategy_risk import enrich_and_filter_sell_put_underwriting
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    portfolio_ctx = {
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
    }

    filtered = enrich_and_filter_sell_put_underwriting(
        df_labeled=pd.DataFrame([_candidate()]),
        symbol="NVDA",
        sell_put_cfg={"strategy": "insurance_underwriting"},
        portfolio_ctx=portfolio_ctx,
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
    )

    assert len(filtered) == 1
    row = filtered.iloc[0]
    assert row["portfolio_nav_cny"] == 850_000.0
    assert row["assignment_notional_cny"] == 70_000.0
    assert row["symbol_concentration_after"] == 0.2
    assert row["total_short_put_concentration_after"] == 0.141176
    assert row["concentration_score"] == 0.8
    assert bool(row["concentration_evaluable"])


def test_sell_put_cross_symbol_ranking_uses_projected_assignment_concentration(tmp_path: Path) -> None:
    from domain.domain.engine import rank_candidate_rows
    from src.application.sell_put_strategy_risk import enrich_and_filter_sell_put_underwriting
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    converter = CurrencyConverter(ExchangeRates(usd_per_cny=0.14))
    portfolio_ctx = {
        "_global_portfolio_ctx": {
            "cash_by_currency": {"CNY": 500_000.0},
            "stocks_by_symbol": {
                "NVDA": {"symbol": "NVDA", "shares": 10, "market_value_cny": 400_000.0},
                "AAPL": {"symbol": "AAPL", "shares": 10, "market_value_cny": 50_000.0},
            },
        },
        "_global_option_ctx": {
            "cash_secured_by_symbol_by_ccy": {"NVDA": {"USD": 14_000.0}},
            "cash_secured_total_cny": 100_000.0,
        },
    }
    rows: list[dict] = []
    for symbol in ("NVDA", "AAPL"):
        filtered = enrich_and_filter_sell_put_underwriting(
            df_labeled=pd.DataFrame(
                [
                    _candidate(
                        symbol=symbol,
                        contract_symbol=f"{symbol}_PUT",
                    )
                ]
            ),
            symbol=symbol,
            sell_put_cfg={"strategy": "insurance_underwriting"},
            portfolio_ctx=portfolio_ctx,
            exchange_rate_converter=converter,
        )
        rows.extend(filtered.to_dict("records"))

    ranked = rank_candidate_rows(rows, mode="put")

    assert [row["symbol"] for row in ranked] == ["AAPL", "NVDA"]
    assert ranked[0]["symbol_concentration_after"] < ranked[1]["symbol_concentration_after"]


def test_sell_put_underwriting_ranking_prefers_period_return_then_discount(tmp_path: Path) -> None:
    from src.application.sell_put_strategy_risk import enrich_and_filter_sell_put_underwriting
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

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
    )

    assert list(filtered["contract_symbol"]) == ["RICH", "FAR", "NEAR"]
    by_contract = filtered.set_index("contract_symbol")
    assert "premium_edge_score" not in by_contract.columns
    assert by_contract.loc["RICH", "strike_safety_margin_pct"] > by_contract.loc["NEAR", "strike_safety_margin_pct"]
