from __future__ import annotations

import re
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd


def _add_repo_to_syspath() -> Path:
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    return base


def _extract_title_strikes(text: str) -> list[float]:
    strikes: list[float] = []
    for line in text.splitlines():
        if not line.startswith("[Sell Call 候选]"):
            continue
        m = re.search(r" ([0-9]+(?:\.[0-9]+)?)C$", line.strip())
        if m:
            strikes.append(float(m.group(1)))
    return strikes


def test_scan_sell_call_filter_and_rank_baseline() -> None:
    _add_repo_to_syspath()
    from scripts.scan_sell_call import run_sell_call_scan

    with TemporaryDirectory() as td:
        root = Path(td)
        parsed = root / "parsed"
        parsed.mkdir(parents=True, exist_ok=True)
        out_path = root / "sell_call_candidates.csv"

        pd.DataFrame(
            [
                # pass, lower if_exercised_total_return
                {
                    "symbol": "AAPL",
                    "option_type": "call",
                    "expiration": "2026-05-15",
                    "dte": 30,
                    "contract_symbol": "A",
                    "multiplier": 100,
                    "currency": "USD",
                    "strike": 115.0,
                    "spot": 100.0,
                    "bid": 1.4,
                    "ask": 1.6,
                    "last_price": 1.5,
                    "mid": 1.5,
                    "open_interest": 200,
                    "volume": 50,
                    "implied_volatility": 0.30,
                    "delta": 0.25,
                },
                # pass, same annualized but higher if_exercised_total_return => should rank first
                {
                    "symbol": "AAPL",
                    "option_type": "call",
                    "expiration": "2026-05-15",
                    "dte": 30,
                    "contract_symbol": "B",
                    "multiplier": 100,
                    "currency": "USD",
                    "strike": 120.0,
                    "spot": 100.0,
                    "bid": 1.4,
                    "ask": 1.6,
                    "last_price": 1.5,
                    "mid": 1.5,
                    "open_interest": 200,
                    "volume": 50,
                    "implied_volatility": 0.30,
                    "delta": 0.22,
                },
                # fail annualized
                {
                    "symbol": "AAPL",
                    "option_type": "call",
                    "expiration": "2026-05-15",
                    "dte": 30,
                    "contract_symbol": "C",
                    "multiplier": 100,
                    "currency": "USD",
                    "strike": 130.0,
                    "spot": 100.0,
                    "bid": 0.15,
                    "ask": 0.25,
                    "last_price": 0.2,
                    "mid": 0.2,
                    "open_interest": 200,
                    "volume": 50,
                    "implied_volatility": 0.30,
                    "delta": 0.10,
                },
                # fail if_exercised_total_return
                {
                    "symbol": "AAPL",
                    "option_type": "call",
                    "expiration": "2026-05-15",
                    "dte": 30,
                    "contract_symbol": "D",
                    "multiplier": 100,
                    "currency": "USD",
                    "strike": 101.0,
                    "spot": 100.0,
                    "bid": 1.4,
                    "ask": 1.6,
                    "last_price": 1.5,
                    "mid": 1.5,
                    "open_interest": 200,
                    "volume": 50,
                    "implied_volatility": 0.30,
                    "delta": 0.45,
                },
                # fail D3 open-interest
                {
                    "symbol": "AAPL",
                    "option_type": "call",
                    "expiration": "2026-05-15",
                    "dte": 30,
                    "contract_symbol": "E",
                    "multiplier": 100,
                    "currency": "USD",
                    "strike": 125.0,
                    "spot": 100.0,
                    "bid": 0.5,
                    "ask": 1.5,
                    "last_price": 1.0,
                    "mid": 1.0,
                    "open_interest": 5,
                    "volume": 50,
                    "implied_volatility": 0.30,
                    "delta": 0.2,
                },
            ]
        ).to_csv(parsed / "AAPL_required_data.csv", index=False)

        out = run_sell_call_scan(
            symbols=["AAPL"],
            input_root=root,
            output=out_path,
            avg_cost=100.0,
            shares=100,
            min_annualized_net_return=0.10,
            min_if_exercised_total_return=0.15,
            min_open_interest=10,
            quiet=True,
        )

        assert list(out["contract_symbol"]) == ["B", "A"]
        reject_path = out_path.with_name(f"{out_path.stem}_reject_log.csv")
        reject_log = pd.read_csv(reject_path)
        assert not reject_log.empty
        assert set(reject_log["reject_stage"].dropna().astype(str).tolist()) == {"step3_risk_gate"}


