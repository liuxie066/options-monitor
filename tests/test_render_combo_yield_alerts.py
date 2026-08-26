from __future__ import annotations

from pathlib import Path

import pandas as pd


def _sample_candidate(symbol: str = "NVDA") -> dict:
    return {
        "symbol": symbol,
        "expiration": "2026-06-19",
        "put_strike": 95.0,
        "call_strike": 110.0,
        "option_ccy": "USD",
        "currency": "USD",
        "dte": 44,
        "put_delta": -0.25,
        "put_bid": 3.0,
        "call_ask": 1.5,
        "call_delta": 0.32,
        "net_credit": 145.33,
        "annualized_net_credit_yield": 0.13,
        "scenario_score": 0.0458,
        "annualized_scenario_score": 0.38,
        "expected_move": 14.24,
        "expected_move_iv": 0.41,
        "combo_spread_ratio": 0.10,
        "call_candidate_count": 2,
        "put_open_interest": 1200,
        "call_open_interest": 980,
    }


def test_render_combo_yield_alerts_renders_typed_candidates(tmp_path: Path) -> None:
    from src.application.render_combo_yield_alerts import render_combo_yield_alerts

    output_path = tmp_path / "nvda_combo_yield_alerts.txt"

    text = render_combo_yield_alerts(
        candidates=pd.DataFrame([_sample_candidate()]),
        output_path=output_path,
        top=1,
    )

    assert "[组合收益推荐] NVDA 2026-06-19 95P + 110C" in text
    assert "净权利金年化: 13.00%" in text
    assert "Put: strike=95 | bid=3.00 | delta=-0.25" in text
    assert "Call候选: 2个" in text
    assert output_path.exists()
    assert output_path.read_text(encoding="utf-8") == text


def test_render_combo_yield_alerts_accepts_record_lists(tmp_path: Path) -> None:
    from src.application.render_combo_yield_alerts import render_combo_yield_alerts

    output_path = tmp_path / "combo_yield_alerts.txt"

    text = render_combo_yield_alerts(
        candidates=[_sample_candidate("AAPL")],
        output_path=output_path,
        top=1,
    )

    assert "[组合收益推荐] AAPL 2026-06-19 95P + 110C" in text
    assert output_path.exists()
    assert output_path.read_text(encoding="utf-8") == text


def test_render_combo_yield_alerts_writes_empty_message(tmp_path: Path) -> None:
    from src.application.render_combo_yield_alerts import render_combo_yield_alerts

    output_path = tmp_path / "custom.txt"

    text = render_combo_yield_alerts(
        candidates=pd.DataFrame(),
        output_path=output_path,
        top=1,
    )

    assert text == "无候选提醒。"
    assert output_path.read_text(encoding="utf-8") == text
