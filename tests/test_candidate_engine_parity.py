from __future__ import annotations

import pandas as pd


def test_candidate_engine_put_rank_is_canonical() -> None:
    from domain.domain.engine import rank_candidate_rows

    rows = [
        {"contract_symbol": "A", "period_net_return_on_cash_basis": 0.012, "net_income": 70},
        {"contract_symbol": "B", "period_net_return_on_cash_basis": 0.015, "net_income": 60},
        {"contract_symbol": "C", "period_net_return_on_cash_basis": 0.012, "net_income": 80},
    ]
    engine = rank_candidate_rows(rows, mode="put")

    assert [r["contract_symbol"] for r in engine] == ["B", "C", "A"]


def test_candidate_engine_call_rank_is_canonical() -> None:
    from domain.domain.engine import rank_candidate_rows

    rows = [
        {"contract_symbol": "C1", "annualized_net_premium_return": 0.10, "if_exercised_total_return": 0.20, "net_income": 110},
        {"contract_symbol": "C2", "annualized_net_premium_return": 0.10, "if_exercised_total_return": 0.21, "net_income": 100},
        {"contract_symbol": "C3", "annualized_net_premium_return": 0.09, "if_exercised_total_return": 0.30, "net_income": 130},
    ]
    engine = rank_candidate_rows(rows, mode="call")

    assert [r["contract_symbol"] for r in engine] == ["C1", "C2", "C3"]


def test_candidate_engine_put_summary_uses_canonical_recommendation_order() -> None:
    from domain.domain.engine import rank_candidate_rows
    from src.application.report_summaries import summarize_sell_put

    rows = [
        {
            "symbol": "NVDA",
            "contract_symbol": "P_TARGET_DELTA",
            "expiration": "2026-06-18",
            "strike": 130.0,
            "dte": 45,
            "mid": 1.5,
            "net_income": 150.0,
            "annualized_net_return_on_cash_basis": 0.12,
            "delta": -0.22,
        },
        {
            "symbol": "NVDA",
            "contract_symbol": "P_FAR_DELTA",
            "expiration": "2026-06-18",
            "strike": 140.0,
            "dte": 45,
            "mid": 2.0,
            "net_income": 200.0,
            "annualized_net_return_on_cash_basis": 0.20,
            "delta": -0.10,
        },
    ]
    summary = summarize_sell_put(pd.DataFrame(rows), "NVDA")
    engine_top = rank_candidate_rows(rows, mode="put")[0]

    assert engine_top["contract_symbol"] == "P_FAR_DELTA"
    assert summary["top_contract"] == "2026-06-18 140P"
    assert summary["cash_required_usd"] is None


def test_candidate_engine_put_summary_keeps_same_symbol_usage_fields() -> None:
    from src.application.report_summaries import summarize_sell_put

    rows = [
        {
            "symbol": "3690.HK",
            "contract_symbol": "P_TOP",
            "expiration": "2026-05-28",
            "strike": 75.0,
            "dte": 36,
            "mid": 0.965,
            "net_income": 468.0,
            "annualized_net_return_on_cash_basis": 0.128,
            "delta": -0.16,
            "implied_volatility": 0.4138,
            "cash_secured_used_usd": 0.0,
            "cash_secured_used_cny_total": 200000.0,
            "cash_secured_used_cny_symbol": 45000.0,
            "cash_required_cny": 32715.0,
        },
    ]

    summary = summarize_sell_put(pd.DataFrame(rows), "3690.HK")

    assert summary["cash_secured_used_cny_total"] == 200000.0
    assert summary["cash_secured_used_cny_symbol"] == 45000.0


def test_candidate_engine_put_summary_keeps_opend_earnings_fields() -> None:
    from src.application.report_summaries import summarize_sell_put

    rows = [
        {
            "symbol": "AAPL",
            "contract_symbol": "P_TOP",
            "expiration": "2026-06-19",
            "strike": 180.0,
            "dte": 24,
            "mid": 2.1,
            "net_income": 210.0,
            "annualized_net_return_on_cash_basis": 0.18,
            "delta": -0.22,
            "earnings_evidence_status": "ready",
            "earnings_has_event": True,
            "earnings_event_dates": "2026-06-10",
        },
    ]

    summary = summarize_sell_put(pd.DataFrame(rows), "AAPL")

    assert summary["earnings_evidence_status"] == "ready"
    assert summary["earnings_has_event"] is True
    assert summary["earnings_event_dates"] == "2026-06-10"


def test_candidate_engine_call_summary_uses_canonical_recommendation_order() -> None:
    from domain.domain.engine import rank_candidate_rows
    from src.application.report_summaries import summarize_sell_call

    rows = [
        {
            "symbol": "AAPL",
            "contract_symbol": "C_TARGET_DELTA",
            "expiration": "2026-06-18",
            "strike": 230.0,
            "dte": 45,
            "mid": 1.5,
            "net_income": 150.0,
            "annualized_net_premium_return": 0.12,
            "if_exercised_total_return": 0.10,
            "delta": 0.28,
            "covered_contracts_available": 1,
        },
        {
            "symbol": "AAPL",
            "contract_symbol": "C_FAR_DELTA",
            "expiration": "2026-06-18",
            "strike": 220.0,
            "dte": 45,
            "mid": 2.0,
            "net_income": 200.0,
            "annualized_net_premium_return": 0.20,
            "if_exercised_total_return": 0.15,
            "delta": 0.40,
        },
    ]
    summary = summarize_sell_call(pd.DataFrame(rows), "AAPL")
    engine_top = rank_candidate_rows(rows, mode="call")[0]

    assert engine_top["contract_symbol"] == "C_FAR_DELTA"
    assert summary["top_contract"] == "2026-06-18 220C"
