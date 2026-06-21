from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _params() -> dict:
    return {
        "baseline": "production",
        "variants": [
            {
                "name": "iv_rv_1_10",
                "insurance_underwriting": {
                    "min_iv_rv_ratio": 1.10,
                    "min_iv_minus_rv": 0.05,
                    "min_abs_delta": 0.15,
                    "max_abs_delta": 0.30,
                    "min_dte": 20,
                    "max_dte": 60,
                },
            }
        ],
    }


def _write_cli_candidate_run(root: Path) -> None:
    account_dir = root / "output_runs" / "20260602T010000Z-run" / "accounts" / "lx"
    account_dir.mkdir(parents=True, exist_ok=True)
    (account_dir / "sell_put_candidates.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,expiration,dte,delta,strike,"
            "strategy_profile,iv_rv_ratio,iv_minus_rv,spread_ratio,single_trade_concentration,net_income\n"
            "NVDA,lx,put,NVDA260619P00100000,2026-06-19,30,-0.2,100,"
            "short_vol,1.25,0.08,0.10,0.02,120\n"
        ),
        encoding="utf-8",
    )


def test_parameter_set_rejects_non_tunable_safety_floor() -> None:
    from src.application.shadow_replay.parameter_sets import parse_parameter_set

    with pytest.raises(ValueError, match="non-tunable"):
        parse_parameter_set(
            {
                "variants": [
                    {
                        "name": "unsafe",
                        "insurance_underwriting": {
                            "min_iv_rv_ratio": 1.10,
                            "max_spread_ratio": 0.50,
                        },
                    }
                ]
            }
        )


def test_candidate_impact_compares_dataset_variants_and_preserves_safety_floors(tmp_path: Path) -> None:
    from src.application.shadow_replay import run_shadow_replay_candidate_impact

    dataset = tmp_path / "output_shared" / "research" / "shadow_replay" / "datasets" / "case"
    _write_jsonl(
        dataset / "candidate_snapshots.jsonl",
        [
            {
                "contract_symbol": "NVDA260619P00100000",
                "symbol": "NVDA",
                "account": "lx",
                "option_type": "put",
                "status": "accepted",
                "strategy_profile": "short_vol",
                "iv_rv_ratio": 1.25,
                "iv_minus_rv": 0.08,
                "delta": -0.20,
                "dte": 30,
                "spread_ratio": 0.10,
                "single_trade_concentration": 0.02,
                "net_income": 120,
            },
            {
                "contract_symbol": "AMD260619P00080000",
                "symbol": "AMD",
                "account": "lx",
                "option_type": "put",
                "status": "rejected",
                "strategy_profile": "short_vol",
                "filter_rule": "iv_rv_ratio_below_minimum",
                "iv_rv_ratio": 1.12,
                "iv_minus_rv": 0.06,
                "delta": -0.18,
                "dte": 30,
                "spread_ratio": 0.10,
                "single_trade_concentration": 0.02,
                "net_income": 90,
            },
            {
                "contract_symbol": "TSLA260619P00150000",
                "symbol": "TSLA",
                "account": "lx",
                "option_type": "put",
                "status": "accepted",
                "strategy_profile": "short_vol",
                "iv_rv_ratio": 1.05,
                "iv_minus_rv": 0.04,
                "delta": -0.20,
                "dte": 30,
                "spread_ratio": 0.10,
                "single_trade_concentration": 0.02,
                "net_income": 100,
            },
            {
                "contract_symbol": "WIDE260619P00100000",
                "symbol": "WIDE",
                "account": "lx",
                "option_type": "put",
                "status": "rejected",
                "strategy_profile": "short_vol",
                "filter_rule": "spread_too_wide",
                "iv_rv_ratio": 1.40,
                "iv_minus_rv": 0.10,
                "delta": -0.20,
                "dte": 30,
                "spread_ratio": 0.45,
                "single_trade_concentration": 0.02,
                "net_income": 100,
            },
        ],
    )
    _write_jsonl(
        dataset / "filter_decisions.jsonl",
        [
            {"contract_symbol": "AMD260619P00080000", "status": "rejected", "rule": "iv_rv_ratio_below_minimum"},
            {"contract_symbol": "WIDE260619P00100000", "status": "rejected", "rule": "spread_too_wide"},
        ],
    )
    _write_jsonl(
        dataset / "mark_path_snapshots.jsonl",
        [
            {"contract_symbol": "NVDA260619P00100000", "mark_at": "2026-06-03", "unrealized_pnl": 20},
            {"contract_symbol": "AMD260619P00080000", "mark_at": "2026-06-03", "unrealized_pnl": 15},
            {"contract_symbol": "TSLA260619P00150000", "mark_at": "2026-06-03", "unrealized_pnl": -20},
            {"contract_symbol": "WIDE260619P00100000", "mark_at": "2026-06-03", "unrealized_pnl": -40},
        ],
    )
    _write_jsonl(
        dataset / "outcome_facts.jsonl",
        [
            {"contract_symbol": "NVDA260619P00100000", "outcome": "expired_worthless", "realized_pnl": 120},
            {"contract_symbol": "AMD260619P00080000", "outcome": "expired_worthless", "realized_pnl": 90},
            {"contract_symbol": "TSLA260619P00150000", "outcome": "expired_worthless", "realized_pnl": 100},
            {"contract_symbol": "WIDE260619P00100000", "outcome": "would_close_loss", "realized_pnl": -100},
        ],
    )

    result = run_shadow_replay_candidate_impact(
        repo_root=tmp_path,
        dataset=dataset,
        params=_params(),
        min_sample=4,
    )

    variant = result["variants"][0]
    assert result["schema_version"] == "shadow_replay_candidate_impact.v1"
    assert result["data_mode"] == "closed_replay"
    assert result["baseline"]["accepted_count"] == 2
    assert variant["accepted_count"] == 2
    assert variant["newly_accepted_count"] == 1
    assert variant["newly_rejected_count"] == 1
    assert variant["newly_accepted_samples"][0]["symbol"] == "AMD"
    assert variant["newly_rejected_samples"][0]["symbol"] == "TSLA"
    assert variant["safety_reasons"] == {"spread_ratio_above_safety_floor": 1}
    assert result["recommendation"]["status"] == "ready_for_live_shadow_review"
    assert result["safety"]["writes_runtime_config"] is False


