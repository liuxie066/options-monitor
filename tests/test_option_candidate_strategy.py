from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


def _add_repo_to_syspath() -> None:
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))


def test_put_filter_and_rank_consistent_with_legacy_sort() -> None:
    _add_repo_to_syspath()
    from domain.domain.engine import (
        build_strategy_config,
        filter_candidates,
        rank_candidates,
        score_candidates,
    )

    df = pd.DataFrame(
        [
            {"contract_symbol": "A", "annualized_net_return_on_cash_basis": 0.12, "net_income": 70, "otm_pct": 0.06, "spread_ratio": 0.20},
            {"contract_symbol": "B", "annualized_net_return_on_cash_basis": 0.15, "net_income": 60, "otm_pct": 0.07, "spread_ratio": 0.10},
            {"contract_symbol": "C", "annualized_net_return_on_cash_basis": 0.09, "net_income": 80, "otm_pct": 0.08, "spread_ratio": 0.10},
            {"contract_symbol": "D", "annualized_net_return_on_cash_basis": 0.14, "net_income": 90, "otm_pct": 0.06, "spread_ratio": 0.40},
            {"contract_symbol": "E", "annualized_net_return_on_cash_basis": 0.14, "net_income": 40, "otm_pct": 0.06, "spread_ratio": 0.20},
        ]
    )

    cfg = build_strategy_config(
        "put",
        min_annualized_return=0.10,
        min_net_income=50,
        max_spread_ratio=0.30,
    )
    ranked = rank_candidates(score_candidates(filter_candidates(df, cfg), cfg), cfg)
    assert list(ranked["contract_symbol"]) == ["B", "A"]


def test_put_layered_rank_matches_previous_fill_limit_behavior() -> None:
    _add_repo_to_syspath()
    from domain.domain.engine import build_strategy_config, rank_candidates

    df = pd.DataFrame(
        [
            {"symbol": "NVDA", "expiration": "2026-06-18", "strike": 1, "risk_label": "激进", "annualized_net_return_on_cash_basis": 0.20, "net_income": 100},
            {"symbol": "NVDA", "expiration": "2026-06-18", "strike": 2, "risk_label": "激进", "annualized_net_return_on_cash_basis": 0.18, "net_income": 300},
            {"symbol": "NVDA", "expiration": "2026-06-18", "strike": 3, "risk_label": "中性", "annualized_net_return_on_cash_basis": 0.16, "net_income": 90},
            {"symbol": "NVDA", "expiration": "2026-06-18", "strike": 4, "risk_label": "保守", "annualized_net_return_on_cash_basis": 0.14, "net_income": 80},
            {"symbol": "NVDA", "expiration": "2026-06-18", "strike": 5, "risk_label": "中性", "annualized_net_return_on_cash_basis": 0.13, "net_income": 120},
            {"symbol": "NVDA", "expiration": "2026-06-18", "strike": 6, "risk_label": "保守", "annualized_net_return_on_cash_basis": 0.12, "net_income": 130},
        ]
    )

    cfg = build_strategy_config("put")
    layered = rank_candidates(df, cfg, layered=True, top=10)
    assert list(layered["strike"]) == [1, 3, 4, 2, 5]
    assert len(layered) == 5


def test_call_mode_rank_uses_call_sort_columns() -> None:
    _add_repo_to_syspath()
    from domain.domain.engine import build_strategy_config, rank_candidates

    df = pd.DataFrame(
        [
            {"contract_symbol": "C1", "annualized_net_premium_return": 0.10, "if_exercised_total_return": 0.20, "net_income": 110},
            {"contract_symbol": "C2", "annualized_net_premium_return": 0.10, "if_exercised_total_return": 0.21, "net_income": 100},
            {"contract_symbol": "C3", "annualized_net_premium_return": 0.09, "if_exercised_total_return": 0.30, "net_income": 130},
        ]
    )

    cfg = build_strategy_config("call")
    ranked = rank_candidates(df, cfg)
    assert list(ranked["contract_symbol"]) == ["C1", "C2", "C3"]


def test_strategy_param_table_v1_default_weights_split_put_call() -> None:
    _add_repo_to_syspath()
    from domain.domain.engine import build_strategy_config

    put_cfg = build_strategy_config("put")
    call_cfg = build_strategy_config("call")

    assert put_cfg.param_table_version == "v1"
    assert call_cfg.param_table_version == "v1"
    assert put_cfg.score_weight_net_income == 1e-6
    assert call_cfg.score_weight_net_income == 1e-6


