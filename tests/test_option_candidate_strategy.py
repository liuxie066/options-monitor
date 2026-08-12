from __future__ import annotations

import importlib.util
from pathlib import Path

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


def test_removed_candidate_strategy_module_and_parallel_runtime_imports_stay_absent() -> None:
    assert importlib.util.find_spec("domain.domain.engine.candidate_strategy") is None
    for relative in (
        "src/application/candidate_scanning.py",
        "src/application/scan_sell_put.py",
        "src/application/scan_sell_call.py",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "candidate_strategy" not in text
        assert "rank_scored_candidates" not in text
        assert "strategy_score" not in text