def test_candidate_impact_uses_canonical_schema(tmp_path: Path) -> None:
    from src.application.shadow_replay import run_shadow_replay_candidate_impact

    _write_cli_candidate_run(tmp_path)

    result = run_shadow_replay_candidate_impact(
        repo_root=tmp_path,
        runs_root=tmp_path / "output_runs",
        start_date="2026-06-02",
        end_date="2026-06-02",
        params=_params(),
        min_sample=1,
    )

    assert result["schema_version"] == "shadow_replay_candidate_impact.v1"
    assert result["coverage"]["strict_backtest_allowed"] is True
    assert result["candidate_impact"]["allowed"] is True


def test_candidate_impact_merges_reject_log_replay_fields_into_trace_rows(tmp_path: Path) -> None:
    from src.application.shadow_replay import run_shadow_replay_candidate_impact

    account_dir = tmp_path / "output_runs" / "20260602T010000Z-run" / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    _write_jsonl(
        account_dir / "candidate_filter_trace.jsonl",
        [
            {
                "schema_version": "candidate_filter_trace.v1",
                "run_id": "20260602T010000Z-run",
                "account": "lx",
                "symbol": "NVDA",
                "function": "sell_put",
                "mode": "put",
                "option_type": "put",
                "strategy_family": "sell_put",
                "strategy_profile": "short_vol",
                "status": "rejected",
                "stage": "stage3_risk_filter",
                "rule": "vol_edge_ratio_below_min",
                "contract_symbol": "NVDA260619P00100000",
                "expiration": "2026-06-19",
                "strike": 100,
                "metric_value": 1.12,
                "threshold": 1.25,
            }
        ],
    )
    (account_dir / "nvda_sell_put_candidates_reject_log.csv").write_text(
        (
            "reject_stage,reject_rule,metric_value,threshold,symbol,contract_symbol,expiration,strike,mode,"
            "dte,delta,abs_delta,iv_rv_ratio,iv_minus_rv,annualized_return,spread_ratio,"
            "open_interest,volume,net_income,multiplier,engine_reject_stage,engine_reject_reason\n"
            "step3_risk_gate,vol_edge_ratio_below_min,1.12,1.25,NVDA,NVDA260619P00100000,"
            "2026-06-19,100,put,30,-0.2,0.2,1.12,0.06,0.20,0.10,120,20,100,100,"
            "stage3_risk_filter,vol_edge_ratio_below_min\n"
        ),
        encoding="utf-8",
    )

    result = run_shadow_replay_candidate_impact(
        repo_root=tmp_path,
        runs_root=tmp_path / "output_runs",
        start_date="2026-06-02",
        end_date="2026-06-02",
        params=_params(),
        min_sample=1,
    )

    assert result["summary"]["underwriting_candidate_count"] == 1
    assert result["evidence_quality"]["complete_candidate_count"] == 1
    assert result["evidence_quality"]["missing_required_fields"] == []
    assert result["variants"][0]["accepted_count"] == 1
    assert result["variants"][0]["newly_accepted_count"] == 1
    assert result["variants"][0]["newly_accepted_samples"][0]["symbol"] == "NVDA"