def test_filter_candidates_with_reject_log_contains_required_fields() -> None:
    _add_repo_to_syspath()
    from domain.domain.engine import (
        build_strategy_config,
        filter_candidates_with_reject_log,
    )

    df = pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "contract_symbol": "PASS",
                "expiration": "2026-06-18",
                "strike": 110.0,
                "annualized_net_return_on_cash_basis": 0.12,
                "net_income": 80.0,
                "otm_pct": 0.07,
                "spread_ratio": 0.20,
            },
            {
                "symbol": "NVDA",
                "contract_symbol": "FAIL_RET",
                "expiration": "2026-06-18",
                "strike": 115.0,
                "annualized_net_return_on_cash_basis": 0.08,
                "net_income": 90.0,
                "otm_pct": 0.07,
                "spread_ratio": 0.20,
            },
            {
                "symbol": "NVDA",
                "contract_symbol": "FAIL_SPREAD",
                "expiration": "2026-06-18",
                "strike": 105.0,
                "annualized_net_return_on_cash_basis": 0.13,
                "net_income": 90.0,
                "otm_pct": 0.07,
                "spread_ratio": 0.40,
            },
        ]
    )

    cfg = build_strategy_config(
        "put",
        min_annualized_return=0.10,
        max_spread_ratio=0.30,
    )
    out, reject_log = filter_candidates_with_reject_log(df, cfg, reject_stage="step3_risk_gate")

    assert list(out["contract_symbol"]) == ["PASS"]
    assert len(reject_log) == 2
    assert set(["reject_stage", "reject_rule", "metric_value", "threshold", "symbol", "contract_symbol"]).issubset(
        set(reject_log.columns)
    )
    assert list(reject_log["engine_reject_stage"]) == ["stage2_return_floor", "stage3_risk_filter"]
    assert list(reject_log["engine_reject_reason"]) == ["return_annualized", "risk_spread"]


def test_filter_rank_candidates_with_reject_log_matches_manual_pipeline() -> None:
    _add_repo_to_syspath()
    from domain.domain.engine import (
        build_strategy_config,
        filter_candidates_with_reject_log,
        filter_rank_candidates_with_reject_log,
        rank_scored_candidates,
    )

    df = pd.DataFrame(
        [
            {"contract_symbol": "A", "annualized_net_return_on_cash_basis": 0.12, "net_income": 70},
            {"contract_symbol": "B", "annualized_net_return_on_cash_basis": 0.15, "net_income": 60},
            {"contract_symbol": "C", "annualized_net_return_on_cash_basis": 0.08, "net_income": 80},
        ]
    )
    cfg = build_strategy_config("put", min_annualized_return=0.10)

    manual_filtered, manual_reject_log = filter_candidates_with_reject_log(df, cfg, reject_stage="step3_risk_gate")
    manual_ranked = rank_scored_candidates(manual_filtered, cfg, layered=False)
    ranked, reject_log = filter_rank_candidates_with_reject_log(df, cfg, reject_stage="step3_risk_gate", layered=False)

    assert list(ranked["contract_symbol"]) == list(manual_ranked["contract_symbol"])
    assert reject_log.to_dict("records") == manual_reject_log.to_dict("records")


def test_option_candidate_strategy_script_is_engine_compat_wrapper() -> None:
    _add_repo_to_syspath()
    from domain.domain.engine import build_strategy_config as engine_build_strategy_config
    from scripts.option_candidate_strategy import build_strategy_config as script_build_strategy_config

    assert script_build_strategy_config is engine_build_strategy_config


def test_strategy_production_scripts_import_engine_directly() -> None:
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    production_scripts = [
        repo / "scripts" / "scan_sell_put.py",
        repo / "scripts" / "scan_sell_call.py",
        repo / "scripts" / "render_sell_put_alerts.py",
        repo / "scripts" / "render_sell_call_alerts.py",
        repo / "scripts" / "tools" / "compare_strategy_replay.py",
    ]

    for path in production_scripts:
        text = path.read_text(encoding="utf-8")
        assert "from domain.domain.engine import (" in text
        assert "from scripts.option_candidate_strategy import" not in text
