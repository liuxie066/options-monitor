from __future__ import annotations

import pandas as pd
import pytest


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


def test_summarize_combo_yield_tolerates_incomplete_ranked_candidate() -> None:
    from src.application.report_summaries import summarize_combo_yield

    summary = summarize_combo_yield(
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



@pytest.mark.parametrize("symbol,currency", [("NVDA", "USD"), ("0700.HK", "HKD")])

@pytest.mark.parametrize("capacity", [0, None, float("nan"), float("inf"), -1, 2])
def test_summary_to_alert_preserves_canonical_capacity(symbol, currency, capacity, tmp_path):
    from src.application.report_summaries import summarize_sell_put
    from src.application.alert_engine import classify_alert
    candidate = {"symbol": symbol, "contract_symbol": "synthetic-contract", "strike": 100,
        "annualized_net_return_on_cash_basis": 1.0, "spread_ratio": 0.01,
        "max_new_contracts": capacity, "cash_native_currency": currency,
        "cash_required_native": 10000, "cash_free_effective_native": 20000,
        "cash_available_effective_native": 20000, "cash_capacity_basis": "native_cash_first",
        "cash_fx_status": "missing" if capacity is None else ""}
    from domain.domain import normalize_processor_row
    summary = normalize_processor_row(summarize_sell_put(pd.DataFrame([candidate]), symbol))
    assert summary["cash_native_currency"] == currency
    assert summary["cash_required_native"] == 10000
    # Prove the public CSV consumer, not just the in-memory summary.
    path = tmp_path / "summary.csv"
    pd.DataFrame([summary]).to_csv(path, index=False)
    row = pd.read_csv(path).iloc[0]
    level, reason = classify_alert(row)
    if capacity == 2:
        assert level == "high"
    else:
        assert level == "low"
        assert "容量" in reason
    if capacity == 0:
        assert summary["max_new_contracts"] == 0


def test_summary_native_capacity_belongs_to_ranked_top_candidate():
    from src.application.report_summaries import summarize_sell_put
    rows = [{"contract_symbol": "lower", "annualized_net_return_on_cash_basis": .1, "max_new_contracts": 0,
             "cash_required_native": 100, "cash_native_currency": "HKD"},
            {"contract_symbol": "higher", "annualized_net_return_on_cash_basis": 1, "max_new_contracts": 2,
             "cash_required_native": 200, "cash_native_currency": "HKD"}]
    summary = summarize_sell_put(pd.DataFrame(rows), "0700.HK")
    assert "higher" in summary["top_contract"]
    assert summary["max_new_contracts"] == 2
    assert summary["cash_required_native"] == 200


def test_empty_sell_put_summary_keeps_capacity_unknown():
    from src.application.report_summaries import summarize_sell_put
    summary = summarize_sell_put(pd.DataFrame(), "0700.HK")
    assert summary["max_new_contracts"] is None
    assert summary["cash_native_currency"] is None
