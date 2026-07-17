from __future__ import annotations

import pandas as pd


def test_summarize_sell_put_tolerates_incomplete_ranked_candidate() -> None:
    from src.application.report_summaries import summarize_sell_put

    summary = summarize_sell_put(
        pd.DataFrame(
            [
                {
                    "symbol": "NVDA",
                    "contract_symbol": "NVDA-INCOMPLETE",
                }
            ]
        ),
        "NVDA",
    )

    assert summary["candidate_count"] == 1
    assert summary["top_contract"] == "NVDA-INCOMPLETE"
    assert summary["expiration"] == ""
    assert summary["strike"] is None
    assert summary["dte"] is None
    assert summary["net_income"] is None
    assert summary["annualized_return"] is None


def test_summarize_yield_enhancement_tolerates_incomplete_ranked_candidate() -> None:
    from src.application.report_summaries import summarize_yield_enhancement

    summary = summarize_yield_enhancement(
        pd.DataFrame(
            [
                {
                    "symbol": "NVDA",
                    "combo_contract": "NVDA-COMBO-INCOMPLETE",
                }
            ]
        ),
        "NVDA",
    )

    assert summary["candidate_count"] == 1
    assert summary["top_contract"] == "NVDA-COMBO-INCOMPLETE"
    assert summary["strike"] is None
    assert summary["dte"] is None
    assert summary["net_income"] is None
    assert summary["annualized_return"] is None


def test_summarize_staggered_combo_preserves_leg_horizons_and_no_shared_annualization() -> None:
    from src.application.report_summaries import summarize_yield_enhancement

    summary = summarize_yield_enhancement(
        pd.DataFrame(
            [
                {
                    "symbol": "NVDA",
                    "structure_mode": "staggered_expiry_pair",
                    "put_expiration": "2026-08-21",
                    "put_dte": 35,
                    "call_expiration": "2026-10-16",
                    "call_dte": 91,
                    "expiry_gap_days": 56,
                    "put_strike": 100.0,
                    "call_strike": 120.0,
                    "combo_net_credit": 10.0,
                    "net_credit": 10.0,
                    "funding_accepted": True,
                    "call_cost_to_put_credit": 0.956,
                    "funding_ratio": 1.046,
                    "strike_safety_margin_pct": 0.18,
                }
            ]
        ),
        "NVDA",
    )

    assert summary["top_contract"] == "2026-08-21 100P + 2026-10-16 120C"
    assert summary["put_expiration"] == "2026-08-21"
    assert summary["call_expiration"] == "2026-10-16"
    assert summary["put_dte"] == 35
    assert summary["call_dte"] == 91
    assert summary["dte"] is None
    assert summary["annualized_return"] is None
    assert summary["note"] == "Put已独立通过接货、现金、事件、收益和流动性门槛"