def test_candidate_impact_date_window_reports_missing_prefix_data(tmp_path: Path) -> None:
    from src.application.shadow_replay import run_shadow_replay_candidate_impact

    account_dir = tmp_path / "output_runs" / "20260602T010000Z-run" / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    (account_dir / "sell_put_candidates.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,expiration,dte,delta,strike,"
            "strategy_profile,iv_rv_ratio,iv_minus_rv,spread_ratio,single_trade_concentration,net_income\n"
            "NVDA,lx,put,NVDA260619P00100000,2026-06-19,30,-0.2,100,"
            "short_vol,1.25,0.08,0.10,0.02,120\n"
        ),
        encoding="utf-8",
    )

    result = run_shadow_replay_candidate_impact(
        repo_root=tmp_path,
        runs_root=tmp_path / "output_runs",
        start_date="2026-06-01",
        end_date="2026-06-02",
        params=_params(),
        min_sample=1,
    )

    assert result["coverage"]["selected_scanned_runs"] == 1
    assert result["coverage"]["strict_backtest_allowed"] is False
    assert result["coverage"]["reason"] == "requested_start_date_has_no_scan_artifacts"
    assert result["recommendation"]["next_action"] == "collect_scan_artifacts_for_requested_window"


def test_candidate_impact_maps_legacy_short_vol_candidate_profile_to_underwriting_params(tmp_path: Path) -> None:
    from src.application.shadow_replay import run_shadow_replay_candidate_impact

    account_dir = tmp_path / "output_runs" / "20260602T010000Z-run" / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    (account_dir / "nvda_sell_put_candidates.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,expiration,dte,delta,strike,"
            "iv_rv_ratio,iv_minus_rv,spread_ratio,single_trade_concentration,net_income\n"
            "NVDA,lx,put,NVDA260619P00100000,2026-06-19,30,-0.2,100,"
            "1.25,0.08,0.10,0.02,120\n"
        ),
        encoding="utf-8",
    )

    result = run_shadow_replay_candidate_impact(
        repo_root=tmp_path,
        runs_root=tmp_path / "output_runs",
        start_date="2026-06-02",
        end_date="2026-06-02",
        params=_params(),
        min_sample=1,
    )

    assert result["summary"]["underwriting_candidate_count"] == 1
    assert result["summary"]["parameter_complete_candidate_count"] == 1
    assert result["evidence_quality"]["parameter_fields_ready"] is True
    assert result["baseline"]["accepted_count"] == 1
    assert result["variants"][0]["accepted_count"] == 1


