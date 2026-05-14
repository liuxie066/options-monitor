from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def test_yield_enhancement_defaults_match_system_template() -> None:
    from src.application.yield_enhancement_config import yield_enhancement_defaults_for_market

    system_config = json.loads((Path(__file__).resolve().parents[1] / "configs" / "system.json").read_text())
    for market in ("us", "hk"):
        template = system_config["markets"][market]["symbol_defaults"]["yield_enhancement"]
        assert template == yield_enhancement_defaults_for_market(market)


def _write_single_call(
    input_root: Path,
    *,
    dte: int,
    contract_symbol: str = "NVDA_C110",
    strike: float = 110.0,
    bid: float = 0.24,
    ask: float = 0.25,
    implied_volatility: float = 0.80,
    delta: float = 0.20,
) -> None:
    parsed = input_root / "parsed"
    parsed.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "call",
                "expiration": "2026-06-19",
                "dte": dte,
                "contract_symbol": contract_symbol,
                "strike": strike,
                "spot": 100,
                "bid": bid,
                "ask": ask,
                "mid": (bid + ask) / 2,
                "volume": 65,
                "open_interest": 980,
                "implied_volatility": implied_volatility,
                "currency": "USD",
                "delta": delta,
                "multiplier": 100,
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)


def _single_put_df(*, dte: int, bid: float = 3.0, ask: float = 3.01, implied_volatility: float = 0.80) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "expiration": "2026-06-19",
                "dte": dte,
                "contract_symbol": "NVDA_P95",
                "multiplier": 100,
                "currency": "USD",
                "strike": 95.0,
                "spot": 100.0,
                "bid": bid,
                "ask": ask,
                "mid": (bid + ask) / 2,
                "open_interest": 1200,
                "volume": 80,
                "implied_volatility": implied_volatility,
                "delta": -0.25,
            }
        ]
    )


def test_enrich_sell_put_candidates_with_linked_calls_selects_best_call(tmp_path: Path) -> None:
    from src.application.sell_put_call_helper import (
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
                "contract_symbol": "NVDA_C112",
                "strike": 112,
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
            "call": {
                "min_otm_pct": 0.03,
                "max_otm_pct": 0.20,
                "min_delta": 0.10,
                "max_delta": 0.45,
            },
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
    assert set(persisted_pairs["call_contract_symbol"]) == {"NVDA_C110", "NVDA_C112"}


def test_yield_enhancement_requires_iv_for_expected_move_scoring(tmp_path: Path) -> None:
    from src.application.sell_put_call_helper import find_sell_put_yield_enhancement_pairs

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


def test_yield_enhancement_rejects_unfunded_call_by_default(tmp_path: Path) -> None:
    from src.application.sell_put_call_helper import find_sell_put_yield_enhancement_pairs

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
                "bid": 3.9,
                "ask": 4.0,
                "mid": 3.95,
                "volume": 65,
                "open_interest": 980,
                "implied_volatility": 0.40,
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
            "min_open_interest": 100,
            "min_volume": 5,
            "min_scenario_score": 0.0,
        },
    )

    assert pairs.empty


def test_yield_enhancement_accepts_premium_funded_call_with_clear_upside(tmp_path: Path) -> None:
    from src.application.sell_put_call_helper import find_sell_put_yield_enhancement_pairs

    parsed = tmp_path / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "call",
                "expiration": "2026-06-19",
                "dte": 44,
                "contract_symbol": "NVDA_C110_LOW",
                "strike": 110,
                "spot": 100,
                "bid": 0.24,
                "ask": 0.25,
                "mid": 0.245,
                "volume": 65,
                "open_interest": 980,
                "implied_volatility": 0.40,
                "currency": "USD",
                "delta": 0.20,
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
            "min_open_interest": 100,
            "min_volume": 5,
            "min_scenario_score": 0.0,
        },
    )

    assert len(pairs) == 1
    row = pairs.iloc[0]
    assert bool(row["funding_accepted"]) is True
    assert row["call_contract_symbol"] == "NVDA_C110_LOW"
    assert float(row["call_cost_to_put_credit"]) <= 1.0
    assert float(row["upside_lift_to_call_cost"]) >= 1.5
    assert float(row["upside_lift_to_put_credit"]) >= 0.5
    assert float(row["premium_funding_score"]) > 0


