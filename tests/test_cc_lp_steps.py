from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.application.cc_lp_steps import (
    CC_LP_FAMILY,
    run_cc_lp_scan,
    summarize_cc_lp_result,
)
from src.application.combo_yield_steps import run_cc_lp_variant
from src.application.yield_enhancement_config import (
    derive_yield_enhancement_policy,
    resolve_yield_enhancement_cfg,
)


def _write_required_data(tmp_path: Path, *, call_rows: list[dict], put_rows: list[dict]) -> Path:
    required_data = tmp_path / "required_data"
    parsed = required_data / "parsed"
    parsed.mkdir(parents=True, exist_ok=True)
    rows = []
    for row in call_rows:
        r = dict(row)
        r["option_type"] = "call"
        rows.append(r)
    for row in put_rows:
        r = dict(row)
        r["option_type"] = "put"
        rows.append(r)
    pd.DataFrame(rows).to_csv(parsed / "NVDA_required_data.csv", index=False)
    return required_data


def _call_row(**overrides) -> dict:
    row = {
        "symbol": "NVDA",
        "expiration": "2026-08-28",
        "dte": 20,
        "contract_symbol": "NVDA260828C110000",
        "multiplier": 100.0,
        "currency": "USD",
        "strike": 110.0,
        "spot": 100.0,
        "bid": 4.0,
        "ask": 4.2,
        "mid": 4.1,
        "open_interest": 500.0,
        "volume": 20.0,
        "delta": 0.30,
        "implied_volatility": 0.35,
        "spread": 0.2,
        "spread_ratio": 0.05,
        "avg_cost": 90.0,
        "shares_total": 100,
        "shares_can_sell": 100,
        "shares_eligible": 100,
        "shares_locked": 0,
        "shares_available_for_cover": 1,
        "covered_contracts_available": 1,
        "max_new_contracts": 1,
        "is_fully_covered_available": True,
        "net_income": 400.0,
        "annualized_net_premium_return": 0.5,
    }
    row.update(overrides)
    return row


def _put_row(**overrides) -> dict:
    row = {
        "symbol": "NVDA",
        "expiration": "2026-08-28",
        "dte": 20,
        "contract_symbol": "NVDA260828P90000",
        "multiplier": 100.0,
        "currency": "USD",
        "strike": 90.0,
        "spot": 100.0,
        "bid": 2.0,
        "ask": 2.2,
        "mid": 2.1,
        "open_interest": 600.0,
        "volume": 15.0,
        "delta": -0.15,
        "implied_volatility": 0.40,
        "spread": 0.2,
        "spread_ratio": 0.10,
    }
    row.update(overrides)
    return row


def _scan_call_row(**overrides) -> dict:
    """A call row as produced by run_sell_call_scan (passed to _call_leg_from_required_data)."""

    row = {
        "symbol": "NVDA",
        "option_type": "call",
        "expiration": "2026-08-28",
        "dte": 20,
        "contract_symbol": "NVDA260828C110000",
        "multiplier": 100.0,
        "currency": "USD",
        "strike": 110.0,
        "spot": 100.0,
        "bid": 4.0,
        "ask": 4.2,
        "mid": 4.1,
        "open_interest": 500.0,
        "volume": 20.0,
        "delta": 0.30,
        "implied_volatility": 0.35,
        "spread": 0.2,
        "spread_ratio": 0.05,
    }
    row.update(overrides)
    return row


def test_run_cc_lp_scan_produces_candidates(tmp_path: Path) -> None:
    required_data = _write_required_data(
        tmp_path,
        call_rows=[_call_row()],
        put_rows=[_put_row()],
    )
    df = run_cc_lp_scan(
        symbol="NVDA",
        required_data_dir=required_data,
        report_dir=tmp_path,
        sell_call_cfg={"enabled": True},
        portfolio_ctx=None,
        stock={"shares": 100, "can_sell_qty": 100, "avg_cost": 90.0},
        run_sell_call_scan_fn=lambda **kwargs: pd.DataFrame([_scan_call_row()]),
    )
    assert not df.empty
    row = df.iloc[0]
    assert row["strategy_family"] == CC_LP_FAMILY == "combo_yield"
    assert row["variant"] == "cc_lp"
    assert row["call_strike"] > row["put_strike"]
    assert row["net_credit_retention"] > 0.20
    assert abs(row["put_delta"]) >= 0.10
    assert row["covered_notional"] == pytest.approx(100.0 * 100.0)
    assert abs(float(row["net_return"]) - float(row["net_credit"]) / (100.0 * 100.0)) < 1e-5


def test_run_cc_lp_scan_skips_without_stock(tmp_path: Path) -> None:
    required_data = _write_required_data(
        tmp_path,
        call_rows=[_call_row()],
        put_rows=[_put_row()],
    )
    df = run_cc_lp_scan(
        symbol="NVDA",
        required_data_dir=required_data,
        report_dir=tmp_path,
        sell_call_cfg={"enabled": True},
        portfolio_ctx=None,
        stock=None,
    )
    assert df.empty


def test_run_cc_lp_scan_rejects_retention_below_floor(tmp_path: Path) -> None:
    # expensive put -> retention below 0.20
    put = _put_row(ask=4.0)
    required_data = _write_required_data(
        tmp_path,
        call_rows=[_call_row(bid=4.0)],
        put_rows=[put],
    )
    df = run_cc_lp_scan(
        symbol="NVDA",
        required_data_dir=required_data,
        report_dir=tmp_path,
        sell_call_cfg={"enabled": True},
        portfolio_ctx=None,
        stock={"shares": 100, "can_sell_qty": 100, "avg_cost": 90.0},
        run_sell_call_scan_fn=lambda **kwargs: pd.DataFrame([_scan_call_row(bid=4.0)]),
    )
    assert df.empty


