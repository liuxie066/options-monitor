from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd

from domain.domain.engine import rank_candidate_rows


ROOT = Path(__file__).resolve().parents[1]


def test_sell_put_rank_uses_period_return_band_and_assignment_discount() -> None:
    rows = [
        {
            "symbol": "NVDA",
            "contract_symbol": "HIGH_RETURN",
            "period_net_return_on_cash_basis": 0.0100,
            "net_assignment_discount_pct": 0.05,
        },
        {
            "symbol": "NVDA",
            "contract_symbol": "NEAR_RETURN_SAFER",
            "period_net_return_on_cash_basis": 0.0081,
            "net_assignment_discount_pct": 0.08,
        },
        {
            "symbol": "NVDA",
            "contract_symbol": "NEXT_BAND",
            "period_net_return_on_cash_basis": 0.0079,
            "net_assignment_discount_pct": 0.20,
        },
    ]

    assert [
        row["contract_symbol"] for row in rank_candidate_rows(rows, mode="put")
    ] == ["NEAR_RETURN_SAFER", "HIGH_RETURN", "NEXT_BAND"]


def test_manual_alert_renderer_uses_canonical_candidate_rank(tmp_path: Path) -> None:
    from src.application.render_sell_put_alerts import render_sell_put_alerts

    input_path = tmp_path / "sell_put_candidates.csv"
    output_path = tmp_path / "sell_put_alerts.txt"
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "contract_symbol": "LOW",
                "expiration": "2026-09-18",
                "strike": 90.0,
                "spot": 100.0,
                "dte": 43,
                "mid": 2.0,
                "net_income": 190.0,
                "period_net_return_on_cash_basis": 0.008,
                "annualized_net_return_on_cash_basis": 0.068,
                "breakeven": 88.1,
                "otm_pct": 0.10,
                "risk_label": "保守",
                "spread_ratio": 0.05,
                "open_interest": 100,
                "volume": 10,
            },
            {
                "symbol": "NVDA",
                "contract_symbol": "HIGH",
                "expiration": "2026-09-18",
                "strike": 95.0,
                "spot": 100.0,
                "dte": 43,
                "mid": 3.0,
                "net_income": 290.0,
                "period_net_return_on_cash_basis": 0.012,
                "annualized_net_return_on_cash_basis": 0.102,
                "breakeven": 92.1,
                "otm_pct": 0.05,
                "risk_label": "中性",
                "spread_ratio": 0.05,
                "open_interest": 100,
                "volume": 10,
            },
        ]
    ).to_csv(input_path, index=False)

    rendered = render_sell_put_alerts(
        input_path=input_path,
        output_path=output_path,
        top=1,
        base_dir=ROOT,
    )

    assert " 95P" in rendered
    assert " 90P" not in rendered


def test_removed_candidate_strategy_module_and_parallel_runtime_imports_stay_absent() -> None:
    assert importlib.util.find_spec("domain.domain.engine.candidate_strategy") is None
    for relative in (
        "src/application/candidate_scanning.py",
        "src/application/scan_sell_put.py",
        "src/application/scan_sell_call.py",
        "src/application/render_sell_put_alerts.py",
        "src/application/render_sell_call_alerts.py",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "candidate_strategy" not in text
        assert "rank_scored_candidates" not in text
        assert "strategy_score" not in text