def test_render_sell_call_rank_order_consistent_with_strategy() -> None:
    _add_repo_to_syspath()
    from scripts.option_candidate_strategy import build_strategy_config, rank_candidates, score_candidates
    from scripts.render_sell_call_alerts import render_sell_call_alerts

    with TemporaryDirectory() as td:
        root = Path(td)
        in_path = root / "sell_call_candidates.csv"
        out_path = root / "sell_call_alerts.txt"

        df = pd.DataFrame(
            [
                {
                    "symbol": "AAPL",
                    "expiration": "2026-05-15",
                    "strike": 112.0,
                    "spot": 100.0,
                    "dte": 30,
                    "mid": 1.2,
                    "avg_cost": 90.0,
                    "shares_total": 200,
                    "shares_locked": 0,
                    "shares_available_for_cover": 200,
                    "covered_contracts_available": 2,
                    "is_fully_covered_available": True,
                    "net_income": 120.0,
                    "annualized_net_premium_return": 0.11,
                    "if_exercised_total_return": 0.20,
                    "strike_above_spot_pct": 0.12,
                    "strike_above_cost_pct": 0.24,
                    "risk_label": "保守",
                    "spread_ratio": 0.10,
                    "open_interest": 100,
                    "volume": 40,
                },
                {
                    "symbol": "AAPL",
                    "expiration": "2026-05-15",
                    "strike": 106.0,
                    "spot": 100.0,
                    "dte": 30,
                    "mid": 1.4,
                    "avg_cost": 90.0,
                    "shares_total": 200,
                    "shares_locked": 0,
                    "shares_available_for_cover": 200,
                    "covered_contracts_available": 2,
                    "is_fully_covered_available": True,
                    "net_income": 140.0,
                    "annualized_net_premium_return": 0.13,
                    "if_exercised_total_return": 0.18,
                    "strike_above_spot_pct": 0.06,
                    "strike_above_cost_pct": 0.18,
                    "risk_label": "中性",
                    "spread_ratio": 0.10,
                    "open_interest": 120,
                    "volume": 35,
                },
                {
                    "symbol": "AAPL",
                    "expiration": "2026-05-15",
                    "strike": 103.0,
                    "spot": 100.0,
                    "dte": 30,
                    "mid": 1.6,
                    "avg_cost": 90.0,
                    "shares_total": 200,
                    "shares_locked": 0,
                    "shares_available_for_cover": 200,
                    "covered_contracts_available": 2,
                    "is_fully_covered_available": True,
                    "net_income": 160.0,
                    "annualized_net_premium_return": 0.13,
                    "if_exercised_total_return": 0.17,
                    "strike_above_spot_pct": 0.03,
                    "strike_above_cost_pct": 0.14,
                    "risk_label": "中性",
                    "spread_ratio": 0.10,
                    "open_interest": 90,
                    "volume": 30,
                },
                {
                    "symbol": "AAPL",
                    "expiration": "2026-05-15",
                    "strike": 101.0,
                    "spot": 100.0,
                    "dte": 30,
                    "mid": 1.8,
                    "avg_cost": 90.0,
                    "shares_total": 200,
                    "shares_locked": 0,
                    "shares_available_for_cover": 200,
                    "covered_contracts_available": 2,
                    "is_fully_covered_available": True,
                    "net_income": 180.0,
                    "annualized_net_premium_return": 0.15,
                    "if_exercised_total_return": 0.15,
                    "strike_above_spot_pct": 0.01,
                    "strike_above_cost_pct": 0.12,
                    "risk_label": "激进",
                    "spread_ratio": 0.10,
                    "open_interest": 110,
                    "volume": 45,
                },
            ]
        )
        df.to_csv(in_path, index=False)

        cfg = build_strategy_config("call")
        expected = rank_candidates(score_candidates(df, cfg), cfg, layered=True, top=3)

        text = render_sell_call_alerts(
            input_path=in_path,
            output_path=out_path,
            top=3,
            layered=True,
            base_dir=_add_repo_to_syspath(),
        )

        assert _extract_title_strikes(text) == [float(v) for v in expected["strike"].tolist()]
