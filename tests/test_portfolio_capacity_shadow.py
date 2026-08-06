from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.application.portfolio_capacity_shadow import write_portfolio_capacity_shadow


def test_write_portfolio_capacity_shadow_writes_header_when_no_candidates(tmp_path: Path) -> None:
    result = write_portfolio_capacity_shadow(report_dir=tmp_path, account="lx")

    rows = pd.read_csv(tmp_path / "portfolio_capacity_shadow.csv")
    assert result["rows"] == 0
    assert rows.empty
    assert {"account", "symbol", "allocation_status", "capacity_before", "capacity_required"}.issubset(rows.columns)


def test_write_portfolio_capacity_shadow_keeps_candidate_files_unchanged(tmp_path: Path) -> None:
    candidate = tmp_path / "nvda_sell_put_candidates_labeled.csv"
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "contract_symbol": "NVDA260619P100000",
                "cash_free_cny": 20_000,
                "cash_required_cny": 10_000,
            }
        ]
    ).to_csv(candidate, index=False)
    before = candidate.read_bytes()

    result = write_portfolio_capacity_shadow(report_dir=tmp_path, account="lx")

    assert candidate.read_bytes() == before
    assert result["shadow_only"] is True
    rows = pd.read_csv(tmp_path / "portfolio_capacity_shadow.csv").to_dict("records")
    assert rows[0]["allocation_status"] == "allocated"
    assert rows[0]["account"] == "lx"


def test_sell_put_capacity_uses_strategy_rank_across_symbol_files(tmp_path: Path) -> None:
    common = {
        "cash_free_cny": 10_000,
        "cash_required_cny": 10_000,
        "strategy_profile": "insurance_underwriting",
        "spread_ratio": 0.1,
        "net_income": 100,
    }
    pd.DataFrame(
        [
            {
                **common,
                "symbol": "AAPL",
                "contract_symbol": "AAPL260619P100000",
                "annualized_net_return_on_cash_basis": 0.18,
                "period_net_return_on_cash_basis": 0.18,
                "premium_edge_score": 0.8,
                "strike_safety_margin_pct": 0.05,
            }
        ]
    ).to_csv(tmp_path / "aapl_sell_put_candidates_labeled.csv", index=False)
    pd.DataFrame(
        [
            {
                **common,
                "symbol": "NVDA",
                "contract_symbol": "NVDA260619P100000",
                "annualized_net_return_on_cash_basis": 0.20,
                "period_net_return_on_cash_basis": 0.20,
                "premium_edge_score": 1.2,
                "strike_safety_margin_pct": 0.15,
            }
        ]
    ).to_csv(tmp_path / "nvda_sell_put_candidates_labeled.csv", index=False)

    write_portfolio_capacity_shadow(report_dir=tmp_path, account="lx")

    rows = pd.read_csv(tmp_path / "portfolio_capacity_shadow.csv").to_dict("records")
    assert [row["contract_symbol"] for row in rows] == [
        "NVDA260619P100000",
        "AAPL260619P100000",
    ]
    assert [row["allocation_status"] for row in rows] == ["allocated", "capacity_blocked"]


def test_covered_call_capacity_uses_annualized_return_before_upside_margin(tmp_path: Path) -> None:
    common = {
        "symbol": "NVDA",
        "shares_available_for_cover": 100,
        "multiplier": 100,
        "strategy_profile": "insurance_underwriting",
        "spread_ratio": 0.1,
        "open_interest": 100,
        "net_income": 100,
    }
    pd.DataFrame(
        [
            {
                **common,
                "contract_symbol": "NVDA_HIGH_RETURN",
                "annualized_net_premium_return": 0.20,
                "strike_upside_margin_pct": 0.05,
            },
            {
                **common,
                "contract_symbol": "NVDA_HIGH_UPSIDE",
                "annualized_net_premium_return": 0.18,
                "strike_upside_margin_pct": 0.20,
            },
        ]
    ).to_csv(tmp_path / "nvda_sell_call_candidates.csv", index=False)

    write_portfolio_capacity_shadow(report_dir=tmp_path, account="lx")

    rows = pd.read_csv(tmp_path / "portfolio_capacity_shadow.csv").to_dict("records")
    assert [row["contract_symbol"] for row in rows] == [
        "NVDA_HIGH_RETURN",
        "NVDA_HIGH_UPSIDE",
    ]
    assert [row["allocation_status"] for row in rows] == ["allocated", "alternative_not_allocated"]