def test_candidate_impact_reports_parameter_field_evidence_gap(tmp_path: Path) -> None:
    from src.application.shadow_replay import run_shadow_replay_candidate_impact

    dataset = tmp_path / "output_shared" / "research" / "shadow_replay" / "datasets" / "missing-fields"
    _write_jsonl(
        dataset / "candidate_snapshots.jsonl",
        [
            {
                "contract_symbol": "NVDA260619P00100000",
                "symbol": "NVDA",
                "account": "lx",
                "option_type": "put",
                "status": "rejected",
                "strategy_profile": "short_vol",
                "filter_rule": "delta_below_target_band",
            }
        ],
    )
    _write_jsonl(dataset / "filter_decisions.jsonl", [{"contract_symbol": "NVDA260619P00100000", "status": "rejected"}])
    _write_jsonl(dataset / "mark_path_snapshots.jsonl", [])
    _write_jsonl(dataset / "outcome_facts.jsonl", [])

    result = run_shadow_replay_candidate_impact(
        repo_root=tmp_path,
        dataset=dataset,
        params=_params(),
        min_sample=1,
    )

    assert result["evidence_quality"]["complete_candidate_count"] == 0
    assert result["evidence_quality"]["field_coverage"]["dte"]["missing_count"] == 1
    assert set(result["recommendation"]["missing_required_fields"]) == {
        "abs_delta",
        "dte",
        "iv_minus_rv",
        "iv_rv_ratio",
    }
    assert result["recommendation"]["reason"] == "parameter_fields_missing"
    assert result["recommendation"]["next_action"] == "collect_candidate_parameter_fields"


def test_candidate_impact_allows_filter_only_candidate_impact_with_partial_fields(tmp_path: Path) -> None:
    from src.application.shadow_replay import run_shadow_replay_candidate_impact

    dataset = tmp_path / "output_shared" / "research" / "shadow_replay" / "datasets" / "partial-fields"
    _write_jsonl(
        dataset / "candidate_snapshots.jsonl",
        [
            {
                "contract_symbol": "NVDA260619P00100000",
                "symbol": "NVDA",
                "account": "lx",
                "option_type": "put",
                "status": "rejected",
                "strategy_profile": "short_vol",
                "iv_rv_ratio": 1.20,
                "iv_minus_rv": 0.06,
                "delta": -0.20,
                "dte": 30,
                "spread_ratio": 0.10,
                "single_trade_concentration": 0.02,
                "net_income": 120,
            },
            {
                "contract_symbol": "AMD260619P00080000",
                "symbol": "AMD",
                "account": "lx",
                "option_type": "put",
                "status": "rejected",
                "strategy_profile": "short_vol",
                "delta": -0.20,
                "dte": 30,
                "spread_ratio": 0.10,
                "single_trade_concentration": 0.02,
                "net_income": 90,
            },
        ],
    )
    _write_jsonl(
        dataset / "filter_decisions.jsonl",
        [
            {"contract_symbol": "NVDA260619P00100000", "status": "rejected"},
            {"contract_symbol": "AMD260619P00080000", "status": "rejected"},
        ],
    )
    _write_jsonl(dataset / "mark_path_snapshots.jsonl", [])
    _write_jsonl(dataset / "outcome_facts.jsonl", [])

    result = run_shadow_replay_candidate_impact(
        repo_root=tmp_path,
        dataset=dataset,
        params=_params(),
        min_sample=1,
    )

    assert result["evidence_quality"]["parameter_fields_ready"] is False
    assert result["gates"]["parameter_fields"]["status"] == "warn"
    assert result["gates"]["candidate_impact"]["allowed"] is True
    assert result["candidate_impact"]["allowed"] is True
    assert result["candidate_impact"]["limitations"] == ["parameter_fields_partial_counts_are_lower_bound"]
    assert result["variants"][0]["accepted_count"] == 1
    assert result["recommendation"]["status"] == "ready_for_live_shadow_candidate_review"
    assert result["recommendation"]["production_recommendation_allowed"] is False


