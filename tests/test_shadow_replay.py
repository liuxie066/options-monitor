from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.candidate_evidence_helpers import seal_opening_candidate_fixture


BASE = Path(__file__).resolve().parents[1]


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    if path.name == "mark_path_snapshots.jsonl":
        rows = [
            {
                **row,
                "point_in_time_status": row.get("point_in_time_status")
                or "verified_fresh_collection",
            }
            for row in rows
        ]
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(row) for row in rows)
    path.write_text((text + "\n") if text else "", encoding="utf-8")


def _seal_dataset(dataset_dir: Path) -> None:
    from src.application.shadow_replay.common import DATASET_FILES, refresh_dataset_manifest

    for name in DATASET_FILES:
        path = dataset_dir / name
        if not path.exists():
            path.write_text("", encoding="utf-8")
    refresh_dataset_manifest(dataset_dir)


def _seal_standard_candidate_run(
    root: Path,
    *,
    run_id: str = "run-1",
    include_rejected: bool = True,
) -> None:
    rejected = (
        [
            {
                "symbol": "AMD",
                "account": "lx",
                "option_type": "put",
                "contract_symbol": "AMD260619P00080000",
                "expiration": "2026-06-19",
                "dte": 30,
                "delta": -0.28,
                "strike": 80,
                "spot": 95,
                "net_income": 90,
                "multiplier": 100,
                "spread_ratio": 0.45,
                "rule": "risk_spread",
            }
        ]
        if include_rejected
        else []
    )
    seal_opening_candidate_fixture(
        root,
        run_id=run_id,
        accepted_rows=[
            {
                "symbol": "NVDA",
                "account": "lx",
                "option_type": "put",
                "contract_symbol": "NVDA260619P00100000",
                "expiration": "2026-06-19",
                "dte": 30,
                "delta": -0.2,
                "strike": 100,
                "spot": 110,
                "net_income": 120,
                "multiplier": 100,
            }
        ],
        rejected_rows=rejected,
    )


