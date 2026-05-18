from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pandas as pd
import pytest


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
    assert put_cfg.score_weight_liquidity == 0.0
    assert call_cfg.score_weight_risk_distance == 0.0


def test_score_candidates_uses_independent_strategy_score_method() -> None:
    _add_repo_to_syspath()
    from domain.domain.engine import build_strategy_config, rank_candidates, score_candidates

    df = pd.DataFrame(
        [
            {
                "contract_symbol": "HIGH_RETURN_WIDE",
                "annualized_net_return_on_cash_basis": 0.120,
                "net_income": 100,
                "spread_ratio": 0.95,
                "open_interest": 1,
                "volume": 0,
            },
            {
                "contract_symbol": "LOWER_RETURN_LIQUID",
                "annualized_net_return_on_cash_basis": 0.115,
                "net_income": 100,
                "spread_ratio": 0.05,
                "open_interest": 500,
                "volume": 20,
            },
        ]
    )
    cfg = build_strategy_config("put", score_weight_liquidity=0.02)

    ranked = rank_candidates(score_candidates(df, cfg), cfg)

    assert list(ranked["contract_symbol"]) == ["LOWER_RETURN_LIQUID", "HIGH_RETURN_WIDE"]


def test_rank_candidates_delegates_to_candidate_engine_ranker(monkeypatch) -> None:
    _add_repo_to_syspath()
    import domain.domain.engine.candidate_strategy as strategy

    calls: list[dict] = []

    def _fake_rank_candidate_rows(rows, *, mode, score_weights=None):  # type: ignore[no-untyped-def]
        calls.append({"rows": rows, "mode": mode, "score_weights": score_weights})
        return [rows[1], rows[0]]

    monkeypatch.setattr(strategy, "rank_candidate_rows", _fake_rank_candidate_rows)
    df = pd.DataFrame(
        [
            {"contract_symbol": "A", "annualized_net_return_on_cash_basis": 0.12, "net_income": 100},
            {"contract_symbol": "B", "annualized_net_return_on_cash_basis": 0.11, "net_income": 100},
        ]
    )
    cfg = strategy.build_strategy_config("put", score_weight_liquidity=0.02)

    ranked = strategy.rank_candidates(df, cfg)

    assert list(ranked["contract_symbol"]) == ["B", "A"]
    assert calls[0]["mode"] == "put"
    assert calls[0]["score_weights"].liquidity == 0.02


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


