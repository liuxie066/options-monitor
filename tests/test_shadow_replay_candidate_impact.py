from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.candidate_evidence_helpers import (
    seal_opening_candidate_fixture,
    seal_strict_dataset_fixture,
)


BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


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
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    if path.name == "outcome_facts.jsonl":
        seal_strict_dataset_fixture(path.parent)


def _params() -> dict:
    return {
        "baseline": "production",
        "variants": [
            {
                "name": "iv_rv_1_10",
                "insurance_underwriting": {
                    "min_iv_rv_ratio": 1.10,
                    "min_iv_minus_rv": 0.05,
                    "min_dte": 20,
                    "max_dte": 60,
                },
            }
        ],
    }


def _write_cli_candidate_run(root: Path) -> None:
    run_id = "20260602T010000Z-run"
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
                "strategy_profile": "short_vol",
                "iv_rv_ratio": 1.25,
                "iv_minus_rv": 0.08,
                "spread_ratio": 0.10,
                "single_trade_concentration": 0.02,
                "net_income": 120,
            }
        ],
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


def test_parameter_set_rejects_delta_as_underwriting_filter() -> None:
    from src.application.shadow_replay.parameter_sets import parse_parameter_set

    with pytest.raises(ValueError, match="non-tunable parameters: max_abs_delta"):
        parse_parameter_set(
            {
                "variants": [
                    {
                        "name": "legacy_delta_gate",
                        "insurance_underwriting": {"max_abs_delta": 0.30},
                    }
                ]
            }
        )


def test_parameter_set_marks_only_single_production_field_as_closed_replay_eligible() -> None:
    from src.application.shadow_replay.parameter_sets import parse_parameter_set

    parameter_set = parse_parameter_set(
        {
            "variants": [
                {
                    "name": "single",
                    "insurance_underwriting": {"min_dte": 21},
                },
                {
                    "name": "multi",
                    "insurance_underwriting": {"min_dte": 21, "max_dte": 60},
                },
            ]
        }
    )

    single, multi = parameter_set.to_payload()["variants"]
    assert single["changed_fields"] == ["min_dte"]
    assert single["production_closed_replay_eligible"] is True
    assert multi["changed_fields"] == ["max_dte", "min_dte"]
    assert multi["production_closed_replay_eligible"] is False


@pytest.mark.parametrize(
    ("params", "message"),
    [
        (
            {"min_iv_rv_ratio": 1.0, "min_iv_rv_percentile": 1.1},
            "min_iv_rv_percentile must be between 0 and 1",
        ),
        (
            {
                "min_iv_rv_ratio": 1.0,
                "min_iv_rv_percentile": 0.7,
                "min_iv_rv_history_samples": 0,
            },
            "min_iv_rv_history_samples must be a positive integer",
        ),
        (
            {
                "min_iv_rv_ratio": 1.0,
                "min_iv_rv_percentile": 0.7,
                "min_iv_rv_history_samples": 1.5,
            },
            "min_iv_rv_history_samples must be a positive integer",
        ),
        (
            {"min_iv_rv_percentile": 0.7},
            "min_iv_rv_percentile requires min_iv_rv_ratio absolute floor",
        ),
    ],
)
def test_parameter_set_rejects_invalid_iv_rv_history_parameters(
    params: dict[str, float],
    message: str,
) -> None:
    from src.application.shadow_replay.parameter_sets import parse_parameter_set

    with pytest.raises(ValueError, match=message):
        parse_parameter_set(
            {
                "variants": [
                    {
                        "name": "invalid_history",
                        "insurance_underwriting": params,
                    }
                ]
            }
        )


