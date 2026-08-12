from __future__ import annotations

from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain.domain.engine import (
    ComboYieldResearchPolicy,
    build_candidate_decision,
    combo_yield_proposed_gate_reasons,
    rank_combo_yield_proposed_rows,
    select_best_combo_yield_proposed_pairs,
)
from domain.domain.insurance_underwriting import rank_underwriting_candidates
from src.application.shadow_replay.combo_variants import (
    attach_funding_put_rank_provenance,
    build_combo_pair_decisions,
    combo_entry_quote_quality,
    combo_research_policy_from_dict,
    normalize_combo_variant_spec,
    publish_combo_pair_facet,
)
from src.application.shadow_replay.combo_capture import capture_combo_variants
from src.application.shadow_replay.combo_evaluation import (
    _unavailable_variants_by_symbol,
    evaluate_combo_variant_dataset,
    evaluate_combo_variant_pairs,
)
from src.application.shadow_replay.combo_funding import (
    COMBO_FUNDING_PUT_FILE,
    prepare_combo_funding_puts,
    validate_combo_funding_put_source,
)
from src.application.shadow_replay.combo_settlement import (
    build_combo_variant_scorecards,
    settle_combo_pair_outcomes,
)
from src.application.shadow_replay.common import (
    DATASET_FILES,
    refresh_dataset_manifest,
)
from src.application.required_data_planning import (
    OptionSideFetchPlan,
    RequiredDataFetchPlanBundle,
    StrikeWindowPlan,
)
from src.application.candidate_snapshot_manifest import publish_candidate_snapshot_manifest
from src.application.combo_yield_candidate_snapshot import seal_combo_yield_candidate_snapshot
from src.application.strategy_scan_status import (
    publish_strategy_scan_status,
    publish_strategy_scan_status_index_v2,
)


def _policy(*, variant_id: str = "same-d20") -> ComboYieldResearchPolicy:
    return ComboYieldResearchPolicy(
        variant_id=variant_id,
        structure_mode="same_expiry_pair",
        min_net_credit_retention=0.75,
        min_abs_call_delta=0.10,
        target_abs_call_delta=0.20,
        max_abs_call_delta=0.30,
    )


def _pair(
    *,
    put: str = "NVDA-P100",
    call: str = "NVDA-C120",
    rank: int = 1,
    delta: float = 0.20,
    retention: float = 0.80,
    cost_ratio: float = 0.20,
) -> dict:
    observed = "2026-07-29T01:00:00Z"
    return {
        "symbol": "NVDA",
        "structure_mode": "same_expiry_pair",
        "put_contract_symbol": put,
        "call_contract_symbol": call,
        "put_expiration": "2026-08-21",
        "call_expiration": "2026-08-21",
        "expiry_gap_days": 0,
        "put_strike": 100,
        "call_strike": 120,
        "currency": "USD",
        "multiplier": 100,
        "put_bid": 5,
        "call_ask": 1,
        "spot": 110,
        "put_net_credit": 500,
        "call_total_cost": 100,
        "combo_net_credit": 400,
        "net_credit_retention": retention,
        "call_cost_to_put_credit": cost_ratio,
        "call_delta": delta,
        "put_implied_volatility": 0.30,
        "call_implied_volatility": 0.28,
        "put_spread_ratio": 0.08,
        "put_open_interest": 600,
        "call_spread_ratio": 0.10,
        "call_open_interest": 500,
        "funding_put_rank": rank,
        "funding_put_rank_key": [{"type": "int", "value": rank}],
        "source_candidate_count": 2,
        "put_quote_observed_at_utc": observed,
        "call_quote_observed_at_utc": observed,
        "spot_observed_at_utc": observed,
    }


def _spec() -> dict:
    return normalize_combo_variant_spec(
        {
            "schema_version": "shadow_combo_variant_spec.v1",
            "max_estimated_option_chain_calls": 20,
            "max_entry_quote_age_seconds": 60,
            "max_entry_leg_skew_seconds": 2,
            "variants": [
                {
                    "variant_id": "same-d20",
                    "structure_mode": "same_expiry_pair",
                    "min_net_credit_retention": 0.75,
                    "min_abs_call_delta": 0.10,
                    "target_abs_call_delta": 0.20,
                    "max_abs_call_delta": 0.30,
                }
            ],
        }
    )