def test_filter_candidates_uses_candidate_engine_return_floor(monkeypatch) -> None:
    _add_repo_to_syspath()
    import domain.domain.engine.candidate_strategy as strategy

    calls: list[dict] = []
    original = strategy.evaluate_candidate_return_floor

    def _counting_return_floor(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(dict(kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(strategy, "evaluate_candidate_return_floor", _counting_return_floor)
    df = pd.DataFrame(
        [
            {"contract_symbol": "PASS", "annualized_net_return_on_cash_basis": 0.12, "net_income": 80},
            {"contract_symbol": "FAIL_RET", "annualized_net_return_on_cash_basis": 0.08, "net_income": 80},
        ]
    )
    cfg = strategy.build_strategy_config("put", min_annualized_return=0.10)

    out, reject_log = strategy.filter_candidates_with_reject_log(df, cfg)

    assert list(out["contract_symbol"]) == ["PASS"]
    assert list(reject_log["engine_reject_reason"]) == ["return_annualized"]
    assert [call["min_annualized_return"] for call in calls] == [0.10, 0.10]


def test_option_candidate_strategy_script_is_removed() -> None:
    _add_repo_to_syspath()
    from domain.domain.engine import build_strategy_config

    assert callable(build_strategy_config)
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.option_candidate_strategy")


def test_strategy_production_scripts_import_engine_directly() -> None:
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    production_scripts = {
        repo / "src" / "application" / "scan_sell_put.py",
        repo / "src" / "application" / "scan_sell_call.py",
    }
    engine_scripts = {
        repo / "src" / "application" / "render_sell_put_alerts.py",
        repo / "src" / "application" / "render_sell_call_alerts.py",
        repo / "scripts" / "tools" / "compare_strategy_replay.py",
    }

    for path in production_scripts | engine_scripts:
        text = path.read_text(encoding="utf-8")
        assert "from scripts.option_candidate_strategy import" not in text
        if path in production_scripts:
            assert "from src.application.candidate_scanning import (" in text
        else:
            assert "from domain.domain.engine import (" in text


def test_run_candidate_scan_reuses_stage1_gate_once_per_contract(tmp_path: Path) -> None:
    _add_repo_to_syspath()
    import src.application.candidate_scanning as scan

    parsed_dir = tmp_path / "input" / "parsed"
    parsed_dir.mkdir(parents=True)
    (parsed_dir / "NVDA_required_data.csv").write_text(
        (
            "symbol,option_type,expiration,contract_symbol,currency,dte,strike,spot,bid,ask,last_price,mid,"
            "open_interest,volume,implied_volatility,delta,multiplier\n"
            "NVDA,put,2026-06-19,NVDA240619P00100000,USD,30,100,110,1.0,1.2,1.1,1.1,100,50,0.3,-0.2,100\n"
        ),
        encoding="utf-8",
    )

    old_gate = scan.evaluate_candidate_hard_constraints
    calls: list[dict] = []
    try:
        def _counting_gate(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append({"args": args, "kwargs": kwargs})
            return old_gate(*args, **kwargs)

        scan.evaluate_candidate_hard_constraints = _counting_gate  # type: ignore[assignment]
        out = scan.run_candidate_scan(
            config=scan.CandidateScanConfig(
                mode="put",
                symbols=["NVDA"],
                input_root=tmp_path / "input",
                output=tmp_path / "output.csv",
                empty_output_columns=["symbol"],
                min_dte=7,
                max_dte=60,
                min_strike=None,
                max_strike=None,
                min_open_interest=0,
                min_volume=0,
                max_spread_ratio=1.0,
                min_annualized_net_return=0.01,
                min_net_income=1,
                quiet=True,
            ),
            deps=scan.CandidateScanDependencies(
                compute_metrics_fn=lambda contract: {"net_income": 50.0, "annualized": 0.12},
                build_row_fn=lambda contract, base_values, metrics: {
                    "symbol": contract.symbol,
                    "contract_symbol": contract.contract_symbol,
                    "expiration": contract.expiration,
                    "strike": contract.strike,
                    "open_interest": base_values.open_interest,
                    "volume": base_values.volume,
                    "spread_ratio": base_values.spread_ratio,
                    "annualized": metrics["annualized"],
                    "net_income": metrics["net_income"],
                },
                build_hard_constraint_kwargs_fn=lambda contract: {},
                annualized_return_value_fn=lambda metrics: float(metrics["annualized"]),
                annotate_event_risk_fn=lambda df, base_dir, cfg: df,
                print_summary_fn=lambda df, out_path, reject_path: None,
            ),
            event_risk_cfg=None,
            base_dir=tmp_path,
        )
    finally:
        scan.evaluate_candidate_hard_constraints = old_gate  # type: ignore[assignment]

    assert len(out) == 1
    assert len(calls) == 1


def test_run_candidate_scan_uses_configured_score_weights(tmp_path: Path) -> None:
    _add_repo_to_syspath()
    import src.application.candidate_scanning as scan

    parsed = tmp_path / "input" / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-18",
                "dte": 30,
                "contract_symbol": "HIGH_RETURN_WIDE",
                "multiplier": 100,
                "currency": "USD",
                "strike": 100,
                "spot": 110,
                "bid": 0.025,
                "ask": 0.975,
                "last_price": 0.5,
                "mid": 0.5,
                "open_interest": 1,
                "volume": 0,
                "implied_volatility": 0.3,
                "delta": -0.35,
            },
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-18",
                "dte": 30,
                "contract_symbol": "LOWER_RETURN_LIQUID",
                "multiplier": 100,
                "currency": "USD",
                "strike": 95,
                "spot": 110,
                "bid": 0.4875,
                "ask": 0.5125,
                "last_price": 0.5,
                "mid": 0.5,
                "open_interest": 500,
                "volume": 20,
                "implied_volatility": 0.3,
                "delta": -0.18,
            },
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    annualized_by_contract = {
        "HIGH_RETURN_WIDE": 0.120,
        "LOWER_RETURN_LIQUID": 0.115,
    }

    out = scan.run_candidate_scan(
        config=scan.CandidateScanConfig(
            mode="put",
            symbols=["NVDA"],
            input_root=tmp_path / "input",
            output=tmp_path / "output.csv",
            empty_output_columns=["symbol"],
            min_dte=7,
            max_dte=60,
            min_strike=None,
            max_strike=None,
            min_open_interest=0,
            min_volume=0,
            max_spread_ratio=2.0,
            min_annualized_net_return=0.01,
            min_net_income=1,
            score_weights=scan.resolve_candidate_score_weights({"liquidity": 0.02}),
            quiet=True,
        ),
        deps=scan.CandidateScanDependencies(
            compute_metrics_fn=lambda contract: {
                "net_income": 100.0,
                "annualized_net_return_on_cash_basis": annualized_by_contract[contract.contract_symbol],
            },
            build_row_fn=lambda contract, base_values, metrics: {
                "symbol": contract.symbol,
                "contract_symbol": contract.contract_symbol,
                "expiration": contract.expiration,
                "dte": contract.dte,
                "strike": contract.strike,
                "open_interest": base_values.open_interest,
                "volume": base_values.volume,
                "spread_ratio": base_values.spread_ratio,
                "annualized_net_return_on_cash_basis": metrics["annualized_net_return_on_cash_basis"],
                "net_income": metrics["net_income"],
            },
            build_hard_constraint_kwargs_fn=lambda contract: {},
            annualized_return_value_fn=lambda metrics: float(metrics["annualized_net_return_on_cash_basis"]),
            annotate_event_risk_fn=lambda df, base_dir, cfg: df,
            print_summary_fn=lambda df, out_path, reject_path: None,
        ),
        event_risk_cfg=None,
        base_dir=tmp_path,
    )

    assert list(out["contract_symbol"]) == ["LOWER_RETURN_LIQUID", "HIGH_RETURN_WIDE"]


def test_run_candidate_scan_applies_sell_put_min_otm_pct(tmp_path: Path) -> None:
    _add_repo_to_syspath()
    import src.application.candidate_scanning as scan

    parsed = tmp_path / "input" / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-18",
                "dte": 30,
                "contract_symbol": "NEAR_SPOT",
                "multiplier": 100,
                "currency": "USD",
                "strike": 98,
                "spot": 100,
                "bid": 1.0,
                "ask": 1.2,
                "last_price": 1.1,
                "mid": 1.1,
                "open_interest": 100,
                "volume": 20,
                "implied_volatility": 0.3,
                "delta": -0.35,
            },
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-18",
                "dte": 30,
                "contract_symbol": "FIVE_PCT_OTM",
                "multiplier": 100,
                "currency": "USD",
                "strike": 95,
                "spot": 100,
                "bid": 0.8,
                "ask": 1.0,
                "last_price": 0.9,
                "mid": 0.9,
                "open_interest": 100,
                "volume": 20,
                "implied_volatility": 0.3,
                "delta": -0.2,
            },
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    out_path = tmp_path / "output.csv"
    out = scan.run_candidate_scan(
        config=scan.CandidateScanConfig(
            mode="put",
            symbols=["NVDA"],
            input_root=tmp_path / "input",
            output=out_path,
            empty_output_columns=["symbol"],
            min_dte=7,
            max_dte=60,
            min_strike=None,
            max_strike=None,
            min_open_interest=0,
            min_volume=0,
            max_spread_ratio=1.0,
            min_annualized_net_return=0.01,
            min_net_income=1,
            min_otm_pct=0.05,
            quiet=True,
        ),
        deps=scan.CandidateScanDependencies(
            compute_metrics_fn=lambda contract: {
                "net_income": 100.0,
                "annualized_net_return_on_cash_basis": 0.12,
            },
            build_row_fn=lambda contract, base_values, metrics: {
                "symbol": contract.symbol,
                "contract_symbol": contract.contract_symbol,
                "expiration": contract.expiration,
                "dte": contract.dte,
                "strike": contract.strike,
                "open_interest": base_values.open_interest,
                "volume": base_values.volume,
                "spread_ratio": base_values.spread_ratio,
                "annualized_net_return_on_cash_basis": metrics["annualized_net_return_on_cash_basis"],
                "net_income": metrics["net_income"],
            },
            build_hard_constraint_kwargs_fn=lambda contract: {},
            annualized_return_value_fn=lambda metrics: float(metrics["annualized_net_return_on_cash_basis"]),
            annotate_event_risk_fn=lambda df, base_dir, cfg: df,
            print_summary_fn=lambda df, out_path, reject_path: None,
        ),
        event_risk_cfg=None,
        base_dir=tmp_path,
    )

    assert list(out["contract_symbol"]) == ["FIVE_PCT_OTM"]
    reject_log = pd.read_csv(out_path.with_name("output_reject_log.csv"))
    assert list(reject_log["contract_symbol"]) == ["NEAR_SPOT"]
    assert list(reject_log["reject_rule"]) == ["strike"]
    assert list(reject_log["metric_value"]) == [0.02]
    assert list(reject_log["threshold"]) == [0.05]