def test_iv_rv_history_percentile_uses_prior_runs_only() -> None:
    from src.application.shadow_replay.candidate_impact import _enrich_iv_rv_history_percentiles

    rows, summary = _enrich_iv_rv_history_percentiles(
        [
            {
                "run_id": "20260603T010000Z-run",
                "contract_symbol": "NVDA-3",
                "symbol": "NVDA",
                "option_type": "put",
                "dte": 30,
                "iv_rv_ratio": 4.0,
            },
            {
                "run_id": "20260601T010000Z-run",
                "contract_symbol": "NVDA-1A",
                "symbol": "NVDA",
                "option_type": "put",
                "dte": 30,
                "iv_rv_ratio": 1.0,
            },
            {
                "run_id": "20260601T010000Z-run",
                "contract_symbol": "NVDA-1B",
                "symbol": "NVDA",
                "option_type": "put",
                "dte": 30,
                "iv_rv_ratio": 3.0,
            },
            {
                "run_id": "20260602T010000Z-run",
                "contract_symbol": "NVDA-2",
                "symbol": "NVDA",
                "option_type": "put",
                "dte": 30,
                "iv_rv_ratio": 2.0,
            },
        ]
    )
    by_contract = {row["contract_symbol"]: row for row in rows}

    assert by_contract["NVDA-1A"]["iv_rv_history_sample_count"] == 0
    assert by_contract["NVDA-1B"]["iv_rv_history_sample_count"] == 0
    assert by_contract["NVDA-2"]["iv_rv_history_sample_count"] == 2
    assert by_contract["NVDA-2"]["iv_rv_history_percentile"] == 0.5
    assert by_contract["NVDA-3"]["iv_rv_history_sample_count"] == 3
    assert by_contract["NVDA-3"]["iv_rv_history_percentile"] == 1.0
    assert summary["lookahead_allowed"] is False


def test_iv_rv_history_percentile_falls_back_to_absolute_floor(tmp_path: Path) -> None:
    from src.application.shadow_replay import run_shadow_replay_candidate_impact

    dataset = tmp_path / "dataset"
    _write_jsonl(
        dataset / "candidate_snapshots.jsonl",
        [
            {
                "run_id": "20260601T010000Z-run",
                "contract_symbol": "NVDA-1",
                "symbol": "NVDA",
                "option_type": "put",
                "status": "rejected",
                "strategy_profile": "insurance_underwriting",
                "filter_rule": "iv_rv_ratio_below_minimum",
                "iv_rv_ratio": 1.2,
                "dte": 30,
            }
        ],
    )
    _write_jsonl(dataset / "filter_decisions.jsonl", [])
    _write_jsonl(dataset / "mark_path_snapshots.jsonl", [])
    _write_jsonl(dataset / "outcome_facts.jsonl", [])

    result = run_shadow_replay_candidate_impact(
        repo_root=tmp_path,
        dataset=dataset,
        params={
            "variants": [
                {
                    "name": "historical_iv_rv",
                    "insurance_underwriting": {
                        "min_iv_rv_ratio": 1.1,
                        "min_iv_rv_percentile": 0.7,
                        "min_iv_rv_history_samples": 20,
                    },
                }
            ]
        },
        min_sample=1,
    )

    variant = result["variants"][0]
    assert variant["accepted_count"] == 1
    assert variant["iv_rv_history_modes"] == {"fallback_absolute_floor": 1}
    assert variant["iv_rv_history_status"] == "insufficient_history"
    assert variant["comparison_eligible"] is False
    assert result["gates"]["candidate_impact"]["reason"] == "iv_rv_history_insufficient"


def test_iv_rv_history_percentile_rejects_after_history_is_sufficient(tmp_path: Path) -> None:
    from src.application.shadow_replay import run_shadow_replay_candidate_impact

    dataset = tmp_path / "dataset"
    candidates = []
    for run_id, contract, ratio in [
        ("20260601T010000Z-run", "NVDA-1A", 1.4),
        ("20260601T010000Z-run", "NVDA-1B", 1.5),
        ("20260602T010000Z-run", "NVDA-2", 1.3),
        ("20260603T010000Z-run", "NVDA-3", 1.1),
    ]:
        candidates.append(
            {
                "run_id": run_id,
                "contract_symbol": contract,
                "symbol": "NVDA",
                "option_type": "put",
                "status": "accepted",
                "strategy_profile": "insurance_underwriting",
                "iv_rv_ratio": ratio,
                "dte": 30,
            }
        )
    _write_jsonl(dataset / "candidate_snapshots.jsonl", candidates)
    _write_jsonl(dataset / "filter_decisions.jsonl", [])
    _write_jsonl(dataset / "mark_path_snapshots.jsonl", [])
    _write_jsonl(dataset / "outcome_facts.jsonl", [])

    result = run_shadow_replay_candidate_impact(
        repo_root=tmp_path,
        dataset=dataset,
        params={
            "variants": [
                {
                    "name": "historical_iv_rv",
                    "insurance_underwriting": {
                        "min_iv_rv_ratio": 1.0,
                        "min_iv_rv_percentile": 0.7,
                        "min_iv_rv_history_samples": 3,
                    },
                }
            ]
        },
        min_sample=1,
    )

    variant = result["variants"][0]
    assert variant["iv_rv_history_modes"] == {
        "evaluated": 1,
        "fallback_absolute_floor": 3,
    }
    assert variant["iv_rv_history_status"] == "evaluated"
    assert variant["comparison_eligible"] is True
    assert variant["newly_rejected_count"] == 1
    assert variant["newly_rejected_samples"][0]["contract_symbol"] == "NVDA-3"
    assert variant["newly_rejected_samples"][0]["iv_rv_history_percentile"] == 0.0