def _capture_spec() -> dict:
    return normalize_combo_variant_spec(
        {
            "schema_version": "shadow_combo_variant_spec.v1",
            "max_estimated_option_chain_calls": 20,
            "max_entry_quote_age_seconds": 60,
            "max_entry_leg_skew_seconds": 2,
            "variants": [
                {
                    "variant_id": "same-d20",
                    "structure_mode": "same_expiry_pair",
                    "min_net_credit_retention": 0.75,
                    "min_abs_call_delta": 0.10,
                    "target_abs_call_delta": 0.20,
                    "max_abs_call_delta": 0.30,
                },
            ],
        }
    )


def _fake_capture_plan(**kwargs) -> RequiredDataFetchPlanBundle:
    put_exp = "2026-08-21"
    call_exp = put_exp
    window = StrikeWindowPlan(
        min_strike=80,
        max_strike=140,
        source="test",
    )
    sides = [
        OptionSideFetchPlan(
            option_type="put",
            min_dte=1,
            max_dte=60,
            explicit_expirations=[put_exp],
            strike_window=window,
            planning_reason="test",
        ),
        OptionSideFetchPlan(
            option_type="call",
            min_dte=1,
            max_dte=90,
            explicit_expirations=[call_exp],
            strike_window=window,
            planning_reason="test",
        ),
    ]
    return RequiredDataFetchPlanBundle(
        symbol=kwargs["symbol"],
        spot_reference=110,
        side_plans=sides,
        merged_specs=[],
    )


def test_proposed_rank_ignores_premium_funding_score_and_keeps_put_rank_dominant() -> None:
    policy = _policy()
    high_put = {
        **_pair(call="NVDA-C125", rank=1, delta=0.29),
        "premium_funding_score": -100,
    }
    low_put = {
        **_pair(put="NVDA-P95", call="NVDA-C121", rank=2, delta=0.20),
        "premium_funding_score": 100,
    }

    first = select_best_combo_yield_proposed_pairs([low_put, high_put], policy)[0]
    assert first["put_contract_symbol"] == "NVDA-P100"

    changed = [{**high_put, "premium_funding_score": 999}, {**low_put, "premium_funding_score": -999}]
    assert [
        (row["put_contract_symbol"], row["call_contract_symbol"])
        for row in rank_combo_yield_proposed_rows(changed, policy)
    ] == [
        (row["put_contract_symbol"], row["call_contract_symbol"])
        for row in rank_combo_yield_proposed_rows([high_put, low_put], policy)
    ]


def test_proposed_dual_funding_gates_are_independent_and_equality_passes() -> None:
    policy = ComboYieldResearchPolicy(
        **{
            **_policy().__dict__,
            "max_call_cost_to_put_credit": 0.20,
        }
    )
    equal = _pair(retention=0.75, cost_ratio=0.20)
    assert combo_yield_proposed_gate_reasons(equal, policy) == ()

    reasons = combo_yield_proposed_gate_reasons(
        _pair(retention=0.74, cost_ratio=0.21),
        policy,
    )
    assert "min_net_credit_retention" in reasons
    assert "max_call_cost_to_put_credit" in reasons


def test_combo_research_policy_rejects_removed_expiry_gap_fields() -> None:
    base = {
        "variant_id": "same-d20",
        "structure_mode": "same_expiry_pair",
        "min_net_credit_retention": 0.75,
        "min_abs_call_delta": 0.10,
        "target_abs_call_delta": 0.20,
        "max_abs_call_delta": 0.30,
    }
    for field in ("min_expiry_gap_days", "target_expiry_gap_days", "max_expiry_gap_days"):
        with pytest.raises(ValueError):
            combo_research_policy_from_dict({**base, field: 28})


def test_rank_provenance_keeps_original_put_rank_after_pair_filtering() -> None:
    puts = [
        {
            "symbol": "NVDA",
            "contract_symbol": "NVDA-P100",
            "annualized_net_return_on_cash_basis": 0.20,
            "dte": 30,
            "net_assignment_discount_pct": 0.10,
            "spread_ratio": 0.10,
            "open_interest": 100,
            "net_income": 500,
        },
        {
            "symbol": "NVDA",
            "contract_symbol": "NVDA-P95",
            "annualized_net_return_on_cash_basis": 0.15,
            "dte": 30,
            "net_assignment_discount_pct": 0.15,
            "spread_ratio": 0.10,
            "open_interest": 100,
            "net_income": 400,
        },
    ]
    ranked_puts = rank_underwriting_candidates(puts, mode="put")
    rows = attach_funding_put_rank_provenance(
        pair_rows=[_pair(put="NVDA-P95", rank=99)],
        ranked_put_rows=ranked_puts,
        combo_rank_scope_hash_value="scope",
    )
    assert rows[0]["funding_put_rank"] == 2
    assert rows[0]["source_candidate_count"] == 2
    assert rows[0]["funding_put_rank_key"]