def test_run_cc_lp_variant_returns_summary(tmp_path: Path) -> None:
    required_data = _write_required_data(
        tmp_path,
        call_rows=[_call_row()],
        put_rows=[_put_row()],
    )
    symbol_cfg = {
        "symbol": "NVDA",
        "combo_yield": {"enabled": True, "variant": "cc_lp"},
        "sell_call": {"enabled": True},
        "_global_sell_call_liquidity": {},
    }
    summary = run_cc_lp_variant(
        base=tmp_path,
        sym="NVDA",
        symbol="NVDA",
        symbol_lower="nvda",
        symbol_cfg=symbol_cfg,
        yield_cfg=resolve_yield_enhancement_cfg(symbol_cfg),
        policy=derive_yield_enhancement_policy(
            resolve_yield_enhancement_cfg(symbol_cfg),
            market="us",
        ),
        required_data_dir=required_data,
        report_dir=tmp_path,
        portfolio_ctx={"stock": {"shares": 100, "can_sell_qty": 100, "avg_cost": 90.0}},
        run_cc_lp_scan_fn=lambda **kwargs: pd.DataFrame(
            [
                {
                    "strategy_family": CC_LP_FAMILY,
                    "symbol": "NVDA",
                    "call_strike": 110.0,
                    "put_strike": 90.0,
                    "net_credit_retention": 0.30,
                }
            ]
        ),
    )
    assert summary is not None
    assert summary["strategy_family"] == CC_LP_FAMILY
    assert summary["status"] == "candidates_found"
    assert summary["candidate_count"] == 1


def test_run_cc_lp_variant_forwards_pairs_to_sink(tmp_path: Path) -> None:
    required_data = _write_required_data(
        tmp_path,
        call_rows=[_call_row()],
        put_rows=[_put_row()],
    )
    symbol_cfg = {
        "symbol": "NVDA",
        "combo_yield": {"enabled": True, "variant": "cc_lp"},
        "sell_call": {"enabled": True},
        "_global_sell_call_liquidity": {},
    }
    captured: list[dict] = []
    summary = run_cc_lp_variant(
        base=tmp_path,
        sym="NVDA",
        symbol="NVDA",
        symbol_lower="nvda",
        symbol_cfg=symbol_cfg,
        yield_cfg=resolve_yield_enhancement_cfg(symbol_cfg),
        policy=derive_yield_enhancement_policy(
            resolve_yield_enhancement_cfg(symbol_cfg),
            market="us",
        ),
        required_data_dir=required_data,
        report_dir=tmp_path,
        portfolio_ctx={"stock": {"shares": 100, "can_sell_qty": 100, "avg_cost": 90.0}},
        run_cc_lp_scan_fn=lambda **kwargs: pd.DataFrame(
            [
                {
                    "strategy_family": "combo_yield",
                    "variant": "cc_lp",
                    "symbol": "NVDA",
                    "candidate_pair_id": "cc_lp:NVDA:C:P",
                    "call_strike": 110.0,
                    "put_strike": 90.0,
                    "net_credit_retention": 0.30,
                }
            ]
        ),
        combo_pairs_sink_fn=captured.extend,
    )
    assert summary["status"] == "candidates_found"
    assert len(captured) == 1
    assert captured[0]["variant"] == "cc_lp"


def test_run_cc_lp_variant_not_applicable_without_stock(tmp_path: Path) -> None:
    required_data = _write_required_data(
        tmp_path,
        call_rows=[_call_row()],
        put_rows=[_put_row()],
    )
    symbol_cfg = {
        "symbol": "NVDA",
        "combo_yield": {"enabled": True, "variant": "cc_lp"},
        "sell_call": {"enabled": True},
        "_global_sell_call_liquidity": {},
    }
    summary = run_cc_lp_variant(
        base=tmp_path,
        sym="NVDA",
        symbol="NVDA",
        symbol_lower="nvda",
        symbol_cfg=symbol_cfg,
        yield_cfg=resolve_yield_enhancement_cfg(symbol_cfg),
        policy=derive_yield_enhancement_policy(
            resolve_yield_enhancement_cfg(symbol_cfg),
            market="us",
        ),
        required_data_dir=required_data,
        report_dir=tmp_path,
        portfolio_ctx=None,
        run_cc_lp_scan_fn=lambda **kwargs: pd.DataFrame(),
    )
    assert summary is not None
    assert summary["status"] == "not_applicable"
    assert summary["reason"] == "stock_context_missing"


def test_variant_config_parses() -> None:
    cfg = resolve_yield_enhancement_cfg({"combo_yield": {"enabled": True, "variant": "cc_lp"}})
    policy = derive_yield_enhancement_policy(cfg)
    assert policy.config["variant"] == "cc_lp"
    cfg_default = resolve_yield_enhancement_cfg({"combo_yield": {"enabled": True}})
    policy_default = derive_yield_enhancement_policy(cfg_default)
    assert policy_default.config["variant"] == "sp_lc"


def test_summarize_cc_lp_result() -> None:
    summary = summarize_cc_lp_result(
        df=pd.DataFrame([{"a": 1}]),
        symbol="NVDA",
        status="candidates_found",
    )
    assert summary["candidate_count"] == 1
    assert summary["status"] == "candidates_found"