def test_candidate_impact_scopes_variants_to_strategy_family(tmp_path: Path) -> None:
    from src.application.shadow_replay import run_shadow_replay_candidate_impact

    dataset = tmp_path / "dataset"
    _write_jsonl(
        dataset / "candidate_snapshots.jsonl",
        [
            {
                "contract_symbol": "NVDA-P",
                "symbol": "NVDA",
                "option_type": "put",
                "strategy_family": "sell_put",
                "strategy_profile": "insurance_underwriting",
                "status": "rejected",
                "filter_rule": "iv_rv_ratio_below_minimum",
                "iv_rv_ratio": 1.2,
            },
            {
                "contract_symbol": "AAPL-C",
                "symbol": "AAPL",
                "option_type": "call",
                "strategy_family": "sell_call",
                "strategy_profile": "insurance_underwriting",
                "status": "rejected",
                "iv_rv_ratio": 1.2,
            },
        ],
    )
    _write_jsonl(dataset / "filter_decisions.jsonl", [])
    _write_jsonl(dataset / "mark_path_snapshots.jsonl", [])
    _write_jsonl(dataset / "outcome_facts.jsonl", [])

    result = run_shadow_replay_candidate_impact(
        repo_root=tmp_path,
        dataset=dataset,
        params={
            "variants": [
                {
                    "name": "put_only",
                    "strategy_family": "sell_put",
                    "insurance_underwriting": {"min_iv_rv_ratio": 1.1},
                },
                {
                    "name": "call_only",
                    "strategy_family": "covered_call",
                    "insurance_underwriting": {"min_iv_rv_ratio": 1.3},
                },
            ]
        },
        min_sample=1,
    )

    put_variant, call_variant = result["variants"]
    assert put_variant["strategy_family"] == "sell_put"
    assert put_variant["candidate_count"] == 1
    assert put_variant["newly_accepted_count"] == 1
    assert call_variant["strategy_family"] == "covered_call"
    assert call_variant["candidate_count"] == 1
    assert call_variant["newly_accepted_count"] == 0


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
                "single_trade_concentration": 0.20,
                "max_single_trade_nav_pct": 0.05,
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
    assert result["data_mode"] == "outcome_incomplete"
    assert result["baseline"]["accepted_count"] == 2
    assert variant["accepted_count"] == 2
    assert variant["newly_accepted_count"] == 1
    assert variant["newly_rejected_count"] == 1
    assert variant["newly_accepted_samples"][0]["symbol"] == "AMD"
    assert variant["newly_rejected_samples"][0]["symbol"] == "TSLA"
    assert variant["safety_reasons"] == {
        "preserved_production_gate:spread_too_wide": 1,
        "spread_ratio_above_safety_floor": 1,
    }
    assert variant["safety_rejected_count"] == 1
    assert variant["safety_violation_count"] == 0
    assert result["recommendation"]["status"] == "ready_for_live_shadow_candidate_review"
    assert result["recommendation"]["production_recommendation_allowed"] is False
    assert result["safety"]["writes_runtime_config"] is False


