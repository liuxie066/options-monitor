from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.position_advice_authority import scope_for
from domain.domain.position_advice_promotion import (
    REQUIRED_CRITICAL_REPLAY_FIXTURES,
    SAFETY_METRICS,
)
from src.application.position_advice_authority_service import (
    apply_authority_change,
    build_identity_binding_evidence,
)
from src.application.position_advice_promotion import (
    PositionAdvicePromotionError,
    build_position_advice_promotion_evidence,
    publish_position_advice_promotion_evidence,
)


NOW = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
IDENTITY = "a" * 64
POLICY_HASH = "b" * 64
GENERATION = 1


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _source_manifest() -> list[dict[str, object]]:
    kinds = (
        "quotes",
        "candidate_decisions",
        "portfolio",
        "ledger_decision_state",
        "cash_capacity",
        "share_coverage",
        "fx",
    )
    return [
        {
            "source_kind": kind,
            "snapshot_id": f"{index + 1:x}" * 64,
            "receipt_hash": f"{index + 8:x}" * 64,
            "payload_sha256": f"{index + 1:x}" * 64,
            "source_observed_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(days=30)).isoformat(),
            "dependencies": [],
            "capacity_pool_authority_id": None,
        }
        for index, kind in enumerate(kinds)
    ]


def _plan(
    root: Path,
    *,
    index: int,
    selected: bool,
    alternative: bool,
    decision_fingerprint: str | None = None,
    authority_generation: int = GENERATION,
    authority_policy_hash: str = POLICY_HASH,
    included_markets: list[str] | None = None,
    checked_at: datetime | None = None,
) -> Path:
    run_id = f"run-{index:02d}"
    account_root = root / "output_runs" / run_id / "accounts" / "lx"
    source_manifest = _source_manifest()
    source_manifest_hash = canonical_sha256(
        {"source_manifest": source_manifest}
    )
    fingerprint = decision_fingerprint or f"{index + 1:064x}"
    immutable_input: dict[str, object] = {
        "schema_version": "position_advice_input.v2",
        "account_run_id": run_id,
        "portfolio_scope_id": scope_for("lx"),
        "portfolio_account_identity_hash": IDENTITY,
        "authority_mode": "v2_shadow",
        "authority_generation": authority_generation,
        "authority_policy_hash": authority_policy_hash,
        "decision_state_fingerprint": fingerprint,
        "source_manifest_hash": source_manifest_hash,
        "economic_inputs": {"fees": "v1", "index": index},
    }
    immutable_input["input_hash"] = canonical_sha256(immutable_input)
    proposal = {
        "proposal_id": f"proposal-{index}",
        "source_position_ids": [f"lot-{index}"],
        "candidate_id": f"candidate-{index}",
        "candidate_contracts": 1,
        "resource_deltas": [
            {
                "resource_kind": "cash_base_cny",
                "pool_key": f"cash:{scope_for('lx')}:{'c' * 64}",
                "unit": "CNY",
                "released": "100",
                "required": "90",
            }
        ],
        "current_daily_carry_base_cny": "1",
        "candidate_daily_carry_base_cny": "2",
        "friction_base_cny": "1",
        "comparison_horizon_days": "10",
        "net_carry_improvement_H_base_cny": "9",
        "pool_efficiency_improvement": "0.01",
        "replacement_eligibility": (
            "capacity_deferred_to_allocator" if index < 3 else "accepted_opening"
        ),
        "risk_eligibility_status": "accepted",
    }
    advice_checked_at = checked_at or NOW + timedelta(days=index % 15)
    plan: dict[str, object] = {
        "schema_version": "position_advice.output.v2",
        "account_run_id": run_id,
        "normalized_account": "lx",
        "included_markets": included_markets or ["US"],
        "portfolio_scope_id": scope_for("lx"),
        "portfolio_account_identity_hash": IDENTITY,
        "authority_mode": "v2_shadow",
        "authority_generation": authority_generation,
        "authority_policy_hash": authority_policy_hash,
        "decision_state_fingerprint": fingerprint,
        "decision_snapshot_status": "trusted",
        "input_hash": immutable_input["input_hash"],
        "source_manifest_hash": source_manifest_hash,
        "source_manifest": source_manifest,
        "freshness": {"status": "fresh"},
        "advice_checked_at": advice_checked_at.isoformat(),
        "portfolio_plan_id": f"portfolio-plan-{index}",
        "selected_proposals": [proposal] if selected else [],
        "alternative_proposals": [proposal] if alternative else [],
        "rows": [
            {
                "position_id": f"lot-{index}",
                "strategy_family": "short_put",
                "lifecycle_state": "open",
                "reason_codes": (
                    []
                    if selected or alternative
                    else ["no_economically_eligible_replacement"]
                ),
            }
        ],
    }
    plan["artifact_hash"] = canonical_sha256(plan)
    _write_json(
        account_root / "state" / "position_advice_input.v2.json",
        immutable_input,
    )
    advice_path = account_root / "position_advice.v2.json"
    _write_json(advice_path, plan)
    return advice_path