def test_quote_quality_fails_missing_stale_and_skewed_evidence_closed() -> None:
    captured = datetime(2026, 7, 29, 1, 0, 30, tzinfo=timezone.utc)
    complete = combo_entry_quote_quality(
        _pair(),
        captured_at=captured,
        max_age_seconds=60,
        max_skew_seconds=2,
    )
    assert complete["status"] == "complete"

    broken = {
        **_pair(),
        "call_quote_observed_at_utc": "2026-07-29T00:58:00Z",
        "spot_observed_at_utc": None,
    }
    unavailable = combo_entry_quote_quality(
        broken,
        captured_at=captured,
        max_age_seconds=60,
        max_skew_seconds=2,
    )
    assert unavailable["status"] == "unavailable"
    assert "call_quote_stale" in unavailable["reason_codes"]
    assert "spot_quote_timestamp_missing" in unavailable["reason_codes"]


def test_decision_facet_has_research_identity_and_does_not_reuse_strategy_group_id() -> None:
    decisions = build_combo_pair_decisions(
        dataset_id="dataset-1",
        account="lx",
        pair_rows=[_pair()],
        effective_combo_policy_hash="policy",
        variant_spec=_spec(),
        required_data_file_sha256={"required.csv": "abc"},
        entry_observed_at_utc="2026-07-29T01:00:30Z",
        baseline_structure_mode="same_expiry_pair",
    )
    assert len(decisions) == 1
    assert decisions[0]["shadow_combo_pair_id"]
    assert "strategy_group_id" not in decisions[0]
    assert decisions[0]["variant_decisions"][0]["selected"] is True


def test_variant_capture_failure_is_isolated_to_affected_symbol() -> None:
    manifest = {
        "symbols": ["NVDA", "PDD"],
        "variant_completeness": [
            {
                "variant_id": "same-21d",
                "status": "unavailable",
                "missing_expirations_or_contracts": [
                    {"symbol": "NVDA", "expiration": "2026-08-21"}
                ],
            }
        ],
    }

    unavailable = _unavailable_variants_by_symbol(manifest)

    assert unavailable["NVDA"] == {"same-21d"}
    assert unavailable["PDD"] == set()


def test_combo_facet_preview_is_read_only_and_write_hashes_files(tmp_path) -> None:
    from src.application.shadow_replay import analyze_shadow_replay_dataset

    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "manifest.json").write_text(json.dumps({"schema_version": "shadow_replay_dataset.v1"}))
    for name in DATASET_FILES:
        (dataset / name).write_text("")
    refresh_dataset_manifest(dataset)
    preview = publish_combo_pair_facet(dataset=dataset, decisions=[{"x": 1}], write=False)
    assert preview["written"] is False
    assert not (dataset / "combo_pair_decisions.jsonl").exists()

    written = publish_combo_pair_facet(dataset=dataset, decisions=[{"x": 1}], write=True)
    assert written["file_sha256"]["combo_pair_decisions.jsonl"]
    manifest = json.loads((dataset / "manifest.json").read_text())
    assert manifest["combo_pair_facet"]["completeness"]["decision_count"] == 1
    analysis = analyze_shadow_replay_dataset(dataset=dataset, min_sample=1)
    assert analysis["combo_pair_analysis"]["decision_count"] == 1
    assert analysis["combo_pair_analysis"]["production_config_patch"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_estimated_option_chain_calls", 0),
        ("max_entry_quote_age_seconds", 0),
        ("max_entry_leg_skew_seconds", -1),
    ],
)
def test_variant_spec_rejects_invalid_safety_bounds(field: str, value: int) -> None:
    payload = _spec()
    payload[field] = value
    with pytest.raises(ValueError):
        normalize_combo_variant_spec(payload)