def test_candidate_impact_closed_replay_uses_complete_lifecycles_and_single_parameter_variant(
    tmp_path: Path,
) -> None:
    from src.application.shadow_replay import run_shadow_replay_candidate_impact

    dataset = tmp_path / "dataset"
    candidates = [
        {
            "contract_symbol": "NVDA260619P00100000",
            "symbol": "NVDA",
            "account": "lx",
            "option_type": "put",
            "status": "accepted",
            "strategy_profile": "insurance_underwriting",
            "iv_rv_ratio": 1.30,
            "iv_minus_rv": 0.08,
            "dte": 30,
            "spread_ratio": 0.10,
            "net_income": 20,
        },
        {
            "contract_symbol": "AMD260619P00080000",
            "symbol": "AMD",
            "account": "lx",
            "option_type": "put",
            "status": "rejected",
            "strategy_profile": "insurance_underwriting",
            "filter_rule": "iv_rv_ratio_below_minimum",
            "iv_rv_ratio": 1.15,
            "iv_minus_rv": 0.06,
            "dte": 30,
            "spread_ratio": 0.10,
            "net_income": 20,
        },
    ]
    _write_jsonl(dataset / "candidate_snapshots.jsonl", candidates)
    _write_jsonl(dataset / "filter_decisions.jsonl", [])
    _write_jsonl(
        dataset / "mark_path_snapshots.jsonl",
        [
            {"contract_symbol": "NVDA260619P00100000", "unrealized_pnl": -40},
            {"contract_symbol": "AMD260619P00080000", "unrealized_pnl": 20},
        ],
    )
    _write_jsonl(
        dataset / "outcome_facts.jsonl",
        [
            {
                "contract_symbol": "NVDA260619P00100000",
                "symbol": "NVDA",
                "outcome": "closed",
                "realized_pnl": -100,
                "lifecycle_pnl_net": -100,
                "capital_days": 10_000,
                "annualized_capital_efficiency": -3.65,
                "fee_basis": "actual",
                "fee_missing_components": [],
                "covered_call_allocation_status": "none",
                "lifecycle_quality": "complete_closed",
            },
            {
                "contract_symbol": "AMD260619P00080000",
                "symbol": "AMD",
                "outcome": "closed",
                "realized_pnl": 200,
                "lifecycle_pnl_net": 200,
                "capital_days": 20_000,
                "annualized_capital_efficiency": 3.65,
                "fee_basis": "estimated",
                "fee_missing_components": [],
                "covered_call_allocation_status": "none",
                "lifecycle_quality": "complete_closed",
            },
        ],
    )

    result = run_shadow_replay_candidate_impact(
        repo_root=tmp_path,
        dataset=dataset,
        params={
            "variants": [
                {
                    "name": "iv_rv_1_10",
                    "insurance_underwriting": {"min_iv_rv_ratio": 1.10},
                }
            ]
        },
        min_sample=1,
    )

    assert result["data_mode"] == "closed_replay"
    assert result["summary"]["complete_closed_outcome_fact_count"] == 2
    assert result["gates"]["closed_lifecycle_evidence"]["allowed"] is True
    assert result["gates"]["production_recommendation"]["allowed"] is True
    baseline = result["baseline"]["closed_lifecycle_metrics"]
    variant = result["variants"][0]
    assert baseline["complete_closed_count"] == 1
    assert baseline["weighted_annualized_capital_efficiency"] == -3.65
    assert variant["changed_fields"] == ["min_iv_rv_ratio"]
    assert variant["production_closed_replay_eligible"] is True
    assert variant["closed_lifecycle_metrics"]["complete_closed_count"] == 2
    assert result["closed_replay_comparison"]["suggested_variant_for_manual_review"] == "iv_rv_1_10"
    assert result["recommendation"]["status"] == "ready_for_manual_closed_replay_review"
    assert result["recommendation"]["runtime_config_write_allowed"] is False


def test_candidate_impact_assignment_transition_without_stock_lifecycle_is_not_closed_replay(
    tmp_path: Path,
) -> None:
    from src.application.shadow_replay import run_shadow_replay_candidate_impact

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
                "strategy_profile": "insurance_underwriting",
                "iv_rv_ratio": 1.30,
                "dte": 30,
                "spread_ratio": 0.10,
                "net_income": 20,
            }
        ],
    )
    _write_jsonl(dataset / "filter_decisions.jsonl", [])
    _write_jsonl(
        dataset / "mark_path_snapshots.jsonl",
        [{"contract_symbol": "NVDA260619P00100000", "unrealized_pnl": -40}],
    )
    _write_jsonl(
        dataset / "outcome_facts.jsonl",
        [
            {
                "contract_symbol": "NVDA260619P00100000",
                "outcome": "assigned_at_expiry",
                "realized_pnl": -100,
                "lifecycle_quality": "transition_only",
            }
        ],
    )

    result = run_shadow_replay_candidate_impact(
        repo_root=tmp_path,
        dataset=dataset,
        params={
            "variants": [
                {
                    "name": "iv_rv_1_10",
                    "insurance_underwriting": {"min_iv_rv_ratio": 1.10},
                }
            ]
        },
        min_sample=1,
    )

    assert result["data_mode"] == "outcome_incomplete"
    assert result["summary"]["complete_closed_outcome_fact_count"] == 0
    assert result["gates"]["production_recommendation"]["allowed"] is False
    assert result["recommendation"]["reason"] == "complete_closed_lifecycle_evidence_missing"


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