def _plans(
    root: Path,
    *,
    authority_generation: int = GENERATION,
    authority_policy_hash: str = POLICY_HASH,
) -> list[Path]:
    return [
        _plan(
            root,
            index=index,
            selected=index < 5,
            alternative=5 <= index < 10,
            authority_generation=authority_generation,
            authority_policy_hash=authority_policy_hash,
        )
        for index in range(30)
    ]


def _binding() -> dict[str, object]:
    return build_identity_binding_evidence(
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        authoring_config_hash="d" * 64,
        market_bindings=[
            {
                "market": "US",
                "generated_config_hash": "e" * 64,
                "source_receipt_hash": "f" * 64,
                "normalized_account": "lx",
                "normalized_portfolio_source": "futu",
                "portfolio_account_identity_hash": IDENTITY,
                "source_receipt_fresh": True,
            }
        ],
    )


def test_promotion_aggregator_builds_non_vacuous_passing_evidence(
    tmp_path: Path,
) -> None:
    evidence = build_position_advice_promotion_evidence(
        plan_paths=_plans(tmp_path),
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        authority_generation=GENERATION,
        authority_policy_hash=POLICY_HASH,
        covered_strategy_families=["short_put"],
        safety={metric: 0 for metric in SAFETY_METRICS},
        critical_replay_fixtures={
            name: True for name in REQUIRED_CRITICAL_REPLAY_FIXTURES
        },
        generated_at=NOW + timedelta(days=15),
    )

    assert evidence["authority_mode"] == "v2_shadow"
    assert len(evidence["opportunities"]) == 30
    assert evidence["reason_distribution"]["selected"] == 5
    assert evidence["realized_outcome"]["status"] == "unknown"
    assert (
        evidence["economic"]["modeled_daily_carry_uplift_base_cny"]
        == "4.5"
    )


def test_promotion_aggregator_deduplicates_repeated_facts_and_rejects_drift(
    tmp_path: Path,
) -> None:
    paths = _plans(tmp_path)
    duplicate = _plan(
        tmp_path,
        index=30,
        selected=True,
        alternative=False,
        decision_fingerprint=f"{1:064x}",
    )
    duplicate_payload = json.loads(duplicate.read_text())
    for index, source in enumerate(
        duplicate_payload["source_manifest"],
        start=1,
    ):
        source["receipt_hash"] = canonical_sha256(
            {"republished_by_run": "run-30", "source_index": index}
        )
    duplicate_payload["source_manifest_hash"] = canonical_sha256(
        {"source_manifest": duplicate_payload["source_manifest"]}
    )
    duplicate_input_path = (
        duplicate.parent / "state" / "position_advice_input.v2.json"
    )
    duplicate_input = json.loads(duplicate_input_path.read_text())
    duplicate_input["source_manifest_hash"] = duplicate_payload[
        "source_manifest_hash"
    ]
    duplicate_input.pop("input_hash")
    duplicate_input["economic_inputs"] = {"fees": "v1", "index": 0}
    duplicate_input["input_hash"] = canonical_sha256(duplicate_input)
    duplicate_payload["input_hash"] = duplicate_input["input_hash"]
    duplicate_payload["selected_proposals"][0]["proposal_id"] = "proposal-0"
    duplicate_payload["selected_proposals"][0][
        "source_position_ids"
    ] = ["lot-0"]
    duplicate_payload["selected_proposals"][0]["candidate_id"] = "candidate-0"
    duplicate_payload["selected_proposals"][0][
        "replacement_eligibility"
    ] = "capacity_deferred_to_allocator"
    duplicate_payload["rows"][0]["position_id"] = "lot-0"
    duplicate_payload.pop("artifact_hash")
    duplicate_payload["artifact_hash"] = canonical_sha256(duplicate_payload)
    _write_json(duplicate_input_path, duplicate_input)
    _write_json(duplicate, duplicate_payload)

    evidence = build_position_advice_promotion_evidence(
        plan_paths=[*paths, duplicate],
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        authority_generation=GENERATION,
        authority_policy_hash=POLICY_HASH,
        covered_strategy_families=["short_put"],
        safety={metric: 0 for metric in SAFETY_METRICS},
        critical_replay_fixtures={
            name: True for name in REQUIRED_CRITICAL_REPLAY_FIXTURES
        },
        generated_at=NOW + timedelta(days=15),
    )
    assert len(evidence["opportunities"]) == 30

    duplicate_payload["authority_generation"] = 2
    duplicate_payload.pop("artifact_hash")
    duplicate_payload["artifact_hash"] = canonical_sha256(duplicate_payload)
    _write_json(duplicate, duplicate_payload)
    with pytest.raises(PositionAdvicePromotionError, match="authority mismatch"):
        build_position_advice_promotion_evidence(
            plan_paths=[duplicate],
            normalized_account="lx",
            normalized_portfolio_source="futu",
            portfolio_account_identity_hash=IDENTITY,
            authority_generation=GENERATION,
            authority_policy_hash=POLICY_HASH,
            covered_strategy_families=["short_put"],
            safety={metric: 0 for metric in SAFETY_METRICS},
            critical_replay_fixtures={},
            generated_at=NOW,
        )


