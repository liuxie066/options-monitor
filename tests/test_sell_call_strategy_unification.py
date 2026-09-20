from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from conftest import phase2_opening_row

_SCAN_NOW = datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc)


def _aapl_call_row(
    contract_symbol: str,
    *,
    strike: float,
    bid: float,
    ask: float,
    last_price: float,
    mid: float,
    delta: float,
    open_interest: int = 200,
) -> dict:
    """AAPL call row with the fixtures shared by every AAPL opening row in this module."""

    return {
        "symbol": "AAPL",
        "option_type": "call",
        "expiration": "2026-05-15",
        "dte": 30,
        "contract_symbol": contract_symbol,
        "multiplier": 100,
        "currency": "USD",
        "strike": strike,
        "spot": 100.0,
        "bid": bid,
        "ask": ask,
        "last_price": last_price,
        "mid": mid,
        "open_interest": open_interest,
        "volume": 50,
        "implied_volatility": 0.30,
        "delta": delta,
    }


def _rank_call_row(
    *,
    strike: float,
    mid: float,
    net_income: float,
    annualized_net_premium_return: float,
    if_exercised_total_return: float,
    strike_above_spot_pct: float,
    strike_above_cost_pct: float,
    risk_label: str,
    open_interest: int,
    volume: int,
) -> dict:
    """Row shape consumed by rank_candidate_rows(mode="call") in this module."""

    return {
        "symbol": "AAPL",
        "expiration": "2026-05-15",
        "strike": strike,
        "spot": 100.0,
        "dte": 30,
        "mid": mid,
        "avg_cost": 90.0,
        "shares_total": 200,
        "shares_locked": 0,
        "shares_available_for_cover": 200,
        "covered_contracts_available": 2,
        "is_fully_covered_available": True,
        "net_income": net_income,
        "annualized_net_premium_return": annualized_net_premium_return,
        "if_exercised_total_return": if_exercised_total_return,
        "strike_above_spot_pct": strike_above_spot_pct,
        "strike_above_cost_pct": strike_above_cost_pct,
        "risk_label": risk_label,
        "spread_ratio": 0.10,
        "open_interest": open_interest,
        "volume": volume,
    }


def test_scan_sell_call_calculates_without_applying_strategy_filter_or_rank(tmp_path: Path) -> None:
    from src.application.scan_sell_call import run_sell_call_scan

    td = tmp_path
    root = Path(td)
    parsed = root / "parsed"
    parsed.mkdir(parents=True, exist_ok=True)

    pd.DataFrame(
        [phase2_opening_row(row) for row in [
                # pass; same annualized return as B, lower strike-upside tie-break
            _aapl_call_row("A", strike=115.0, bid=1.4, ask=1.6, last_price=1.5, mid=1.5, delta=0.25),
            # pass; if_exercised_total_return must not affect rank
            _aapl_call_row("B", strike=120.0, bid=1.4, ask=1.6, last_price=1.5, mid=1.5, delta=0.22),
            # fail annualized
            _aapl_call_row("C", strike=130.0, bid=0.15, ask=0.25, last_price=0.2, mid=0.2, delta=0.10),
            # fail net income
            _aapl_call_row("D", strike=101.0, bid=0.35, ask=0.45, last_price=0.4, mid=0.4, delta=0.45),
            # fail if-exercised total return versus avg_cost
            _aapl_call_row("F", strike=95.0, bid=1.4, ask=1.6, last_price=1.5, mid=1.5, delta=0.65),
            # fail liquidity open-interest
            _aapl_call_row("E", strike=125.0, bid=0.5, ask=1.5, last_price=1.0, mid=1.0, delta=0.2, open_interest=5),
        ]]
    ).to_csv(parsed / "AAPL_required_data.csv", index=False)

    out = run_sell_call_scan(
        symbols=["AAPL"],
        input_root=root,
        avg_cost=100.0,
        shares=100,
        shares_can_sell=100,
        shares_locked=0,
        min_annualized_net_return=0.10,
        min_net_income=100,
        min_open_interest=10,
        quote_freshness_now_utc=_SCAN_NOW,
    )

    assert list(out["contract_symbol"]) == ["A", "B", "C", "D", "F", "E"]
    assert out["period_net_premium_return"].notna().all()
    assert list(root.glob("*candidates*.csv")) == []
    assert list(root.glob("*reject_log*.csv")) == []


def test_scan_sell_call_uses_contract_multiplier_for_share_capacity(tmp_path: Path) -> None:
    from src.application.scan_sell_call import run_sell_call_scan

    parsed = tmp_path / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            phase2_opening_row({
                "symbol": "SMALL",
                "option_type": "call",
                "expiration": "2026-05-15",
                "dte": 30,
                "contract_symbol": "SMALL_CALL",
                "multiplier": 10,
                "currency": "USD",
                "strike": 110.0,
                "spot": 100.0,
                "bid": 1.0,
                "ask": 1.2,
                "last_price": 1.1,
                "mid": 1.1,
                "open_interest": 100,
                "volume": 50,
                "implied_volatility": 0.3,
                "delta": 0.2,
            })
        ]
    ).to_csv(parsed / "SMALL_required_data.csv", index=False)

    out = run_sell_call_scan(
        symbols=["SMALL"],
        input_root=tmp_path,
        avg_cost=90.0,
        shares=10,
        shares_can_sell=10,
        shares_locked=0,
        min_annualized_net_return=0.01,
        min_net_income=1,
        min_open_interest=1,
        min_volume=1,
        quote_freshness_now_utc=_SCAN_NOW,
    )

    assert list(out["contract_symbol"]) == ["SMALL_CALL"]
    assert int(out.iloc[0]["covered_contracts_available"]) == 1


