from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pandas as pd
import pytest


def _combo_rank_pair(decisive: str) -> list[dict]:
    common = {
        "funding_accepted": True, "net_credit_retention": 0.8, "call_delta": 0.15,
        "combo_spread_ratio": 0.1, "put_open_interest": 1000, "call_open_interest": 1000,
        "put_assignment_margin_pct": 0.05, "put_otm_pct": 0.05,
        "annualized_net_credit_yield": 0.2, "upside_breakeven_pct_above_spot": 0.1,
        "call_cost_to_put_credit": 0.2, "put_strike": 95, "call_strike": 110,
        "put_bid": 6, "call_ask": 1, "dte": 30, "expiration": "2026-11-20",
        "risk_label": "ok", "option_ccy": "USD",
    }
    a = {**common, "symbol": "AAPL", "combo_contract": "AAPL-COMBO", "net_credit": 5, "combo_net_credit": 5}
    b = {**common, "symbol": "NVDA", "combo_contract": "NVDA-COMBO", "net_credit": 4, "combo_net_credit": 4}
    if decisive in {"put_open_interest", "call_open_interest"}:
        a[decisive] = 100
    elif decisive == "put_assignment_margin_pct":
        b[decisive] = 0.1
    elif decisive == "annualized_net_credit_yield":
        b[decisive] = 0.3
    return [a, b]


@pytest.mark.parametrize("field", ["put_open_interest", "call_open_interest", "put_assignment_margin_pct", "annualized_net_credit_yield"])
def test_combo_summary_keeps_original_rank_inputs(field):
    from src.application.report_summaries import summarize_combo_yield

    candidate = _combo_rank_pair(field)[1]
    summary = summarize_combo_yield(pd.DataFrame([candidate]), candidate["symbol"])
    assert summary[field] == candidate[field]


def test_combo_rank_inputs_have_none_defaults_for_empty_and_partial_summary():
    from domain.domain.canonical_schema import normalize_processor_row
    from src.application.report_summaries import summarize_combo_yield

    for frame in (pd.DataFrame(), pd.DataFrame([{"symbol": "NVDA", "combo_contract": "partial"}])):
        summary = summarize_combo_yield(frame, "NVDA")
        canonical = normalize_processor_row(summary)
        for field in ("put_open_interest", "call_open_interest", "put_assignment_margin_pct", "annualized_net_credit_yield"):
            assert summary[field] is None
            assert canonical[field] is None


def _combo_stage_alert(tmp_path, example_config_path, candidates):
    from domain.domain.canonical_schema import normalize_processor_rows
    from src.application.report_summaries import summarize_combo_yield

    reports, state = tmp_path / "reports", tmp_path / "state"
    reports.mkdir()
    state.mkdir()
    rows = [summarize_combo_yield(pd.DataFrame([row]), row["symbol"]) for row in candidates]
    current = reports / "symbols_summary.csv"
    pd.DataFrame(normalize_processor_rows(rows)).to_csv(current, index=False)
    before = current.read_bytes()
    config_before = example_config_path.read_bytes()
    result = subprocess.run(
        [sys.executable, "-m", "src.interfaces.cli.main", "scan-pipeline",
         "--config", str(example_config_path), "--stage-only", "alert",
         "--report-dir", str(reports), "--state-dir", str(state)],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "OM_RUNTIME_ROOT": str(tmp_path)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert current.read_bytes() == before
    assert example_config_path.read_bytes() == config_before
    assert not (state / "symbols_summary_prev.csv").exists()
    return (reports / "symbols_alerts.txt").read_text(encoding="utf-8"), pd.read_csv(current)


@pytest.mark.parametrize("reverse_input", [False, True])
@pytest.mark.parametrize("decisive", ["put_open_interest", "call_open_interest", "put_assignment_margin_pct", "annualized_net_credit_yield", "tie"])
def test_combo_candidate_order_survives_summary_canonical_csv_and_public_stage_cli(
    tmp_path, example_config_path, decisive, reverse_input,
):
    from domain.domain.engine import rank_combo_yield_rows
    from src.application.alert_engine import classify_alert

    candidates = _combo_rank_pair(decisive)
    expected = [row["symbol"] for row in rank_combo_yield_rows(candidates)]
    assert expected == (["AAPL", "NVDA"] if decisive == "tie" else ["NVDA", "AAPL"])
    if reverse_input:
        candidates.reverse()
    text, frame = _combo_stage_alert(tmp_path, example_config_path, candidates)
    assert [classify_alert(row)[0] for _, row in frame.iterrows()] == ["high", "high"]
    lines = [line for line in text.splitlines() if " | combo_yield | " in line]
    assert len(lines) == 2
    assert all(symbol in line.split(" | ", 1)[0] for symbol, line in zip(expected, lines, strict=True))


def test_combo_alert_priority_groups_stay_ahead_of_candidate_rank(tmp_path, example_config_path):
    from domain.domain.engine import rank_combo_yield_rows
    from src.application.alert_engine import classify_alert

    candidates = _combo_rank_pair("put_open_interest")
    candidates[1]["annualized_net_credit_yield"] = 0
    assert rank_combo_yield_rows(candidates)[0]["symbol"] == "NVDA"
    text, frame = _combo_stage_alert(tmp_path, example_config_path, candidates)
    assert [classify_alert(row)[0] for _, row in frame.iterrows()] == ["high", "low"]
    lines = [line for line in text.splitlines() if " | combo_yield | " in line]
    assert len(lines) == 2
    assert "AAPL" in lines[0] and "NVDA" in lines[1]
    assert text.index("## 高优先级") < text.index("## 低优先级")


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
