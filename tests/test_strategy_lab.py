from __future__ import annotations

import json
from pathlib import Path


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
            {"contract_symbol": "TSLA260619P00150000", "mark_at": "2026-06-03", "option_mid": 2.0},
            {"contract_symbol": "TSLA260619C00220000", "mark_at": "2026-06-03", "option_mid": 1.5},
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
    run_account = runs_root / "run-evidence" / "accounts" / "lx"
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
    return runs_root


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


def test_shadow_replay_capture_preserves_combo_yield_group_fields(tmp_path: Path) -> None:
    from src.application.shadow_replay import build_shadow_replay_dataset

    run_dir = tmp_path / "output_runs" / "20260602T010000Z-run" / "accounts" / "lx"
    run_dir.mkdir(parents=True)
    (run_dir / "combo_yield_candidates.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,expiration,strike,status,"
            "strategy_group_id,leg_role\n"
            "TSLA,lx,put,TSLA260619P00150000,2026-06-19,150,accepted,combo-1,funding_put\n"
            "TSLA,lx,call,TSLA260619C00220000,2026-06-19,220,accepted,combo-1,participation_call\n"
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

    assert {row["strategy_group_id"] for row in rows} == {"combo-1"}
    assert {row["leg_role"] for row in rows} == {"funding_put", "participation_call"}


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
    combo = next(item for item in result["domain_hypotheses"] if item["strategy_family"] == "combo_yield")
    assert combo["status"] == "group_experiment_delegated"
    assert combo["adapter"]["hypothesis_enabled"] is False
    assert "group_optimizer_not_implemented" not in combo["blockers"]
    assert "combo_yield_group_optimizer_runs_in_strategy_lab_experiment" in combo["limitations"]


def test_combo_yield_group_optimizer_scores_observed_group_universe(tmp_path: Path) -> None:
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
    assert result["summary"]["optimization_claim"] == "observed_group_universe_only"
    assert result["summary"]["production_recommendation_allowed"] is False
    assert result["summary"]["ready_group_count"] == 1
    assert result["summary"]["variant_count"] == 5
    assert result["scorecard"]["status"] == "ready"
    assert result["scorecard"]["best_variant"]["strategy_family"] == "combo_yield"
    assert result["variants"][0]["accepted_group_count"] == 1
    assert result["group_universe"]["groups"][0]["metrics"]["net_premium"] == 200
    assert result["safety"]["runtime_config_write_allowed"] is False


def test_strategy_lab_experiment_runs_candidate_impact_scorecard(tmp_path: Path) -> None:
    from src.application.strategy_lab import run_strategy_lab_experiment

    dataset = tmp_path / "dataset"
    _write_readiness_dataset(dataset)

    result = run_strategy_lab_experiment(repo_root=tmp_path, dataset=dataset, min_sample=1)

    assert result["schema_version"] == "strategy_lab_experiment.v1"
    assert result["summary"]["status"] == "ready_for_scorecard_review"
    assert result["summary"]["production_recommendation_allowed"] is False
    assert result["summary"]["combo_yield_group_optimizer_status"] == "ready"
    assert result["summary"]["combo_yield_group_variant_count"] == 5
    assert result["summary"]["combo_yield_group_experiment_allowed"] is True
    assert result["evaluation"]["schema_version"] == "shadow_replay_candidate_impact.v1"
    assert result["group_experiments"]["combo_yield"]["schema_version"] == "strategy_lab_combo_yield_group_experiment.v1"
    assert result["group_experiments"]["combo_yield"]["scorecard"]["status"] == "ready"
    assert result["scorecard"]["best_variant"]["variant"]
    assert "combo_yield_group_optimizer_not_implemented" not in result["scorecard"]["limitations"]
    assert "combo_yield_group_experiment_reported_separately" in result["scorecard"]["limitations"]
    assert result["safety"]["writes_runtime_config"] is False


def test_strategy_lab_proposal_builds_advisory_dry_run_patch(tmp_path: Path) -> None:
    from src.application.strategy_lab import build_strategy_lab_proposal, run_strategy_lab_experiment

    dataset = tmp_path / "dataset"
    _write_readiness_dataset(dataset)
    experiment = run_strategy_lab_experiment(repo_root=tmp_path, dataset=dataset, min_sample=1)

    proposal = build_strategy_lab_proposal(experiment=experiment)

    assert proposal["schema_version"] == "strategy_lab_proposal.v1"
    assert proposal["status"] in {"shadow_rollout_candidate", "no_change_recommended"}
    assert proposal["runtime_config_write_allowed"] is False
    assert proposal["production_recommendation_allowed"] is False
    assert proposal["dry_run_patch"]
    assert all(key.startswith(("sell_put.", "covered_call.")) for key in proposal["dry_run_patch"])
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
    assert proposal["recommended_variant"]
    assert proposal["dry_run_patch"] == {}
    assert proposal["group_advisory"]["status"] == "ready"
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
    assert (
        context["context"]["strategy_family_boundaries"]["combo_yield"]["allowed_first_stage_experiment"]
        == "group_level_observed_universe_optimizer"
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
    assert result["scorecard"]["status"] == "ready"
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