def test_promotion_publish_binds_current_shadow_policy_and_writes_once(
    tmp_path: Path,
) -> None:
    applied = apply_authority_change(
        base=tmp_path,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        target_mode="v2_shadow",
        expected_policy_hash="absent",
        actor="operator@example",
        requested_at=NOW,
        confirm=True,
        identity_binding_evidence=_binding(),
    )
    policy = dict(applied["policy"])
    result = publish_position_advice_promotion_evidence(
        base=tmp_path,
        plan_paths=_plans(
            tmp_path,
            authority_generation=int(policy["generation"]),
            authority_policy_hash=str(policy["policy_hash"]),
        ),
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        covered_strategy_families=["short_put"],
        safety={metric: 0 for metric in SAFETY_METRICS},
        critical_replay_fixtures={
            name: True for name in REQUIRED_CRITICAL_REPLAY_FIXTURES
        },
        generated_at=NOW + timedelta(days=15),
    )

    assert result["status"] == "pass"
    assert Path(result["evidence_path"]).is_file()
    assert Path(result["gate_path"]).is_file()


def test_promotion_publish_rejects_plan_outside_runtime_root(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "output_runs").mkdir()
    applied = apply_authority_change(
        base=runtime,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        target_mode="v2_shadow",
        expected_policy_hash="absent",
        actor="operator@example",
        requested_at=NOW,
        confirm=True,
        identity_binding_evidence=_binding(),
    )
    policy = dict(applied["policy"])
    outside_plan = _plan(
        tmp_path / "outside",
        index=0,
        selected=True,
        alternative=False,
        authority_generation=int(policy["generation"]),
        authority_policy_hash=str(policy["policy_hash"]),
    )

    with pytest.raises(PositionAdvicePromotionError, match="escapes runtime"):
        publish_position_advice_promotion_evidence(
            base=runtime,
            plan_paths=[outside_plan],
            normalized_account="lx",
            normalized_portfolio_source="futu",
            portfolio_account_identity_hash=IDENTITY,
            covered_strategy_families=["short_put"],
            safety={metric: 0 for metric in SAFETY_METRICS},
            critical_replay_fixtures={
                name: True for name in REQUIRED_CRITICAL_REPLAY_FIXTURES
            },
            generated_at=NOW + timedelta(days=15),
        )


def test_promotion_sessions_use_exchange_local_dates(tmp_path: Path) -> None:
    path = _plan(
        tmp_path,
        index=0,
        selected=True,
        alternative=False,
        included_markets=["US", "HK"],
        checked_at=datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc),
    )

    evidence = build_position_advice_promotion_evidence(
        plan_paths=[path],
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        authority_generation=GENERATION,
        authority_policy_hash=POLICY_HASH,
        covered_strategy_families=["short_put"],
        safety={metric: 0 for metric in SAFETY_METRICS},
        critical_replay_fixtures={
            name: True for name in REQUIRED_CRITICAL_REPLAY_FIXTURES
        },
        generated_at=NOW + timedelta(days=15),
    )

    assert evidence["market_session_ids"] == [
        "HK:2026-07-01",
        "US:2026-06-30",
    ]