def test_scan_sell_call_demo_capacity_uses_multiplier_and_spot(tmp_path: Path) -> None:
    from src.application.scan_sell_call import run_sell_call_scan

    frame = pd.DataFrame(
        [
            phase2_opening_row(
                {
                    "symbol": "DEMO",
                    "option_type": "call",
                    "expiration": "2026-05-15",
                    "dte": 30,
                    "contract_symbol": "DEMO_CALL",
                    "multiplier": 10,
                    "currency": "USD",
                    "strike": 110.0,
                    "spot": 100.0,
                    "bid": 1.0,
                    "ask": 1.2,
                    "last_price": 1.1,
                    "mid": 1.1,
                    "open_interest": 100,
                    "volume": 50,
                    "implied_volatility": 0.3,
                    "delta": 0.2,
                }
            )
        ]
    )
    out = run_sell_call_scan(
        symbols=["DEMO"],
        input_root=tmp_path,
        avg_cost=None,
        shares=None,
        shares_can_sell=None,
        shares_locked=None,
        min_annualized_net_return=0.01,
        min_net_income=1,
        min_open_interest=1,
        min_volume=1,
        quote_freshness_now_utc=_SCAN_NOW,
        required_data_frames={"DEMO": frame},
        demo_capacity=True,
    )

    assert list(out["contract_symbol"]) == ["DEMO_CALL"]
    assert int(out.iloc[0]["shares_total"]) == 10
    assert int(out.iloc[0]["shares_can_sell"]) == 10
    assert int(out.iloc[0]["covered_contracts_available"]) == 1
    assert float(out.iloc[0]["avg_cost"]) == 100.0
    assert out.iloc[0]["capacity_source"] == "demo_scenario"


def test_scan_sell_call_defers_cost_multiplier_strike_floor_to_policy(tmp_path: Path) -> None:
    from src.application.scan_sell_call import run_sell_call_scan

    td = tmp_path
    root = Path(td)
    parsed = root / "parsed"
    parsed.mkdir(parents=True, exist_ok=True)

    pd.DataFrame(
        [phase2_opening_row(row) for row in [
            _aapl_call_row("BELOW_COST", strike=99.0, bid=3.9, ask=4.1, last_price=4.0, mid=4.0, delta=0.55),
            _aapl_call_row("AT_COST", strike=100.0, bid=3.9, ask=4.1, last_price=4.0, mid=4.0, delta=0.50),
            _aapl_call_row("AT_COST_MULTIPLIER", strike=102.0, bid=3.9, ask=4.1, last_price=4.0, mid=4.0, delta=0.45),
        ]]
    ).to_csv(parsed / "AAPL_required_data.csv", index=False)

    out = run_sell_call_scan(
        symbols=["AAPL"],
        input_root=root,
        avg_cost=100.0,
        shares=100,
        shares_can_sell=100,
        shares_locked=0,
        min_strike_cost_multiplier=1.02,
        min_annualized_net_return=0.10,
        min_net_income=100,
        min_open_interest=10,
        quote_freshness_now_utc=_SCAN_NOW,
    )

    assert list(out["contract_symbol"]) == [
        "BELOW_COST",
        "AT_COST",
        "AT_COST_MULTIPLIER",
    ]
    assert float(out["strike"].min()) == 99.0


def test_sell_call_risk_bands_are_stable() -> None:
    from domain.domain.sell_call_risk_bands import classify_sell_call_risk

    aggressive = classify_sell_call_risk(0.02)
    neutral = classify_sell_call_risk(0.05)
    conservative = classify_sell_call_risk(0.10)

    assert (aggressive.band, aggressive.risk_label) == ("<3%", "激进")
    assert (neutral.band, neutral.risk_label) == ("3%-8%", "中性")
    assert (conservative.band, conservative.risk_label) == (">=8%", "保守")


def test_sell_call_rank_order_is_consistent_with_strategy() -> None:
    from domain.domain.engine import rank_candidate_rows

    df = pd.DataFrame(
            [
                _rank_call_row(
                    strike=112.0, mid=1.2, net_income=120.0, annualized_net_premium_return=0.11,
                    if_exercised_total_return=0.20, strike_above_spot_pct=0.12, strike_above_cost_pct=0.24,
                    risk_label="保守", open_interest=100, volume=40,
                ),
                _rank_call_row(
                    strike=106.0, mid=1.4, net_income=140.0, annualized_net_premium_return=0.13,
                    if_exercised_total_return=0.18, strike_above_spot_pct=0.06, strike_above_cost_pct=0.18,
                    risk_label="中性", open_interest=120, volume=35,
                ),
                _rank_call_row(
                    strike=103.0, mid=1.6, net_income=160.0, annualized_net_premium_return=0.13,
                    if_exercised_total_return=0.17, strike_above_spot_pct=0.03, strike_above_cost_pct=0.14,
                    risk_label="中性", open_interest=90, volume=30,
                ),
                _rank_call_row(
                    strike=101.0, mid=1.8, net_income=180.0, annualized_net_premium_return=0.15,
                    if_exercised_total_return=0.15, strike_above_spot_pct=0.01, strike_above_cost_pct=0.12,
                    risk_label="激进", open_interest=110, volume=45,
                ),
            ]
    )

    ranked = rank_candidate_rows(df.to_dict("records"), mode="call")

    assert [float(row["strike"]) for row in ranked] == [106.0, 103.0, 101.0, 112.0]