def test_combo_capture_preview_is_read_only_and_write_hashes_union(
    tmp_path,
    monkeypatch,
) -> None:
    from src.application.shadow_replay import combo_capture

    spec_path = tmp_path / "variants.json"
    spec_path.write_text(json.dumps(_capture_spec()))
    dataset_root = tmp_path / "datasets"
    monkeypatch.setattr(
        combo_capture,
        "load_runtime_config",
        lambda **_kwargs: (
            tmp_path / "config.us.json",
            {"runtime": {}, "portfolio": {"futu": {}}},
        ),
    )
    monkeypatch.setattr(
        combo_capture,
        "build_account_runtime_config",
        lambda **kwargs: kwargs["base_cfg"],
    )
    monkeypatch.setattr(
        combo_capture,
        "_resolved_symbol_configs",
        lambda *_args, **_kwargs: {
            "NVDA": {
                "symbol": "NVDA",
                "sell_put": {},
                "combo_yield": {"enabled": False, "structure_mode": "same_expiry_pair"},
            }
        },
    )
    monkeypatch.setattr(
        combo_capture,
        "build_prefetch_budget_plan",
        lambda *_args, **_kwargs: SimpleNamespace(
            estimated_option_chain_calls=3,
            summary=lambda: {"estimated_option_chain_calls": 3, "waves": []},
        ),
    )

    preview = capture_combo_variants(
        repo_root=tmp_path,
        config_key="us",
        account="lx",
        symbols=["NVDA"],
        variant_spec_path=spec_path,
        dataset_root=dataset_root,
        dataset_id="preview",
        plan_builder=_fake_capture_plan,
    )
    assert preview["written"] is False
    assert preview["planned_call_expirations_by_variant"]["NVDA"]["same-d20"] == [
        "2026-08-21"
    ]
    assert not (dataset_root / "preview").exists()

    def fetch_executor(**_kwargs):
        return {
            "symbol": "NVDA",
            "spot": 110,
            "expirations": ["2026-08-21", "2026-09-18"],
            "rows": [
                {
                    "symbol": "NVDA",
                    "option_type": "put",
                    "expiration": "2026-08-21",
                    "dte": 23,
                    "contract_symbol": "NVDA-P100",
                    "strike": 100,
                    "spot": 110,
                    "bid": 5,
                    "ask": 5.2,
                },
                {
                    "symbol": "NVDA",
                    "option_type": "call",
                    "expiration": "2026-08-21",
                    "dte": 23,
                    "contract_symbol": "NVDA-C120-A",
                    "strike": 120,
                    "spot": 110,
                    "bid": 0.9,
                    "ask": 1,
                },
                {
                    "symbol": "NVDA",
                    "option_type": "call",
                    "expiration": "2026-09-18",
                    "dte": 51,
                    "contract_symbol": "NVDA-C120-B",
                    "strike": 120,
                    "spot": 110,
                    "bid": 1.9,
                    "ask": 2,
                },
            ],
            "meta": {"source": "test"},
        }

    written = capture_combo_variants(
        repo_root=tmp_path,
        config_key="us",
        account="lx",
        symbols=["NVDA"],
        variant_spec_path=spec_path,
        dataset_root=dataset_root,
        dataset_id="written",
        write=True,
        plan_builder=_fake_capture_plan,
        fetch_executor=fetch_executor,
    )
    assert written["variant_completeness"] == [
        {
            "variant_id": "same-d20",
            "status": "complete",
            "missing_expirations_or_contracts": [],
        },
    ]
    assert written["required_data_file_sha256"]
    assert written["source_quote_observations"]["NVDA"]
    assert (dataset_root / "written" / "manifest.json").is_file()


def test_combo_capture_fails_before_write_when_budget_exceeds_authored_cap(
    tmp_path,
    monkeypatch,
) -> None:
    from src.application.shadow_replay import combo_capture

    spec = _capture_spec()
    spec["max_estimated_option_chain_calls"] = 2
    spec_path = tmp_path / "variants.json"
    spec_path.write_text(json.dumps(spec))
    monkeypatch.setattr(
        combo_capture,
        "load_runtime_config",
        lambda **_kwargs: (tmp_path / "config.us.json", {"runtime": {}, "portfolio": {}}),
    )
    monkeypatch.setattr(
        combo_capture,
        "build_account_runtime_config",
        lambda **kwargs: kwargs["base_cfg"],
    )
    monkeypatch.setattr(
        combo_capture,
        "_resolved_symbol_configs",
        lambda *_args, **_kwargs: {
            "NVDA": {"symbol": "NVDA", "sell_put": {}, "combo_yield": {}}
        },
    )
    monkeypatch.setattr(
        combo_capture,
        "build_prefetch_budget_plan",
        lambda *_args, **_kwargs: SimpleNamespace(
            estimated_option_chain_calls=3,
            summary=lambda: {"estimated_option_chain_calls": 3},
        ),
    )
    with pytest.raises(ValueError, match="exceeds authored cap"):
        capture_combo_variants(
            repo_root=tmp_path,
            config_key="us",
            account="lx",
            symbols=["NVDA"],
            variant_spec_path=spec_path,
            dataset_root=tmp_path / "datasets",
            dataset_id="over-budget",
            write=True,
            plan_builder=_fake_capture_plan,
        )
    assert not (tmp_path / "datasets" / "over-budget").exists()


