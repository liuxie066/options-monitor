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


def test_render_yield_enhancement_alerts_defaults_to_symbol_scoped_paths(tmp_path: Path) -> None:
    from src.application.render_yield_enhancement_alerts import render_yield_enhancement_alerts

    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    input_path = report_dir / "nvda_yield_enhancement_candidates.csv"
    pd.DataFrame([_sample_candidate()]).to_csv(input_path, index=False)

    text = render_yield_enhancement_alerts(
        report_dir=report_dir,
        symbol="NVDA",
        top=1,
    )

    output_path = report_dir / "nvda_combo_yield_alerts.txt"
    assert "[组合收益推荐] NVDA 2026-06-19 95P + 110C" in text
    assert "净权利金年化: 13.00%" in text
    assert "Put: strike=95 | bid=3.00 | delta=-0.25" in text
    assert "Call候选: 2个" in text
    assert output_path.exists()
    assert output_path.read_text(encoding="utf-8") == text
    assert not (report_dir / "yield_enhancement_alerts.txt").exists()


def test_render_yield_enhancement_alerts_keeps_aggregate_fallback_without_symbol(tmp_path: Path) -> None:
    from src.application.render_yield_enhancement_alerts import render_yield_enhancement_alerts

    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    pd.DataFrame([_sample_candidate("AAPL")]).to_csv(
        report_dir / "yield_enhancement_candidates.csv",
        index=False,
    )

    text = render_yield_enhancement_alerts(
        report_dir=report_dir,
        top=1,
    )

    output_path = report_dir / "combo_yield_alerts.txt"
    assert "[组合收益推荐] AAPL 2026-06-19 95P + 110C" in text
    assert output_path.exists()
    assert output_path.read_text(encoding="utf-8") == text


def test_render_yield_enhancement_alerts_preserves_explicit_paths(tmp_path: Path) -> None:
    from src.application.render_yield_enhancement_alerts import render_yield_enhancement_alerts

    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    input_path = tmp_path / "custom.csv"
    output_path = tmp_path / "custom.txt"
    pd.DataFrame([_sample_candidate()]).to_csv(input_path, index=False)

    text = render_yield_enhancement_alerts(
        input_path=input_path,
        output_path=output_path,
        report_dir=report_dir,
        symbol="NVDA",
        top=1,
    )

    assert "[组合收益推荐] NVDA 2026-06-19 95P + 110C" in text
    assert output_path.exists()
    assert output_path.read_text(encoding="utf-8") == text
    assert not (report_dir / "nvda_combo_yield_alerts.txt").exists()


def test_render_yield_enhancement_alerts_shows_diagonal_horizons_without_terminal_prediction(tmp_path: Path) -> None:
    from src.application.render_yield_enhancement_alerts import render_yield_enhancement_alerts

    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    row = _sample_candidate()
    row.update(
        {
            "expiry_structure": "diagonal",
            "put_expiration": "2026-06-19",
            "call_expiration": "2026-07-17",
            "put_dte": 44,
            "call_dte": 72,
            "terminal_metrics_status": "not_evaluable_diagonal",
            "terminal_metrics_unsupported_reason": "future_call_residual_value_not_modeled",
            "expected_move": None,
            "scenario_score": None,
            "annualized_scenario_score": None,
        }
    )
    pd.DataFrame([row]).to_csv(report_dir / "nvda_combo_yield_candidates.csv", index=False)

    text = render_yield_enhancement_alerts(report_dir=report_dir, symbol="NVDA", top=1)

    assert "[组合收益推荐] NVDA Put 2026-06-19 95P + Call 2026-07-17 110C" in text
    assert "DTE: Put=44 | Call=72" in text
    assert "到期终值指标: 不可评估（不预测 Put 到期时 Call 剩余价值）" in text
    assert "Expected Move:" not in text
    assert "场景评分:" not in text
    assert "上行弹性:" not in text