def test_candidate_impact_without_outcomes_is_filter_only(tmp_path: Path) -> None:
    from src.application.shadow_replay import run_shadow_replay_candidate_impact

    dataset = tmp_path / "output_shared" / "research" / "shadow_replay" / "datasets" / "filter-only"
    _write_jsonl(
        dataset / "candidate_snapshots.jsonl",
        [
            {
                "contract_symbol": "NVDA260619P00100000",
                "symbol": "NVDA",
                "account": "lx",
                "option_type": "put",
                "status": "rejected",
                "strategy_profile": "short_vol",
                "iv_rv_ratio": 1.20,
                "iv_minus_rv": 0.06,
                "delta": -0.20,
                "dte": 30,
                "spread_ratio": 0.10,
                "single_trade_concentration": 0.02,
                "net_income": 120,
            }
        ],
    )
    _write_jsonl(dataset / "filter_decisions.jsonl", [{"contract_symbol": "NVDA260619P00100000", "status": "rejected"}])
    _write_jsonl(dataset / "mark_path_snapshots.jsonl", [])
    _write_jsonl(dataset / "outcome_facts.jsonl", [])

    result = run_shadow_replay_candidate_impact(
        repo_root=tmp_path,
        dataset=dataset,
        params=_params(),
        min_sample=1,
    )

    assert result["data_mode"] == "filter_only"
    assert result["variants"][0]["newly_accepted_count"] == 1
    assert result["gates"]["candidate_impact"]["status"] == "ready"
    assert result["gates"]["production_recommendation"]["status"] == "blocked"
    assert result["candidate_impact"]["best_variant_by_new_accepts"] == "iv_rv_1_10"
    assert result["recommendation"]["status"] == "ready_for_live_shadow_candidate_review"
    assert result["recommendation"]["reason"] == "outcome_evidence_missing"


def test_cli_shadow_replay_candidate_impact_command(capsys, monkeypatch, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    monkeypatch.setattr(cli, "repo_base", lambda: tmp_path)
    params_path = tmp_path / "params.json"
    params_path.write_text(json.dumps(_params()), encoding="utf-8")
    _write_cli_candidate_run(tmp_path)

    rc = cli.main(
        [
            "research",
            "shadow-replay",
            "candidate-impact",
            "--params",
            str(params_path),
            "--start-date",
            "2026-06-02",
            "--account",
            "lx",
            "--min-sample",
            "1",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["tool_name"] == "research.shadow-replay.candidate-impact"
    assert payload["data"]["schema_version"] == "shadow_replay_candidate_impact.v1"
    assert payload["data"]["coverage"]["strict_backtest_allowed"] is True
    assert payload["data"]["candidate_impact"]["allowed"] is True


def test_cli_shadow_replay_candidate_impact_report_command(capsys, monkeypatch, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    monkeypatch.setattr(cli, "repo_base", lambda: tmp_path)
    params_path = tmp_path / "params.json"
    params_path.write_text(json.dumps(_params()), encoding="utf-8")
    _write_cli_candidate_run(tmp_path)

    rc = cli.main(
        [
            "research",
            "shadow-replay",
            "candidate-impact-report",
            "--params",
            str(params_path),
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
    json_output = Path(payload["data"]["json_output"])
    markdown_output = Path(payload["data"]["markdown_output"])
    output_dir = Path(payload["data"]["output_dir"])
    result = json.loads(json_output.read_text(encoding="utf-8"))

    assert rc == 0
    assert payload["tool_name"] == "research.shadow-replay.candidate-impact-report"
    assert payload["data"]["schema_version"] == "shadow_replay_candidate_impact_report.v1"
    assert output_dir.name.startswith("candidate-impact-report-us-2026-06-02-to-2026-06-02-")
    assert json_output == output_dir / "result.us.json"
    assert markdown_output == output_dir / "result.us.md"
    assert result["schema_version"] == "shadow_replay_candidate_impact.v1"
    assert payload["data"]["candidate_impact_result"]["candidate_impact"]["allowed"] is True