def test_combo_settlement_models_assignment_capital_at_same_expiry() -> None:
    decision = {
        **_pair(retention=0.80),
        "shadow_combo_pair_id": "pair-1",
        "dataset_id": "dataset-1",
        "account": "lx",
        "entry_observed_at_utc": "2026-07-29T00:00:00Z",
        "stock_liquidation_fee": 0,
        "baseline_selected": False,
        "variant_decisions": [{"variant_id": "same-d20", "selected": True}],
    }
    marks = [
        {
            "shadow_combo_pair_id": "pair-1",
            "horizon": "put_expiry",
            "leg_role": "underlying",
            "marked_at_utc": "2026-08-21T00:00:00Z",
            "spot": 90,
            "mark_quality": "settlement",
            "settlement_authority": True,
        },
        {
            "shadow_combo_pair_id": "pair-1",
            "horizon": "put_expiry",
            "leg_role": "participation_call",
            "marked_at_utc": "2026-08-21T00:00:00Z",
            "bid": 0.4,
            "future_close_fee": 0,
            "mark_quality": "usable",
        },
    ]
    outcome = settle_combo_pair_outcomes(decisions=[decision], marks=marks)[0]
    assert outcome["evidence_status"] == "complete"
    assert outcome["put_assignment_state"] == "assigned_stock"
    assert outcome["post_put_expiry_state"] == "terminal"
    assert outcome["put_pnl"] == -500
    assert outcome["call_pnl"] == -100
    assert outcome["assigned_stock_continuation_pnl"] == 0
    assert outcome["full_shadow_group_pnl"] == -600
    assert outcome["funding_horizon_pnl"] == -600
    assert outcome["capital_days"]["assigned_stock_capital_days"] == 0
    assert outcome["early_assignment_stress_status"] == "incomplete"


def test_combo_settlement_fails_closed_and_scorecards_use_identical_complete_instances() -> None:
    decision = {
        **_pair(),
        "shadow_combo_pair_id": "pair-1",
        "dataset_id": "dataset-1",
        "account": "lx",
        "entry_observed_at_utc": "2026-07-29T00:00:00Z",
        "stock_liquidation_fee": 0,
        "baseline_selected": True,
        "variant_decisions": [{"variant_id": "same-d20", "selected": True}],
    }
    outcomes = settle_combo_pair_outcomes(decisions=[decision], marks=[])
    assert {row["evidence_status"] for row in outcomes} == {"unavailable"}
    assert "authoritative_put_expiry_spot_missing" in outcomes[0]["unavailable_reasons"]

    scorecards = build_combo_variant_scorecards(
        [
            {
                **outcomes[0],
                "selector": "baseline",
                "evidence_status": "complete",
                "full_shadow_group_pnl": 10,
                "maximum_observed_drawdown": -5,
            },
            outcomes[1],
        ]
    )
    assert {card["identical_complete_instance_count"] for card in scorecards} == {0}


