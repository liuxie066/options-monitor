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


def test_render_staggered_combo_yield_uses_separate_expirations_and_estimated_fee_wording(tmp_path: Path) -> None:
    from src.application.render_yield_enhancement_alerts import render_yield_enhancement_alerts

    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    candidate = {
        **_sample_candidate(),
        "structure_mode": "staggered_expiry_pair",
        "expiration": "2026-08-21",
        "dte": 35,
        "put_expiration": "2026-08-21",
        "put_dte": 35,
        "call_expiration": "2026-10-16",
        "call_dte": 91,
        "expiry_gap_days": 56,
        "put_net_credit": 228.0,
        "call_total_cost": 218.0,
        "combo_net_credit": 10.0,
        "call_cost_to_put_credit": 218.0 / 228.0,
        "funding_ratio": 228.0 / 218.0,
        "strike_safety_margin_pct": 0.18,
        "cash_required_usd": 10000.0,
        "annualized_net_credit_yield": None,
        "funding_accepted": True,
    }
    input_path = report_dir / "nvda_combo_yield_candidates.csv"
    pd.DataFrame([candidate]).to_csv(input_path, index=False)

    text = render_yield_enhancement_alerts(report_dir=report_dir, symbol="NVDA", top=1)

    assert "🧩 Combo Yield · 错期全额融资 NVDA" in text
    assert "卖 95P @ 2026-08-21（35天）" in text
    assert "买 110C @ 2026-10-16（91天）" in text
    assert "按费用模型估算净收入=228.00 USD" in text
    assert "按费用模型估算总成本=218.00 USD" in text
    assert "资金利用率=95.61%" in text
    assert "覆盖率=104.59%" in text
    assert "Call比Put晚56天 | 两腿各1张" in text
    assert "净权利金年化" not in text
    assert "场景评分" not in text
    assert "Expected Move" not in text
