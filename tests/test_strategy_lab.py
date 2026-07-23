from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _write_readiness_dataset(dataset: Path) -> None:
    _write_jsonl(
        dataset / "candidate_snapshots.jsonl",
        [
            {
                "contract_symbol": "NVDA260619P00100000",
                "symbol": "NVDA",
                "account": "lx",
                "option_type": "put",
                "status": "accepted",
                "strategy_family": "sell_put",
                "strategy_profile": "insurance_underwriting",
                "strike": 100,
                "dte": 30,
                "delta": -0.20,
                "iv_rv_ratio": 1.25,
                "iv_minus_rv": 0.08,
                "annualized_return": 0.22,
                "spread_ratio": 0.10,
                "single_trade_concentration": 0.02,
            },
            {
                "contract_symbol": "AAPL260619C00200000",
                "symbol": "AAPL",
                "account": "lx",
                "option_type": "call",
                "status": "rejected",
                "strategy_family": "sell_call",
                "strategy_profile": "insurance_underwriting",
                "strike": 200,
                "dte": 30,
                "delta": 0.25,
                "iv_rv_ratio": 1.18,
                "iv_minus_rv": 0.06,
                "annualized_return": 0.18,
                "spread_ratio": 0.10,
                "single_trade_concentration": 0.02,
                "covered_share_quantity": 100,
                "cost_basis": 150,
            },
            {
                "contract_symbol": "TSLA260619P00150000",
                "symbol": "TSLA",
                "account": "lx",
                "option_type": "put",
                "status": "accepted",
                "strategy_family": "combo_yield",
                "strategy_group_id": "combo-1",
                "leg_role": "funding_put",
                "strike": 150,
                "expiration": "2026-06-19",
                "side": "short",
                "contracts": 1,
                "multiplier": 100,
                "spot": 180,
                "dte": 30,
                "delta": -0.24,
                "net_income": 600,
            },
            {
                "contract_symbol": "TSLA260619C00220000",
                "symbol": "TSLA",
                "account": "lx",
                "option_type": "call",
                "status": "accepted",
                "strategy_family": "combo_yield",
                "strategy_group_id": "combo-1",
                "leg_role": "participation_call",
                "strike": 220,
                "expiration": "2026-06-19",
                "side": "long",
                "contracts": 1,
                "multiplier": 100,
                "spot": 180,
                "dte": 30,
                "delta": 0.30,
                "net_income": -400,
            },
        ],
    )
    _write_jsonl(
        dataset / "filter_decisions.jsonl",
        [
            {
                "contract_symbol": "AAPL260619C00200000",
                "option_type": "call",
                "status": "rejected",
                "rule": "delta_above_max_abs_delta",
            }
        ],
    )
    _write_jsonl(
        dataset / "mark_path_snapshots.jsonl",
        [
            {"contract_symbol": "NVDA260619P00100000", "mark_at": "2026-06-03", "option_mid": 1.1},
            {"contract_symbol": "AAPL260619C00200000", "mark_at": "2026-06-03", "option_mid": 0.8},
            {
                "contract_symbol": "TSLA260619P00150000",
                "mark_at": "2026-06-03",
                "option_mid": 2.0,
                "counterfactual_pnl": -100,
            },
            {
                "contract_symbol": "TSLA260619C00220000",
                "mark_at": "2026-06-03",
                "option_mid": 1.5,
                "counterfactual_pnl": 50,
            },
        ],
    )
    _write_jsonl(
        dataset / "outcome_facts.jsonl",
        [
            {"contract_symbol": "NVDA260619P00100000", "outcome": "expired_worthless", "realized_pnl": 120},
            {"contract_symbol": "AAPL260619C00200000", "outcome": "expired_worthless", "realized_pnl": 80},
            {"contract_symbol": "TSLA260619P00150000", "outcome": "expired_worthless", "realized_pnl": 150},
            {"contract_symbol": "TSLA260619C00220000", "outcome": "participated_upside", "realized_pnl": 300},
        ],
    )


def _write_update_dataset(dataset: Path) -> None:
    _write_jsonl(
        dataset / "candidate_snapshots.jsonl",
        [
            {
                "contract_symbol": "NVDA260619P00100000",
                "symbol": "NVDA",
                "account": "lx",
                "option_type": "put",
                "status": "accepted",
                "strategy_family": "sell_put",
                "strike": 100,
            },
            {
                "contract_symbol": "AMD260619P00100000",
                "symbol": "AMD",
                "account": "lx",
                "option_type": "put",
                "status": "rejected",
                "strategy_family": "sell_put",
                "strike": 100,
            },
        ],
    )
    _write_jsonl(
        dataset / "filter_decisions.jsonl",
        [{"contract_symbol": "AMD260619P00100000", "rule": "delta_above_max_abs_delta"}],
    )
    _write_jsonl(dataset / "mark_path_snapshots.jsonl", [])
    _write_jsonl(dataset / "outcome_facts.jsonl", [])


def _write_latest_scanned_run(runs_root: Path) -> Path:
    _write_candidate_run(runs_root, "run-evidence")
    return runs_root


