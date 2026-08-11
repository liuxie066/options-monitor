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