def test_shadow_replay_builds_universe_and_analyzes_outcome_incomplete(tmp_path: Path) -> None:
    from src.application.shadow_replay import (
        analyze_shadow_replay_dataset,
        build_shadow_replay_dataset,
        summarize_shadow_replay_readiness,
    )

    account_dir = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    candidate_path = account_dir / "nvda_sell_put_candidates_labeled.csv"
    trace_path = account_dir / "candidate_filter_trace.jsonl"
    mark_path = account_dir / "mark_path_snapshots.jsonl"
    outcome_path = account_dir / "outcome_facts.jsonl"
    candidate_path.write_text(
        (
            "symbol,account,option_type,contract_symbol,expiration,dte,delta,strike,spot,iv_rv_ratio,"
            "strategy_profile,strategy_family,"
            "annualized_net_return_on_cash_basis,net_income,otm_pct,spread_ratio,"
            "single_trade_concentration,multiplier,open_interest,volume\n"
            "NVDA,lx,put,NVDA260619P00100000,2026-06-19,30,-0.2,100,110,1.25,"
            "short_vol,sell_put,"
            "0.12,120,0.09,0.10,0.04,100,500,20\n"
        ),
        encoding="utf-8",
    )
    trace_path.write_text(
        json.dumps(
            {
                "schema_version": "candidate_filter_trace.v1",
                "run_id": "run-1",
                "account": "lx",
                "symbol": "AMD",
                "contract_symbol": "AMD260619P00080000",
                "function": "sell_put",
                "strategy_profile": "short_vol",
                "strategy_family": "sell_put",
                "mode": "put",
                "option_type": "put",
                "expiration": "2026-06-19",
                "strike": 80,
                "spot": 95,
                "dte": 30,
                "delta": -0.28,
                "iv_rv_ratio": 0.9,
                "event_risk_status": "source_unavailable",
                "spread_ratio": 0.45,
                "net_income": 60,
                "multiplier": 100,
                "status": "rejected",
                "stage": "stage3_risk_filter",
                "rule": "spread_too_wide",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    mark_path.write_text(
        "\n".join(
            [
                    json.dumps({"contract_symbol": "NVDA260619P00100000", "mark_at": "2026-05-31", "unrealized_pnl": 12, "point_in_time_status": "verified_fresh_collection"}),
                    json.dumps({"contract_symbol": "AMD260619P00080000", "mark_at": "2026-05-31", "unrealized_pnl": -35, "point_in_time_status": "verified_fresh_collection"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    outcome_path.write_text(
        "\n".join(
            [
                json.dumps({"contract_symbol": "NVDA260619P00100000", "outcome": "expired_worthless", "realized_pnl": 120}),
                json.dumps({"contract_symbol": "AMD260619P00080000", "outcome": "would_close_loss", "realized_pnl": -80}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    seal_opening_candidate_fixture(
        tmp_path,
        run_id="run-1",
        accepted_rows=[
            {
                "symbol": "NVDA",
                "account": "lx",
                "option_type": "put",
                "contract_symbol": "NVDA260619P00100000",
                "expiration": "2026-06-19",
                "dte": 30,
                "delta": -0.2,
                "strike": 100,
                "spot": 110,
                "iv_rv_ratio": 1.25,
                "strategy_profile": "short_vol",
                "strategy_family": "sell_put",
                "annualized_net_return_on_cash_basis": 0.12,
                "net_income": 120,
                "otm_pct": 0.09,
                "spread_ratio": 0.10,
                "single_trade_concentration": 0.04,
                "multiplier": 100,
                "open_interest": 500,
                "volume": 20,
            }
        ],
        rejected_rows=[
            {
                "symbol": "AMD",
                "account": "lx",
                "option_type": "put",
                "contract_symbol": "AMD260619P00080000",
                "expiration": "2026-06-19",
                "strike": 80,
                "spot": 95,
                "dte": 30,
                "delta": -0.28,
                "iv_rv_ratio": 0.9,
                "event_risk_status": "source_unavailable",
                "spread_ratio": 0.45,
                "net_income": 60,
                "multiplier": 100,
                "strategy_profile": "short_vol",
                "rule": "risk_spread",
            }
        ],
    )
    manifest = build_shadow_replay_dataset(repo_root=tmp_path, run_id="run-1", dataset_id="case-1")
    dataset_dir = Path(manifest["dataset_dir"])
    analysis = analyze_shadow_replay_dataset(dataset=dataset_dir, min_sample=2)
    snapshots = _jsonl(dataset_dir / "candidate_snapshots.jsonl")
    readiness = summarize_shadow_replay_readiness(
        candidate_snapshots=snapshots,
        filter_decisions=_jsonl(dataset_dir / "filter_decisions.jsonl"),
        trace_paths=[trace_path],
        mark_paths=[mark_path],
        outcome_paths=[outcome_path],
        source_paths=[dataset_dir / "candidate_snapshots.jsonl"],
        candidate_evidence_coverage=manifest["source"][
            "candidate_evidence_coverage"
        ],
        base=tmp_path,
        min_sample=2,
    )

    assert manifest["schema_version"] == "shadow_replay_dataset.v1"
    assert manifest["summary"]["candidate_snapshot_count"] == 2
    assert manifest["summary"]["rejected_count"] == 1
    assert manifest["summary"]["mark_path_snapshot_count"] == 2
    assert manifest["summary"]["outcome_fact_count"] == 2
    assert {row["status"] for row in snapshots} == {"accepted", "rejected"}
    assert {row["strategy_profile"] for row in snapshots} == {"short_vol"}
    assert next(row for row in snapshots if row["symbol"] == "NVDA")["open_interest"] == 500
    assert next(row for row in snapshots if row["symbol"] == "NVDA")["volume"] == 20
    assert next(row for row in snapshots if row["status"] == "rejected")["event_risk_status"] == "source_unavailable"
    assert analysis["summary"]["status"] == "needs_human_review"
    assert analysis["summary"]["evidence_level"] == "outcome_incomplete"
    assert analysis["outcome_coverage"]["marked_instrument_count"] == 2
    assert analysis["outcome_coverage"]["outcome_instrument_count"] == 2
    assert analysis["path_risk"]["by_status"]["rejected"]["max_adverse_pnl"] == -35
    assert analysis["outcome_stats"]["by_status"]["accepted"]["realized_pnl_total"] == 120
    assert analysis["outcome_stats"]["by_status"]["rejected"]["win_rate"] == 0
    assert analysis["insurance_metrics"]["by_status"]["accepted"]["premium_collected_total"] == 120
    assert analysis["insurance_metrics"]["by_status"]["accepted"]["loss_ratio"] == 0
    assert analysis["insurance_metrics"]["by_status"]["rejected"]["premium_collected_total"] == 60
    assert analysis["insurance_metrics"]["by_status"]["rejected"]["liability_cost_total"] == 140
    assert analysis["insurance_metrics"]["by_status"]["rejected"]["loss_ratio"] == pytest.approx(140 / 60)
    assert analysis["insurance_metrics"]["by_status"]["rejected"]["path_adverse_loss_to_premium"] == pytest.approx(35 / 60)
    assert analysis["insurance_metrics"]["by_bucket"]["dte"]["30-44"]["premium_collected_total"] == 180
    assert analysis["outcome_by_bucket"]["dte"]["30-44"]["realized_pnl_total"] == 40
    assert analysis["outcome_by_bucket"]["dte"]["30-44"]["by_status"]["accepted"]["realized_pnl_total"] == 120
    assert analysis["outcome_by_bucket"]["spread_ratio"]["0.40+"]["by_status"]["rejected"]["loss_count"] == 1
    assert readiness["summary"]["status"] == "needs_human_review"
    assert readiness["evidence_checks"]["survivorship_bias_risk"] == "low"
    assert readiness["insurance_metrics"]["by_status"]["accepted"]["premium_to_capital"] == pytest.approx(120 / 10000)
    assert readiness["outcome_by_bucket"]["dte"]["30-44"]["win_count"] == 1
    assert readiness["review_readiness"]["status"] == analysis["review_readiness"]["status"]
    assert readiness["parameter_advice_gate"]["status"] == analysis["parameter_advice_gate"]["status"]
    assert readiness["decision_quality"]["by_strategy_profile"]["insurance_underwriting"]["good_accept"] == 1
    assert readiness["safety"]["writes_runtime_config"] is False


def test_shadow_replay_evidence_level_requires_complete_closed_lifecycle() -> None:
    from src.application.shadow_replay.analysis import _evidence_level

    candidates = [{"contract_symbol": "NVDA260619P00100000"}]
    decisions = [{"contract_symbol": "NVDA260619P00100000", "status": "accepted"}]
    marks = [
        {
            "contract_symbol": "NVDA260619P00100000",
            "unrealized_pnl": 10,
            "point_in_time_status": "verified_fresh_collection",
        }
    ]
    incomplete = [{"contract_symbol": "NVDA260619P00100000", "outcome": "expired_worthless"}]
    complete = [
        {
            **incomplete[0],
            "lifecycle_quality": "complete_closed",
            "lifecycle_pnl_net": 10,
            "capital_days": 1_000,
            "fee_basis": "actual",
            "fee_missing_components": [],
            "covered_call_allocation_status": "none",
        }
    ]

    assert _evidence_level(candidates, decisions, marks, incomplete) == "outcome_incomplete"
    assert _evidence_level(candidates, decisions, marks, complete) == "closed_replay"


def test_shadow_replay_readiness_flags_final_candidates_only_survivorship_bias(tmp_path: Path) -> None:
    from src.application.shadow_replay import summarize_shadow_replay_readiness

    readiness = summarize_shadow_replay_readiness(
        candidate_snapshots=[
            {
                "schema_version": "shadow_replay_candidate_snapshot.v1",
                "source_kind": "sealed_candidate_snapshot",
                "source_path": "output_runs/run-1/accounts/lx/state/opening_candidate_snapshot.json",
                "symbol": "NVDA",
                "account": "lx",
                "option_type": "put",
                "contract_symbol": "NVDA260619P00100000",
                "dte": 30,
                "delta": -0.2,
                "strike": 100,
                "iv_rv_ratio": 1.25,
                "spread_ratio": 0.10,
                "status": "accepted",
            }
        ],
        filter_decisions=[],
        trace_paths=[],
        base=tmp_path,
        candidate_evidence_coverage={
            "strict_replay_authority": True,
            "reason_code": "all_accounts_manifest_supported",
        },
        min_sample=1,
    )

    assert readiness["summary"]["status"] == "evidence_incomplete"
    assert readiness["summary"]["reason"] == "rejected_universe_missing"
    assert readiness["evidence_checks"]["final_candidates_only"] is True
    assert readiness["evidence_checks"]["survivorship_bias_risk"] == "high"
    assert readiness["recommendations"][0]["writes_runtime_config"] is False


def test_shadow_replay_insurance_metrics_cover_put_and_call_outcomes() -> None:
    from src.application.shadow_replay.analysis import analyze_rows

    analysis = analyze_rows(
        candidate_snapshots=[
            {
                "contract_symbol": "NVDA260619P00100000",
                "option_type": "put",
                "status": "accepted",
                "strike": 100,
                "net_income": 120,
                "multiplier": 100,
                "dte": 30,
                "abs_delta": 0.20,
                "iv_rv_ratio": 1.25,
                "spread_ratio": 0.12,
                "single_trade_concentration": 0.04,
            },
            {
                "contract_symbol": "NVDA260619C00110000",
                "option_type": "call",
                "status": "accepted",
                "strike": 110,
                "spot": 100,
                "net_income": 80,
                "multiplier": 100,
                "dte": 30,
                "abs_delta": 0.22,
                "iv_rv_ratio": 1.30,
                "spread_ratio": 0.10,
                "single_trade_concentration": 0.04,
            },
        ],
        filter_decisions=[{"contract_symbol": "NOPE", "status": "rejected"}],
        mark_snapshots=[
            {"contract_symbol": "NVDA260619P00100000", "unrealized_pnl": -50},
            {"contract_symbol": "NVDA260619C00110000", "unrealized_pnl": -240},
        ],
        outcome_facts=[
            {
                "contract_symbol": "NVDA260619P00100000",
                "outcome": "assigned_at_expiry",
                "realized_pnl": -380,
                "assignment_lifecycle_pnl": 40,
            },
            {
                "contract_symbol": "NVDA260619C00110000",
                "outcome": "called_away_at_expiry",
                "realized_pnl": -220,
                "callaway_lifecycle_pnl": 120,
            },
        ],
        min_sample=2,
    )

    metrics = analysis["insurance_metrics"]
    assert metrics["by_mode"]["put"]["assignment_rate"] == 1
    assert metrics["by_mode"]["put"]["loss_ratio"] == pytest.approx(80 / 120)
    assert metrics["by_mode"]["call"]["called_away_rate"] == 1
    assert metrics["by_mode"]["call"]["capital_at_risk_total"] == 10000
    assert metrics["by_mode"]["call"]["loss_ratio"] == 0
    assert metrics["by_mode_status"]["put"]["accepted"]["assignment_rate"] == 1
    assert metrics["by_mode_status"]["call"]["accepted"]["called_away_rate"] == 1
    assert metrics["by_status"]["accepted"]["loss_ratio"] == pytest.approx(80 / 200)
    assert metrics["by_status"]["accepted"]["realized_pnl_total"] == 160
    assert metrics["by_status"]["accepted"]["pnl_basis_counts"] == {"wheel_lifecycle": 2}
    assert metrics["by_status"]["accepted"]["lifecycle_pnl_missing_count"] == 0
    assert metrics["by_status"]["accepted"]["tail_risk"]["status"] == "not_evaluable"
    assert metrics["by_status"]["accepted"]["path_adverse_loss_to_premium"] == pytest.approx(290 / 200)
    assert metrics["by_bucket"]["abs_delta"]["0.20-0.30"]["exercise_rate"] == 1


def test_shadow_replay_insurance_metrics_report_empirical_tail_risk() -> None:
    from src.application.shadow_replay.analysis import analyze_rows

    pnl_values = [-3000, -2000, -1000] + [100] * 27
    analysis = analyze_rows(
        candidate_snapshots=[
            {
                "contract_symbol": f"TAIL{i:02d}",
                "option_type": "put",
                "status": "accepted",
                "strike": 100,
                "multiplier": 100,
                "net_income": 100,
            }
            for i in range(30)
        ],
        filter_decisions=[],
        mark_snapshots=[],
        outcome_facts=[
            {
                "contract_symbol": f"TAIL{i:02d}",
                "outcome": "closed",
                "realized_pnl": pnl,
            }
            for i, pnl in enumerate(pnl_values)
        ],
        min_sample=30,
    )

    tail = analysis["insurance_metrics"]["by_mode"]["put"]["tail_risk"]
    assert tail["status"] == "evaluable"
    assert tail["observation_count"] == 30
    assert tail["tail_observation_count"] == 3
    assert tail["var_90"] == pytest.approx(-0.1)
    assert tail["cvar_90"] == pytest.approx(-0.2)


def test_shadow_replay_preserves_and_summarizes_wheel_capacity_context(tmp_path: Path) -> None:
    from src.application.shadow_replay import analyze_shadow_replay_dataset, build_shadow_replay_dataset

    account_dir = tmp_path / "output_runs" / "run-wheel" / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    (account_dir / "nvda_sell_put_candidates_labeled.csv").write_text(
        (
            "symbol,option_type,contract_symbol,expiration,strike,multiplier,portfolio_nav_cny,"
            "assignment_notional_cny,cash_required_cny,cash_free_total_cny,"
            "existing_stock_value_cny_symbol,existing_short_put_assignment_cny_symbol,"
            "existing_short_put_assignment_cny_total\n"
            "NVDA,put,NVDA260619P00100000,2026-06-19,100,100,1000000,"
            "10000,10000,50000,20000,10000,30000\n"
        ),
        encoding="utf-8",
    )
    (account_dir / "nvda_sell_put_candidates.csv").write_text(
        (
            "symbol,option_type,contract_symbol,expiration,strike,multiplier\n"
            "NVDA,put,NVDA260619P00090000,2026-06-19,90,100\n"
        ),
        encoding="utf-8",
    )
    (account_dir / "nvda_sell_call_candidates.csv").write_text(
        (
            "symbol,option_type,contract_symbol,expiration,strike,multiplier,"
            "shares_total,shares_can_sell,shares_locked,shares_available_for_cover,covered_contracts_available\n"
            "NVDA,call,NVDA260619C00120000,2026-06-19,120,100,300,300,100,200,2\n"
        ),
        encoding="utf-8",
    )
    seal_opening_candidate_fixture(
        tmp_path,
        run_id="run-wheel",
        accepted_rows=[
            {
                "symbol": "NVDA",
                "option_type": "put",
                "contract_symbol": "NVDA260619P00100000",
                "expiration": "2026-06-19",
                "strike": 100,
                "spot": 110,
                "multiplier": 100,
                "portfolio_nav_cny": 1_000_000,
                "assignment_notional_cny": 10_000,
                "cash_required_cny": 10_000,
                "cash_free_total_cny": 50_000,
                "existing_stock_value_cny_symbol": 20_000,
                "existing_short_put_assignment_cny_symbol": 10_000,
                "existing_short_put_assignment_cny_total": 30_000,
            },
            {
                "symbol": "NVDA",
                "option_type": "call",
                "contract_symbol": "NVDA260619C00120000",
                "expiration": "2026-06-19",
                "strike": 120,
                "spot": 110,
                "multiplier": 100,
                "shares_total": 300,
                "shares_can_sell": 300,
                "shares_locked": 100,
                "shares_available_for_cover": 200,
                "covered_contracts_available": 2,
            },
        ],
    )

    manifest = build_shadow_replay_dataset(repo_root=tmp_path, run_id="run-wheel", dataset_id="wheel")
    dataset_dir = Path(manifest["dataset_dir"])
    analysis = analyze_shadow_replay_dataset(dataset=dataset_dir, min_sample=1)
    snapshots = _jsonl(dataset_dir / "candidate_snapshots.jsonl")

    put = next(
        row
        for row in snapshots
        if row["option_type"] == "put"
    )
    call = next(row for row in snapshots if row["option_type"] == "call")
    assert put["portfolio_nav_cny"] == 1_000_000
    assert put["cash_free_total_cny"] == 50_000
    assert put["existing_short_put_assignment_cny_total"] == 30_000
    assert call["shares_total"] == 300
    assert call["shares_can_sell"] == 300
    assert call["shares_locked"] == 100
    assert call["shares_available_for_cover"] == 200

    risk = analysis["wheel_lifecycle_risk"]
    assert risk["summary"]["status"] == "evaluable"
    assert risk["summary"]["production_gate_applied"] is False
    assert risk["summary"]["scenario_basis"] == "one_candidate_contract_at_a_time"
    group = risk["by_account_symbol"][0]
    assert group["account"] == "lx"
    assert group["symbol"] == "NVDA"
    assert group["status"] == "evaluable"
    assert group["sell_put"]["candidate_scenario_count"] == 1
    assert group["sell_put"]["candidate_assignment_obligation_cny"] == {"min": 10_000, "max": 10_000}
    assert group["sell_put"]["post_assignment_symbol_exposure_cny"] == {"min": 40_000, "max": 40_000}
    assert group["sell_put"]["post_assignment_account_obligation_cny"] == {"min": 40_000, "max": 40_000}
    assert group["sell_put"]["post_assignment_symbol_nav_ratio"] == {"min": 0.04, "max": 0.04}
    assert group["sell_put"]["cash_capacity"]["supported_scenario_count"] == 1
    assert group["covered_call"]["locked_share_ratio"] == pytest.approx(1 / 3)
    assert group["covered_call"]["candidate_called_away_shares"] == {"min": 100, "max": 100}
    assert group["covered_call"]["candidate_called_away_share_ratio"]["min"] == pytest.approx(1 / 3)
    assert group["covered_call"]["post_candidate_locked_share_ratio"]["max"] == pytest.approx(2 / 3)
    assert group["covered_call"]["share_capacity"]["supported_scenario_count"] == 1


def test_shadow_replay_wheel_capacity_is_not_evaluable_without_account_context() -> None:
    from src.application.shadow_replay.analysis import analyze_rows

    analysis = analyze_rows(
        candidate_snapshots=[
            {
                "source_kind": "candidate_csv",
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "contract_symbol": "NVDA260619P00100000",
                "assignment_notional_cny": 10_000,
            },
            {
                "source_kind": "candidate_csv",
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "call",
                "contract_symbol": "NVDA260619C00120000",
                "multiplier": 100,
            },
        ],
        filter_decisions=[],
        mark_snapshots=[],
        outcome_facts=[],
        min_sample=1,
    )

    risk = analysis["wheel_lifecycle_risk"]
    assert risk["summary"]["status"] == "not_evaluable"
    group = risk["by_account_symbol"][0]
    assert group["status"] == "not_evaluable"
    assert "portfolio_nav_cny" in group["sell_put"]["missing_fields"]
    assert "cash_capacity_context" in group["sell_put"]["missing_fields"]
    assert "shares_total" in group["covered_call"]["missing_fields"]
    assert "shares_locked" in group["covered_call"]["missing_fields"]
    assert risk["summary"]["production_gate_applied"] is False


def test_shadow_replay_wheel_capacity_does_not_hide_incomplete_contracts() -> None:
    from src.application.shadow_replay.analysis import analyze_rows

    analysis = analyze_rows(
        candidate_snapshots=[
            {
                "source_kind": "candidate_csv",
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "contract_symbol": "NVDA260619P00100000",
                "source_path": "output_runs/run/accounts/lx/nvda_sell_put_candidates_labeled.csv",
                "portfolio_nav_cny": 1_000_000,
                "assignment_notional_cny": 10_000,
                "cash_free_total_cny": 50_000,
                "existing_stock_value_cny_symbol": 20_000,
                "existing_short_put_assignment_cny_symbol": 10_000,
                "existing_short_put_assignment_cny_total": 30_000,
            },
            {
                "source_kind": "candidate_csv",
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "contract_symbol": "NVDA260619P00090000",
                "source_path": "output_runs/run/accounts/lx/nvda_sell_put_candidates_labeled.csv",
            },
        ],
        filter_decisions=[],
        mark_snapshots=[],
        outcome_facts=[],
        min_sample=1,
    )

    sell_put = analysis["wheel_lifecycle_risk"]["by_account_symbol"][0]["sell_put"]
    assert sell_put["candidate_scenario_count"] == 2
    assert sell_put["status"] == "not_evaluable"
    assert "assignment_notional_cny" in sell_put["missing_fields"]
    assert sell_put["cash_capacity"]["not_evaluable_scenario_count"] == 1


def test_shadow_replay_decision_quality_is_not_pnl_only() -> None:
    from src.application.shadow_replay.analysis import analyze_rows

    analysis = analyze_rows(
        candidate_snapshots=[
            {
                "contract_symbol": "BADACCEPT260619P00100000",
                "symbol": "BADACCEPT",
                "option_type": "put",
                "status": "accepted",
                "strategy_profile": "short_vol",
                "strategy_family": "sell_put",
                "iv_rv_ratio": 0.90,
                "delta": -0.20,
                "spread_ratio": 0.10,
                "single_trade_concentration": 0.02,
                "net_income": 100,
            },
            {
                "contract_symbol": "GOODLOSS260619P00100000",
                "symbol": "GOODLOSS",
                "option_type": "put",
                "status": "accepted",
                "strategy_profile": "short_vol",
                "strategy_family": "sell_put",
                "iv_rv_ratio": 1.30,
                "delta": -0.20,
                "spread_ratio": 0.10,
                "single_trade_concentration": 0.02,
                "net_income": 120,
            },
            {
                "contract_symbol": "EVENTREJ260619P00100000",
                "symbol": "EVENTREJ",
                "option_type": "put",
                "status": "rejected",
                "strategy_profile": "short_vol",
                "strategy_family": "sell_put",
                "iv_rv_ratio": 1.35,
                "delta": -0.20,
                "event_risk_status": "source_unavailable",
                "net_income": 90,
            },
            {
                "contract_symbol": "RETGOOD260619P00100000",
                "symbol": "RETGOOD",
                "option_type": "put",
                "status": "accepted",
                "strategy_profile": "return_first",
                "strategy_family": "sell_put",
                "iv_rv_ratio": 0.80,
                "annualized_net_return_on_cash_basis": 0.12,
                "net_income": 80,
                "dte": 30,
            },
            {
                "contract_symbol": "BADREJ260619P00100000",
                "symbol": "BADREJ",
                "option_type": "put",
                "status": "rejected",
                "strategy_profile": "short_vol",
                "strategy_family": "sell_put",
                "iv_rv_ratio": 1.35,
                "delta": -0.20,
                "spread_ratio": 0.10,
                "single_trade_concentration": 0.02,
                "net_income": 110,
            },
        ],
        filter_decisions=[{"contract_symbol": "BADREJ260619P00100000", "status": "rejected"}],
        mark_snapshots=[
            {"contract_symbol": "BADACCEPT260619P00100000", "unrealized_pnl": 20, "point_in_time_status": "verified_fresh_collection"},
            {"contract_symbol": "GOODLOSS260619P00100000", "unrealized_pnl": -50, "point_in_time_status": "verified_fresh_collection"},
            {"contract_symbol": "EVENTREJ260619P00100000", "unrealized_pnl": 10, "point_in_time_status": "verified_fresh_collection"},
            {"contract_symbol": "RETGOOD260619P00100000", "unrealized_pnl": 15, "point_in_time_status": "verified_fresh_collection"},
            {"contract_symbol": "BADREJ260619P00100000", "unrealized_pnl": 25, "point_in_time_status": "verified_fresh_collection"},
        ],
        outcome_facts=[
            {"contract_symbol": "BADACCEPT260619P00100000", "outcome": "expired_worthless", "realized_pnl": 100},
            {"contract_symbol": "GOODLOSS260619P00100000", "outcome": "assigned_at_expiry", "realized_pnl": -50},
            {"contract_symbol": "EVENTREJ260619P00100000", "outcome": "expired_worthless", "realized_pnl": 90},
            {"contract_symbol": "RETGOOD260619P00100000", "outcome": "expired_worthless", "realized_pnl": 80},
            {"contract_symbol": "BADREJ260619P00100000", "outcome": "expired_worthless", "realized_pnl": 110},
        ],
        min_sample=5,
    )

    samples = {row["symbol"]: row for row in analysis["decision_quality"]["samples"]}
    assert samples["BADACCEPT"]["label"] == "bad_accept"
    assert "iv_rv_ratio_below_minimum" in samples["BADACCEPT"]["reasons"]
    assert samples["GOODLOSS"]["label"] == "inconclusive"
    assert "assigned_at_expiry_lifecycle_pnl_missing" in samples["GOODLOSS"]["reasons"]
    assert samples["EVENTREJ"]["label"] == "good_reject"
    assert samples["RETGOOD"]["label"] == "good_accept"
    assert samples["BADREJ"]["label"] == "bad_reject"
    assert analysis["decision_quality"]["summary"]["label_counts"] == {
        "bad_accept": 1,
        "bad_reject": 1,
        "good_accept": 1,
        "good_reject": 1,
        "inconclusive": 1,
    }
    assert analysis["decision_quality"]["summary"]["parameter_advice_allowed"] is True
    assert analysis["decision_quality"]["summary"]["manual_strategy_review_ready"] is True
    assert analysis["decision_quality"]["summary"]["review_readiness_status"] == "ready_for_manual_strategy_review"
    assert analysis["summary"]["manual_strategy_review_ready"] is True
    assert analysis["summary"]["review_readiness_status"] == "ready_for_manual_strategy_review"
    assert analysis["summary"]["decision_quality_status"] == "ready_for_parameter_review"
    assert analysis["summary"]["bad_decision_count"] == 2
    assert analysis["summary"]["inconclusive_count"] == 1
    assert analysis["summary"]["parameter_advice_allowed"] is True
    assert analysis["review_readiness"]["status"] == "ready_for_manual_strategy_review"
    assert analysis["review_readiness"]["manual_strategy_review_ready"] is True
    assert analysis["review_readiness"]["compatibility"] == {
        "legacy_field": "parameter_advice_gate",
        "legacy_status": "ready_for_parameter_review",
        "legacy_allowed_field": "parameter_advice_allowed",
        "legacy_allowed": True,
    }
    assert analysis["parameter_advice_gate"] == {
        "status": "ready_for_parameter_review",
        "parameter_advice_allowed": True,
        "shadow_dry_run_only": True,
        "sample_count": 5,
        "min_sample": 5,
        "sample_floor_met": True,
        "candidate_universe_missing": False,
        "strategy_profiles": ["insurance_underwriting", "return_first"],
        "has_strategy_profile_breakdown": True,
        "instrument_identity_ready_count": 5,
        "instrument_identity_missing_count": 0,
        "has_instrument_identity": True,
        "strategy_profile_ready_count": 5,
        "strategy_profile_missing_count": 0,
        "trace_only_evidence": False,
        "usable_mark_ready_count": 5,
        "usable_mark_missing_count": 0,
        "outcome_ready_count": 5,
        "outcome_missing_count": 0,
        "bad_accept_count": 1,
        "bad_reject_count": 1,
        "bad_decision_count": 2,
        "has_bad_decision_signal": True,
        "inconclusive_count": 1,
        "inconclusive_rate": 0.2,
        "inconclusive_too_high": False,
        "blockers": [],
    }


def test_shadow_replay_underwriting_observes_delta_and_requires_wheel_lifecycle_pnl() -> None:
    from src.application.shadow_replay.analysis import analyze_rows

    analysis = analyze_rows(
        candidate_snapshots=[
            {
                "contract_symbol": "PUT260619P00100000",
                "symbol": "PUT",
                "option_type": "put",
                "status": "accepted",
                "strategy_profile": "insurance_underwriting",
                "strategy_family": "sell_put",
                "iv_rv_ratio": 1.30,
                "iv_minus_rv": 0.10,
                "delta": -0.95,
                "single_trade_concentration": 0.90,
                "max_single_trade_nav_pct": 0.05,
                "spread_ratio": 0.10,
                "net_income": 100,
            },
            {
                "contract_symbol": "CALL260619C00110000",
                "symbol": "CALL",
                "option_type": "call",
                "status": "accepted",
                "strategy_profile": "insurance_underwriting",
                "strategy_family": "sell_call",
                "iv_rv_ratio": 1.30,
                "iv_minus_rv": 0.10,
                "delta": 0.95,
                "single_trade_concentration": 0.90,
                "max_single_trade_nav_pct": 0.05,
                "spread_ratio": 0.10,
                "net_income": 100,
            },
        ],
        filter_decisions=[],
        mark_snapshots=[
            {"contract_symbol": "PUT260619P00100000", "unrealized_pnl": -900},
            {"contract_symbol": "CALL260619C00110000", "unrealized_pnl": -900},
        ],
        outcome_facts=[
            {
                "contract_symbol": "PUT260619P00100000",
                "outcome": "assigned_at_expiry",
                "realized_pnl": -900,
            },
            {
                "contract_symbol": "CALL260619C00110000",
                "outcome": "called_away_at_expiry",
                "realized_pnl": -900,
                "callaway_lifecycle_pnl": 250,
            },
        ],
        min_sample=2,
    )

    samples = {row["symbol"]: row for row in analysis["decision_quality"]["samples"]}
    assert samples["PUT"]["label"] == "inconclusive"
    assert "assigned_at_expiry_lifecycle_pnl_missing" in samples["PUT"]["reasons"]
    assert samples["CALL"]["label"] == "good_accept"
    assert samples["CALL"]["decision_pnl"] == 250
    assert all("delta" not in reason for reason in samples["CALL"]["reasons"])
    assert all("concentration" not in reason for reason in samples["CALL"]["reasons"])
    put_metrics = analysis["insurance_metrics"]["by_mode"]["put"]
    assert put_metrics["pnl_observation_count"] == 0
    assert put_metrics["lifecycle_pnl_missing_count"] == 1


def test_shadow_replay_decision_quality_requires_sample_floor() -> None:
    from src.application.shadow_replay.analysis import analyze_rows

    analysis = analyze_rows(
        candidate_snapshots=[
            {
                "contract_symbol": "NVDA260619P00100000",
                "symbol": "NVDA",
                "option_type": "put",
                "status": "accepted",
                "strategy_profile": "short_vol",
                "iv_rv_ratio": 1.30,
                "delta": -0.20,
                "net_income": 120,
            }
        ],
        filter_decisions=[],
        mark_snapshots=[{"contract_symbol": "NVDA260619P00100000", "unrealized_pnl": 10}],
        outcome_facts=[{"contract_symbol": "NVDA260619P00100000", "outcome": "expired_worthless", "realized_pnl": 120}],
        min_sample=2,
    )

    quality = analysis["decision_quality"]
    assert quality["summary"]["label_counts"] == {"inconclusive": 1}
    assert quality["summary"]["parameter_advice_allowed"] is False
    assert quality["samples"][0]["reasons"] == ["sample_size_below_min_sample"]
    assert analysis["summary"]["decision_quality_status"] == "not_ready_for_parameter_review"
    assert analysis["summary"]["review_readiness_status"] == "not_ready_for_manual_strategy_review"
    assert analysis["summary"]["manual_strategy_review_ready"] is False
    assert analysis["summary"]["parameter_advice_allowed"] is False
    assert analysis["review_readiness"]["blockers"] == [
        "sample_size_below_min_sample",
        "bad_decision_signal_missing",
        "inconclusive_rate_too_high",
    ]
    assert analysis["parameter_advice_gate"]["blockers"] == [
        "sample_size_below_min_sample",
        "bad_decision_signal_missing",
        "inconclusive_rate_too_high",
    ]


def test_shadow_replay_gate_separates_evidence_gap_blockers() -> None:
    from src.application.shadow_replay.analysis import analyze_rows

    analysis = analyze_rows(
        candidate_snapshots=[
            {
                "symbol": "NOID",
                "status": "accepted",
                "source_kind": "candidate_csv",
                "strategy_profile": "short_vol",
                "iv_rv_ratio": 1.30,
                "delta": -0.20,
                "net_income": 120,
            },
            {
                "contract_symbol": "NOPROFILE260619P00100000",
                "symbol": "NOPROFILE",
                "option_type": "put",
                "status": "accepted",
                "source_kind": "candidate_csv",
                "iv_rv_ratio": 1.30,
                "delta": -0.20,
                "net_income": 120,
            },
            {
                "contract_symbol": "NOOUTCOME260619P00100000",
                "symbol": "NOOUTCOME",
                "option_type": "put",
                "status": "accepted",
                "source_kind": "filter_decision",
                "strategy_profile": "short_vol",
                "iv_rv_ratio": 1.30,
                "delta": -0.20,
                "net_income": 120,
            },
        ],
        filter_decisions=[],
        mark_snapshots=[
            {"contract_symbol": "NOPROFILE260619P00100000", "unrealized_pnl": 10, "point_in_time_status": "verified_fresh_collection"},
        ],
        outcome_facts=[
            {"contract_symbol": "NOPROFILE260619P00100000", "outcome": "expired_worthless", "realized_pnl": 120},
        ],
        min_sample=3,
    )

    gate = analysis["parameter_advice_gate"]
    assert gate["sample_floor_met"] is True
    assert gate["instrument_identity_ready_count"] == 2
    assert gate["instrument_identity_missing_count"] == 1
    assert gate["strategy_profile_ready_count"] == 2
    assert gate["strategy_profile_missing_count"] == 1
    assert gate["usable_mark_ready_count"] == 1
    assert gate["usable_mark_missing_count"] == 1
    assert gate["outcome_ready_count"] == 1
    assert gate["outcome_missing_count"] == 1
    assert gate["trace_only_evidence"] is False
    assert gate["blockers"] == [
        "instrument_identity_missing",
        "strategy_profile_missing",
        "usable_mark_path_missing",
        "outcome_fact_missing",
        "bad_decision_signal_missing",
        "inconclusive_rate_too_high",
    ]
    assert analysis["evidence_checks"]["instrument_identity_missing_count"] == 1
    assert analysis["evidence_checks"]["strategy_profile_missing_count"] == 1
    assert analysis["evidence_checks"]["outcome_missing_count"] == 1


def test_shadow_replay_build_selects_latest_runtime_run_with_evidence(tmp_path: Path) -> None:
    from src.application.shadow_replay import build_shadow_replay_dataset

    runtime_root = tmp_path / "runtime"
    runs_root = runtime_root / "output_runs"
    empty_newer = runs_root / "run-empty" / "accounts" / "lx"
    evidence_older = runs_root / "run-evidence" / "accounts" / "lx"
    empty_newer.mkdir(parents=True)
    evidence_older.mkdir(parents=True)
    (evidence_older / "sell_put_candidates.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,expiration,dte,delta,strike,net_income\n"
            "NVDA,lx,put,NVDA260619P00100000,2026-06-19,30,-0.2,100,120\n"
        ),
        encoding="utf-8",
    )
    (evidence_older / "candidate_filter_trace.jsonl").write_text(
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
    seal_opening_candidate_fixture(
        runtime_root,
        run_id="run-evidence",
        accepted_rows=[
            {
                "symbol": "NVDA",
                "account": "lx",
                "option_type": "put",
                "contract_symbol": "NVDA260619P00100000",
                "expiration": "2026-06-19",
                "dte": 30,
                "delta": -0.2,
                "strike": 100,
                "net_income": 120,
            }
        ],
        rejected_rows=[
            {
                "symbol": "AMD",
                "account": "lx",
                "option_type": "put",
                "contract_symbol": "AMD260619P00080000",
                "expiration": "2026-06-19",
                "strike": 80,
                "rule": "risk_spread",
            }
        ],
    )
    os.utime(runs_root / "run-evidence", (100, 100))
    os.utime(runs_root / "run-empty", (200, 200))

    manifest = build_shadow_replay_dataset(
        repo_root=tmp_path,
        runs_root=runs_root,
        latest_scanned_run=True,
        dataset_root=runtime_root / "output_shared" / "research" / "shadow_replay" / "datasets",
        dataset_id="latest-case",
    )

    assert Path(manifest["dataset_dir"]).parent == runtime_root / "output_shared" / "research" / "shadow_replay" / "datasets"
    assert manifest["source"]["run_id"] == "run-evidence"
    assert manifest["source"]["latest_scanned_run_selection"]["found"] is True
    assert manifest["source"]["latest_scanned_run_selection"]["skipped_without_evidence_count"] == 1
    assert manifest["summary"]["candidate_snapshot_count"] == 2
    assert manifest["summary"]["rejected_count"] == 1


def test_shadow_replay_dataset_status_dashboard_guides_next_actions(tmp_path: Path) -> None:
    from src.application.shadow_replay import shadow_replay_dataset_status

    root = tmp_path / "output_shared" / "research" / "shadow_replay" / "datasets"
    accepted_nvda = {
        "account": "lx",
        "symbol": "NVDA",
        "option_type": "put",
        "contract_symbol": "NVDA260619P00100000",
        "status": "accepted",
        "strike": 100,
    }
    accepted_tsla = {
        "account": "lx",
        "symbol": "TSLA",
        "option_type": "put",
        "contract_symbol": "TSLA260619P00200000",
        "status": "accepted",
        "strike": 200,
    }
    rejected_amd = {
        "account": "lx",
        "symbol": "AMD",
        "option_type": "put",
        "contract_symbol": "AMD260619P00080000",
        "status": "rejected",
        "strike": 80,
    }
    filter_decision = {
        "account": "lx",
        "symbol": "AMD",
        "contract_symbol": "AMD260619P00080000",
        "status": "rejected",
        "rule": "spread_too_wide",
    }
    marks = [
        {"contract_symbol": "NVDA260619P00100000", "mark_at": "2026-05-31T00:00:00Z", "unrealized_pnl": 10},
        {"contract_symbol": "AMD260619P00080000", "mark_at": "2026-06-01T00:00:00Z", "unrealized_pnl": -20},
    ]
    stale_single_mark = [
        {"contract_symbol": "NVDA260619P00100000", "mark_at": "2026-05-30T00:00:00Z", "unrealized_pnl": 10}
    ]
    outcomes = [
        {"contract_symbol": "NVDA260619P00100000", "outcome": "expired_worthless", "realized_pnl": 120},
        {"contract_symbol": "AMD260619P00080000", "outcome": "would_close_loss", "realized_pnl": -40},
    ]

    def dataset(
        name: str,
        *,
        candidates: list[dict],
        decisions: list[dict] | None = None,
        mark_rows: list[dict] | None = None,
        outcome_rows: list[dict] | None = None,
    ) -> None:
        directory = root / name
        _write_jsonl(directory / "candidate_snapshots.jsonl", candidates)
        _write_jsonl(directory / "filter_decisions.jsonl", decisions or [])
        _write_jsonl(directory / "mark_path_snapshots.jsonl", mark_rows or [])
        _write_jsonl(directory / "outcome_facts.jsonl", outcome_rows or [])

    dataset("below-min", candidates=[accepted_nvda])
    dataset("final-only", candidates=[accepted_nvda, accepted_tsla])
    dataset("ready-sampling", candidates=[accepted_nvda, rejected_amd], decisions=[filter_decision])
    dataset(
        "needs-more-samples",
        candidates=[accepted_nvda, rejected_amd],
        decisions=[filter_decision],
        mark_rows=stale_single_mark,
    )
    dataset("ready-settlement", candidates=[accepted_nvda, rejected_amd], decisions=[filter_decision], mark_rows=marks)
    dataset(
        "ready-review",
        candidates=[accepted_nvda, rejected_amd],
        decisions=[filter_decision],
        mark_rows=marks,
        outcome_rows=outcomes,
    )
    before = {path.relative_to(root): path.stat().st_mtime_ns for path in root.rglob("*") if path.is_file()}

    status = shadow_replay_dataset_status(
        repo_root=tmp_path,
        min_sample=2,
        min_mark_points=2,
        mark_stale_hours=24,
        now_utc="2026-06-01T12:00:00Z",
    )
    by_id = {row["dataset_id"]: row for row in status["datasets"]}
    after = {path.relative_to(root): path.stat().st_mtime_ns for path in root.rglob("*") if path.is_file()}

    assert before == after
    assert status["schema_version"] == "shadow_replay_dataset_status.v1"
    assert status["summary"]["dataset_count"] == 6
    assert status["summary"]["by_status"] == {
        "evidence_incomplete": 1,
        "needs_human_review": 1,
        "not_ready": 1,
        "ready_for_sampling": 1,
        "ready_for_settlement": 2,
    }
    assert status["summary"]["sampling_due_count"] == 2
    assert status["summary"]["stale_mark_count"] == 1
    assert status["summary"]["data_plan_actions"] == {
        "collect_marks": 2,
        "settle": 1,
    }
    assert status["summary"]["review_queue_count"] == 1
    assert by_id["below-min"]["reason"] == "candidate_snapshot_count_below_min_sample"
    assert by_id["below-min"]["next_suggested_action"] == "wait"
    assert by_id["final-only"]["status"] == "evidence_incomplete"
    assert by_id["final-only"]["has_rejected_universe"] is False
    assert by_id["ready-sampling"]["status"] == "ready_for_sampling"
    assert by_id["ready-sampling"]["next_suggested_action"] == "collect_marks"
    assert by_id["needs-more-samples"]["status"] == "ready_for_settlement"
    assert by_id["needs-more-samples"]["next_suggested_action"] == "collect_marks"
    assert by_id["needs-more-samples"]["sampling"]["state"] == "needs_more_path_samples"
    assert by_id["needs-more-samples"]["sampling"]["priority"] == "medium"
    assert by_id["needs-more-samples"]["sampling"]["mark_age_hours"] == 60.0
    assert by_id["needs-more-samples"]["sampling"]["is_mark_stale"] is True
    assert "collect-marks" in by_id["needs-more-samples"]["sampling"]["suggested_command"]
    assert by_id["ready-settlement"]["status"] == "ready_for_settlement"
    assert by_id["ready-settlement"]["last_mark_at"] == "2026-06-01T00:00:00Z"
    assert by_id["ready-settlement"]["next_suggested_action"] == "settle"
    assert by_id["ready-review"]["status"] == "needs_human_review"
    assert by_id["ready-review"]["missing_outcome_instrument_count"] == 0
    assert by_id["ready-review"]["next_suggested_action"] == "analyze"
    assert status["data_plan"][0]["priority"] == "high"
    assert status["data_plan"][0]["action"] in {"collect_marks", "settle"}
    assert {row["action"] for row in status["data_plan"]} == {"collect_marks", "settle"}
    assert [row["dataset_id"] for row in status["review_queue"]] == ["ready-review"]
    assert status["review_queue"][0]["action"] == "analyze"
    assert status["review_queue"][0]["suggested_command"].endswith("--min-sample 2")
    assert status["safety"]["writes_runtime_config"] is False
    assert status["safety"]["writes_trade_state"] is False
    assert status["safety"]["sends_notifications"] is False


def test_shadow_replay_incomplete_outcomes_choose_only_recoverable_actions(tmp_path: Path) -> None:
    from src.application.shadow_replay import shadow_replay_dataset_status

    root = tmp_path / "output_shared" / "research" / "shadow_replay" / "datasets"
    settled = {
        "account": "lx",
        "symbol": "NVDA",
        "option_type": "put",
        "contract_symbol": "NVDA260619P00100000",
        "expiration": "2026-06-19",
        "strike": 100,
        "net_income": 120,
        "status": "accepted",
    }
    settled_mark = {
        "contract_symbol": settled["contract_symbol"],
        "mark_at": "2026-06-10T00:00:00Z",
        "unrealized_pnl": 20,
    }
    settled_outcome = {
        "contract_symbol": settled["contract_symbol"],
        "outcome": "expired_worthless",
        "realized_pnl": 120,
    }

    def dataset(name: str, candidate: dict, mark: dict | None) -> None:
        directory = root / name
        _write_jsonl(directory / "candidate_snapshots.jsonl", [settled, candidate])
        _write_jsonl(
            directory / "filter_decisions.jsonl",
            [{"contract_symbol": candidate["contract_symbol"], "status": "rejected", "rule": "test"}],
        )
        _write_jsonl(directory / "mark_path_snapshots.jsonl", [settled_mark] + ([mark] if mark else []))
        _write_jsonl(directory / "outcome_facts.jsonl", [settled_outcome])

    settleable = {
        "account": "lx",
        "symbol": "AMD",
        "option_type": "put",
        "contract_symbol": "AMD260619P00080000",
        "expiration": "2026-06-19",
        "strike": 80,
        "net_income": 90,
        "status": "rejected",
    }
    dataset(
        "settleable",
        settleable,
        {
            "contract_symbol": settleable["contract_symbol"],
            "mark_at": "2026-06-10T00:00:00Z",
            "quote_status": "matched",
            "mark_quality": "usable",
            "option_mid": 0.2,
        },
    )
    needs_mark = {
        "account": "lx",
        "symbol": "CRDO",
        "option_type": "put",
        "contract_symbol": "CRDO260717P00100000",
        "expiration": "2026-07-17",
        "strike": 100,
        "net_income": 80,
        "status": "rejected",
    }
    dataset("needs-mark", needs_mark, None)
    blocked = {
        "account": "lx",
        "symbol": "GOOGL",
        "option_type": "call",
        "contract_symbol": "GOOGL260619C00300000",
        "expiration": "2026-06-19",
        "strike": 300,
        "status": "rejected",
    }
    dataset(
        "blocked",
        blocked,
        {
            "contract_symbol": blocked["contract_symbol"],
            "mark_at": "2026-06-10T00:00:00Z",
            "quote_status": "matched",
            "mark_quality": "usable",
            "option_mid": 1.0,
        },
    )

    status = shadow_replay_dataset_status(
        repo_root=tmp_path,
        min_sample=2,
        now_utc="2026-07-12T00:00:00Z",
    )
    by_id = {row["dataset_id"]: row for row in status["datasets"]}

    assert by_id["settleable"]["next_suggested_action"] == "settle"
    assert by_id["settleable"]["outcome_gaps"]["ready_to_settle_count"] == 1
    assert by_id["needs-mark"]["next_suggested_action"] == "collect_marks"
    assert by_id["needs-mark"]["sampling"]["state"] == "needs_outcome_mark"
    assert by_id["needs-mark"]["outcome_gaps"]["needs_mark_count"] == 1
    assert by_id["blocked"]["next_suggested_action"] == "analyze"
    assert by_id["blocked"]["sampling"]["state"] == "outcome_collection_blocked"
    assert by_id["blocked"]["outcome_gaps"]["blocker_counts"] == {"missing_entry_premium": 1}

    after_expiry = shadow_replay_dataset_status(
        repo_root=tmp_path,
        min_sample=2,
        now_utc="2026-07-18T00:00:00Z",
    )
    expired = {row["dataset_id"]: row for row in after_expiry["datasets"]}["needs-mark"]
    assert expired["next_suggested_action"] == "analyze"
    assert expired["outcome_gaps"]["blocker_counts"] == {"expired_without_usable_mark": 1}


def test_shadow_replay_data_plan_dry_run_is_read_only(tmp_path: Path) -> None:
    from src.application.shadow_replay import run_shadow_replay_data_plan

    dataset_dir = tmp_path / "output_shared" / "research" / "shadow_replay" / "datasets" / "case-plan"
    _write_jsonl(
        dataset_dir / "candidate_snapshots.jsonl",
        [
            {"symbol": "NVDA", "contract_symbol": "NVDA260619P00100000", "option_type": "put", "status": "accepted"},
            {"symbol": "AMD", "contract_symbol": "AMD260619P00080000", "option_type": "put", "status": "rejected"},
        ],
    )
    _write_jsonl(
        dataset_dir / "filter_decisions.jsonl",
        [{"symbol": "AMD", "contract_symbol": "AMD260619P00080000", "status": "rejected"}],
    )
    before = {path.relative_to(tmp_path): path.stat().st_mtime_ns for path in tmp_path.rglob("*") if path.is_file()}

    result = run_shadow_replay_data_plan(
        repo_root=tmp_path,
        min_sample=2,
        min_mark_points=1,
        now_utc="2026-06-01T00:00:00Z",
    )
    after = {path.relative_to(tmp_path): path.stat().st_mtime_ns for path in tmp_path.rglob("*") if path.is_file()}

    assert before == after
    assert result["schema_version"] == "shadow_replay_data_plan_run.v1"
    assert result["summary"]["planned_count"] == 1
    assert result["summary"]["executed_count"] == 0
    assert result["summary"]["receipt_written"] is False
    assert result["actions"][0]["action"] == "collect_marks"
    assert result["actions"][0]["result_status"] == "planned"
    assert result["status_before"]["data_plan"][0]["dataset_integrity"] == {
        "status": "legacy_unverified",
        "reason": "manifest_missing",
        "generation_id": None,
        "revision": None,
    }
    assert result["actions"][0]["dataset_integrity_status"] == "legacy_unverified"
    assert result["actions"][0]["dataset_integrity_reason"] == "manifest_missing"
    assert result["status_after"] is None
    assert result["safety"]["writes_runtime_config"] is False
    assert result["safety"]["writes_trade_state"] is False
    assert result["safety"]["sends_notifications"] is False


def test_shadow_replay_data_plan_defers_remaining_collects_after_opend_rate_limit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.shadow_replay.data_plan as data_plan_module

    plan_rows = [
        {
            "dataset_id": dataset_id,
            "dataset_dir": str(tmp_path / dataset_id),
            "action": "collect_marks",
            "reason": "sampling_due",
            "dataset_integrity": {"status": "verified"},
        }
        for dataset_id in ("first", "second")
    ]
    monkeypatch.setattr(
        data_plan_module,
        "shadow_replay_dataset_status",
        lambda **_kwargs: {
            "dataset_root": str(tmp_path),
            "summary": {"dataset_count": 2},
            "data_plan": plan_rows,
            "datasets": [],
        },
    )
    calls: list[dict] = []

    def _rate_limited_collect(**kwargs):
        calls.append(kwargs)
        return {
            "schema_version": "shadow_replay_mark_collection.v1",
            "summary": {
                "status": "deferred",
                "opend_fetch_error_count": 1,
                "opend_rate_limit_count": 1,
                "opend_non_rate_limit_error_count": 0,
                "opend_rate_limit_circuit_open": True,
            },
            "safety": {"writes_local_dataset": True},
        }

    monkeypatch.setattr(data_plan_module, "collect_shadow_replay_marks", _rate_limited_collect)

    result = data_plan_module.run_shadow_replay_data_plan(
        repo_root=tmp_path,
        source="opend",
        write=True,
        fail_fast_on_opend_rate_limit=True,
        now_utc="2026-08-01T00:00:00Z",
    )

    assert len(calls) == 1
    assert calls[0]["fail_fast_on_opend_rate_limit"] is True
    assert result["summary"]["status"] == "deferred"
    assert result["summary"]["deferred_count"] == 2
    assert result["summary"]["error_count"] == 0
    assert [row["result_status"] for row in result["actions"]] == ["deferred", "deferred"]
    assert result["actions"][0]["reason"] == "opend_rate_limited"
    assert result["actions"][1]["reason"] == "opend_rate_limit_circuit_open"


def test_shadow_replay_data_plan_receipt_preserves_bounded_exception_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import hashlib
    import src.application.shadow_replay.data_plan as data_plan_module

    message = "OpenD timed out at /private/runtime/account-secret"
    plan_rows = [
        {
            "dataset_id": "failed",
            "dataset_dir": str(tmp_path / "failed"),
            "action": "collect_marks",
            "reason": "sampling_due",
            "dataset_integrity": {"status": "verified"},
        }
    ]
    monkeypatch.setattr(
        data_plan_module,
        "shadow_replay_dataset_status",
        lambda **_kwargs: {
            "dataset_root": str(tmp_path),
            "summary": {"dataset_count": 1},
            "data_plan": plan_rows,
            "datasets": [],
        },
    )
    monkeypatch.setattr(
        data_plan_module,
        "collect_shadow_replay_marks",
        lambda **_kwargs: (_ for _ in ()).throw(TimeoutError(message)),
    )

    result = data_plan_module.run_shadow_replay_data_plan(
        repo_root=tmp_path,
        source="opend",
        write=True,
        receipt_dir=tmp_path / "receipts",
        now_utc="2026-08-01T00:00:00Z",
    )

    action = result["actions"][0]
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    receipt_action = receipt["actions"][0]
    assert action["error"] == f"TimeoutError: {message}"
    assert receipt_action["error_type"] == "TimeoutError"
    assert receipt_action["error_message_sha256"] == hashlib.sha256(
        message.encode("utf-8")
    ).hexdigest()
    assert "error" not in receipt_action
    assert "account-secret" not in json.dumps(receipt, ensure_ascii=False)


def test_shadow_replay_data_plan_skips_unverified_without_consuming_limit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.shadow_replay.data_plan as data_plan_module

    plan_rows = [
        {
            "dataset_id": "legacy",
            "dataset_dir": str(tmp_path / "legacy"),
            "action": "collect_marks",
            "reason": "sampling_due",
            "dataset_integrity": {
                "status": "legacy_unverified",
                "reason": "manifest_missing",
            },
        },
        {
            "dataset_id": "verified",
            "dataset_dir": str(tmp_path / "verified"),
            "action": "collect_marks",
            "reason": "sampling_due",
            "dataset_integrity": {"status": "verified"},
        },
        {
            "dataset_id": "overflow",
            "dataset_dir": str(tmp_path / "overflow"),
            "action": "collect_marks",
            "reason": "sampling_due",
            "dataset_integrity": {"status": "verified"},
        },
    ]
    monkeypatch.setattr(
        data_plan_module,
        "shadow_replay_dataset_status",
        lambda **_kwargs: {
            "dataset_root": str(tmp_path),
            "summary": {"dataset_count": 3},
            "data_plan": plan_rows,
            "datasets": [],
        },
    )
    calls: list[dict] = []

    def _collect(**kwargs):
        calls.append(kwargs)
        return {
            "schema_version": "shadow_replay_mark_collection.v1",
            "summary": {
                "status": "success",
                "opend_fetch_error_count": 0,
                "opend_rate_limit_count": 0,
                "opend_non_rate_limit_error_count": 0,
                "opend_rate_limit_circuit_open": False,
            },
            "safety": {"writes_local_dataset": True},
        }

    monkeypatch.setattr(data_plan_module, "collect_shadow_replay_marks", _collect)

    result = data_plan_module.run_shadow_replay_data_plan(
        repo_root=tmp_path,
        source="opend",
        write=True,
        max_datasets=1,
        now_utc="2026-08-01T00:00:00Z",
    )

    assert [Path(call["dataset"]).name for call in calls] == ["verified"]
    assert [row["result_status"] for row in result["actions"]] == ["skipped", "ok", "skipped"]
    assert [row["reason"] for row in result["actions"]] == [
        "dataset_integrity_unverified",
        "executed",
        "max_datasets_reached",
    ]
    assert result["summary"]["status"] == "success"
    assert result["summary"]["executed_count"] == 1
    assert result["summary"]["skipped_count"] == 2
    assert result["summary"]["integrity_skipped_count"] == 1


def test_shadow_replay_data_plan_rejects_review_and_dry_run_receipt_writes(tmp_path: Path) -> None:
    from src.application.shadow_replay import run_shadow_replay_data_plan

    dataset_dir = tmp_path / "output_shared" / "research" / "shadow_replay" / "datasets" / "case-plan"
    _write_jsonl(
        dataset_dir / "candidate_snapshots.jsonl",
        [
            {"symbol": "NVDA", "contract_symbol": "NVDA260619P00100000", "option_type": "put", "status": "accepted"},
            {"symbol": "AMD", "contract_symbol": "AMD260619P00080000", "option_type": "put", "status": "rejected"},
        ],
    )
    _write_jsonl(
        dataset_dir / "filter_decisions.jsonl",
        [{"symbol": "AMD", "contract_symbol": "AMD260619P00080000", "status": "rejected"}],
    )
    receipt_path = tmp_path / "dry-run-receipt.json"

    with pytest.raises(ValueError, match="unsupported.*analyze"):
        run_shadow_replay_data_plan(repo_root=tmp_path, actions=["analyze"])
    with pytest.raises(ValueError, match="require write=True"):
        run_shadow_replay_data_plan(repo_root=tmp_path, receipt_output=receipt_path)

    assert not receipt_path.exists()


def test_shadow_replay_data_plan_collects_local_marks_and_receipt(tmp_path: Path) -> None:
    from src.application.shadow_replay import run_shadow_replay_data_plan

    dataset_dir = tmp_path / "output_shared" / "research" / "shadow_replay" / "datasets" / "case-collect"
    _write_jsonl(
        dataset_dir / "candidate_snapshots.jsonl",
        [
                {
                    "symbol": "NVDA",
                    "contract_symbol": "NVDA260619P00100000",
                    "option_type": "put",
                    "expiration": "2026-06-19",
                    "strike": 100,
                    "net_income": 120,
                    "status": "accepted",
                },
                {
                    "symbol": "AMD",
                    "contract_symbol": "AMD260619P00080000",
                    "option_type": "put",
                    "expiration": "2026-06-19",
                    "strike": 80,
                    "net_income": 90,
                    "status": "rejected",
                },
        ],
    )
    _write_jsonl(
        dataset_dir / "filter_decisions.jsonl",
        [{"symbol": "AMD", "contract_symbol": "AMD260619P00080000", "status": "rejected"}],
    )
    required_parsed = tmp_path / "output_shared" / "required_data" / "parsed"
    required_parsed.mkdir(parents=True)
    (required_parsed / "NVDA_required_data.csv").write_text(
        (
            "symbol,option_type,contract_symbol,expiration,strike,bid,ask,last_price,multiplier\n"
            "NVDA,put,NVDA260619P00100000,2026-06-19,100,0.7,0.9,0.8,100\n"
        ),
        encoding="utf-8",
    )
    (required_parsed / "AMD_required_data.csv").write_text(
        (
            "symbol,option_type,contract_symbol,expiration,strike,bid,ask,last_price,multiplier\n"
            "AMD,put,AMD260619P00080000,2026-06-19,80,1.4,1.8,1.6,100\n"
        ),
        encoding="utf-8",
    )
    _seal_dataset(dataset_dir)

    result = run_shadow_replay_data_plan(
        repo_root=tmp_path,
        min_sample=2,
        min_mark_points=1,
        write=True,
        receipt_dir=tmp_path / "receipts",
        now_utc="2026-06-01T00:00:00Z",
    )

    receipt_path = Path(result["receipt_path"])
    assert result["summary"]["executed_count"] == 1
    assert result["status_before"]["data_plan"][0]["dataset_integrity"]["status"] == "verified"
    assert result["actions"][0]["action"] == "collect_marks"
    assert result["actions"][0]["result_status"] == "ok"
    assert result["actions"][0]["dataset_integrity_status"] == "verified"
    assert result["actions"][0]["operation"]["summary"]["generated_mark_snapshot_count"] == 2
    assert result["status_after"]["datasets"][0]["next_suggested_action"] == "collect_marks"
    assert result["safety"]["persistent_write_targets"] == ["shadow_replay_dataset", "shadow_replay_receipt"]
    assert receipt_path.exists()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema_version"] == "shadow_replay_data_plan_receipt.v2"
    assert "status_before" not in receipt
    assert "status_after" not in receipt
    assert receipt["status_before_sha256"]
    assert receipt["status_after_sha256"]
    assert receipt_path.stat().st_size < 512 * 1024
    assert result["receipt_schema_version"] == receipt["schema_version"]
    assert result["receipt_sha256"]
    assert len(_jsonl(dataset_dir / "mark_path_snapshots.jsonl")) == 2


def test_shadow_replay_data_plan_settles_due_dataset(tmp_path: Path) -> None:
    from src.application.shadow_replay import run_shadow_replay_data_plan

    dataset_dir = tmp_path / "output_shared" / "research" / "shadow_replay" / "datasets" / "case-settle"
    _write_jsonl(
        dataset_dir / "candidate_snapshots.jsonl",
        [
            {"symbol": "NVDA", "contract_symbol": "NVDA260619P00100000", "option_type": "put", "status": "accepted"},
            {"symbol": "AMD", "contract_symbol": "AMD260619P00080000", "option_type": "put", "status": "rejected"},
        ],
    )
    _write_jsonl(
        dataset_dir / "filter_decisions.jsonl",
        [{"symbol": "AMD", "contract_symbol": "AMD260619P00080000", "status": "rejected"}],
    )
    _write_jsonl(
        dataset_dir / "mark_path_snapshots.jsonl",
        [
            {"contract_symbol": "NVDA260619P00100000", "mark_at": "2026-06-01T00:00:00Z", "unrealized_pnl": 10},
            {"contract_symbol": "AMD260619P00080000", "mark_at": "2026-06-01T00:00:00Z", "unrealized_pnl": -20},
        ],
    )
    _seal_dataset(dataset_dir)

    result = run_shadow_replay_data_plan(
        repo_root=tmp_path,
        min_sample=2,
        min_mark_points=1,
        actions=["settle"],
        write=True,
        receipt_output=tmp_path / "settle-receipt.json",
        now_utc="2026-06-01T01:00:00Z",
    )

    assert result["summary"]["executed_count"] == 1
    assert result["actions"][0]["action"] == "settle"
    assert result["actions"][0]["operation"]["summary"]["generated_outcome_fact_count"] == 2
    assert result["status_after"]["datasets"][0]["next_suggested_action"] == "analyze"
    assert (tmp_path / "settle-receipt.json").exists()
    assert len(_jsonl(dataset_dir / "outcome_facts.jsonl")) == 2


def test_shadow_replay_settle_derives_outcomes_from_mark_path(tmp_path: Path) -> None:
    from src.application.shadow_replay import (
        analyze_shadow_replay_dataset,
        build_shadow_replay_dataset,
        settle_shadow_replay_dataset,
    )

    account_dir = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    (account_dir / "sell_put_candidates.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,dte,delta,strike,net_income\n"
            "NVDA,lx,put,NVDA260619P00100000,30,-0.2,100,120\n"
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
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (account_dir / "mark_path_snapshots.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"contract_symbol": "NVDA260619P00100000", "mark_at": "2026-05-31", "unrealized_pnl": 15, "point_in_time_status": "verified_fresh_collection"}),
                json.dumps({"contract_symbol": "AMD260619P00080000", "mark_at": "2026-05-31", "unrealized_pnl": -40, "point_in_time_status": "verified_fresh_collection"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    _seal_standard_candidate_run(tmp_path)
    manifest = build_shadow_replay_dataset(repo_root=tmp_path, run_id="run-1", dataset_id="case-settle")
    dataset_dir = Path(manifest["dataset_dir"])
    before = analyze_shadow_replay_dataset(dataset=dataset_dir, min_sample=2)
    settlement = settle_shadow_replay_dataset(dataset=dataset_dir, write=True)
    after = analyze_shadow_replay_dataset(dataset=dataset_dir, min_sample=2)

    assert before["summary"]["reason"] == "outcome_facts_missing"
    assert settlement["summary"]["generated_outcome_fact_count"] == 2
    assert settlement["summary"]["written"] is True
    assert after["summary"]["status"] == "needs_human_review"
    assert after["outcome_stats"]["by_status"]["rejected"]["realized_pnl_total"] == -40


def test_shadow_replay_settlement_propagates_complete_lifecycle_quality_and_blocks_assignment_transition() -> None:
    from src.application.shadow_replay.settlement import derive_outcome_facts

    candidate = {
        "contract_symbol": "NVDA260619P00100000",
        "symbol": "NVDA",
        "option_type": "put",
        "expiration": "2026-06-19",
        "strike": 100,
        "multiplier": 100,
        "net_income": 120,
    }
    complete = derive_outcome_facts(
        [candidate],
        [
            {
                "contract_symbol": "NVDA260619P00100000",
                "unrealized_pnl": 80,
                "lifecycle_pnl_net": 75,
                "capital_days": 10_000,
                "annualized_capital_efficiency": 2.7375,
                "fee_basis": "mixed",
                "fee_missing_components": [],
                "covered_call_allocation_status": "none",
                "lifecycle_quality": "complete_closed",
                "point_in_time_status": "verified_fresh_collection",
            }
        ],
        existing_outcomes=[],
    )[0]
    transition = derive_outcome_facts(
        [candidate],
        [
                {
                    "contract_symbol": "NVDA260619P00100000",
                    "option_type": "put",
                    "strike": 100,
                    "dte": 0,
                "spot": 90,
                "mark_at": "2026-06-19",
                "point_in_time_status": "verified_fresh_collection",
            }
        ],
        existing_outcomes=[],
    )[0]

    assert complete["lifecycle_pnl_net"] == 75
    assert complete["capital_days"] == 10_000
    assert complete["lifecycle_quality"] == "complete_closed"
    assert complete["production_parameter_eligible"] is True
    assert transition["outcome"] == "assigned_at_expiry"
    assert transition["lifecycle_quality"] == "transition_only"
    assert transition["production_parameter_eligible"] is False


def test_shadow_replay_long_call_uses_positive_entry_cost_from_signed_income() -> None:
    from src.application.shadow_replay.settlement import derive_outcome_result

    pnl, model, quality, outcome = derive_outcome_result(
        {
            "option_type": "call",
            "side": "long",
            "expiration": "2026-06-19",
            "strike": 220,
            "contracts": 1,
            "multiplier": 100,
            "net_income": -400,
            "entry_cost": 400,
        },
        {
            "mark_at": "2026-06-19",
            "dte": 0,
            "spot": 230,
        },
    )

    assert pnl == 600
    assert model == "long_option_expiration_intrinsic_minus_entry_cost"
    assert quality == "derived_from_expiration_spot"
    assert outcome == "expired_in_the_money"


def test_shadow_replay_settle_derives_expiration_outcomes_from_spot_marks(tmp_path: Path) -> None:
    from src.application.shadow_replay import (
        analyze_shadow_replay_dataset,
        build_shadow_replay_dataset,
        settle_shadow_replay_dataset,
    )

    account_dir = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    (account_dir / "sell_put_candidates.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,expiration,dte,delta,strike,net_income,multiplier\n"
            "NVDA,lx,put,NVDA260619P00100000,2026-06-19,30,-0.2,100,120,100\n"
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
                "option_type": "put",
                "contract_symbol": "AMD260619P00080000",
                "expiration": "2026-06-19",
                "strike": 80,
                "net_income": 90,
                "multiplier": 100,
                "status": "rejected",
                "rule": "spread_too_wide",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (account_dir / "mark_path_snapshots.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "contract_symbol": "NVDA260619P00100000",
                        "option_type": "put",
                        "expiration": "2026-06-19",
                        "strike": 100,
                        "spot": 110,
                        "dte": 0,
                        "mark_at": "2026-06-19",
                        "point_in_time_status": "verified_fresh_collection",
                    }
                ),
                json.dumps(
                    {
                        "contract_symbol": "AMD260619P00080000",
                        "option_type": "put",
                        "expiration": "2026-06-19",
                        "strike": 80,
                        "spot": 70,
                        "dte": 0,
                        "mark_at": "2026-06-19",
                        "point_in_time_status": "verified_fresh_collection",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    _seal_standard_candidate_run(tmp_path)
    manifest = build_shadow_replay_dataset(repo_root=tmp_path, run_id="run-1", dataset_id="case-expiry")
    dataset_dir = Path(manifest["dataset_dir"])
    settlement = settle_shadow_replay_dataset(dataset=dataset_dir, write=True)
    analysis = analyze_shadow_replay_dataset(dataset=dataset_dir, min_sample=2)
    outcomes = _jsonl(dataset_dir / "outcome_facts.jsonl")

    assert settlement["summary"]["generated_outcome_fact_count"] == 2
    assert {row["outcome"] for row in outcomes} == {"expired_worthless", "assigned_at_expiry"}
    assert {row["quality"] for row in outcomes} == {"derived_from_expiration_spot"}
    assert analysis["summary"]["status"] == "needs_human_review"
    assert analysis["outcome_stats"]["by_status"]["accepted"]["realized_pnl_total"] == 120
    assert analysis["outcome_stats"]["by_status"]["rejected"]["realized_pnl_total"] == -910


def test_shadow_replay_mark_generates_required_data_marks_and_settles(tmp_path: Path) -> None:
    from src.application.shadow_replay import (
        analyze_shadow_replay_dataset,
        build_shadow_replay_dataset,
        mark_shadow_replay_dataset,
        settle_shadow_replay_dataset,
    )

    account_dir = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    (account_dir / "sell_put_candidates.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,expiration,dte,delta,strike,net_income,multiplier\n"
            "NVDA,lx,put,NVDA260619P00100000,2026-06-19,30,-0.2,100,120,100\n"
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
                "option_type": "put",
                "contract_symbol": "AMD260619P00080000",
                "expiration": "2026-06-19",
                "strike": 80,
                "net_income": 90,
                "status": "rejected",
                "rule": "spread_too_wide",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    required_parsed = tmp_path / "output_shared" / "required_data" / "parsed"
    required_parsed.mkdir(parents=True)
    (required_parsed / "NVDA_required_data.csv").write_text(
        (
            "symbol,option_type,contract_symbol,expiration,strike,bid,ask,last_price,delta,implied_volatility,dte,spot,multiplier\n"
            "NVDA,put,NVDA260619P00100000,2026-06-19,100,0.7,0.9,0.8,-0.2,0.31,30,110,100\n"
        ),
        encoding="utf-8",
    )
    (required_parsed / "AMD_required_data.csv").write_text(
        (
            "symbol,option_type,contract_symbol,expiration,strike,bid,ask,last_price,delta,implied_volatility,dte,spot,multiplier\n"
            "AMD,put,AMD260619P00080000,2026-06-19,80,1.4,1.8,1.6,-0.28,0.36,30,95,100\n"
        ),
        encoding="utf-8",
    )

    _seal_standard_candidate_run(tmp_path)
    manifest = build_shadow_replay_dataset(repo_root=tmp_path, run_id="run-1", dataset_id="case-mark")
    dataset_dir = Path(manifest["dataset_dir"])
    marking = mark_shadow_replay_dataset(
        dataset=dataset_dir,
        required_data_root=tmp_path / "output_shared" / "required_data",
        as_of="2026-05-31T00:00:00Z",
        repo_root=tmp_path,
        write=True,
    )
    settlement = settle_shadow_replay_dataset(dataset=dataset_dir, write=True)
    analysis = analyze_shadow_replay_dataset(dataset=dataset_dir, min_sample=2)
    marks = _jsonl(dataset_dir / "mark_path_snapshots.jsonl")

    assert marking["summary"]["generated_mark_snapshot_count"] == 2
    assert marking["summary"]["usable_mark_snapshot_count"] == 0
    assert marking["summary"]["missing_quote_count"] == 0
    assert {row["matched_by"] for row in marks} == {"contract_symbol"}
    assert marks[0]["quote_status"] == "matched"
    assert marks[0]["quote_flags"] == ["mid_from_bid_ask"]
    assert settlement["summary"]["generated_outcome_fact_count"] == 0
    assert analysis["summary"]["reason"] == "usable_mark_path_snapshots_missing"


def test_shadow_replay_collect_marks_rejects_unverified_before_opend_fetch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application.shadow_replay import collect_shadow_replay_marks
    import src.application.shadow_replay.collection as collection

    dataset = tmp_path / "legacy-dataset"
    _write_jsonl(
        dataset / "candidate_snapshots.jsonl",
        [
            {
                "contract_symbol": "NVDA260619P00100000",
                "symbol": "NVDA",
                "account": "lx",
                "option_type": "put",
                "status": "accepted",
            }
        ],
    )
    calls: list[dict] = []

    def _unexpected_fetch(*_args, **kwargs):
        calls.append(kwargs)
        raise AssertionError("OpenD fetch must not run for an unverified dataset")

    monkeypatch.setattr(collection, "_fetch_required_data_from_opend", _unexpected_fetch)

    with pytest.raises(ValueError, match="dataset manifest missing"):
        collect_shadow_replay_marks(
            dataset=dataset,
            required_data_root=tmp_path / "output_shared" / "required_data",
            source="opend",
            repo_root=tmp_path,
            opend_base_root=tmp_path / "runtime",
            write=True,
        )

    assert calls == []
    assert not (tmp_path / "runtime").exists()
    assert not (tmp_path / "output_shared" / "required_data").exists()


def test_shadow_replay_collect_marks_fetches_opend_before_marking(monkeypatch, tmp_path: Path) -> None:
    from src.application.shadow_replay import build_shadow_replay_dataset, collect_shadow_replay_marks
    import src.application.shadow_replay.collection as collection

    account_dir = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    (account_dir / "sell_put_candidates.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,expiration,dte,delta,strike,net_income,multiplier\n"
            "NVDA,lx,put,NVDA260619P00100000,2026-06-19,30,-0.2,100,120,100\n"
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
                "option_type": "put",
                "contract_symbol": "AMD260619P00080000",
                "expiration": "2026-06-19",
                "strike": 80,
                "net_income": 90,
                "status": "rejected",
                "rule": "spread_too_wide",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    calls = []

    def _fake_execute_required_data_opend(*, base: Path, request):
        calls.append((base, request))
        contract = "NVDA260619P00100000" if request.symbol == "NVDA" else "AMD260619P00080000"
        strike = 100 if request.symbol == "NVDA" else 80
        bid = 0.7 if request.symbol == "NVDA" else 1.4
        ask = 0.9 if request.symbol == "NVDA" else 1.8
        return {
            "symbol": request.symbol,
            "expiration_count": 1,
            "expirations": ["2026-06-19"],
            "rows": [
                {
                    "symbol": request.symbol,
                    "option_type": "put",
                    "contract_symbol": contract,
                    "expiration": "2026-06-19",
                    "strike": strike,
                    "bid": bid,
                    "ask": ask,
                    "last_price": (bid + ask) / 2,
                    "dte": 30,
                    "spot": 110,
                    "multiplier": 100,
                }
            ],
            "meta": {"source": "opend", "status": "ok"},
        }

    monkeypatch.setattr(collection, "execute_required_data_opend", _fake_execute_required_data_opend)

    _seal_standard_candidate_run(tmp_path)
    manifest = build_shadow_replay_dataset(repo_root=tmp_path, run_id="run-1", dataset_id="case-collect")
    dataset_dir = Path(manifest["dataset_dir"])
    result = collect_shadow_replay_marks(
        dataset=dataset_dir,
        required_data_root=tmp_path / "output_shared" / "required_data",
        source="opend",
        repo_root=tmp_path,
        opend_base_root=tmp_path / "runtime",
        opend_fetch_config={
            "option_chain_max_calls": 9,
            "option_chain_window_sec": 30.0,
            "max_wait_sec": 600.0,
        },
        write=True,
    )
    marks = _jsonl(dataset_dir / "mark_path_snapshots.jsonl")
    outcomes = _jsonl(dataset_dir / "outcome_facts.jsonl")

    assert [request.symbol for _, request in calls] == ["AMD", "NVDA"]
    assert {base for base, _ in calls} == {(tmp_path / "runtime").resolve()}
    assert {tuple(request.explicit_expirations or []) for _, request in calls} == {("2026-06-19",)}
    assert {request.option_chain_max_calls for _, request in calls} == {9}
    assert {request.option_chain_window_sec for _, request in calls} == {30.0}
    assert {request.max_wait_sec for _, request in calls} == {600.0}
    assert result["fetch"]["opend_fetch_config"]["option_chain_max_calls"] == 9
    assert result["summary"]["opend_fetch_ok_count"] == 2
    assert result["summary"]["generated_mark_snapshot_count"] == 2
    assert result["summary"]["usable_mark_snapshot_count"] == 2
    assert result["summary"]["settled"] is False
    assert result["summary"]["generated_outcome_fact_count"] == 0
    assert result["safety"]["reads_opend"] is True
    assert result["safety"]["writes_required_data_cache"] is True
    assert result["safety"]["writes_persistent_outputs"] is True
    assert result["safety"]["persistent_write_targets"] == [
        "shadow_replay_dataset",
        "required_data_cache",
        "opend_rate_limit_state",
        "opend_cache",
    ]
    assert {row["quote_status"] for row in marks} == {"matched"}
    assert outcomes == []
    assert (tmp_path / "output_shared" / "required_data" / "parsed" / "NVDA_required_data.csv").exists()


def test_shadow_replay_opend_dry_run_keeps_runtime_root_read_only(monkeypatch, tmp_path: Path) -> None:
    from src.application.shadow_replay import collect_shadow_replay_marks
    import src.application.shadow_replay.collection as collection

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
                "strike": 100,
            }
        ],
    )
    _seal_dataset(dataset)
    observed: dict[str, object] = {}

    def _fake_fetch(_rows, **kwargs):
        observed.update(kwargs)
        return {
            "schema_version": "shadow_replay_required_data_fetch.v1",
            "source": "opend",
            "summary": {
                "candidate_snapshot_count": 1,
                "symbol_count": 1,
                "requested_symbol_count": 0,
                "ok_count": 0,
                "partial_count": 0,
                "error_count": 0,
                "rate_limit_count": 0,
                "non_rate_limit_error_count": 0,
                "rate_limit_circuit_open": False,
                "row_count": 0,
                "skipped_symbol_count": 0,
            },
            "requests": [],
            "skipped_symbols": [],
            "stop_reason": None,
        }

    monkeypatch.setattr(collection, "_fetch_required_data_from_opend", _fake_fetch)
    runtime = tmp_path / "runtime"

    result = collect_shadow_replay_marks(
        dataset=dataset,
        required_data_root=runtime / "output_shared" / "required_data",
        source="opend",
        repo_root=tmp_path,
        opend_base_root=runtime,
        write=False,
    )

    temporary_base = Path(observed["base"])
    assert temporary_base != runtime.resolve()
    assert temporary_base.name.startswith("shadow-replay-opend-base-")
    assert not temporary_base.exists()
    assert not runtime.exists()
    assert result["safety"]["writes_persistent_outputs"] is False


def test_shadow_replay_collect_marks_does_not_reuse_stale_cache_after_partial_opend_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application.shadow_replay import build_shadow_replay_dataset, collect_shadow_replay_marks
    import src.application.shadow_replay.collection as collection

    account_dir = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    (account_dir / "sell_put_candidates.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,expiration,dte,delta,strike,net_income,multiplier\n"
            "NVDA,lx,put,NVDA260619P00100000,2026-06-19,30,-0.2,100,120,100\n"
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
                "option_type": "put",
                "contract_symbol": "AMD260619P00080000",
                "expiration": "2026-06-19",
                "strike": 80,
                "net_income": 90,
                "status": "rejected",
                "rule": "spread_too_wide",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    stale_parsed = tmp_path / "output_shared" / "required_data" / "parsed"
    stale_parsed.mkdir(parents=True)
    (stale_parsed / "AMD_required_data.csv").write_text(
        (
            "symbol,option_type,contract_symbol,expiration,strike,bid,ask,last_price,dte,spot,multiplier\n"
            "AMD,put,AMD260619P00080000,2026-06-19,80,1.4,1.8,1.6,30,95,100\n"
        ),
        encoding="utf-8",
    )

    def _fake_execute_required_data_opend(*, base: Path, request):
        if request.symbol == "AMD":
            raise RuntimeError("OpenD partial fetch failure")
        return {
            "symbol": "NVDA",
            "expiration_count": 1,
            "expirations": ["2026-06-19"],
            "rows": [
                {
                    "symbol": "NVDA",
                    "option_type": "put",
                    "contract_symbol": "NVDA260619P00100000",
                    "expiration": "2026-06-19",
                    "strike": 100,
                    "bid": 0.7,
                    "ask": 0.9,
                    "last_price": 0.8,
                    "dte": 30,
                    "spot": 110,
                    "multiplier": 100,
                }
            ],
            "meta": {"source": "opend", "status": "ok"},
        }

    monkeypatch.setattr(collection, "execute_required_data_opend", _fake_execute_required_data_opend)

    _seal_standard_candidate_run(tmp_path)
    manifest = build_shadow_replay_dataset(
        repo_root=tmp_path,
        run_id="run-1",
        dataset_id="case-partial-collect",
    )
    dataset_dir = Path(manifest["dataset_dir"])
    result = collect_shadow_replay_marks(
        dataset=dataset_dir,
        required_data_root=tmp_path / "output_shared" / "required_data",
        source="opend",
        repo_root=tmp_path,
        write=True,
    )
    marks = {
        row["contract_symbol"]: row
        for row in _jsonl(dataset_dir / "mark_path_snapshots.jsonl")
    }

    assert result["summary"]["status"] == "partial_failed"
    assert result["summary"]["opend_fetch_ok_count"] == 1
    assert result["summary"]["opend_fetch_error_count"] == 1
    assert result["summary"]["usable_mark_snapshot_count"] == 1
    assert marks["NVDA260619P00100000"]["quote_status"] == "matched"
    assert marks["NVDA260619P00100000"]["point_in_time_status"] == "verified_fresh_collection"
    assert marks["AMD260619P00080000"]["quote_status"] == "missing_quote"
    assert marks["AMD260619P00080000"]["point_in_time_status"] == "missing_quote"


def test_shadow_replay_collect_marks_fail_fast_on_opend_rate_limit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application.shadow_replay import collect_shadow_replay_marks
    import src.application.shadow_replay.collection as collection

    dataset_dir = (
        tmp_path
        / "output_shared"
        / "research"
        / "shadow_replay"
        / "datasets"
        / "case-rate-limit"
    )
    _write_jsonl(
        dataset_dir / "candidate_snapshots.jsonl",
        [
            {
                "symbol": symbol,
                "contract_symbol": contract,
                "option_type": "put",
                "expiration": "2026-06-19",
                "strike": strike,
                "status": "accepted",
            }
            for symbol, contract, strike in (
                ("AMD", "AMD260619P00080000", 80),
                ("NVDA", "NVDA260619P00100000", 100),
            )
        ],
    )
    _seal_dataset(dataset_dir)
    calls = []

    def _rate_limited_fetch(*, base: Path, request):
        calls.append(request)
        return {
            "symbol": request.symbol,
            "expiration_count": 0,
            "expirations": [],
            "rows": [],
            "meta": {
                "source": "opend",
                "status": "error",
                "error_code": "RATE_LIMIT",
                "error": "every 30 seconds at most 10 calls",
                "source_outcome": "provider_error",
                "reason_code": "RATE_LIMIT",
            },
        }

    monkeypatch.setattr(collection, "execute_required_data_opend", _rate_limited_fetch)

    result = collect_shadow_replay_marks(
        dataset=dataset_dir,
        required_data_root=tmp_path / "output_shared" / "required_data",
        source="opend",
        repo_root=tmp_path,
        write=True,
        fail_fast_on_opend_rate_limit=True,
    )

    assert [request.symbol for request in calls] == ["AMD"]
    assert calls[0].no_retry is True
    assert result["summary"]["status"] == "deferred"
    assert result["summary"]["opend_rate_limit_count"] == 1
    assert result["summary"]["opend_non_rate_limit_error_count"] == 0
    assert result["fetch"]["summary"]["requested_symbol_count"] == 1
    assert result["fetch"]["summary"]["skipped_symbol_count"] == 1
    assert result["fetch"]["skipped_symbols"] == ["NVDA"]
    assert result["fetch"]["stop_reason"] == "opend_rate_limited"


def test_shadow_replay_collect_marks_opend_preview_does_not_persist(monkeypatch, tmp_path: Path) -> None:
    from src.application.shadow_replay import build_shadow_replay_dataset, collect_shadow_replay_marks
    import src.application.shadow_replay.collection as collection

    account_dir = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    (account_dir / "sell_put_candidates.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,expiration,dte,delta,strike,net_income,multiplier\n"
            "NVDA,lx,put,NVDA260619P00100000,2026-06-19,30,-0.2,100,120,100\n"
        ),
        encoding="utf-8",
    )

    fetch_bases = []

    def _fake_execute_required_data_opend(*, base: Path, request):
        fetch_bases.append(Path(base))
        return {
            "symbol": request.symbol,
            "expiration_count": 1,
            "expirations": ["2026-06-19"],
            "rows": [
                {
                    "symbol": request.symbol,
                    "option_type": "put",
                    "contract_symbol": "NVDA260619P00100000",
                    "expiration": "2026-06-19",
                    "strike": 100,
                    "bid": 0.7,
                    "ask": 0.9,
                    "last_price": 0.8,
                    "dte": 30,
                    "spot": 110,
                    "multiplier": 100,
                }
            ],
            "meta": {"source": "opend", "status": "ok"},
        }

    monkeypatch.setattr(collection, "execute_required_data_opend", _fake_execute_required_data_opend)

    _seal_standard_candidate_run(tmp_path, include_rejected=False)
    manifest = build_shadow_replay_dataset(repo_root=tmp_path, run_id="run-1", dataset_id="case-preview")
    dataset_dir = Path(manifest["dataset_dir"])
    result = collect_shadow_replay_marks(
        dataset=dataset_dir,
        required_data_root=tmp_path / "output_shared" / "required_data",
        source="opend",
        repo_root=tmp_path,
        as_of="2026-05-31T00:00:00Z",
        write=False,
    )

    assert result["summary"]["opend_fetch_attempted"] is True
    assert result["summary"]["opend_fetch_persisted"] is False
    assert result["summary"]["generated_mark_snapshot_count"] == 1
    assert result["safety"]["writes_required_data_cache"] is False
    assert result["safety"]["writes_persistent_outputs"] is False
    assert result["safety"]["persistent_write_targets"] == []
    assert fetch_bases and fetch_bases[0] != tmp_path
    assert not fetch_bases[0].exists()
    assert _jsonl(dataset_dir / "mark_path_snapshots.jsonl") == []
    assert not (tmp_path / "output_shared" / "required_data").exists()


def test_shadow_replay_mark_uses_expiration_spot_when_mid_is_missing(tmp_path: Path) -> None:
    from src.application.shadow_replay import (
        analyze_shadow_replay_dataset,
        build_shadow_replay_dataset,
        mark_shadow_replay_dataset,
        settle_shadow_replay_dataset,
    )

    account_dir = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    (account_dir / "sell_put_candidates.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,expiration,dte,delta,strike,net_income,multiplier\n"
            "NVDA,lx,put,NVDA260619P00100000,2026-06-19,30,-0.2,100,120,100\n"
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
                "option_type": "put",
                "contract_symbol": "AMD260619P00080000",
                "expiration": "2026-06-19",
                "strike": 80,
                "net_income": 90,
                "multiplier": 100,
                "status": "rejected",
                "rule": "spread_too_wide",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    required_parsed = tmp_path / "output_shared" / "required_data" / "parsed"
    required_parsed.mkdir(parents=True)
    (required_parsed / "NVDA_required_data.csv").write_text(
        (
            "symbol,option_type,contract_symbol,expiration,strike,dte,spot,multiplier\n"
            "NVDA,put,NVDA260619P00100000,2026-06-19,100,0,110,100\n"
        ),
        encoding="utf-8",
    )
    (required_parsed / "AMD_required_data.csv").write_text(
        (
            "symbol,option_type,contract_symbol,expiration,strike,dte,spot,multiplier\n"
            "AMD,put,AMD260619P00080000,2026-06-19,80,0,70,100\n"
        ),
        encoding="utf-8",
    )

    _seal_standard_candidate_run(tmp_path)
    manifest = build_shadow_replay_dataset(repo_root=tmp_path, run_id="run-1", dataset_id="case-expiry-mark")
    dataset_dir = Path(manifest["dataset_dir"])
    marking = mark_shadow_replay_dataset(
        dataset=dataset_dir,
        required_data_root=tmp_path / "output_shared" / "required_data",
        as_of="2026-06-19",
        repo_root=tmp_path,
        write=True,
    )
    settlement = settle_shadow_replay_dataset(dataset=dataset_dir, write=True)
    analysis = analyze_shadow_replay_dataset(dataset=dataset_dir, min_sample=2)
    marks = _jsonl(dataset_dir / "mark_path_snapshots.jsonl")

    assert marking["summary"]["usable_mark_snapshot_count"] == 0
    assert {row["mark_quality"] for row in marks} == {"missing_mid"}
    assert {row["point_in_time_status"] for row in marks} == {
        "unverified_operator_as_of"
    }
    assert all("pnl_outcome" not in row for row in marks)
    assert settlement["summary"]["generated_outcome_fact_count"] == 0
    assert analysis["summary"]["reason"] == "usable_mark_path_snapshots_missing"


def test_shadow_replay_mark_missing_quote_is_not_usable_evidence(tmp_path: Path) -> None:
    from src.application.shadow_replay import (
        analyze_shadow_replay_dataset,
        build_shadow_replay_dataset,
        mark_shadow_replay_dataset,
        settle_shadow_replay_dataset,
    )

    account_dir = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    (account_dir / "sell_put_candidates.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,expiration,dte,delta,strike,net_income\n"
            "NVDA,lx,put,NVDA260619P00100000,2026-06-19,30,-0.2,100,120\n"
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
                "expiration": "2026-06-19",
                "strike": 80,
                "status": "rejected",
                "rule": "spread_too_wide",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    required_parsed = tmp_path / "output_shared" / "required_data" / "parsed"
    required_parsed.mkdir(parents=True)
    (required_parsed / "NVDA_required_data.csv").write_text(
        (
            "symbol,option_type,contract_symbol,expiration,strike,bid,ask,last_price,mid\n"
            "NVDA,put,NVDA260619P00100000,2026-06-19,100,0,0,0,0\n"
        ),
        encoding="utf-8",
    )

    _seal_standard_candidate_run(tmp_path)
    manifest = build_shadow_replay_dataset(repo_root=tmp_path, run_id="run-1", dataset_id="case-missing-mark")
    dataset_dir = Path(manifest["dataset_dir"])
    marking = mark_shadow_replay_dataset(
        dataset=dataset_dir,
        required_data_root=tmp_path / "output_shared" / "required_data",
        repo_root=tmp_path,
        write=True,
    )
    settlement = settle_shadow_replay_dataset(dataset=dataset_dir, write=True)
    analysis = analyze_shadow_replay_dataset(dataset=dataset_dir, min_sample=2)
    marks = _jsonl(dataset_dir / "mark_path_snapshots.jsonl")

    assert marking["summary"]["generated_mark_snapshot_count"] == 2
    assert marking["summary"]["usable_mark_snapshot_count"] == 0
    assert marking["summary"]["missing_quote_count"] == 1
    assert {row["quote_status"] for row in marks} == {"matched", "missing_quote"}
    assert {row["mark_quality"] for row in marks} == {"missing_mid", "missing_quote"}
    assert settlement["summary"]["generated_outcome_fact_count"] == 0
    assert analysis["summary"]["status"] == "not_ready"
    assert analysis["summary"]["reason"] == "usable_mark_path_snapshots_missing"
    assert analysis["outcome_coverage"]["usable_marked_instrument_count"] == 0


def test_shadow_replay_pipeline_stays_split_by_stage() -> None:
    module_dir = BASE / "src" / "application" / "shadow_replay"
    assert {
        "capture.py",
        "marking.py",
        "settlement.py",
        "analysis.py",
        "readiness.py",
    }.issubset({path.name for path in module_dir.glob("*.py")})

    facade = (module_dir / "evidence.py").read_text(encoding="utf-8")
    status = (module_dir / "status.py").read_text(encoding="utf-8")
    assert len(facade.splitlines()) <= 80
    assert "CandidateScoreWeights" not in facade
    assert "read_candidate_filter_trace" not in facade
    assert "load_runtime_symbol_aliases" not in facade
    assert "tick_cron" not in status
    assert "multi_account_tick" not in status
    assert "notify_symbols" not in status
    assert "trade_events" not in status
    assert "strategy_lab" not in status
    assert "strategy-lab" not in status


def test_shadow_replay_marks_fail_closed_without_verified_collection_receipt() -> None:
    from src.application.shadow_replay.settlement import is_usable_mark

    economic_mark = {
        "contract_symbol": "NVDA260619P00100000",
        "unrealized_pnl": 10,
    }

    assert is_usable_mark(economic_mark) is False
    assert is_usable_mark(
        {
            **economic_mark,
            "point_in_time_status": "unverified_operator_as_of",
        }
    ) is False
    assert is_usable_mark(
        {
            **economic_mark,
            "point_in_time_status": "verified_fresh_collection",
        }
    ) is True


def test_shadow_replay_decision_evidence_is_account_scoped_and_terminal_is_monotonic() -> None:
    from src.application.shadow_replay.settlement import derive_outcome_facts

    candidate = {
        "run_id": "run-1",
        "account": "lx",
        "contract_symbol": "NVDA260619P00100000",
        "symbol": "NVDA",
        "option_type": "put",
        "expiration": "2026-06-19",
        "strike": 100,
        "multiplier": 100,
        "net_income": 120,
        "status": "accepted",
    }
    cross_account_mark = {
        "run_id": "run-1",
        "account": "sy",
        "contract_symbol": "NVDA260619P00100000",
        "mark_at": "2026-06-01T00:00:00Z",
        "unrealized_pnl": 10,
        "point_in_time_status": "verified_fresh_collection",
    }
    assert derive_outcome_facts(
        [candidate],
        [cross_account_mark],
        existing_outcomes=[],
    ) == []

    provisional_mark = {
        **cross_account_mark,
        "account": "lx",
        "mark_at": "2026-06-01T00:00:00Z",
    }
    provisional = derive_outcome_facts(
        [candidate],
        [provisional_mark],
        existing_outcomes=[],
    )[0]
    assert provisional["outcome"] == "counterfactual_mark_to_market"
    assert provisional["revision"] == 1

    expiry_mark = {
        **provisional_mark,
        "mark_at": "2026-06-19T08:00:00Z",
        "spot": 110,
        "dte": 0,
    }
    upgraded = derive_outcome_facts(
        [candidate],
        [provisional_mark, expiry_mark],
        existing_outcomes=[provisional],
    )[0]
    assert upgraded["outcome"] == "expired_worthless"
    assert upgraded["revision"] == 2
    assert upgraded["supersedes"]["outcome"] == "counterfactual_mark_to_market"


def test_shadow_replay_group_occurrence_ignores_leg_status_but_scopes_account() -> None:
    from src.application.shadow_replay.common import group_occurrence_key

    accepted = {
        "run_id": "run-1",
        "account": "lx",
        "strategy_group_id": "combo-1",
        "status": "accepted",
    }
    rejected = {**accepted, "status": "rejected"}
    other_account = {**accepted, "account": "sy"}

    assert group_occurrence_key(accepted) == group_occurrence_key(rejected)
    assert group_occurrence_key(accepted) != group_occurrence_key(other_account)


def test_shadow_replay_integrity_and_output_paths_fail_closed(tmp_path: Path) -> None:
    from src.application.shadow_replay.common import (
        DATASET_FILES,
        read_jsonl,
        refresh_dataset_manifest,
        resolve_output_path,
    )

    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text('{"ok": true}\nnot-json\n', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSONL"):
        read_jsonl(malformed)

    dataset = tmp_path / "dataset"
    dataset.mkdir()
    for name in DATASET_FILES:
        (dataset / name).write_text("", encoding="utf-8")
    (dataset / "candidate_snapshots.jsonl").write_text(
        '{"schema_version":"shadow_replay_candidate_snapshot.v0"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="dataset schema mismatch"):
        refresh_dataset_manifest(dataset)

    for protected in (
        tmp_path / "config.us.json",
        tmp_path / "state" / "receipt.json",
        tmp_path / "ledger.sqlite3",
        tmp_path / "research-recorder.service",
    ):
        with pytest.raises(ValueError, match="protected"):
            resolve_output_path(protected)


def test_shadow_replay_build_refuses_existing_dataset_without_erasing_evidence(
    tmp_path: Path,
) -> None:
    from src.application.shadow_replay import build_shadow_replay_dataset
    from src.application.shadow_replay.common import refresh_dataset_manifest

    account_dir = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    (account_dir / "sell_put_candidates.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,expiration,strike\n"
            "NVDA,lx,put,NVDA260619P00100000,2026-06-19,100\n"
        ),
        encoding="utf-8",
    )
    _seal_standard_candidate_run(tmp_path, include_rejected=False)
    first = build_shadow_replay_dataset(
        repo_root=tmp_path,
        run_id="run-1",
        dataset_id="stable-dataset",
    )
    dataset = Path(first["dataset_dir"])
    accumulated = {
        "schema_version": "shadow_replay_mark_path_snapshot.v1",
        "contract_symbol": "NVDA260619P00100000",
        "point_in_time_status": "verified_fresh_collection",
        "unrealized_pnl": 10,
    }
    _write_jsonl(dataset / "mark_path_snapshots.jsonl", [accumulated])
    refresh_dataset_manifest(dataset)

    with pytest.raises(ValueError, match="already exists"):
        build_shadow_replay_dataset(
            repo_root=tmp_path,
            run_id="run-1",
            dataset_id="stable-dataset",
        )

    assert _jsonl(dataset / "mark_path_snapshots.jsonl") == [accumulated]


def test_shadow_replay_data_plan_tracks_close_facet_independently(tmp_path: Path) -> None:
    from src.application.shadow_replay import shadow_replay_dataset_status

    dataset = (
        tmp_path
        / "output_shared"
        / "research"
        / "shadow_replay"
        / "datasets"
        / "close-only"
    )
    _write_jsonl(dataset / "candidate_snapshots.jsonl", [])
    _write_jsonl(dataset / "filter_decisions.jsonl", [])
    _write_jsonl(dataset / "rank_snapshots.jsonl", [])
    _write_jsonl(dataset / "mark_path_snapshots.jsonl", [])
    _write_jsonl(dataset / "outcome_facts.jsonl", [])
    _write_jsonl(
        dataset / "close_decision_episodes.jsonl",
        [
            {
                "schema_version": "shadow_replay_close_episode.v1",
                "episode_id": "episode-1",
                "account": "lx",
                "position_lot_id": "lot-1",
                "observed_at_utc": "2026-07-23T14:00:00Z",
            }
        ],
    )
    _write_jsonl(dataset / "close_decision_marks.jsonl", [])
    _write_jsonl(dataset / "close_decision_outcomes.jsonl", [])
    _seal_dataset(dataset)

    status = shadow_replay_dataset_status(
        repo_root=tmp_path,
        min_sample=1,
        now_utc="2026-07-24T14:00:00Z",
    )

    assert len(status["data_plan"]) == 1
    row = status["data_plan"][0]
    assert row["facet"] == "close"
    assert row["facets"] == ["close"]
    assert row["action"] == "collect_marks"
    assert row["state"] == "collect_fresh_opend_marks"