def test_candidate_impact_recomputes_coverage_for_requested_account(
    tmp_path: Path,
) -> None:
    from src.application.shadow_replay import run_shadow_replay_candidate_impact

    _write_cli_candidate_run(tmp_path)
    sy_dir = (
        tmp_path
        / "output_runs"
        / "20260602T010000Z-run"
        / "accounts"
        / "sy"
    )
    _write_jsonl(
        sy_dir / "candidate_filter_trace.jsonl",
        [
            {
                "schema_version": "candidate_filter_trace.v1",
                "run_id": "20260602T010000Z-run",
                "account": "sy",
                "symbol": "0700.HK",
                "status": "rejected",
                "rule": "fixture",
            }
        ],
    )

    unscoped = run_shadow_replay_candidate_impact(
        repo_root=tmp_path,
        runs_root=tmp_path / "output_runs",
        start_date="2026-06-02",
        end_date="2026-06-02",
        params=_params(),
        min_sample=1,
    )
    scoped = run_shadow_replay_candidate_impact(
        repo_root=tmp_path,
        runs_root=tmp_path / "output_runs",
        start_date="2026-06-02",
        end_date="2026-06-02",
        accounts=["lx"],
        params=_params(),
        min_sample=1,
    )

    assert unscoped["coverage"]["strict_backtest_allowed"] is False
    assert scoped["coverage"]["strict_backtest_allowed"] is True
    assert {
        row["account"]
        for row in scoped["coverage"]["candidate_evidence_coverage"]["accounts"]
    } == {"lx"}


def test_candidate_coverage_market_scope_excludes_only_known_other_market() -> None:
    from src.application.shadow_replay.candidate_impact import (
        _scope_candidate_evidence_coverage,
    )

    coverage = {
        "accounts": [
            {"account": "lx", "status": "supported", "markets": ["us"]},
            {
                "account": "sy",
                "status": "supported_limited_legacy_snapshot",
                "markets": ["hk"],
            },
        ]
    }

    scoped = _scope_candidate_evidence_coverage(
        coverage,
        accounts=set(),
        market="us",
    )
    assert scoped["strict_replay_authority"] is True
    assert [row["account"] for row in scoped["accounts"]] == ["lx"]

    coverage["accounts"].append(
        {"account": "unknown", "status": "unsupported_snapshot_schema"}
    )
    unknown_market = _scope_candidate_evidence_coverage(
        coverage,
        accounts=set(),
        market="us",
    )
    assert unknown_market["strict_replay_authority"] is False
    assert {row["account"] for row in unknown_market["accounts"]} == {
        "lx",
        "unknown",
    }