def _write_candidate_run(runs_root: Path, run_id: str) -> Path:
    run_account = runs_root / run_id / "accounts" / "lx"
    run_account.mkdir(parents=True, exist_ok=True)
    (run_account / "sell_put_candidates.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,expiration,dte,delta,strike,net_income\n"
            "NVDA,lx,put,NVDA260619P00100000,2026-06-19,30,-0.2,100,120\n"
        ),
        encoding="utf-8",
    )
    (run_account / "candidate_filter_trace.jsonl").write_text(
        json.dumps(
            {
                "symbol": "AMD",
                "account": "lx",
                "function": "sell_put",
                "mode": "put",
                "contract_symbol": "AMD260619P00080000",
                "status": "rejected",
                "rule": "spread_too_wide",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return run_account.parent.parent


def _write_close_run(
    runs_root: Path,
    run_id: str,
    *,
    include_audit: bool = True,
    include_candidate: bool = False,
) -> Path:
    account_dir = runs_root / run_id / "accounts" / "lx"
    close_path = account_dir / "close_advice.csv"
    row = {
        "account": "lx",
        "position_lot_id": "lot-1",
        "symbol": "NVDA",
        "option_type": "put",
        "expiration": "2026-08-21",
        "strike": 100,
        "position_side": "short",
        "strategy_family": "sell_put",
        "strategy_profile": "insurance_underwriting",
        "tier": "medium",
        "exit_state": "profit_capture",
        "evaluation_status": "priced",
        "fee_calc_status": "schedule_estimate",
        "estimated_pnl_if_close_net": 80,
        "short_vol_thesis_status": "valid",
        "continued_willingness": "true",
        "close_calibration_status": "complete",
        "capture_ratio": 0.85,
        "remaining_annualized_return": 0.07,
        "dte": 29,
        "close_mid": 0.2,
        "bid": 0.19,
        "ask": 0.21,
        "remaining_premium": 20,
        "estimated_close_fee": 1.5,
        "fee_calc_basis": "futu_us_fixed_package_2026-07-22",
        "contracts_open": 1,
        "multiplier": 100,
        "currency": "USD",
        "policy_version": "p0_current.v1",
        "recommendation_state": "close",
        "decision_basis": "profit_capture_medium",
        "decision_evidence_status": "complete",
    }
    close_path.parent.mkdir(parents=True, exist_ok=True)
    with close_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    context_path = account_dir / "state" / "option_positions_context.json"
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text(
        json.dumps(
            {
                "as_of_utc": "2026-07-23T01:00:30Z",
                "open_positions_min": [
                    {
                        "record_id": "lot-1",
                        "account": "lx",
                        "symbol": "NVDA",
                        "option_type": "put",
                        "side": "short",
                        "expiration": "2026-08-21",
                        "strike": 100,
                        "contracts": 1,
                        "contracts_open": 1,
                        "multiplier": 100,
                        "currency": "USD",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    if include_audit:
        audit_path = runs_root / run_id / "state" / "audit_events.jsonl"
        _write_jsonl(
            audit_path,
            [
                {
                    "run_id": run_id,
                    "account": "lx",
                    "action": "close_advice",
                    "status": "ok",
                    "event_at_utc": "2026-07-23T01:01:00Z",
                }
            ],
        )
    if include_candidate:
        _write_candidate_run(runs_root, run_id)
    return runs_root / run_id


def _write_strategy_lab_window_run(root: Path) -> Path:
    account_dir = root / "output_runs" / "20260602T010000Z-run" / "accounts" / "lx"
    account_dir.mkdir(parents=True, exist_ok=True)
    (account_dir / "sell_put_candidates.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,expiration,dte,delta,strike,"
            "strategy_profile,iv_rv_ratio,iv_minus_rv,annualized_return,spread_ratio,"
            "single_trade_concentration,net_income\n"
            "NVDA,lx,put,NVDA260619P00100000,2026-06-19,30,-0.2,100,"
            "insurance_underwriting,1.25,0.08,0.22,0.10,0.02,120\n"
        ),
        encoding="utf-8",
    )
    (account_dir / "candidate_filter_trace.jsonl").write_text(
        json.dumps(
            {
                "symbol": "AMD",
                "account": "lx",
                "function": "sell_put",
                "mode": "put",
                "contract_symbol": "AMD260619P00080000",
                "status": "rejected",
                "rule": "spread_too_wide",
                "dte": 30,
                "delta": -0.22,
                "iv_rv_ratio": 1.10,
                "iv_minus_rv": 0.04,
                "annualized_return": 0.18,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return root / "output_runs"


def test_strategy_lab_readiness_builds_decision_instances_by_strategy_family(tmp_path: Path) -> None:
    from src.application.strategy_lab import analyze_strategy_lab_readiness

    dataset = tmp_path / "dataset"
    _write_readiness_dataset(dataset)

    result = analyze_strategy_lab_readiness(dataset=dataset, min_sample=1)

    assert result["schema_version"] == "strategy_lab_readiness.v1"
    assert result["summary"]["status"] == "ready_for_proposal"
    assert result["summary"]["data_mode"] == "closed_replay"
    assert result["decision_instances"]["summary"]["strategy_family_counts"] == {
        "combo_yield": 1,
        "covered_call": 1,
        "sell_put": 1,
    }
    assert result["readiness"]["domain_readiness"]["sell_put"]["ready"] is True
    assert result["readiness"]["domain_readiness"]["covered_call"]["ready"] is True
    assert result["readiness"]["domain_readiness"]["combo_yield"]["group_ready_count"] == 1
    assert result["safety"]["writes_runtime_config"] is False
    assert result["safety"]["runtime_config_write_allowed"] is False


def test_strategy_lab_readiness_flags_combo_yield_missing_group_identity(tmp_path: Path) -> None:
    from src.application.strategy_lab import analyze_strategy_lab_readiness

    dataset = tmp_path / "dataset"
    _write_jsonl(
        dataset / "candidate_snapshots.jsonl",
        [
            {
                "contract_symbol": "TSLA260619P00150000",
                "symbol": "TSLA",
                "option_type": "put",
                "status": "accepted",
                "strategy_family": "combo_yield",
            }
        ],
    )
    _write_jsonl(dataset / "filter_decisions.jsonl", [])
    _write_jsonl(dataset / "mark_path_snapshots.jsonl", [])
    _write_jsonl(dataset / "outcome_facts.jsonl", [])

    result = analyze_strategy_lab_readiness(dataset=dataset, min_sample=1)

    assert result["summary"]["status"] == "partial_ready"
    assert "combo_yield_group_identity_missing" in result["readiness"]["blockers"]
    assert result["readiness"]["domain_readiness"]["combo_yield"]["ready"] is False
    assert result["readiness"]["domain_readiness"]["combo_yield"]["supported_scope"] == "group_readiness_only"


def test_strategy_lab_readiness_blocks_covered_call_without_holding_context(tmp_path: Path) -> None:
    from src.application.strategy_lab import analyze_strategy_lab_readiness

    dataset = tmp_path / "dataset"
    _write_jsonl(
        dataset / "candidate_snapshots.jsonl",
        [
            {
                "contract_symbol": "AAPL260619C00200000",
                "symbol": "AAPL",
                "account": "lx",
                "option_type": "call",
                "status": "accepted",
                "strategy_family": "covered_call",
                "strike": 200,
                "dte": 30,
                "delta": 0.25,
                "iv_rv_ratio": 1.18,
                "iv_minus_rv": 0.06,
                "annualized_return": 0.18,
            }
        ],
    )
    _write_jsonl(dataset / "filter_decisions.jsonl", [])
    _write_jsonl(dataset / "mark_path_snapshots.jsonl", [])
    _write_jsonl(dataset / "outcome_facts.jsonl", [])

    result = analyze_strategy_lab_readiness(dataset=dataset, min_sample=1)

    blockers = result["readiness"]["domain_readiness"]["covered_call"]["blockers"]
    assert result["readiness"]["domain_readiness"]["covered_call"]["ready"] is False
    assert blockers["covered_call_coverage_context_missing"] == 1
    assert blockers["covered_call_cost_basis_context_missing"] == 1


def test_shadow_replay_capture_expands_real_combo_pair_row_once_per_leg(tmp_path: Path) -> None:
    from src.application.shadow_replay import build_shadow_replay_dataset

    run_dir = tmp_path / "output_runs" / "20260602T010000Z-run" / "accounts" / "lx"
    run_dir.mkdir(parents=True)
    (run_dir / "combo_yield_candidates.csv").write_text(
        (
            "symbol,account,expiration,dte,spot,multiplier,put_contract_symbol,put_strike,"
            "put_bid,put_ask,put_mid,put_delta,put_open_interest,put_volume,put_spread_ratio,"
            "call_contract_symbol,call_strike,call_bid,call_ask,call_mid,call_delta,"
            "call_open_interest,call_volume,call_spread_ratio,put_net_credit,call_total_cost,"
            "combo_net_credit,net_credit_retention,call_cost_to_put_credit\n"
            "TSLA,lx,2026-06-19,30,180,100,TSLA260619P00150000,150,6.0,6.2,6.1,-0.24,"
            "500,100,0.03,TSLA260619C00220000,220,3.8,4.0,3.9,0.30,400,80,0.05,600,400,"
            "200,0.333333,0.666667\n"
        ),
        encoding="utf-8",
    )

    manifest = build_shadow_replay_dataset(
        repo_root=tmp_path,
        run_dir=tmp_path / "output_runs" / "20260602T010000Z-run",
        dataset_id="case",
    )
    rows = [
        json.loads(line)
        for line in (Path(manifest["dataset_dir"]) / "candidate_snapshots.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert len(rows) == 2
    assert len({row["strategy_group_id"] for row in rows}) == 1
    assert rows[0]["strategy_group_id"].startswith(
        "combo_yield|20260602T010000Z-run|lx|TSLA|2026-06-19|"
    )
    assert {row["leg_role"] for row in rows} == {"funding_put", "participation_call"}
    by_role = {row["leg_role"]: row for row in rows}
    assert by_role["funding_put"]["side"] == "short"
    assert by_role["funding_put"]["contract_symbol"] == "TSLA260619P00150000"
    assert by_role["funding_put"]["net_income"] == 600
    assert by_role["participation_call"]["side"] == "long"
    assert by_role["participation_call"]["contract_symbol"] == "TSLA260619C00220000"
    assert by_role["participation_call"]["net_income"] == -400
    assert by_role["participation_call"]["entry_cost"] == 400
    assert sum(row["net_income"] for row in rows) == 200
    assert {row["combo_net_credit"] for row in rows} == {200}


def test_shadow_replay_capture_does_not_copy_pair_net_credit_into_combo_legs(tmp_path: Path) -> None:
    from src.application.shadow_replay import build_shadow_replay_dataset
    from src.application.strategy_lab import run_combo_yield_group_experiment

    run_dir = tmp_path / "output_runs" / "20260602T010000Z-run" / "accounts" / "lx"
    run_dir.mkdir(parents=True)
    (run_dir / "combo_yield_candidates.csv").write_text(
        (
            "symbol,account,expiration,spot,multiplier,put_contract_symbol,put_strike,"
            "call_contract_symbol,call_strike,net_credit\n"
            "TSLA,lx,2026-06-19,180,100,TSLA260619P00150000,150,"
            "TSLA260619C00220000,220,200\n"
        ),
        encoding="utf-8",
    )

    manifest = build_shadow_replay_dataset(
        repo_root=tmp_path,
        run_dir=tmp_path / "output_runs" / "20260602T010000Z-run",
        dataset_id="case",
    )
    rows = [
        json.loads(line)
        for line in (Path(manifest["dataset_dir"]) / "candidate_snapshots.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    result = run_combo_yield_group_experiment(candidate_snapshots=rows, min_sample=1)
    group = result["group_universe"]["groups"][0]

    assert all(row["net_income"] is None for row in rows)
    assert result["summary"]["ready_group_count"] == 0
    assert group["metrics"]["net_premium"] is None
    assert "combo_yield_group_metric_missing" in group["blockers"]
    assert group["outcome_evaluation"]["status"] == "not_evaluable"


def test_shadow_replay_capture_preserves_underwriting_ranking_fields(tmp_path: Path) -> None:
    from src.application.shadow_replay import build_shadow_replay_dataset

    run_dir = tmp_path / "output_runs" / "20260602T010000Z-run" / "accounts" / "lx"
    run_dir.mkdir(parents=True)
    (run_dir / "nvda_sell_put_candidates_labeled.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,status,strategy_profile,strike,max_strike,"
            "premium_edge_score,strike_safety_margin_pct,annualized_return,iv_rv_ratio,iv_minus_rv,"
            "spread_ratio,open_interest,net_income_cny\n"
            "NVDA,lx,put,NVDA260619P00100000,accepted,insurance_underwriting,100,110,"
            "1.2,0.090909,0.20,1.30,0.08,0.05,500,1200\n"
        ),
        encoding="utf-8",
    )

    manifest = build_shadow_replay_dataset(
        repo_root=tmp_path,
        run_dir=tmp_path / "output_runs" / "20260602T010000Z-run",
        dataset_id="case",
    )
    row = json.loads(
        (Path(manifest["dataset_dir"]) / "candidate_snapshots.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )

    assert row["premium_edge_score"] == 1.2
    assert row["strike_safety_margin_pct"] == 0.090909
    assert row["max_strike"] == 110.0
    assert row["open_interest"] == 500.0
    assert row["net_income_cny"] == 1200.0


def test_cli_strategy_lab_readiness(capsys, monkeypatch, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    monkeypatch.setattr(cli, "repo_base", lambda: tmp_path)
    dataset = tmp_path / "dataset"
    _write_readiness_dataset(dataset)

    rc = cli.main(
        [
            "research",
            "strategy-lab",
            "readiness",
            "--dataset",
            str(dataset),
            "--min-sample",
            "1",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["tool_name"] == "research.strategy-lab.readiness"
    assert payload["data"]["schema_version"] == "strategy_lab_readiness.v1"
    assert payload["data"]["summary"]["status"] == "ready_for_proposal"


def test_cli_strategy_lab_readiness_run_window(capsys, monkeypatch, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    monkeypatch.setattr(cli, "repo_base", lambda: tmp_path)
    _write_strategy_lab_window_run(tmp_path)

    rc = cli.main(
        [
            "research",
            "strategy-lab",
            "readiness",
            "--start-date",
            "2026-06-02",
            "--account",
            "lx",
            "--market",
            "us",
            "--min-sample",
            "1",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["tool_name"] == "research.strategy-lab.readiness"
    assert payload["data"]["dataset_dir"] is None
    assert payload["data"]["input_scope"]["coverage"]["selected_scanned_runs"] == 1
    assert payload["data"]["input_scope"]["filters"]["accounts"] == ["lx"]
    assert payload["data"]["summary"]["ready_for_experiment"] is True


def test_strategy_lab_hypotheses_generate_parameter_set_and_domain_adapters(tmp_path: Path) -> None:
    from src.application.strategy_lab import generate_strategy_lab_hypotheses

    dataset = tmp_path / "dataset"
    _write_readiness_dataset(dataset)

    result = generate_strategy_lab_hypotheses(dataset=dataset, min_sample=1)

    assert result["schema_version"] == "strategy_lab_hypotheses.v1"
    assert result["summary"]["parameter_set_ready"] is True
    variants = result["parameter_set"]["variants"]
    assert any(variant["name"].startswith("sell_put_") for variant in variants)
    assert any(variant["name"].startswith("covered_call_") for variant in variants)
    assert {
        variant["strategy_family"]
        for variant in variants
    } == {"sell_put", "covered_call"}
    assert all("delta" not in variant["name"] for variant in variants)
    assert all(
        not ({"min_abs_delta", "max_abs_delta"} & set(variant["profiles"]["insurance_underwriting"]))
        for variant in variants
    )
    history_variants = [
        variant
        for variant in variants
        if variant["name"].endswith("_historical_iv_rv_percentile")
    ]
    assert {variant["name"].split("_historical_iv_rv_percentile")[0] for variant in history_variants} == {
        "covered_call",
        "sell_put",
    }
    assert all(
        variant["profiles"]["insurance_underwriting"]["min_iv_rv_percentile"] == 0.7
        and variant["profiles"]["insurance_underwriting"]["min_iv_rv_history_samples"] == 20.0
        for variant in history_variants
    )
    baselines = {
        item["strategy_family"]: item["baseline_parameters"]
        for item in result["domain_hypotheses"]
        if item["strategy_family"] in {"sell_put", "covered_call"}
    }
    for variant in history_variants:
        family = variant["strategy_family"]
        params = variant["profiles"]["insurance_underwriting"]
        assert params["min_iv_rv_ratio"] == baselines[family]["min_iv_rv_ratio"]
        assert params["min_iv_minus_rv"] == baselines[family]["min_iv_minus_rv"]
    single_leg_adapters = [
        item["adapter"]
        for item in result["domain_hypotheses"]
        if item["strategy_family"] in {"sell_put", "covered_call"}
    ]
    assert all("min_abs_delta" not in adapter["tunable_parameters"] for adapter in single_leg_adapters)
    assert all("max_abs_delta" not in adapter["tunable_parameters"] for adapter in single_leg_adapters)
    assert all("min_iv_rv_percentile" in adapter["tunable_parameters"] for adapter in single_leg_adapters)
    combo = next(item for item in result["domain_hypotheses"] if item["strategy_family"] == "combo_yield")
    assert combo["status"] == "group_experiment_delegated"
    assert combo["adapter"]["hypothesis_enabled"] is False
    assert combo["adapter"]["hypothesis_scope"] == "group_level_outcome_evaluation"
    assert combo["adapter"]["tunable_parameters"] == []
    assert combo["blockers"] == []
    assert "combo_yield_group_evaluator_runs_in_strategy_lab_experiment" in combo["limitations"]


def test_combo_yield_group_experiment_does_not_select_without_outcomes(tmp_path: Path) -> None:
    from src.application.strategy_lab import run_combo_yield_group_experiment
    from src.application.strategy_lab.evidence import load_strategy_lab_dataset

    dataset = tmp_path / "dataset"
    _write_readiness_dataset(dataset)
    evidence = load_strategy_lab_dataset(dataset)

    result = run_combo_yield_group_experiment(
        candidate_snapshots=evidence["candidate_snapshots"],
        min_sample=1,
    )

    assert result["schema_version"] == "strategy_lab_combo_yield_group_experiment.v1"
    assert result["summary"]["status"] == "ready"
    assert result["summary"]["production_recommendation_allowed"] is False
    assert result["summary"]["ready_group_count"] == 1
    assert result["summary"]["evaluable_group_count"] == 0
    assert result["scorecard"]["status"] == "not_evaluable"
    assert {"variant_count", "best_variant", "optimization_claim"}.isdisjoint(result["summary"])
    assert {"best_variant", "best_variant_basis", "optimization_claim"}.isdisjoint(result["scorecard"])
    assert "variants" not in result
    assert result["group_universe"]["groups"][0]["metrics"]["net_premium"] == 200
    assert result["group_universe"]["groups"][0]["outcome_evaluation"]["status"] == "not_evaluable"
    assert result["safety"]["runtime_config_write_allowed"] is False


def test_combo_yield_group_evaluator_aggregates_complete_leg_outcomes_once(tmp_path: Path) -> None:
    from src.application.strategy_lab import run_combo_yield_group_experiment
    from src.application.strategy_lab.evidence import load_strategy_lab_dataset

    dataset = tmp_path / "dataset"
    _write_readiness_dataset(dataset)
    evidence = load_strategy_lab_dataset(dataset)

    result = run_combo_yield_group_experiment(
        candidate_snapshots=evidence["candidate_snapshots"],
        mark_snapshots=evidence["mark_snapshots"],
        outcome_facts=evidence["outcome_facts"],
        min_sample=1,
    )

    group = result["group_universe"]["groups"][0]
    outcome = group["outcome_evaluation"]
    assert "variant_count" not in result["summary"]
    assert result["scorecard"]["status"] == "ready"
    assert group["metrics"]["net_premium"] == 200
    assert outcome["status"] == "evaluable"
    assert outcome["realized_pnl"] == 450
    assert outcome["capital_at_risk"] == 14_800
    assert outcome["return_on_capital"] == 0.030405
    assert outcome["max_adverse_pnl"] == -50
    assert outcome["max_adverse_return_on_capital"] == -0.003378
    assert outcome["mark_path"] == [{"mark_at": "2026-06-03", "group_pnl": -50.0}]


def test_combo_yield_group_evaluator_rejects_invalid_leg_structures() -> None:
    from src.application.strategy_lab import run_combo_yield_group_experiment

    put = {
        "contract_symbol": "TSLA260619P00150000",
        "symbol": "TSLA",
        "account": "lx",
        "option_type": "put",
        "status": "accepted",
        "strategy_family": "combo_yield",
        "leg_role": "funding_put",
        "side": "short",
        "strike": 150,
        "expiration": "2026-06-19",
        "contracts": 1,
        "multiplier": 100,
        "spot": 180,
        "net_income": 600,
    }
    call = {
        **put,
        "contract_symbol": "TSLA260619C00220000",
        "option_type": "call",
        "leg_role": "participation_call",
        "side": "long",
        "strike": 220,
        "net_income": -400,
    }
    rows = [
        {**put, "strategy_group_id": "duplicate"},
        {**put, "strategy_group_id": "duplicate"},
        {**call, "strategy_group_id": "duplicate"},
        {**put, "strategy_group_id": "wrong-side"},
        {**call, "strategy_group_id": "wrong-side", "side": "short"},
        {**put, "strategy_group_id": "mismatch"},
        {
            **call,
            "strategy_group_id": "mismatch",
            "account": "sy",
            "expiration": "2026-07-17",
            "multiplier": 50,
        },
        {**put, "strategy_group_id": "missing-call"},
    ]

    result = run_combo_yield_group_experiment(candidate_snapshots=rows, min_sample=1)
    blockers = result["group_universe"]["blockers"]

    assert result["summary"]["ready_group_count"] == 0
    assert blockers["combo_yield_group_leg_count_invalid"] == 2
    assert blockers["combo_yield_funding_put_leg_invalid"] == 1
    assert blockers["combo_yield_participation_call_leg_invalid"] == 1
    assert blockers["combo_yield_contract_duplicate"] == 1
    assert blockers["combo_yield_participation_call_side_invalid"] == 1
    assert blockers["combo_yield_account_mismatch"] == 1
    assert blockers["combo_yield_expiration_mismatch"] == 1
    assert blockers["combo_yield_multiplier_mismatch"] == 1


def test_combo_yield_invalid_structure_makes_complete_outcome_not_evaluable() -> None:
    from src.application.strategy_lab import run_combo_yield_group_experiment

    put = {
        "contract_symbol": "TSLA260619P00150000",
        "symbol": "TSLA",
        "account": "lx",
        "option_type": "put",
        "status": "accepted",
        "strategy_family": "combo_yield",
        "strategy_group_id": "wrong-side",
        "leg_role": "funding_put",
        "side": "short",
        "strike": 150,
        "expiration": "2026-06-19",
        "contracts": 1,
        "multiplier": 100,
        "spot": 180,
        "net_income": 600,
    }
    call = {
        **put,
        "contract_symbol": "TSLA260619C00220000",
        "option_type": "call",
        "leg_role": "participation_call",
        "side": "short",
        "strike": 220,
        "net_income": -400,
    }
    marks = [
        {"contract_symbol": put["contract_symbol"], "mark_at": "2026-06-03", "unrealized_pnl": -100},
        {"contract_symbol": call["contract_symbol"], "mark_at": "2026-06-03", "unrealized_pnl": 50},
    ]
    outcomes = [
        {"contract_symbol": put["contract_symbol"], "realized_pnl": 300},
        {"contract_symbol": call["contract_symbol"], "realized_pnl": 150},
    ]

    result = run_combo_yield_group_experiment(
        candidate_snapshots=[put, call],
        mark_snapshots=marks,
        outcome_facts=outcomes,
        min_sample=1,
    )
    group = result["group_universe"]["groups"][0]

    assert group["ready_for_group_experiment"] is False
    assert "combo_yield_participation_call_side_invalid" in group["blockers"]
    assert group["outcome_evaluation"]["status"] == "not_evaluable"
    assert "combo_yield_participation_call_side_invalid" in group["outcome_evaluation"]["blockers"]


def test_strategy_lab_experiment_runs_candidate_impact_scorecard(tmp_path: Path) -> None:
    from src.application.strategy_lab import run_strategy_lab_experiment

    dataset = tmp_path / "dataset"
    _write_readiness_dataset(dataset)

    result = run_strategy_lab_experiment(repo_root=tmp_path, dataset=dataset, min_sample=1)

    assert result["schema_version"] == "strategy_lab_experiment.v1"
    assert result["summary"]["status"] == "ready_for_scorecard_review"
    assert result["summary"]["production_recommendation_allowed"] is False
    assert result["summary"]["combo_yield_group_evaluator_status"] == "ready"
    assert result["summary"]["combo_yield_evaluable_group_count"] == 1
    assert result["summary"]["combo_yield_group_experiment_allowed"] is True
    assert result["evaluation"]["schema_version"] == "shadow_replay_candidate_impact.v1"
    assert result["group_experiments"]["combo_yield"]["schema_version"] == "strategy_lab_combo_yield_group_experiment.v1"
    combo = result["group_experiments"]["combo_yield"]
    assert combo["scorecard"]["status"] == "ready"
    assert {"best_variant", "best_variant_basis", "optimization_claim"}.isdisjoint(combo["scorecard"])
    assert combo["scorecard"]["group_outcome_metrics"]["realized_pnl_total"] == 450
    assert combo["scorecard"]["group_outcome_metrics"]["max_adverse_pnl_worst"] == -50
    assert result["scorecard"]["status"] == "not_evaluable"
    assert result["scorecard"]["best_variant"] is None
    assert result["scorecard"]["optimization_claim"] == "none"
    assert result["scorecard"]["best_variant_basis"] is None
    assert all(row["domain_metrics_status"] == "not_evaluable" for row in result["scorecard"]["rows"])
    assert "candidate_counts_are_review_context_not_selection_score" in result["scorecard"]["limitations"]
    assert "combo_yield_group_evaluator_not_implemented" not in result["scorecard"]["limitations"]
    assert "combo_yield_group_experiment_reported_separately" in result["scorecard"]["limitations"]
    assert result["safety"]["writes_runtime_config"] is False


def test_strategy_lab_compares_observed_and_deduplicated_underwriting_rankings(tmp_path: Path) -> None:
    from src.application.strategy_lab import run_strategy_lab_experiment

    dataset = tmp_path / "dataset"
    common = {
        "run_id": "run-1",
        "source_path": "output_runs/run-1/accounts/lx/nvda_sell_put_candidates_labeled.csv",
        "symbol": "NVDA",
        "account": "lx",
        "option_type": "put",
        "status": "accepted",
        "strategy_family": "sell_put",
        "strategy_profile": "insurance_underwriting",
        "spread_ratio": 0.05,
        "open_interest": 500,
        "event_source_status": "ok",
    }
    _write_jsonl(
        dataset / "candidate_snapshots.jsonl",
        [
            {
                **common,
                "source_row_number": 1,
                "contract_symbol": "RICH",
                "strike": 104,
                "max_strike": 110,
                "strike_safety_margin_pct": 0.05,
                "annualized_return": 0.30,
                "iv_rv_ratio": 1.50,
                "iv_minus_rv": 0.12,
                "premium_edge_score": 1.50,
                "net_income_cny": 10_000,
            },
            {
                **common,
                "source_row_number": 2,
                "contract_symbol": "NEAR",
                "strike": 108,
                "max_strike": 110,
                "strike_safety_margin_pct": 0.02,
                "annualized_return": 0.25,
                "iv_rv_ratio": 1.40,
                "iv_minus_rv": 0.10,
                "premium_edge_score": 1.45,
                "net_income_cny": 200,
            },
            {
                **common,
                "source_row_number": 3,
                "contract_symbol": "SAFE_LOW_INCOME",
                "strike": 93.5,
                "max_strike": 110,
                "strike_safety_margin_pct": 0.15,
                "annualized_return": 0.12,
                "iv_rv_ratio": 1.20,
                "iv_minus_rv": 0.06,
                "premium_edge_score": 1.00,
                "net_income_cny": 100,
            },
            {
                **common,
                "source_row_number": 4,
                "contract_symbol": "SAFE_HIGH_INCOME",
                "strike": 93.5,
                "max_strike": 110,
                "strike_safety_margin_pct": 0.15,
                "annualized_return": 0.12,
                "iv_rv_ratio": 1.20,
                "iv_minus_rv": 0.06,
                "premium_edge_score": 1.40,
                "net_income_cny": 500,
            },
        ],
    )
    _write_jsonl(dataset / "filter_decisions.jsonl", [])
    _write_jsonl(dataset / "mark_path_snapshots.jsonl", [])
    _write_jsonl(dataset / "outcome_facts.jsonl", [])

    result = run_strategy_lab_experiment(repo_root=tmp_path, dataset=dataset, min_sample=1)
    experiment = result["ranking_experiments"]["underwriting_deduplicated"]
    group = experiment["groups"][0]
    by_contract = {row["contract_symbol"]: row for row in group["deduplicated"]}

    assert experiment["summary"]["status"] == "ready"
    assert experiment["summary"]["production_recommendation_allowed"] is False
    assert experiment["policy"]["net_income_in_primary_score"] is False
    assert experiment["policy"]["net_income_ranking_role"] == "final_tiebreak_only"
    assert group["top_n"]["production_observed"] == ["RICH", "NEAR", "SAFE_LOW_INCOME"]
    assert group["top_n"]["deduplicated"] == ["SAFE_HIGH_INCOME", "SAFE_LOW_INCOME", "RICH"]
    assert group["top_n"]["changed"] is True
    assert by_contract["SAFE_HIGH_INCOME"]["deduplicated_compensation_score"] == by_contract[
        "SAFE_LOW_INCOME"
    ]["deduplicated_compensation_score"]
    assert by_contract["RICH"]["vol_edge_score"] == min(
        by_contract["RICH"]["iv_rv_edge_score"],
        by_contract["RICH"]["iv_minus_rv_edge_score"],
    )
    assert "ranking_only_cannot_claim_return_drawdown_or_cvar_improvement" in experiment["limitations"]
    assert result["summary"]["underwriting_ranking_comparable_group_count"] == 1


def test_strategy_lab_builds_fixed_vs_historical_and_observed_vs_deduplicated_matrix() -> None:
    from src.application.strategy_lab.experiment import _underwriting_factorial_experiment

    common = {
        "source_path": "output_runs/run/accounts/lx/nvda_sell_put_candidates_labeled.csv",
        "symbol": "NVDA",
        "account": "lx",
        "option_type": "put",
        "expiration": "2026-08-21",
        "dte": 30,
        "status": "accepted",
        "strategy_family": "sell_put",
        "strategy_profile": "insurance_underwriting",
        "max_strike": 110,
        "annualized_return": 0.12,
        "iv_minus_rv": 0.06,
        "spread_ratio": 0.05,
        "open_interest": 500,
        "event_source_status": "ok",
        "net_income_cny": 100,
    }
    candidates = [
        {
            **common,
            "run_id": "run-1",
            "source_row_number": 1,
            "contract_symbol": "HISTORY",
            "strike": 100,
            "strike_safety_margin_pct": 0.09,
            "iv_rv_ratio": 1.0,
        },
        {
            **common,
            "run_id": "run-2",
            "source_row_number": 1,
            "contract_symbol": "LOW_PERCENTILE",
            "strike": 104.5,
            "strike_safety_margin_pct": 0.05,
            "iv_rv_ratio": 1.0,
        },
        {
            **common,
            "run_id": "run-2",
            "source_row_number": 2,
            "contract_symbol": "HIGH_PERCENTILE",
            "strike": 93.5,
            "strike_safety_margin_pct": 0.15,
            "iv_rv_ratio": 1.4,
        },
    ]
    hypotheses = {
        "candidate_impact_parameter_set": {
            "baseline": "production_observed",
            "variants": [
                {
                    "name": "sell_put_historical_iv_rv_percentile",
                    "strategy_family": "sell_put",
                    "insurance_underwriting": {
                        "min_iv_rv_ratio": 1.0,
                        "min_iv_minus_rv": 0.05,
                        "min_iv_rv_percentile": 0.7,
                        "min_iv_rv_history_samples": 1,
                    },
                }
            ],
        },
        "domain_hypotheses": [
            {
                "strategy_family": "sell_put",
                "baseline_parameters": {
                    "min_annualized_return": 0.1,
                    "min_iv_rv_ratio": 1.0,
                    "min_iv_minus_rv": 0.05,
                },
            }
        ],
    }

    experiment = _underwriting_factorial_experiment(
        candidate_snapshots=candidates,
        mark_snapshots=[],
        outcome_facts=[],
        hypotheses=hypotheses,
        min_sample=1,
        top_n=2,
    )
    sell_put = next(row for row in experiment["families"] if row["strategy_family"] == "sell_put")
    cells = sell_put["cells"]

    def latest(cell_name: str) -> list[str | None]:
        groups = cells[cell_name]["groups"]
        return next(group["selected_contracts"] for group in groups if "|run-2|" in group["group_id"])

    assert experiment["summary"]["status"] == "ready"
    assert sell_put["status"] == "ready"
    assert latest("fixed_iv_rv__production_observed") == ["LOW_PERCENTILE", "HIGH_PERCENTILE"]
    assert latest("historical_iv_rv_percentile__production_observed") == ["HIGH_PERCENTILE"]
    assert latest("fixed_iv_rv__deduplicated") == ["HIGH_PERCENTILE", "LOW_PERCENTILE"]
    assert latest("historical_iv_rv_percentile__deduplicated") == ["HIGH_PERCENTILE"]
    assert cells["historical_iv_rv_percentile__deduplicated"]["outcome_comparison"]["status"] == "not_evaluable"
    assert experiment["summary"]["production_recommendation_allowed"] is False


def test_strategy_lab_proposal_does_not_patch_candidate_count_only_experiment(tmp_path: Path) -> None:
    from src.application.strategy_lab import build_strategy_lab_proposal, run_strategy_lab_experiment

    dataset = tmp_path / "dataset"
    _write_readiness_dataset(dataset)
    experiment = run_strategy_lab_experiment(repo_root=tmp_path, dataset=dataset, min_sample=1)

    proposal = build_strategy_lab_proposal(experiment=experiment)

    assert proposal["schema_version"] == "strategy_lab_proposal.v1"
    assert proposal["status"] == "needs_more_evidence"
    assert proposal["runtime_config_write_allowed"] is False
    assert proposal["production_recommendation_allowed"] is False
    assert proposal["recommended_variant"] is None
    assert proposal["dry_run_patch"] == {}
    assert "strict_outcome_dominance_required_for_patch" in proposal["limitations"]


def test_strategy_lab_proposal_requires_strict_outcome_dominance() -> None:
    from src.application.strategy_lab import build_strategy_lab_proposal
    from src.application.strategy_lab.experiment import _scorecard

    def accepted_metrics(
        *,
        return_on_capital: float,
        max_adverse_return: float,
        cvar: float,
        lifecycle_return: float,
        assignment_rate: float,
    ) -> dict:
        return {
            "return_on_capital_observation_count": 30,
            "return_on_capital_avg": return_on_capital,
            "max_adverse_return_on_capital_observation_count": 30,
            "max_adverse_return_on_capital_worst": max_adverse_return,
            "tail_risk": {"status": "evaluable", "cvar_90": cvar},
            "lifecycle_transition_count": 3,
            "lifecycle_return_on_capital_observation_count": 3,
            "lifecycle_return_on_capital_avg": lifecycle_return,
            "assignment_rate": assignment_rate,
        }

    variant = {
        "name": "sell_put_strictly_better",
        "strategy_family": "sell_put",
        "parameters": {"insurance_underwriting": {"min_iv_rv_ratio": 1.0}},
        "candidate_count": 30,
        "newly_accepted_count": 2,
        "newly_rejected_count": 1,
        "safety_violation_count": 0,
        "safety_rejected_count": 0,
        "comparison_eligible": True,
        "analysis_summary": {"manual_strategy_review_ready": True},
        "insurance_metrics": {
            "by_mode_status": {
                "put": {
                    "accepted": accepted_metrics(
                        return_on_capital=0.03,
                        max_adverse_return=-0.20,
                        cvar=-0.10,
                        lifecycle_return=0.01,
                        assignment_rate=0.30,
                    )
                }
            }
        },
    }
    evaluation = {
        "data_mode": "closed_replay",
        "baseline": {
            "analysis_summary": {"manual_strategy_review_ready": True},
            "insurance_metrics": {
                "by_mode_status": {
                    "put": {
                        "accepted": accepted_metrics(
                            return_on_capital=0.02,
                            max_adverse_return=-0.25,
                            cvar=-0.15,
                            lifecycle_return=-0.02,
                            assignment_rate=0.20,
                        )
                    }
                }
            },
        },
        "variants": [variant],
        "gates": {
            "sample_size": {"min_sample": 30},
            "production_recommendation": {"allowed": True},
        },
    }
    hypotheses = {
        "domain_hypotheses": [
            {
                "strategy_family": "sell_put",
                "baseline_parameters": {"min_iv_rv_ratio": 1.1},
                "adapter": {"scorecard_metrics": ["tail_loss"]},
            }
        ]
    }
    scorecard = _scorecard(evaluation=evaluation, hypotheses=hypotheses)

    proposal = build_strategy_lab_proposal(
        experiment={
            "summary": {"status": "ready_for_scorecard_review"},
            "scorecard": scorecard,
            "evaluation": evaluation,
            "hypotheses": hypotheses,
        }
    )

    assert scorecard["best_variant"]["variant"] == "sell_put_strictly_better"
    assert scorecard["best_variant_basis"] == "strict_outcome_dominance"
    assert scorecard["best_variant"]["outcome_comparison"]["descriptive_transitions"] == {
        "metric": "assignment_rate",
        "baseline": 0.20,
        "variant": 0.30,
        "used_as_failure_penalty": False,
    }
    assert proposal["status"] == "shadow_rollout_candidate"
    assert proposal["recommended_variant"] == "sell_put_strictly_better"
    assert proposal["dry_run_patch"] == {"sell_put.insurance_underwriting.min_iv_rv_ratio": 1.0}


def test_strategy_lab_proposal_does_not_patch_offline_history_parameters() -> None:
    from src.application.strategy_lab import build_strategy_lab_proposal

    variant_name = "sell_put_historical_iv_rv_percentile"
    proposal = build_strategy_lab_proposal(
        experiment={
            "summary": {"status": "ready_for_scorecard_review"},
            "scorecard": {
                "best_variant_basis": "strict_outcome_dominance",
                "best_variant": {
                    "variant": variant_name,
                    "strategy_family": "sell_put",
                },
                "limitations": [],
            },
            "evaluation": {
                "data_mode": "closed_replay",
                "gates": {"production_recommendation": {"allowed": True}},
                "variants": [
                    {
                        "name": variant_name,
                        "parameters": {
                            "insurance_underwriting": {
                                "min_iv_rv_ratio": 1.0,
                                "min_iv_rv_percentile": 0.7,
                                "min_iv_rv_history_samples": 20,
                            }
                        },
                    }
                ],
            },
            "hypotheses": {
                "domain_hypotheses": [
                    {
                        "strategy_family": "sell_put",
                        "baseline_parameters": {"min_iv_rv_ratio": 1.1},
                    }
                ]
            },
        }
    )

    assert proposal["status"] == "needs_more_evidence"
    assert proposal["dry_run_patch"] == {}
    assert "offline_only_variant_not_patchable" in proposal["limitations"]
    assert "proposal_is_advisory_only" in proposal["limitations"]
    assert "# Strategy Lab Proposal" in proposal["proposal_markdown"]


def test_strategy_lab_proposal_blocks_patch_without_closed_replay(tmp_path: Path) -> None:
    from src.application.strategy_lab import build_strategy_lab_proposal, run_strategy_lab_experiment

    dataset = tmp_path / "dataset"
    _write_readiness_dataset(dataset)
    _write_jsonl(dataset / "mark_path_snapshots.jsonl", [])
    _write_jsonl(dataset / "outcome_facts.jsonl", [])
    experiment = run_strategy_lab_experiment(repo_root=tmp_path, dataset=dataset, min_sample=1)

    proposal = build_strategy_lab_proposal(experiment=experiment)

    assert experiment["evaluation"]["data_mode"] == "filter_only"
    assert proposal["status"] == "needs_more_evidence"
    assert proposal["recommended_variant"] is None
    assert proposal["dry_run_patch"] == {}
    assert "closed_replay_outcome_required_for_patch" in proposal["limitations"]


def test_strategy_lab_proposal_reports_combo_yield_group_advisory(tmp_path: Path) -> None:
    from src.application.strategy_lab import build_strategy_lab_proposal, run_strategy_lab_experiment

    dataset = tmp_path / "dataset"
    _write_jsonl(
        dataset / "candidate_snapshots.jsonl",
        [
            {
                "contract_symbol": "TSLA260619P00150000",
                "symbol": "TSLA",
                "account": "lx",
                "option_type": "put",
                "status": "accepted",
                "strategy_family": "combo_yield",
                "strategy_group_id": "combo-1",
                "leg_role": "funding_put",
                "strike": 150,
                "expiration": "2026-06-19",
                "side": "short",
                "contracts": 1,
                "multiplier": 100,
                "spot": 180,
                "net_income": 600,
            },
            {
                "contract_symbol": "TSLA260619C00220000",
                "symbol": "TSLA",
                "account": "lx",
                "option_type": "call",
                "status": "accepted",
                "strategy_family": "combo_yield",
                "strategy_group_id": "combo-1",
                "leg_role": "participation_call",
                "strike": 220,
                "expiration": "2026-06-19",
                "side": "long",
                "contracts": 1,
                "multiplier": 100,
                "spot": 180,
                "net_income": -400,
            },
        ],
    )
    _write_jsonl(dataset / "filter_decisions.jsonl", [])
    _write_jsonl(dataset / "mark_path_snapshots.jsonl", [])
    _write_jsonl(dataset / "outcome_facts.jsonl", [])
    experiment = run_strategy_lab_experiment(repo_root=tmp_path, dataset=dataset, min_sample=1)

    proposal = build_strategy_lab_proposal(experiment=experiment)

    assert experiment["group_experiments"]["combo_yield"]["summary"]["status"] == "ready"
    assert proposal["status"] == "data_gap_only"
    assert proposal["strategy_family"] == "combo_yield"
    assert proposal["recommended_variant"] is None
    assert proposal["dry_run_patch"] == {}
    assert proposal["group_advisory"]["status"] == "ready"
    assert proposal["group_advisory"]["ready_group_count"] == 1
    assert proposal["group_advisory"]["evaluable_group_count"] == 0
    assert {"recommended_variant", "variant_count", "optimization_claim"}.isdisjoint(
        proposal["group_advisory"]
    )
    assert "combo_yield_group_advisory_only" in proposal["limitations"]


def test_strategy_lab_llm_context_redacts_and_preserves_safety_boundary(tmp_path: Path) -> None:
    from src.application.strategy_lab import (
        build_strategy_lab_llm_context,
        build_strategy_lab_proposal,
        run_strategy_lab_experiment,
    )

    dataset = tmp_path / "dataset"
    _write_readiness_dataset(dataset)
    experiment = run_strategy_lab_experiment(repo_root=tmp_path, dataset=dataset, min_sample=1)
    proposal = build_strategy_lab_proposal(experiment=experiment)
    proposal["dry_run_patch"]["webhook_url"] = "DO_NOT_LEAK"
    proposal["impact"]["secret_note"] = "DO_NOT_LEAK"

    context = build_strategy_lab_llm_context(experiment=experiment, proposal=proposal)
    serialized = json.dumps(context, ensure_ascii=False)

    assert context["schema_version"] == "strategy_lab_llm_context.v1"
    assert context["role"] == "strategy_research_assistant"
    assert context["safety"]["online_ai_called"] is False
    assert context["safety"]["runtime_config_write_allowed"] is False
    assert context["safety"]["llm_can_apply_patch"] is False
    assert "modify_runtime_config" in context["forbidden_actions"]
    assert "claim_optimal_parameters" in context["forbidden_actions"]
    assert (
        context["context"]["experiment"]["group_experiments"]["combo_yield"]["schema_version"]
        == "strategy_lab_combo_yield_group_experiment.v1"
    )
    combo_context = context["context"]["experiment"]["group_experiments"]["combo_yield"]
    assert {"variant_count", "optimization_claim"}.isdisjoint(combo_context["summary"])
    assert "best_variant" not in combo_context["scorecard"]
    assert (
        context["context"]["strategy_family_boundaries"]["combo_yield"]["allowed_first_stage_experiment"]
        == "group_level_outcome_evaluator"
    )
    assert context["context"]["strategy_family_boundaries"]["combo_yield"]["single_leg_parameter_patch_allowed"] is False
    assert context["context"]["proposal"]["dry_run_patch"]["webhook_url"] == "[REDACTED]"
    assert context["context"]["proposal"]["impact"]["secret_note"] == "[REDACTED]"
    assert "DO_NOT_LEAK" not in serialized


def test_strategy_lab_scoped_evidence_filters_mark_and_outcome_facts(tmp_path: Path) -> None:
    from src.application.strategy_lab.evidence import load_strategy_lab_evidence

    dataset = tmp_path / "dataset"
    _write_jsonl(
        dataset / "candidate_snapshots.jsonl",
        [
            {
                "contract_symbol": "NVDA260619P00100000",
                "symbol": "NVDA",
                "account": "lx",
                "option_type": "put",
                "status": "accepted",
                "strategy_family": "sell_put",
            },
            {
                "contract_symbol": "0700HK260619P00380000",
                "symbol": "0700.HK",
                "account": "sy",
                "option_type": "put",
                "status": "accepted",
                "strategy_family": "sell_put",
            },
        ],
    )
    _write_jsonl(dataset / "filter_decisions.jsonl", [])
    _write_jsonl(
        dataset / "mark_path_snapshots.jsonl",
        [
            {"contract_symbol": "NVDA260619P00100000", "symbol": "NVDA", "account": "lx", "option_mid": 1.1},
            {"contract_symbol": "0700HK260619P00380000", "symbol": "0700.HK", "account": "sy", "option_mid": 0.8},
        ],
    )
    _write_jsonl(
        dataset / "outcome_facts.jsonl",
        [
            {"contract_symbol": "NVDA260619P00100000", "symbol": "NVDA", "account": "lx", "realized_pnl": 120},
            {"contract_symbol": "0700HK260619P00380000", "symbol": "0700.HK", "account": "sy", "realized_pnl": 80},
        ],
    )

    evidence = load_strategy_lab_evidence(repo_root=tmp_path, dataset=dataset, accounts=["lx"], market="us")

    assert [row["contract_symbol"] for row in evidence["candidate_snapshots"]] == ["NVDA260619P00100000"]
    assert [row["contract_symbol"] for row in evidence["mark_snapshots"]] == ["NVDA260619P00100000"]
    assert [row["contract_symbol"] for row in evidence["outcome_facts"]] == ["NVDA260619P00100000"]


def test_strategy_lab_update_dry_run_wraps_shadow_replay_data_plan(tmp_path: Path) -> None:
    from src.application.strategy_lab import run_strategy_lab_update

    dataset = tmp_path / "output_shared" / "research" / "shadow_replay" / "datasets" / "case-update"
    _write_update_dataset(dataset)

    result = run_strategy_lab_update(repo_root=tmp_path, min_sample=1, latest=True)

    assert result["schema_version"] == "strategy_lab_update.v1"
    assert result["summary"]["status"] == "planned"
    assert result["summary"]["write"] is False
    assert result["selection"]["max_datasets"] == 1
    assert result["strategy_lab"]["data_plan_actions"][0]["action"] == "collect_marks"
    assert result["shadow_replay"]["data_plan_run"]["schema_version"] == "shadow_replay_data_plan_run.v1"
    assert result["shadow_replay"]["data_plan_run"]["summary"]["planned_count"] == 1
    assert result["safety"]["writes_runtime_config"] is False
    assert result["safety"]["writes_trade_state"] is False
    assert result["safety"]["sends_notifications"] is False


def test_strategy_lab_update_build_dataset_dry_run_does_not_write(tmp_path: Path) -> None:
    from src.application.strategy_lab import run_strategy_lab_update

    runs_root = _write_latest_scanned_run(tmp_path / "output_runs")
    dataset_root = tmp_path / "output_shared" / "research" / "shadow_replay" / "datasets"

    result = run_strategy_lab_update(
        repo_root=tmp_path,
        dataset_root=dataset_root,
        runs_root=runs_root,
        dataset_id="from-latest",
        build_dataset=True,
        latest=True,
        max_datasets=0,
        min_sample=1,
    )

    assert result["shadow_replay"]["dataset_build"]["requested"] is True
    assert result["shadow_replay"]["dataset_build"]["executed"] is False
    assert result["shadow_replay"]["dataset_build"]["reason"] == "requires_write"
    assert result["strategy_lab"]["next_action"] == "rerun_with_write_to_build_latest_dataset"
    assert result["summary"]["dataset_built"] is False
    assert not (dataset_root / "from-latest").exists()
    assert result["safety"]["writes_shadow_replay_dataset_build"] is False


def test_strategy_lab_update_latest_build_is_idempotent_by_run_id(tmp_path: Path) -> None:
    from src.application.strategy_lab import run_strategy_lab_update

    runs_root = _write_latest_scanned_run(tmp_path / "output_runs")
    dataset_root = tmp_path / "output_shared" / "research" / "shadow_replay" / "datasets"

    first = run_strategy_lab_update(
        repo_root=tmp_path,
        dataset_root=dataset_root,
        runs_root=runs_root,
        build_dataset=True,
        latest=True,
        write=True,
        max_datasets=0,
        min_sample=1,
    )
    dataset = dataset_root / "run-evidence"
    mark_path = dataset / "mark_path_snapshots.jsonl"
    mark_path.write_text(
        json.dumps({"contract_symbol": "NVDA260619P00100000", "mark_at": "2026-06-03", "option_mid": 1.1}) + "\n",
        encoding="utf-8",
    )

    second = run_strategy_lab_update(
        repo_root=tmp_path,
        dataset_root=dataset_root,
        runs_root=runs_root,
        build_dataset=True,
        latest=True,
        write=True,
        max_datasets=0,
        min_sample=1,
    )

    assert first["summary"]["dataset_built"] is True
    assert first["summary"]["built_dataset_id"] == "run-evidence"
    assert first["shadow_replay"]["dataset_build"]["dataset_id_source"] == "latest_run_id"
    assert second["summary"]["dataset_built"] is False
    assert second["summary"]["dataset_build_reason"] == "dataset_already_exists"
    assert second["shadow_replay"]["dataset_build"]["dataset_id"] == "run-evidence"
    assert second["shadow_replay"]["dataset_build"]["executed"] is False
    assert json.loads(mark_path.read_text(encoding="utf-8"))["option_mid"] == 1.1


def test_strategy_lab_update_builds_close_and_candidate_from_independent_runs(tmp_path: Path) -> None:
    from src.application.strategy_lab import run_strategy_lab_update

    runs_root = tmp_path / "output_runs"
    _write_close_run(runs_root, "20260723T010000Z-close")
    _write_candidate_run(runs_root, "20260723T020000Z-candidate")
    dataset_root = tmp_path / "datasets"

    result = run_strategy_lab_update(
        repo_root=tmp_path,
        dataset_root=dataset_root,
        runs_root=runs_root,
        build_dataset=True,
        include_close_decisions=True,
        latest=True,
        write=True,
        max_datasets=0,
        min_sample=1,
    )

    close_build = result["shadow_replay"]["close_decision_dataset_build"]
    candidate_build = result["shadow_replay"]["dataset_build"]
    assert close_build["executed"] is True
    assert close_build["dataset_id"] == "20260723T010000Z-close"
    assert close_build["source_selection"]["close_row_count"] == 1
    assert candidate_build["executed"] is True
    assert candidate_build["dataset_id"] == "20260723T020000Z-candidate"
    assert result["summary"]["dataset_built"] is True
    assert result["summary"]["built_dataset_id"] == "20260723T020000Z-candidate"
    assert result["summary"]["close_decision_dataset_built"] is True
    assert result["summary"]["built_close_decision_dataset_id"] == "20260723T010000Z-close"
    assert result["safety"]["writes_shadow_replay_dataset_build"] is True
    close_manifest = json.loads(
        (dataset_root / "20260723T010000Z-close" / "manifest.json").read_text(encoding="utf-8")
    )
    assert close_manifest["close_decision_facet"]["episode_count"] == 1


def test_strategy_lab_update_same_run_builds_one_close_aware_dataset(tmp_path: Path) -> None:
    from src.application.strategy_lab import run_strategy_lab_update

    runs_root = tmp_path / "output_runs"
    run_id = "20260723T010000Z-combined"
    _write_close_run(runs_root, run_id, include_candidate=True)
    dataset_root = tmp_path / "datasets"

    result = run_strategy_lab_update(
        repo_root=tmp_path,
        dataset_root=dataset_root,
        runs_root=runs_root,
        build_dataset=True,
        include_close_decisions=True,
        write=True,
        max_datasets=0,
        min_sample=1,
    )

    assert result["summary"]["close_decision_dataset_built"] is True
    assert result["summary"]["dataset_built"] is False
    assert result["summary"]["dataset_build_reason"] == "dataset_already_exists"
    assert (dataset_root / run_id / "close_decision_episodes.jsonl").is_file()
    assert len(list(dataset_root.iterdir())) == 1


def test_strategy_lab_update_skips_empty_close_run_and_keeps_candidate_build(tmp_path: Path) -> None:
    from src.application.strategy_lab import run_strategy_lab_update

    runs_root = tmp_path / "output_runs"
    empty_close = runs_root / "20260723T020000Z-empty" / "accounts" / "lx" / "close_advice.csv"
    empty_close.parent.mkdir(parents=True, exist_ok=True)
    empty_close.write_text("account,position_lot_id\n", encoding="utf-8")
    _write_candidate_run(runs_root, "20260723T030000Z-candidate")
    dataset_root = tmp_path / "datasets"

    result = run_strategy_lab_update(
        repo_root=tmp_path,
        dataset_root=dataset_root,
        runs_root=runs_root,
        build_dataset=True,
        include_close_decisions=True,
        write=True,
        max_datasets=0,
        min_sample=1,
    )

    close_build = result["shadow_replay"]["close_decision_dataset_build"]
    assert close_build["reason"] == "latest_close_decision_run_not_found"
    assert close_build["source_selection"]["skipped_empty_count"] == 1
    assert result["summary"]["dataset_built"] is True


def test_strategy_lab_update_malformed_close_fails_after_independent_candidate_build(tmp_path: Path) -> None:
    from src.application.strategy_lab import run_strategy_lab_update

    runs_root = tmp_path / "output_runs"
    close_run_id = "20260723T010000Z-close"
    candidate_run_id = "20260723T020000Z-candidate"
    _write_close_run(runs_root, close_run_id, include_audit=False)
    _write_candidate_run(runs_root, candidate_run_id)
    dataset_root = tmp_path / "datasets"

    with pytest.raises(ValueError, match="audit timestamp missing"):
        run_strategy_lab_update(
            repo_root=tmp_path,
            dataset_root=dataset_root,
            runs_root=runs_root,
            build_dataset=True,
            include_close_decisions=True,
            write=True,
            max_datasets=0,
            min_sample=1,
        )

    assert (dataset_root / candidate_run_id / "manifest.json").is_file()
    assert not (dataset_root / close_run_id).exists()


def test_strategy_lab_update_same_run_malformed_close_preserves_candidate_evidence(tmp_path: Path) -> None:
    from src.application.strategy_lab import run_strategy_lab_update

    runs_root = tmp_path / "output_runs"
    run_id = "20260723T010000Z-combined"
    _write_close_run(runs_root, run_id, include_audit=False, include_candidate=True)
    dataset_root = tmp_path / "datasets"

    with pytest.raises(ValueError, match="audit timestamp missing"):
        run_strategy_lab_update(
            repo_root=tmp_path,
            dataset_root=dataset_root,
            runs_root=runs_root,
            build_dataset=True,
            include_close_decisions=True,
            write=True,
            max_datasets=0,
            min_sample=1,
        )

    assert (dataset_root / run_id / "manifest.json").is_file()
    assert not (dataset_root / run_id / "close_decision_episodes.jsonl").exists()


def test_strategy_lab_update_close_io_failure_preserves_candidate_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.strategy_lab.update as update_module

    runs_root = tmp_path / "output_runs"
    candidate_run_id = "20260723T020000Z-candidate"
    _write_candidate_run(runs_root, candidate_run_id)
    dataset_root = tmp_path / "datasets"

    def _raise_close_io_error(**_kwargs):
        raise OSError("close source temporarily unreadable")

    monkeypatch.setattr(update_module, "_build_latest_close_decision_dataset", _raise_close_io_error)

    with pytest.raises(OSError, match="temporarily unreadable"):
        update_module.run_strategy_lab_update(
            repo_root=tmp_path,
            dataset_root=dataset_root,
            runs_root=runs_root,
            build_dataset=True,
            include_close_decisions=True,
            write=True,
            max_datasets=0,
            min_sample=1,
        )

    assert (dataset_root / candidate_run_id / "manifest.json").is_file()


def test_strategy_lab_update_reports_candidate_only_close_collision_without_overwrite(tmp_path: Path) -> None:
    from src.application.shadow_replay import build_shadow_replay_dataset
    from src.application.strategy_lab import run_strategy_lab_update

    runs_root = tmp_path / "output_runs"
    run_id = "20260723T010000Z-combined"
    _write_close_run(runs_root, run_id, include_candidate=True)
    dataset_root = tmp_path / "datasets"
    build_shadow_replay_dataset(
        repo_root=tmp_path,
        run_id=run_id,
        runs_root=runs_root,
        dataset_root=dataset_root,
        dataset_id=run_id,
    )
    mark_path = dataset_root / run_id / "mark_path_snapshots.jsonl"
    mark_path.write_text('{"preserved": true}\n', encoding="utf-8")

    result = run_strategy_lab_update(
        repo_root=tmp_path,
        dataset_root=dataset_root,
        runs_root=runs_root,
        build_dataset=True,
        include_close_decisions=True,
        write=True,
        max_datasets=0,
        min_sample=1,
    )

    close_build = result["shadow_replay"]["close_decision_dataset_build"]
    assert close_build["reason"] == "dataset_exists_without_close_decisions"
    assert result["summary"]["close_decision_dataset_built"] is False
    assert json.loads(mark_path.read_text(encoding="utf-8"))["preserved"] is True
    assert not (dataset_root / run_id / "close_decision_episodes.jsonl").exists()


def test_strategy_lab_update_complete_close_dataset_is_idempotent(tmp_path: Path) -> None:
    from src.application.strategy_lab import run_strategy_lab_update

    runs_root = tmp_path / "output_runs"
    run_id = "20260723T010000Z-combined"
    _write_close_run(runs_root, run_id, include_candidate=True)
    dataset_root = tmp_path / "datasets"
    kwargs = {
        "repo_root": tmp_path,
        "dataset_root": dataset_root,
        "runs_root": runs_root,
        "build_dataset": True,
        "include_close_decisions": True,
        "write": True,
        "max_datasets": 0,
        "min_sample": 1,
    }
    run_strategy_lab_update(**kwargs)
    close_marks = dataset_root / run_id / "close_decision_marks.jsonl"
    close_marks.write_text('{"preserved": true}\n', encoding="utf-8")

    second = run_strategy_lab_update(**kwargs)

    close_build = second["shadow_replay"]["close_decision_dataset_build"]
    assert close_build["reason"] == "dataset_already_has_close_decisions"
    assert json.loads(close_marks.read_text(encoding="utf-8"))["preserved"] is True


def test_strategy_lab_update_reports_incomplete_close_dataset_without_overwrite(tmp_path: Path) -> None:
    from src.application.strategy_lab import run_strategy_lab_update

    runs_root = tmp_path / "output_runs"
    run_id = "20260723T010000Z-combined"
    _write_close_run(runs_root, run_id, include_candidate=True)
    dataset_root = tmp_path / "datasets"
    kwargs = {
        "repo_root": tmp_path,
        "dataset_root": dataset_root,
        "runs_root": runs_root,
        "build_dataset": True,
        "include_close_decisions": True,
        "write": True,
        "max_datasets": 0,
        "min_sample": 1,
    }
    run_strategy_lab_update(**kwargs)
    missing_path = dataset_root / run_id / "close_decision_marks.jsonl"
    missing_path.unlink()

    second = run_strategy_lab_update(**kwargs)

    close_build = second["shadow_replay"]["close_decision_dataset_build"]
    assert close_build["reason"] == "dataset_exists_without_complete_close_decisions"
    assert not missing_path.exists()


def test_strategy_lab_update_close_build_dry_run_does_not_write(tmp_path: Path) -> None:
    from src.application.strategy_lab import run_strategy_lab_update

    runs_root = tmp_path / "output_runs"
    _write_close_run(runs_root, "20260723T010000Z-close")
    _write_candidate_run(runs_root, "20260723T020000Z-candidate")
    dataset_root = tmp_path / "datasets"

    result = run_strategy_lab_update(
        repo_root=tmp_path,
        dataset_root=dataset_root,
        runs_root=runs_root,
        build_dataset=True,
        include_close_decisions=True,
        max_datasets=0,
        min_sample=1,
    )

    assert result["summary"]["close_decision_dataset_build_reason"] == "requires_write"
    assert result["shadow_replay"]["dataset_build"]["reason"] == "requires_write"
    assert result["safety"]["writes_shadow_replay_dataset_build"] is False
    assert not dataset_root.exists()


def test_strategy_lab_update_rejects_ambiguous_close_build_arguments(tmp_path: Path) -> None:
    from src.application.strategy_lab import run_strategy_lab_update

    with pytest.raises(ValueError, match="requires build_dataset"):
        run_strategy_lab_update(repo_root=tmp_path, include_close_decisions=True)
    with pytest.raises(ValueError, match="cannot be combined with dataset_id"):
        run_strategy_lab_update(
            repo_root=tmp_path,
            build_dataset=True,
            include_close_decisions=True,
            dataset_id="explicit",
        )


def test_strategy_lab_experiment_supports_run_window_scope(tmp_path: Path) -> None:
    from src.application.strategy_lab import run_strategy_lab_experiment

    runs_root = _write_strategy_lab_window_run(tmp_path)

    result = run_strategy_lab_experiment(
        repo_root=tmp_path,
        runs_root=runs_root,
        start_date="2026-06-02",
        end_date="2026-06-02",
        accounts=["lx"],
        market="us",
        min_sample=1,
    )

    assert result["schema_version"] == "strategy_lab_experiment.v1"
    assert result["dataset_dir"] is None
    assert result["input_scope"]["readiness_scope"]["coverage"]["mode"] == "runs"
    assert result["input_scope"]["readiness_scope"]["coverage"]["selected_scanned_runs"] == 1
    assert result["evaluation"]["coverage"]["strict_backtest_allowed"] is True
    assert result["evaluation"]["filters"]["accounts"] == ["lx"]
    assert result["evaluation"]["filters"]["market"] == "us"
    assert result["scorecard"]["status"] == "not_evaluable"
    assert result["scorecard"]["best_variant"] is None
    assert result["safety"]["runtime_config_write_allowed"] is False


def test_cli_strategy_lab_experiment(capsys, monkeypatch, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    monkeypatch.setattr(cli, "repo_base", lambda: tmp_path)
    dataset = tmp_path / "dataset"
    _write_readiness_dataset(dataset)

    rc = cli.main(
        [
            "research",
            "strategy-lab",
            "experiment",
            "--dataset",
            str(dataset),
            "--min-sample",
            "1",
            "--auto",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["tool_name"] == "research.strategy-lab.experiment"
    assert payload["data"]["schema_version"] == "strategy_lab_experiment.v1"
    assert payload["data"]["summary"]["status"] == "ready_for_scorecard_review"


def test_cli_strategy_lab_proposal_writes_markdown(capsys, monkeypatch, tmp_path: Path) -> None:
    from src.application.strategy_lab import run_strategy_lab_experiment
    import src.interfaces.cli.main as cli

    monkeypatch.setattr(cli, "repo_base", lambda: tmp_path)
    dataset = tmp_path / "dataset"
    _write_readiness_dataset(dataset)
    experiment_path = tmp_path / "experiment.json"
    run_strategy_lab_experiment(repo_root=tmp_path, dataset=dataset, min_sample=1, output=experiment_path)
    markdown_path = tmp_path / "proposal.md"

    rc = cli.main(
        [
            "research",
            "strategy-lab",
            "proposal",
            "--experiment",
            str(experiment_path),
            "--markdown-output",
            str(markdown_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["tool_name"] == "research.strategy-lab.proposal"
    assert payload["data"]["schema_version"] == "strategy_lab_proposal.v1"
    assert markdown_path.exists()
    assert "Runtime config write allowed: False" in markdown_path.read_text(encoding="utf-8")


def test_cli_strategy_lab_llm_context_writes_redacted_json(capsys, monkeypatch, tmp_path: Path) -> None:
    from src.application.strategy_lab import build_strategy_lab_proposal, run_strategy_lab_experiment
    import src.interfaces.cli.main as cli

    monkeypatch.setattr(cli, "repo_base", lambda: tmp_path)
    dataset = tmp_path / "dataset"
    _write_readiness_dataset(dataset)
    experiment_path = tmp_path / "experiment.json"
    proposal_path = tmp_path / "proposal.json"
    context_path = tmp_path / "llm_context.json"
    experiment = run_strategy_lab_experiment(repo_root=tmp_path, dataset=dataset, min_sample=1, output=experiment_path)
    build_strategy_lab_proposal(experiment=experiment, output=proposal_path)

    rc = cli.main(
        [
            "research",
            "strategy-lab",
            "llm-context",
            "--experiment",
            str(experiment_path),
            "--proposal",
            str(proposal_path),
            "--output",
            str(context_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    written = json.loads(context_path.read_text(encoding="utf-8"))

    assert rc == 0
    assert payload["tool_name"] == "research.strategy-lab.llm-context"
    assert payload["data"]["schema_version"] == "strategy_lab_llm_context.v1"
    assert written["schema_version"] == "strategy_lab_llm_context.v1"
    assert written["safety"]["online_ai_called"] is False


def test_cli_strategy_lab_update_latest_dry_run(capsys, monkeypatch, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    monkeypatch.setattr(cli, "repo_base", lambda: tmp_path)
    dataset = tmp_path / "output_shared" / "research" / "shadow_replay" / "datasets" / "case-update"
    _write_update_dataset(dataset)

    rc = cli.main(
        [
            "research",
            "strategy-lab",
            "update",
            "--latest",
            "--min-sample",
            "1",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["tool_name"] == "research.strategy-lab.update"
    assert payload["data"]["schema_version"] == "strategy_lab_update.v1"
    assert payload["data"]["summary"]["planned_count"] == 1
    assert payload["data"]["safety"]["runtime_config_write_allowed"] is False


def test_cli_strategy_lab_update_builds_latest_dataset(capsys, monkeypatch, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    monkeypatch.setattr(cli, "repo_base", lambda: tmp_path)
    runs_root = _write_latest_scanned_run(tmp_path / "output_runs")
    dataset_root = tmp_path / "output_shared" / "research" / "shadow_replay" / "datasets"

    rc = cli.main(
        [
            "research",
            "strategy-lab",
            "update",
            "--latest",
            "--build-dataset",
            "--write",
            "--runs-root",
            str(runs_root),
            "--dataset-root",
            str(dataset_root),
            "--dataset-id",
            "from-latest",
            "--max-datasets",
            "0",
            "--min-sample",
            "1",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    manifest = json.loads((dataset_root / "from-latest" / "manifest.json").read_text(encoding="utf-8"))

    assert rc == 0
    assert payload["tool_name"] == "research.strategy-lab.update"
    assert payload["data"]["summary"]["status"] == "updated"
    assert payload["data"]["summary"]["dataset_built"] is True
    assert payload["data"]["summary"]["built_dataset_id"] == "from-latest"
    assert payload["data"]["shadow_replay"]["dataset_build"]["executed"] is True
    assert payload["data"]["shadow_replay"]["dataset_build"]["source_selection"]["run_id"] == "run-evidence"
    assert payload["data"]["safety"]["writes_shadow_replay_dataset_build"] is True
    assert payload["data"]["safety"]["writes_runtime_config"] is False
    assert manifest["source"]["latest_scanned_run_selection"]["found"] is True
    assert manifest["summary"]["candidate_snapshot_count"] == 2


def test_cli_strategy_lab_experiment_run_window(capsys, monkeypatch, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    monkeypatch.setattr(cli, "repo_base", lambda: tmp_path)
    _write_strategy_lab_window_run(tmp_path)

    rc = cli.main(
        [
            "research",
            "strategy-lab",
            "experiment",
            "--start-date",
            "2026-06-02",
            "--account",
            "lx",
            "--market",
            "us",
            "--min-sample",
            "1",
            "--auto",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["tool_name"] == "research.strategy-lab.experiment"
    assert payload["data"]["dataset_dir"] is None
    assert payload["data"]["input_scope"]["readiness_scope"]["coverage"]["selected_scanned_runs"] == 1
    assert payload["data"]["evaluation"]["candidate_impact"]["allowed"] is True


def test_shadow_replay_capture_and_evaluator_preserve_staggered_combo_horizons(tmp_path: Path) -> None:
    from src.application.shadow_replay import build_shadow_replay_dataset
    from src.application.strategy_lab import run_combo_yield_group_experiment

    run_dir = tmp_path / "output_runs" / "20260717T010000Z-run" / "accounts" / "lx"
    run_dir.mkdir(parents=True)
    (run_dir / "combo_yield_candidates.csv").write_text(
        (
            "symbol,account,structure_mode,candidate_pair_id,put_expiration,put_dte,call_expiration,call_dte,"
            "spot,multiplier,put_contracts,call_contracts,put_contract_symbol,put_strike,put_bid,"
            "call_contract_symbol,call_strike,call_ask,put_net_credit,call_total_cost,combo_net_credit\n"
            "TSLA,lx,staggered_expiry_pair,pair-tsla-1,2026-08-21,35,2026-10-16,91,180,100,1,1,"
            "TSLA260821P00150000,150,6.0,TSLA261016C00220000,220,4.0,600,400,200\n"
        ),
        encoding="utf-8",
    )

    manifest = build_shadow_replay_dataset(
        repo_root=tmp_path,
        run_dir=tmp_path / "output_runs" / "20260717T010000Z-run",
        dataset_id="staggered-case",
    )
    rows = [
        json.loads(line)
        for line in (Path(manifest["dataset_dir"]) / "candidate_snapshots.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    by_role = {row["leg_role"]: row for row in rows}

    assert {row["structure_mode"] for row in rows} == {"staggered_expiry_pair"}
    assert {row["candidate_pair_id"] for row in rows} == {"pair-tsla-1"}
    assert by_role["funding_put"]["expiration"] == "2026-08-21"
    assert by_role["funding_put"]["dte"] == 35
    assert by_role["participation_call"]["expiration"] == "2026-10-16"
    assert by_role["participation_call"]["dte"] == 91

    result = run_combo_yield_group_experiment(candidate_snapshots=rows, min_sample=1)
    group = result["group_universe"]["groups"][0]

    assert group["structure_mode"] == "staggered_expiry_pair"
    assert group["ready_for_group_experiment"] is True
    assert "combo_yield_expiration_mismatch" not in group["blockers"]
    assert "combo_yield_expiration_order_invalid" not in group["blockers"]
    assert group["outcome_evaluation"]["status"] == "not_evaluable"
    assert (
        "combo_yield_multi_horizon_outcome_evidence_insufficient"
        in group["outcome_evaluation"]["blockers"]
    )


def test_combo_yield_group_evaluator_rejects_reversed_staggered_expirations() -> None:
    from src.application.strategy_lab import run_combo_yield_group_experiment

    common = {
        "symbol": "TSLA",
        "account": "lx",
        "status": "accepted",
        "strategy_family": "combo_yield",
        "strategy_group_id": "staggered-reversed",
        "structure_mode": "staggered_expiry_pair",
        "contracts": 1,
        "multiplier": 100,
        "spot": 180,
        "combo_net_credit": 200,
    }
    rows = [
        {
            **common,
            "contract_symbol": "TSLA261016P00150000",
            "option_type": "put",
            "leg_role": "funding_put",
            "side": "short",
            "strike": 150,
            "expiration": "2026-10-16",
            "net_income": 600,
        },
        {
            **common,
            "contract_symbol": "TSLA260821C00220000",
            "option_type": "call",
            "leg_role": "participation_call",
            "side": "long",
            "strike": 220,
            "expiration": "2026-08-21",
            "net_income": -400,
        },
    ]

    result = run_combo_yield_group_experiment(candidate_snapshots=rows, min_sample=1)
    group = result["group_universe"]["groups"][0]

    assert group["ready_for_group_experiment"] is False
    assert "combo_yield_expiration_order_invalid" in group["blockers"]
    assert "combo_yield_expiration_mismatch" not in group["blockers"]