def test_yield_enhancement_pair_filter_inherits_sell_put_dte(tmp_path: Path) -> None:
    from src.application.sell_put_call_helper import find_sell_put_yield_enhancement_pairs

    _write_single_call(tmp_path, dte=10)

    pairs = find_sell_put_yield_enhancement_pairs(
        df_candidates=_single_put_df(dte=10),
        symbol="NVDA",
        input_root=tmp_path,
        yield_enhancement_cfg={
            "enabled": True,
            "min_open_interest": 100,
            "min_volume": 5,
            "min_scenario_score": 0.0,
        },
        sell_put_cfg={"enabled": True, "min_dte": 7, "max_dte": 45},
    )

    assert len(pairs) == 1
    assert int(pairs.iloc[0]["dte"]) == 10


def test_yield_enhancement_max_debit_does_not_apply_default_cost_ratio(tmp_path: Path) -> None:
    from src.application.sell_put_call_helper import find_sell_put_yield_enhancement_pairs
    from src.application.yield_enhancement_config import resolve_yield_enhancement_cfg

    _write_single_call(
        tmp_path,
        dte=44,
        contract_symbol="NVDA_C105_DEBIT",
        strike=105.0,
        bid=3.19,
        ask=3.20,
        implied_volatility=0.80,
        delta=0.45,
    )
    cfg = resolve_yield_enhancement_cfg(
        {
            "yield_enhancement": {
                "enabled": True,
                "funding_mode": "max_debit",
                "max_debit_native": 40.0,
                "min_open_interest": 100,
                "min_volume": 5,
                "min_scenario_score": 0.0,
            }
        }
    )
    cfg = resolve_yield_enhancement_cfg({"yield_enhancement": cfg})

    pairs = find_sell_put_yield_enhancement_pairs(
        df_candidates=_single_put_df(dte=44),
        symbol="NVDA",
        input_root=tmp_path,
        yield_enhancement_cfg=cfg,
        sell_put_cfg={"enabled": True, "min_dte": 20, "max_dte": 60},
    )

    assert len(pairs) == 1
    row = pairs.iloc[0]
    assert row["call_contract_symbol"] == "NVDA_C105_DEBIT"
    assert float(row["net_debit"]) <= 40.0
    assert float(row["call_cost_to_put_credit"]) > 1.0


def test_yield_enhancement_max_debit_respects_explicit_cost_ratio(tmp_path: Path) -> None:
    from src.application.sell_put_call_helper import find_sell_put_yield_enhancement_pairs
    from src.application.yield_enhancement_config import resolve_yield_enhancement_cfg

    _write_single_call(
        tmp_path,
        dte=44,
        contract_symbol="NVDA_C105_DEBIT",
        strike=105.0,
        bid=3.19,
        ask=3.20,
        implied_volatility=0.80,
        delta=0.45,
    )
    cfg = resolve_yield_enhancement_cfg(
        {
            "yield_enhancement": {
                "enabled": True,
                "funding_mode": "max_debit",
                "max_debit_native": 40.0,
                "max_call_cost_to_put_credit": 1.0,
                "min_open_interest": 100,
                "min_volume": 5,
                "min_scenario_score": 0.0,
            }
        }
    )

    pairs = find_sell_put_yield_enhancement_pairs(
        df_candidates=_single_put_df(dte=44),
        symbol="NVDA",
        input_root=tmp_path,
        yield_enhancement_cfg=cfg,
        sell_put_cfg={"enabled": True, "min_dte": 20, "max_dte": 60},
    )

    assert pairs.empty