def test_combo_evaluation_keeps_baseline_and_proposed_authorities_separate(tmp_path) -> None:
    dataset = tmp_path / "dataset"
    required = dataset / "required_data" / "parsed"
    required.mkdir(parents=True)
    required_file = required / "NVDA_required_data.csv"
    required_file.write_text("captured-bytes\n")
    relative = str(required_file.relative_to(dataset))
    digest = hashlib.sha256(required_file.read_bytes()).hexdigest()
    manifest = {
        "schema_version": "shadow_combo_capture_manifest.v1",
        "dataset_id": "dataset-1",
        "capture_observed_at_utc": "2026-07-29T01:00:01Z",
        "account": "lx",
        "symbols": ["NVDA"],
        "normalized_effective_combo_policy": {
            "NVDA": {
                "enabled": True,
                "structure_mode": "same_expiry_pair",
                "min_net_credit_annualized": 0.05,
                "call": {"min_delta": 0.15, "max_delta": 0.25},
            }
        },
        "normalized_sell_put_policy": {"NVDA": {}},
        "normalized_global_combo_liquidity": {"NVDA": {}},
        "effective_combo_policy_hash": "policy-hash",
        "normalized_variant_spec": _capture_spec(),
        "variant_spec_hash": "variant-hash",
        "required_data_file_sha256": {relative: digest},
        "variant_completeness": [
            {"variant_id": "same-d20", "status": "complete"},
        ],
        "source_quote_observations": {
            "NVDA": [
                {
                    "option_types": ["put", "call"],
                    "expirations": ["2026-08-21"],
                    "observed_at_utc": "2026-07-29T01:00:00Z",
                }
            ]
        },
    }
    (dataset / "manifest.json").write_text(json.dumps(manifest))

    def pair_builder(**kwargs):
        cfg = kwargs["yield_enhancement_cfg"]
        is_superset = "_explicit_fields" in cfg
        if not is_superset:
            calls = [("NVDA-CBASE", 0.24, "2026-08-21")]
        else:
            calls = [
                ("NVDA-C20", 0.20, "2026-08-21"),
                ("NVDA-C25", 0.25, "2026-08-21"),
            ]
        return __import__("pandas").DataFrame(
            [
                {
                    **_pair(
                        call=contract,
                        delta=delta,
                        retention=0.85,
                    ),
                    "call_expiration": expiration,
                    "annualized_net_credit_yield": 0.10,
                }
                for contract, delta, expiration in calls
            ]
        )

    result = evaluate_combo_variant_pairs(
        dataset=dataset,
        funding_put_rows=[
            {
                "symbol": "NVDA",
                "contract_symbol": "NVDA-P100",
                "annualized_net_return_on_cash_basis": 0.20,
                "period_net_return_on_cash_basis": 0.02,
                "net_assignment_discount_pct": 0.10,
                "symbol_concentration_after": 0.20,
                "spread_ratio": 0.10,
                "open_interest": 100,
                "net_income": 500,
                "snapshot_received_at_utc": "2026-07-29T01:00:00Z",
                "spot_observed_at_utc": "2026-07-29T01:00:00Z",
            }
        ],
        pair_builder=pair_builder,
    )
    baseline = [row for row in result["decisions"] if row["baseline_selected"]]
    assert [row["call_contract_symbol"] for row in baseline] == ["NVDA-CBASE"]
    proposed = {
        item["variant_id"]: row["call_contract_symbol"]
        for row in result["decisions"]
        for item in row["variant_decisions"]
        if item["selected"]
    }
    assert proposed == {
        "same-d20": "NVDA-C20",
    }


def test_combo_funding_put_preparation_binds_canonical_output_to_capture(
    tmp_path,
) -> None:
    from src.application.shadow_replay.combo_variants import COMBO_PAIR_DATASET_FILES

    dataset = tmp_path / "dataset"
    dataset.mkdir()
    for name in DATASET_FILES + COMBO_PAIR_DATASET_FILES:
        (dataset / name).write_text("")
    manifest = {
        "schema_version": "shadow_combo_capture_manifest.v1",
        "dataset_id": "dataset-1",
        "market": "us",
        "account": "lx",
        "symbols": ["NVDA"],
        "normalized_effective_combo_policy": {
            "NVDA": {"enabled": True, "structure_mode": "same_expiry_pair"}
        },
        "normalized_sell_put_policy": {"NVDA": {}},
        "normalized_global_combo_liquidity": {"NVDA": {}},
        "normalized_global_sell_put_liquidity": {"NVDA": {}},
        "normalized_global_sell_put_event_risk": {"NVDA": {}},
        "required_data_file_sha256": {"required_data/raw/NVDA.json": "abc"},
    }
    (dataset / "manifest.json").write_text(json.dumps(manifest))
    refresh_dataset_manifest(dataset)
    runs_root = tmp_path / "runtime" / "output_runs"
    _publish_combo_funding_source(runs_root.parent)
    preview = prepare_combo_funding_puts(
        dataset=dataset,
        source_run_id="run-1",
        source_runs_root=runs_root,
    )
    assert preview["written"] is False
    assert not (dataset / COMBO_FUNDING_PUT_FILE).exists()

    result = prepare_combo_funding_puts(
        dataset=dataset,
        source_run_id="run-1",
        source_runs_root=runs_root,
        write=True,
    )
    source = dataset / COMBO_FUNDING_PUT_FILE
    assert result["row_count"] == 2
    assert result["accepted_row_count"] == 1
    receipt = validate_combo_funding_put_source(
        dataset=dataset,
        funding_put_path=source,
    )
    assert receipt["projection"] == "manifest_bound_combo_v2_terminal_decisions"
    assert receipt["source_run_id"] == "run-1"

    adopted = prepare_combo_funding_puts(
        dataset=dataset,
        source_run_id="run-1",
        source_runs_root=runs_root,
        write=True,
    )
    assert adopted["funding_put_jsonl_sha256"] == result["funding_put_jsonl_sha256"]

    source.write_text(source.read_text() + "\n")
    with pytest.raises(ValueError, match="integrity mismatch"):
        validate_combo_funding_put_source(
            dataset=dataset,
            funding_put_path=source,
        )


