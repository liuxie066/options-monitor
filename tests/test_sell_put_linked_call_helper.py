from __future__ import annotations

from pathlib import Path

import pandas as pd


def test_enrich_sell_put_candidates_with_linked_calls_selects_best_call(tmp_path: Path) -> None:
    from scripts.sell_put_call_helper import (
        attach_best_linked_calls,
        find_sell_put_yield_enhancement_pairs,
        select_best_yield_enhancement_pairs,
    )

    parsed = tmp_path / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "call",
                "expiration": "2026-06-19",
                "dte": 44,
                "contract_symbol": "NVDA_C110",
                "strike": 110,
                "spot": 100,
                "bid": 1.4,
                "ask": 1.5,
                "last_price": 1.45,
                "mid": 1.45,
                "volume": 65,
                "open_interest": 980,
                "implied_volatility": 0.40,
                "currency": "USD",
                "delta": 0.32,
                "multiplier": 100,
            },
            {
                "symbol": "NVDA",
                "option_type": "call",
                "expiration": "2026-06-19",
                "dte": 44,
                "contract_symbol": "NVDA_C115",
                "strike": 115,
                "spot": 100,
                "bid": 0.95,
                "ask": 1.0,
                "last_price": 0.98,
                "mid": 0.975,
                "volume": 70,
                "open_interest": 1200,
                "implied_volatility": 0.39,
                "currency": "USD",
                "delta": 0.24,
                "multiplier": 100,
            },
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    df = pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "expiration": "2026-06-19",
                "dte": 44,
                "contract_symbol": "NVDA_P95",
                "multiplier": 100,
                "currency": "USD",
                "strike": 95.0,
                "spot": 100.0,
                "bid": 3.0,
                "ask": 3.2,
                "mid": 3.1,
                "open_interest": 1200,
                "volume": 80,
                "implied_volatility": 0.42,
                "delta": -0.25,
            }
        ]
    )

    pairs = find_sell_put_yield_enhancement_pairs(
        df_candidates=df,
        symbol="NVDA",
        input_root=tmp_path,
        yield_enhancement_cfg={
            "enabled": True,
            "min_dte": 20,
            "max_dte": 90,
            "funding_mode": "credit_or_even",
            "min_put_otm_pct": 0.05,
            "min_call_otm_pct": 0.03,
            "max_call_otm_pct": 0.20,
            "min_scenario_score": 0.02,
            "min_open_interest": 100,
            "min_volume": 5,
            "max_combo_spread_ratio": 0.50,
        },
        output_path=tmp_path / "sell_put_linked_calls.csv",
    )
    selected = select_best_yield_enhancement_pairs(pairs)
    out = attach_best_linked_calls(
        df_candidates=df,
        pairs_df=pairs,
        out_path=tmp_path / "sell_put_candidates_labeled.csv",
    )

    assert len(selected) == 1
    assert selected.iloc[0]["call_contract_symbol"] == "NVDA_C110"
    assert int(selected.iloc[0]["call_candidate_count"]) == 2

    row = out.iloc[0]
    assert row["linked_call_contract"] == "2026-06-19 110C"
    assert row["linked_call_contract_symbol"] == "NVDA_C110"
    assert round(float(row["linked_call_scenario_score"]), 4) > 0.04
    assert round(float(row["linked_call_expected_move"]), 1) == 14.2
    assert int(row["linked_call_count"]) == 2

    persisted_pairs = pd.read_csv(tmp_path / "sell_put_linked_calls.csv")
    assert set(persisted_pairs["call_contract_symbol"]) == {"NVDA_C110", "NVDA_C115"}


def test_yield_enhancement_requires_iv_for_expected_move_scoring(tmp_path: Path) -> None:
    from scripts.sell_put_call_helper import find_sell_put_yield_enhancement_pairs

    parsed = tmp_path / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "call",
                "expiration": "2026-06-19",
                "dte": 44,
                "contract_symbol": "NVDA_C110",
                "strike": 110,
                "spot": 100,
                "bid": 1.4,
                "ask": 1.5,
                "mid": 1.45,
                "volume": 65,
                "open_interest": 980,
                "currency": "USD",
                "delta": 0.32,
                "multiplier": 100,
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    df = pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "expiration": "2026-06-19",
                "dte": 44,
                "contract_symbol": "NVDA_P95",
                "multiplier": 100,
                "currency": "USD",
                "strike": 95.0,
                "spot": 100.0,
                "bid": 3.0,
                "ask": 3.2,
                "mid": 3.1,
                "open_interest": 1200,
                "volume": 80,
                "delta": -0.25,
            }
        ]
    )

    pairs = find_sell_put_yield_enhancement_pairs(
        df_candidates=df,
        symbol="NVDA",
        input_root=tmp_path,
        yield_enhancement_cfg={"enabled": True, "min_open_interest": 100, "min_volume": 5},
    )

    assert pairs.empty
