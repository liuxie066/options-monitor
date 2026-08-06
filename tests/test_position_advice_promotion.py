from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.lifecycle_allocation import (
    allocation_id_for,
    terminal_event_id_for,
)
from domain.domain.position_advice_authority import scope_for
from domain.domain.position_advice_promotion import (
    REQUIRED_CRITICAL_REPLAY_FIXTURES,
    SAFETY_METRICS,
)
from src.application.position_advice_authority_service import (
    apply_authority_change,
    build_identity_binding_evidence,
)
from src.application.position_advice_notification_authority import (
    build_notification_authority_token,
    execute_notification_with_authority,
    resolve_notification_unknown,
)
from src.application.position_advice_promotion import (
    PositionAdvicePromotionError,
    build_position_advice_promotion_evidence,
    position_advice_promotion_status,
    publish_position_advice_promotion_evidence,
    refresh_position_advice_promotion,
)
from src.application.position_advice_current_repository import (
    collect_protected_current_runs_under_global_lock,
)
from src.application.ledger.decision_snapshot import (
    POSITION_FACT_SNAPSHOT_CONTRACT,
    decision_state_snapshot_fingerprint,
)
from src.application.ledger.lifecycle_overlay import (
    resolve_account_lifecycle_overlay,
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
        "opening_candidates",
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
    fingerprint_seed = decision_fingerprint or f"{index + 1:064x}"
    position_lots = [
        {
            "record_id": f"lot-{index}",
            "fields": {"contracts_open": 1},
        }
    ]
    decision_snapshot: dict[str, object] = {
        "schema_version": "decision_state_snapshot.v2",
        "fingerprint_schema_version": "decision_state_fingerprint.v2",
        "position_fact_contract_version": (
            POSITION_FACT_SNAPSHOT_CONTRACT
        ),
        "normalized_account": "lx",
        "snapshot_status": "trusted",
        "actionable": True,
        "decision_state_fingerprint": "",
        "fixture_state_identity": fingerprint_seed,
        "account_position_lots": position_lots,
        "account_lifecycle_cases": [],
        "account_lifecycle_evidence": [],
        "account_lifecycle_evidence_received_at_ms_by_id": {},
        "account_lifecycle_allocations": [],
        "account_lifecycle_source_consumptions": [],
        "account_lifecycle_timing_policies": [],
        "account_lifecycle_resolution": (
            resolve_account_lifecycle_overlay(
                account="lx",
                cases=[],
                evidence=[],
                allocations=[],
                source_claims=[],
                timing_policies=[],
                position_lots=position_lots,
            )
        ),
        "effective_void_event_ids": [],
        "account_combo_identities": [],
        "account_combo_group_memberships": [],
    }
    fingerprint = decision_state_snapshot_fingerprint(
        decision_snapshot
    )
    decision_snapshot["decision_state_fingerprint"] = fingerprint
    immutable_input: dict[str, object] = {
        "schema_version": "position_advice_input.v2",
        "account_run_id": run_id,
        "normalized_account": "lx",
        "normalized_portfolio_source": "futu",
        "portfolio_scope_id": scope_for("lx"),
        "portfolio_account_identity_hash": IDENTITY,
        "authority_mode": "v2_shadow",
        "authority_generation": authority_generation,
        "authority_policy_hash": authority_policy_hash,
        "decision_state_fingerprint": fingerprint,
        "source_manifest_hash": source_manifest_hash,
        "decision_state_snapshot": decision_snapshot,
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
    pool_key = str(proposal["resource_deltas"][0]["pool_key"])
    if alternative:
        proposal["resource_deltas"][0].update(
            {"released": "0", "required": "90"}
        )
        proposal["allocator_reason"] = "portfolio_capacity_conflict"
        proposal["selected"] = False
        proposal["actionable"] = False
        proposal["depends_on"] = []
    elif selected:
        proposal["resource_deltas"][0]["net_after"] = "10"
        proposal.update(
            {
                "selected": True,
                "actionable": True,
                "allocator_reason": "selected",
                "execution_order": 1,
                "depends_on": [],
                "candidate_quantity_before": 1,
                "candidate_quantity_after": 0,
            }
        )
    advice_checked_at = checked_at or NOW + timedelta(days=index % 15)
    plan: dict[str, object] = {
        "schema_version": "position_advice.output.v2",
        "account_run_id": run_id,
        "normalized_account": "lx",
        "included_markets": included_markets or ["US"],
        "portfolio_scope_id": scope_for("lx"),
        "normalized_portfolio_source": "futu",
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
        "resource_pools_before": {
            pool_key: {
                "resource_kind": "cash_base_cny",
                "unit": "CNY",
                "available": "0",
            }
        },
        "resource_pools_after": {
            pool_key: {
                "resource_kind": "cash_base_cny",
                "unit": "CNY",
                "available": "10" if selected else "0",
            }
        },
        "candidate_quantity_before": {
            f"candidate-{index}": 1
        }
        if selected or alternative
        else {},
        "candidate_quantity_after": {
            f"candidate-{index}": 0 if selected else 1
        }
        if selected or alternative
        else {},
        "selected_proposals": [proposal] if selected else [],
        "alternative_proposals": [proposal] if alternative else [],
        "rows": [
            {
                "position_id": f"lot-{index}",
                "strategy_family": "short_put",
                "lifecycle_state": "open",
                "model_actionable": selected,
                "actionable": False,
                "promotion_scope_status": "shadow_evaluation",
                "action_scope": "position" if selected else "none",
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


def _rewrite_position_fact_contract(
    path: Path,
    *,
    legacy: bool,
) -> None:
    input_path = path.parent / "state" / "position_advice_input.v2.json"
    immutable_input = json.loads(input_path.read_text())
    decision_snapshot = immutable_input["decision_state_snapshot"]
    if legacy:
        decision_snapshot.pop("position_fact_contract_version")
    else:
        decision_snapshot.pop("account_lifecycle_cases")
    immutable_input.pop("input_hash")
    immutable_input["input_hash"] = canonical_sha256(immutable_input)
    _write_json(input_path, immutable_input)

    plan = json.loads(path.read_text())
    plan["input_hash"] = immutable_input["input_hash"]
    plan.pop("artifact_hash")
    plan["artifact_hash"] = canonical_sha256(plan)
    _write_json(path, plan)


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
        covered_strategy_families=None,
        safety=None,
        critical_replay_fixtures=None,
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


def test_legacy_v2_position_fact_snapshot_is_promotion_ineligible(
    tmp_path: Path,
) -> None:
    path = _plan(
        tmp_path,
        index=0,
        selected=True,
        alternative=False,
    )
    input_path = path.parent / "state" / "position_advice_input.v2.json"
    immutable_input = json.loads(input_path.read_text())
    immutable_input["decision_state_snapshot"].pop(
        "position_fact_contract_version"
    )
    immutable_input.pop("input_hash")
    immutable_input["input_hash"] = canonical_sha256(immutable_input)
    _write_json(input_path, immutable_input)
    plan = json.loads(path.read_text())
    plan["input_hash"] = immutable_input["input_hash"]
    plan.pop("artifact_hash")
    plan["artifact_hash"] = canonical_sha256(plan)
    _write_json(path, plan)

    with pytest.raises(
        PositionAdvicePromotionError,
        match="position_fact_contract_version_invalid",
    ):
        build_position_advice_promotion_evidence(
            plan_paths=[path],
            normalized_account="lx",
            normalized_portfolio_source="futu",
            portfolio_account_identity_hash=IDENTITY,
            authority_generation=GENERATION,
            authority_policy_hash=POLICY_HASH,
            covered_strategy_families=None,
            safety=None,
            critical_replay_fixtures={
                name: True
                for name in REQUIRED_CRITICAL_REPLAY_FIXTURES
            },
            generated_at=NOW,
        )


def test_refresh_waits_when_only_legacy_position_fact_source_is_available(
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
    path = _plan(
        tmp_path,
        index=0,
        selected=True,
        alternative=False,
        authority_generation=int(policy["generation"]),
        authority_policy_hash=str(policy["policy_hash"]),
    )
    _rewrite_position_fact_contract(path, legacy=True)

    result = refresh_position_advice_promotion(
        base=tmp_path,
        normalized_account="lx",
        confirm=True,
    )

    assert result["status"] == "waiting_for_compatible_shadow_plans"
    assert result["reason_codes"] == [
        "current_contract_shadow_plan_set_empty"
    ]
    assert result["source_plan_count"] == 0
    assert result["discovered_source_plan_count"] == 1
    assert result["compatible_source_plan_count"] == 0
    assert result["incompatible_source_plan_count"] == 1
    assert result["published"] is False
    assert len(
        list(
            (
                tmp_path
                / "output_shared"
                / "state"
                / "position_advice"
                / scope_for("lx")
                / "promotion_sources"
            ).glob("*/position_advice.v2.json.gz")
        )
    ) == 1


def test_refresh_uses_only_compatible_position_fact_sources(
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
    paths = _plans(
        tmp_path,
        authority_generation=int(policy["generation"]),
        authority_policy_hash=str(policy["policy_hash"]),
    )
    _rewrite_position_fact_contract(paths[0], legacy=True)

    result = refresh_position_advice_promotion(
        base=tmp_path,
        normalized_account="lx",
    )

    assert result["source_plan_count"] == 29
    assert result["discovered_source_plan_count"] == 30
    assert result["compatible_source_plan_count"] == 29
    assert result["incompatible_source_plan_count"] == 1
    assert len(result["evidence"]["opportunities"]) == 29
    assert result["published"] is False


def test_refresh_rejects_malformed_current_position_fact_source(
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
    path = _plan(
        tmp_path,
        index=0,
        selected=True,
        alternative=False,
        authority_generation=int(policy["generation"]),
        authority_policy_hash=str(policy["policy_hash"]),
    )
    _rewrite_position_fact_contract(path, legacy=False)

    with pytest.raises(
        PositionAdvicePromotionError,
        match="position advice input decision facts are invalid",
    ):
        refresh_position_advice_promotion(
            base=tmp_path,
            normalized_account="lx",
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
    original_input_path = (
        paths[0].parent / "state" / "position_advice_input.v2.json"
    )
    original_input = json.loads(original_input_path.read_text())
    duplicate_input["decision_state_snapshot"] = original_input[
        "decision_state_snapshot"
    ]
    duplicate_input["decision_state_fingerprint"] = original_input[
        "decision_state_fingerprint"
    ]
    duplicate_payload["decision_state_fingerprint"] = original_input[
        "decision_state_fingerprint"
    ]
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
        covered_strategy_families=None,
        safety=None,
        critical_replay_fixtures=None,
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


def test_automatic_refresh_is_deterministic_and_status_exposes_final_cas(
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
    _plans(
        tmp_path,
        authority_generation=int(policy["generation"]),
        authority_policy_hash=str(policy["policy_hash"]),
    )

    preview = refresh_position_advice_promotion(
        base=tmp_path,
        normalized_account="lx",
    )
    assert preview["status"] == "pass"
    assert preview["published"] is False
    assert not (
        tmp_path
        / "output_shared"
        / "state"
        / "position_advice"
        / scope_for("lx")
        / "promotion_evidence"
    ).exists()

    published = refresh_position_advice_promotion(
        base=tmp_path,
        normalized_account="lx",
        confirm=True,
    )
    repeated = refresh_position_advice_promotion(
        base=tmp_path,
        normalized_account="lx",
        confirm=True,
    )
    assert published["status"] == "pass"
    assert repeated["promotion_evidence_hash"] == published[
        "promotion_evidence_hash"
    ]

    assert published["evidence"]["automatic_safety_evaluation"][
        "safety"
    ] == {metric: 0 for metric in SAFETY_METRICS}
    assert all(
        published["evidence"]["critical_replay_fixtures"].values()
    )

    status = position_advice_promotion_status(
        base=tmp_path,
        normalized_account="lx",
    )
    assert status["status"] == "pass"
    assert status["ready_for_final_cas"] is True
    assert status["final_cas"]["expected_policy_hash"] == policy[
        "policy_hash"
    ]
    assert status["final_cas"]["evidence_path"] == published[
        "evidence_path"
    ]

    token = build_notification_authority_token(
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        selected_advice_contract="v1",
        resolved_mode="v2_shadow",
        authority_generation=int(policy["generation"]),
        authority_policy_hash=str(policy["policy_hash"]),
        account_run_id="promotion-status-unknown",
    )
    unknown = execute_notification_with_authority(
        base=tmp_path,
        token=token,
        channel="feishu",
        send=lambda: {
            "ok": False,
            "command_ok": True,
            "delivery_confirmed": False,
            "error_code": "SEND_UNCONFIRMED",
            "ambiguous_send": True,
        },
        now=NOW,
    )
    blocked = position_advice_promotion_status(
        base=tmp_path,
        normalized_account="lx",
    )
    assert blocked["status"] == "blocked"
    assert blocked["promotion_gate_status"] == "pass"
    assert blocked["ready_for_final_cas"] is False
    assert (
        "notification_authority_unknown_unresolved"
        in blocked["reason_codes"]
    )
    assert blocked["outstanding_notification_receipt_ids"] == [
        unknown["authority_receipt_id"]
    ]

    resolve_notification_unknown(
        base=tmp_path,
        normalized_account="lx",
        receipt_id=str(unknown["authority_receipt_id"]),
        resolution="failed",
        evidence={"provider_audit_id": "promotion-failed"},
        actor="operator@example",
        resolved_at=NOW,
        confirm=True,
        dry_run=False,
    )
    recovered = position_advice_promotion_status(
        base=tmp_path,
        normalized_account="lx",
    )
    assert recovered["status"] == "pass"
    assert recovered["ready_for_final_cas"] is True
    assert recovered["outstanding_notification_receipt_ids"] == []


def test_refresh_archives_sources_and_allows_ordinary_run_cleanup(
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
    _plans(
        tmp_path,
        authority_generation=int(policy["generation"]),
        authority_policy_hash=str(policy["policy_hash"]),
    )

    preview = refresh_position_advice_promotion(
        base=tmp_path,
        normalized_account="lx",
    )
    archive_root = (
        tmp_path
        / "output_shared"
        / "state"
        / "position_advice"
        / scope_for("lx")
        / "promotion_sources"
    )
    assert preview["status"] == "pass"
    assert not archive_root.exists()
    (archive_root / ".tmp.interrupted").mkdir(parents=True)

    published = refresh_position_advice_promotion(
        base=tmp_path,
        normalized_account="lx",
        confirm=True,
    )
    protected = collect_protected_current_runs_under_global_lock(
        base=tmp_path
    )
    archived_plans = sorted(
        archive_root.glob("*/position_advice.v2.json.gz")
    )
    archived_inputs = sorted(
        archive_root.glob("*/state/position_advice_input.v2.json.gz")
    )

    assert protected == set()
    assert len(archived_plans) == 30
    assert len(archived_inputs) == 30
    assert all(path.read_bytes().startswith(b"\x1f\x8b") for path in archived_plans)
    assert all(path.read_bytes().startswith(b"\x1f\x8b") for path in archived_inputs)

    shutil.rmtree(tmp_path / "output_runs")
    repeated = refresh_position_advice_promotion(
        base=tmp_path,
        normalized_account="lx",
        confirm=True,
    )

    assert repeated["promotion_evidence_hash"] == published[
        "promotion_evidence_hash"
    ]

    archived_plans[0].write_bytes(b"not-gzip")
    with pytest.raises(
        PositionAdvicePromotionError,
        match="promotion source is unreadable",
    ):
        refresh_position_advice_promotion(
            base=tmp_path,
            normalized_account="lx",
            confirm=True,
        )


def test_automatic_safety_counts_shadow_authority_and_allocator_drift(
    tmp_path: Path,
) -> None:
    path = _plan(
        tmp_path,
        index=0,
        selected=True,
        alternative=False,
    )
    plan = json.loads(path.read_text())
    plan["rows"][0]["actionable"] = True
    pool_key = next(iter(plan["resource_pools_after"]))
    plan["resource_pools_after"][pool_key]["available"] = "999"
    plan.pop("artifact_hash")
    plan["artifact_hash"] = canonical_sha256(plan)
    _write_json(path, plan)

    evidence = build_position_advice_promotion_evidence(
        plan_paths=[path],
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        authority_generation=GENERATION,
        authority_policy_hash=POLICY_HASH,
        covered_strategy_families=None,
        safety=None,
        critical_replay_fixtures={
            name: True for name in REQUIRED_CRITICAL_REPLAY_FIXTURES
        },
        generated_at=NOW,
    )

    safety = evidence["safety"]
    assert safety["authority_mixed_exposure"] == 1
    assert safety["allocator_invariant_violation"] == 1


def test_automatic_safety_rejects_actionable_lifecycle_replay_mismatch(
    tmp_path: Path,
) -> None:
    path = _plan(
        tmp_path,
        index=0,
        selected=True,
        alternative=False,
    )
    input_path = path.parent / "state" / "position_advice_input.v2.json"
    immutable_input = json.loads(input_path.read_text())
    snapshot = immutable_input["decision_state_snapshot"]
    lot = snapshot["account_position_lots"][0]
    lot["fields"].update(
        {
            "symbol": "NVDA",
            "expiration_ymd": "2026-06-20",
            "contracts_open": 1,
        }
    )
    case_id = "case-0"
    evidence_id = "evidence-0"
    lot_id = "lot-0"
    snapshot["account_lifecycle_cases"] = [
        {
                "schema_version": "lifecycle_case.v2",
                "case_id": case_id,
                "account": "lx",
                "symbol": "NVDA",
            "expiration_ymd": "2026-06-20",
            "market": "US",
            "status": "open",
            "target_contracts_by_lot": {lot_id: 1},
        }
    ]
    snapshot["account_lifecycle_evidence"] = [
        {"case_id": case_id, "evidence_id": evidence_id}
    ]
    snapshot["account_lifecycle_evidence_received_at_ms_by_id"] = {
        evidence_id: 1
    }
    snapshot["account_lifecycle_allocations"] = [
        {
            "allocation_id": allocation_id_for(
                case_id=case_id,
                evidence_id=evidence_id,
                target_lot_id=lot_id,
            ),
            "case_id": case_id,
            "evidence_id": evidence_id,
            "target_lot_id": lot_id,
            "terminal_type": "assignment",
            "contracts_allocated": 1,
            "canonical_terminal_event_id": terminal_event_id_for(
                case_id=case_id,
                evidence_id=evidence_id,
                target_lot_id=lot_id,
                terminal_type="assignment",
                contracts_allocated=1,
            ),
            }
        ]
    snapshot["account_lifecycle_resolution"] = (
        resolve_account_lifecycle_overlay(
            account="lx",
            cases=snapshot["account_lifecycle_cases"],
            evidence=snapshot["account_lifecycle_evidence"],
            allocations=snapshot["account_lifecycle_allocations"],
            source_claims=[],
            timing_policies=[],
            position_lots=snapshot["account_position_lots"],
        )
    )
    fingerprint = decision_state_snapshot_fingerprint(snapshot)
    snapshot["decision_state_fingerprint"] = fingerprint
    immutable_input["decision_state_fingerprint"] = fingerprint
    immutable_input.pop("input_hash")
    immutable_input["input_hash"] = canonical_sha256(immutable_input)
    _write_json(input_path, immutable_input)
    plan = json.loads(path.read_text())
    plan["decision_state_fingerprint"] = fingerprint
    plan["input_hash"] = immutable_input["input_hash"]
    plan.pop("artifact_hash")
    plan["artifact_hash"] = canonical_sha256(plan)
    _write_json(path, plan)

    evidence = build_position_advice_promotion_evidence(
        plan_paths=[path],
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        authority_generation=GENERATION,
        authority_policy_hash=POLICY_HASH,
        covered_strategy_families=None,
        safety=None,
        critical_replay_fixtures={
            name: True for name in REQUIRED_CRITICAL_REPLAY_FIXTURES
        },
        generated_at=NOW,
    )

    assert (
        evidence["safety"][
            "lifecycle_or_identity_conflict_actionable"
        ]
        == 1
    )


def test_automatic_safety_counts_terminal_combo_and_stale_source_drift(
    tmp_path: Path,
) -> None:
    path = _plan(
        tmp_path,
        index=0,
        selected=True,
        alternative=False,
    )
    plan = json.loads(path.read_text())
    row = plan["rows"][0]
    row["lifecycle_state"] = "assigned"
    row["group_structure_state"] = "active_combo"
    row["action_scope"] = "combo_group"
    for source in plan["source_manifest"]:
        source["expires_at"] = "2026-06-30T00:00:00Z"
    plan.pop("artifact_hash")
    plan["artifact_hash"] = canonical_sha256(plan)
    _write_json(path, plan)

    evidence = build_position_advice_promotion_evidence(
        plan_paths=[path],
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        authority_generation=GENERATION,
        authority_policy_hash=POLICY_HASH,
        covered_strategy_families=None,
        safety=None,
        critical_replay_fixtures={
            name: True for name in REQUIRED_CRITICAL_REPLAY_FIXTURES
        },
        generated_at=NOW,
    )

    safety = evidence["safety"]
    assert safety["false_assignment_confirmation"] == 1
    assert safety["invalid_combo_continuation"] == 1
    assert safety["stale_or_incomplete_actionable_exposure"] == 1