def test_run_candidate_scan_logs_hard_liquidity_and_missing_spread_rejects(tmp_path: Path) -> None:
    _add_repo_to_syspath()
    import src.application.candidate_scanning as scan

    parsed = tmp_path / "input" / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-18",
                "dte": 5,
                "contract_symbol": "FAIL_DTE",
                "multiplier": 100,
                "currency": "USD",
                "strike": 100,
                "spot": 110,
                "bid": 0.9,
                "ask": 1.1,
                "last_price": 1.0,
                "mid": 1.0,
                "open_interest": 100,
                "volume": 50,
                "implied_volatility": 0.3,
                "delta": -0.2,
            },
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-18",
                "dte": 30,
                "contract_symbol": "FAIL_LIQUIDITY",
                "multiplier": 100,
                "currency": "USD",
                "strike": 100,
                "spot": 110,
                "bid": 0.9,
                "ask": 1.1,
                "last_price": 1.0,
                "mid": 1.0,
                "open_interest": 1,
                "volume": 2,
                "implied_volatility": 0.3,
                "delta": -0.2,
            },
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-18",
                "dte": 30,
                "contract_symbol": "FAIL_MISSING_SPREAD",
                "multiplier": 100,
                "currency": "USD",
                "strike": 99,
                "spot": 110,
                "bid": None,
                "ask": None,
                "last_price": 1.0,
                "mid": 1.0,
                "open_interest": 100,
                "volume": 50,
                "implied_volatility": 0.3,
                "delta": -0.2,
            },
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-18",
                "dte": 30,
                "contract_symbol": "PASS",
                "multiplier": 100,
                "currency": "USD",
                "strike": 98,
                "spot": 110,
                "bid": 0.95,
                "ask": 1.05,
                "last_price": 1.0,
                "mid": 1.0,
                "open_interest": 100,
                "volume": 50,
                "implied_volatility": 0.3,
                "delta": -0.2,
            },
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    out_path = tmp_path / "output.csv"
    out = scan.run_candidate_scan(
        config=scan.CandidateScanConfig(
            mode="put",
            symbols=["NVDA"],
            input_root=tmp_path / "input",
            output=out_path,
            empty_output_columns=["symbol"],
            min_dte=7,
            max_dte=60,
            min_strike=None,
            max_strike=None,
            min_open_interest=10,
            min_volume=10,
            max_spread_ratio=0.3,
            min_annualized_net_return=0.01,
            min_net_income=1,
            quiet=True,
        ),
        deps=scan.CandidateScanDependencies(
            compute_metrics_fn=lambda contract: {
                "net_income": 100.0,
                "annualized_net_return_on_cash_basis": 0.12,
            },
            build_row_fn=lambda contract, base_values, metrics: {
                "symbol": contract.symbol,
                "contract_symbol": contract.contract_symbol,
                "expiration": contract.expiration,
                "dte": contract.dte,
                "strike": contract.strike,
                "open_interest": base_values.open_interest,
                "volume": base_values.volume,
                "spread_ratio": base_values.spread_ratio,
                "annualized_net_return_on_cash_basis": metrics["annualized_net_return_on_cash_basis"],
                "net_income": metrics["net_income"],
            },
            build_hard_constraint_kwargs_fn=lambda contract: {},
            annualized_return_value_fn=lambda metrics: float(metrics["annualized_net_return_on_cash_basis"]),
            annotate_event_risk_fn=lambda df, base_dir, cfg: df,
            print_summary_fn=lambda df, out_path, reject_path: None,
        ),
        event_risk_cfg=None,
        base_dir=tmp_path,
    )

    assert list(out["contract_symbol"]) == ["PASS"]
    reject_log = pd.read_csv(out_path.with_name(f"{out_path.stem}_reject_log.csv"))
    by_contract = {
        contract: group["reject_rule"].tolist()
        for contract, group in reject_log.groupby("contract_symbol", sort=False)
    }
    assert by_contract["FAIL_DTE"] == ["dte"]
    assert by_contract["FAIL_LIQUIDITY"] == ["min_open_interest", "min_volume"]
    assert by_contract["FAIL_MISSING_SPREAD"] == ["max_spread_ratio"]
    assert set(reject_log["engine_reject_stage"]) == {"stage1_hard_constraints", "stage3_risk_filter"}