def test_candidate_impact_recomputes_dataset_coverage_for_requested_scope(
    tmp_path: Path,
) -> None:
    from src.application.shadow_replay import run_shadow_replay_candidate_impact
    from src.application.shadow_replay.common import refresh_dataset_manifest, write_json

    dataset = tmp_path / "scoped-dataset"
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
                "dte": 30,
                "spread_ratio": 0.10,
                "single_trade_concentration": 0.02,
                "net_income": 120,
            }
        ],
    )
    _write_jsonl(dataset / "filter_decisions.jsonl", [])
    _write_jsonl(dataset / "mark_path_snapshots.jsonl", [])
    _write_jsonl(dataset / "outcome_facts.jsonl", [])
    manifest = json.loads((dataset / "manifest.json").read_text())
    manifest["source"]["candidate_evidence_coverage"]["accounts"].append(
        {
            "account": "sy",
            "status": "unsupported_snapshot_missing",
            "reason_code": "candidate_snapshot_manifest_missing",
            "strict_replay_authority": False,
            "markets": ["hk"],
        }
    )
    manifest["source"]["candidate_evidence_coverage"]["counts"].update(
        {"supported": 1, "unsupported_snapshot_missing": 1}
    )
    manifest["source"]["candidate_evidence_coverage"][
        "strict_replay_authority"
    ] = False
    write_json(dataset / "manifest.json", manifest)
    refresh_dataset_manifest(dataset)

    unscoped = run_shadow_replay_candidate_impact(
        repo_root=tmp_path,
        dataset=dataset,
        params=_params(),
        min_sample=1,
    )
    scoped = run_shadow_replay_candidate_impact(
        repo_root=tmp_path,
        dataset=dataset,
        accounts=["lx"],
        market="us",
        params=_params(),
        min_sample=1,
    )

    assert unscoped["coverage"]["strict_backtest_allowed"] is False
    assert scoped["coverage"]["strict_backtest_allowed"] is True
    assert [
        row["account"]
        for row in scoped["coverage"]["candidate_evidence_coverage"]["accounts"]
    ] == ["lx"]


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
                "rule": "risk_iv_rv_ratio",
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
    seal_opening_candidate_fixture(
        tmp_path,
        run_id="20260602T010000Z-run",
        rejected_rows=[
            {
                "symbol": "NVDA",
                "account": "lx",
                "option_type": "put",
                "contract_symbol": "NVDA260619P00100000",
                "expiration": "2026-06-19",
                "strike": 100,
                "dte": 30,
                "delta": -0.2,
                "abs_delta": 0.2,
                "iv_rv_ratio": 1.12,
                "iv_minus_rv": 0.06,
                "annualized_net_return_on_cash_basis": 0.20,
                "spread_ratio": 0.10,
                "open_interest": 120,
                "volume": 20,
                "net_income": 100,
                "multiplier": 100,
                "strategy_profile": "short_vol",
                "stage": "stage3_risk_filter",
                "rule": "risk_iv_rv_ratio",
                "metric_value": 1.12,
                "threshold": 1.25,
            }
        ],
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
    seal_opening_candidate_fixture(
        tmp_path,
        run_id="20260602T010000Z-run",
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
                "iv_minus_rv": 0.08,
                "spread_ratio": 0.10,
                "single_trade_concentration": 0.02,
                "net_income": 120,
            }
        ],
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
                "filter_rule": "iv_rv_ratio_below_minimum",
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
                "filter_rule": "iv_rv_ratio_below_minimum",
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
    assert result["recommendation"]["candidate_review_basis"] == "newly_accepted_count_only"
    assert "candidate_variant" not in result["recommendation"]


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


def test_candidate_impact_preserves_cash_coverage_and_rank_truncation_gates() -> None:
    from src.application.shadow_replay.candidate_impact import _evaluate_candidate
    from src.application.shadow_replay.parameter_sets import parse_parameter_set

    variant = parse_parameter_set(
        {
            "variants": [
                {
                    "name": "relax_iv",
                    "insurance_underwriting": {"min_iv_rv_ratio": 1.10},
                }
            ]
        }
    ).variants[0]
    common = {
        "account": "lx",
        "option_type": "put",
        "strategy_family": "sell_put",
        "strategy_profile": "insurance_underwriting",
        "iv_rv_ratio": 1.25,
        "net_income": 100,
        "status": "rejected",
    }

    cash = _evaluate_candidate(
        {
            **common,
            "contract_symbol": "CASH260619P00100000",
            "filter_rule": "cash_capacity_insufficient",
            "cash_required_cny": 10_000,
            "cash_free_cny": 1_000,
        },
        variant=variant,
    )
    ranked = _evaluate_candidate(
        {
            **common,
            "contract_symbol": "RANK260619P00100000",
            "status": "ranked_below",
        },
        variant=variant,
    )
    covered = _evaluate_candidate(
        {
            **common,
            "contract_symbol": "COVER260619C00100000",
            "option_type": "call",
            "strategy_family": "covered_call",
            "status": "accepted",
        },
        variant=variant,
    )

    assert cash["status"] == "rejected"
    assert "cash_capacity_insufficient" in cash["safety_reasons"]
    assert "preserved_production_gate:cash_capacity_insufficient" in cash["reasons"]
    assert ranked["status"] == "rejected"
    assert "production_rank_truncation_not_replayed" in ranked["reasons"]
    assert covered["status"] == "rejected"
    assert {
        "covered_call_coverage_context_missing",
        "covered_call_cost_basis_context_missing",
    }.issubset(covered["safety_reasons"])