def test_combo_funding_put_validation_binds_receipt_to_manifest_facet(
    tmp_path,
) -> None:
    dataset = _combo_evaluation_dataset(tmp_path)
    source = dataset / COMBO_FUNDING_PUT_FILE
    receipt_path = dataset / "combo_owned_funding_put_decisions.v1.source.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["source_run_id"] = "forged-run"
    receipt_path.write_text(json.dumps(receipt))

    with pytest.raises(ValueError, match="manifest facet mismatch"):
        validate_combo_funding_put_source(
            dataset=dataset,
            funding_put_path=source,
        )


def test_combo_funding_put_validation_rejects_synchronized_jsonl_receipt_tamper(
    tmp_path,
) -> None:
    dataset = _combo_evaluation_dataset(tmp_path)
    source = dataset / COMBO_FUNDING_PUT_FILE
    receipt_path = dataset / "combo_owned_funding_put_decisions.v1.source.json"
    rows = [json.loads(line) for line in source.read_text().splitlines() if line]
    rows[0]["accepted"] = not rows[0]["accepted"]
    rows[0]["opening_decision"]["accepted"] = rows[0]["accepted"]
    source.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    )
    receipt = json.loads(receipt_path.read_text())
    receipt["funding_put_jsonl_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    receipt["accepted_row_count"] = sum(row["accepted"] is True for row in rows)
    receipt_path.write_text(json.dumps(receipt))

    with pytest.raises(ValueError, match="integrity mismatch"):
        validate_combo_funding_put_source(
            dataset=dataset,
            funding_put_path=source,
        )


def test_combo_evaluation_uses_only_accepted_sealed_funding_puts(
    tmp_path,
    monkeypatch,
) -> None:
    dataset = _combo_evaluation_dataset(tmp_path)
    source = dataset / COMBO_FUNDING_PUT_FILE
    captured: list[dict] = []

    def fake_evaluation(**kwargs):
        captured.extend(kwargs["funding_put_rows"])
        return {"decision_count": 0}

    from src.application.shadow_replay import combo_evaluation

    monkeypatch.setattr(
        combo_evaluation,
        "evaluate_combo_variant_pairs",
        fake_evaluation,
    )
    result = evaluate_combo_variant_dataset(
        dataset=dataset,
        funding_put_path=source,
    )

    assert result["decision_count"] == 0
    assert {row["contract_symbol"] for row in captured} == {"NVDA-P100"}


def _combo_evaluation_dataset(tmp_path: Path) -> Path:
    from src.application.shadow_replay.combo_variants import COMBO_PAIR_DATASET_FILES

    dataset = tmp_path / "evaluation-dataset"
    dataset.mkdir()
    for name in DATASET_FILES + COMBO_PAIR_DATASET_FILES:
        (dataset / name).write_text("")
    raw = dataset / "required_data" / "raw" / "NVDA.json"
    raw.parent.mkdir(parents=True)
    raw.write_text("{}")
    manifest = {
        "schema_version": "shadow_combo_capture_manifest.v1",
        "dataset_id": "dataset-1",
        "capture_observed_at_utc": "2026-08-12T01:00:01Z",
        "market": "us",
        "account": "lx",
        "symbols": ["NVDA"],
        "normalized_effective_combo_policy": {
            "NVDA": {"enabled": True, "structure_mode": "same_expiry_pair"}
        },
        "normalized_sell_put_policy": {"NVDA": {}},
        "normalized_global_combo_liquidity": {"NVDA": {}},
        "effective_combo_policy_hash": "policy-hash",
        "normalized_variant_spec": _capture_spec(),
        "variant_spec_hash": "variant-hash",
        "required_data_file_sha256": {
            str(raw.relative_to(dataset)): hashlib.sha256(raw.read_bytes()).hexdigest()
        },
        "variant_completeness": [{"variant_id": "same-d20", "status": "complete"}],
        "source_quote_observations": {"NVDA": []},
    }
    (dataset / "manifest.json").write_text(json.dumps(manifest))
    refresh_dataset_manifest(dataset)
    runtime = tmp_path / "evaluation-runtime"
    _publish_combo_funding_source(runtime)
    prepare_combo_funding_puts(
        dataset=dataset,
        source_run_id="run-1",
        source_runs_root=runtime / "output_runs",
        write=True,
    )
    return dataset


def _publish_combo_funding_source(base: Path) -> None:
    account_dir = base / "output_runs" / "run-1" / "accounts" / "lx"
    (account_dir / "state").mkdir(parents=True, exist_ok=True)
    (account_dir / "nvda_combo_yield_candidates.csv").write_text("symbol\n")
    publish_strategy_scan_status(
        report_dir=account_dir,
        run_id="run-1",
        account="lx",
        market="US",
        symbol="NVDA",
        strategy_family="combo_yield",
        status="completed",
        candidate_count=0,
        snapshot_id="quote-1",
        receipt_relpath="quotes/quote-1/receipt.json",
    )
    publish_strategy_scan_status_index_v2(
        report_dir=account_dir,
        run_id="run-1",
        account="lx",
        account_config_sha256="a" * 64,
        expected=[
            {
                "market": "US",
                "symbol": "NVDA",
                "strategy_family": "combo_yield",
                "strategy_mode": "combo_yield",
                "candidate_owner": "sp_lc",
                "account_config_sha256": "a" * 64,
            }
        ],
    )
    decisions = []
    for contract, accepted in (("NVDA-P100", True), ("NVDA-P90", False)):
        normalized = {
            "symbol": "NVDA",
            "contract_symbol": contract,
            "expiration": "2026-08-21",
            "strike": 100.0 if accepted else 90.0,
            "annualized_net_return_on_cash_basis": 0.20,
            "period_net_return_on_cash_basis": 0.02,
            "net_assignment_discount_pct": 0.10,
            "spread_ratio": 0.10,
            "open_interest": 100,
            "net_income": 500,
            "snapshot_received_at_utc": "2026-08-12T01:00:00Z",
            "spot_observed_at_utc": "2026-08-12T01:00:00Z",
        }
        decisions.append(
            {
                "normalized_input": normalized,
                "opening_decision": build_candidate_decision(
                    mode="put",
                    symbol="NVDA",
                    contract_symbol=contract,
                    accepted=accepted,
                    rejects=[] if accepted else [{"stage": "stage3_risk_filter", "reason": "policy_rejected"}],
                    normalized_input=normalized,
                ),
            }
        )
    seal_combo_yield_candidate_snapshot(
        base=base,
        run_id="run-1",
        account="lx",
        market="us",
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        dependencies=[
            {"kind": kind, "relpath": None, "sha256": character * 64}
            for kind, character in (
                ("required_data", "1"),
                ("portfolio", "2"),
                ("ledger", "3"),
                ("fx", "4"),
                ("earnings_rv", "5"),
            )
        ],
        scan_statuses=[
            {
                "symbol": "NVDA",
                "strategy_mode": "combo_yield",
                "variant": "sp_lc",
                "status": "completed",
                "quote_snapshot_id": "quote-1",
                "quote_receipt_relpath": "quotes/quote-1/receipt.json",
            }
        ],
        funding_put_decisions=decisions,
        ranked_pairs=[],
        sealed_at="2026-08-12T01:00:00Z",
    )
    publish_candidate_snapshot_manifest(
        base=base,
        run_id="run-1",
        account="lx",
        strategy_policy_sha256="b" * 64,
        sealed_at="2026-08-12T01:00:01Z",
    )
