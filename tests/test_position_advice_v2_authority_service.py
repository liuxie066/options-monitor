from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.position_advice_authority import scope_for
from domain.domain.position_advice_promotion import (
    SAFETY_METRICS,
    evaluate_promotion_gate,
)
from src.application.position_advice_authority_service import (
    PositionAdviceAuthorityError,
    apply_authority_change,
    authority_identity_binding_dir,
    authority_policy_path,
    build_identity_binding_evidence,
    plan_authority_change,
    read_authority_resolution,
)
from src.infrastructure.position_advice_manifest_lock import (
    portfolio_scope_state_dir,
)


NOW = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
IDENTITY = "a" * 64


def _binding(
    *,
    account: str = "lx",
    identity: str = IDENTITY,
    markets: tuple[str, ...] = ("US", "HK"),
) -> dict[str, object]:
    return build_identity_binding_evidence(
        normalized_account=account,
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=identity,
        authoring_config_hash="b" * 64,
        market_bindings=[
            {
                "market": market,
                "generated_config_hash": str(index + 1) * 64,
                "source_receipt_hash": chr(ord("c") + index) * 64,
                "normalized_account": account,
                "normalized_portfolio_source": "futu",
                "portfolio_account_identity_hash": identity,
                "source_receipt_fresh": True,
            }
            for index, market in enumerate(markets)
        ],
    )


def _promotion_evidence(
    *,
    generation: int = 1,
    policy_hash: str = "f" * 64,
) -> dict[str, object]:
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    opportunities: list[dict[str, object]] = []
    for index in range(30):
        opportunities.append(
            {
                "opportunity_key": f"op-{index}",
                "eligible": True,
                "replacement_opportunity": index < 10,
                "selected": index < 5,
                "replacement_eligibility": (
                    "capacity_deferred_to_allocator"
                    if index < 3
                    else "accepted_opening"
                ),
                "strategy_family": "short_put",
                "receipt_complete": True,
                "fresh": True,
                "authority_mode": "v2_shadow",
                "pool_efficiency_improvement": "0.01",
                "outcome_reason": "selected" if index < 5 else "eligible",
            }
        )
    return {
        "schema_version": "position_advice_promotion_evidence.v1",
        "normalized_account": "lx",
        "portfolio_scope_id": scope_for("lx"),
        "normalized_portfolio_source": "futu",
        "portfolio_account_identity_hash": IDENTITY,
        "authority_mode": "v2_shadow",
        "authority_generation": generation,
        "authority_policy_hash": policy_hash,
        "safety": {metric: 0 for metric in SAFETY_METRICS},
        "market_session_ids": [f"US:2026-07-{day:02d}" for day in range(1, 11)],
        "first_opportunity_at": start.isoformat(),
        "last_opportunity_at": (start + timedelta(days=14)).isoformat(),
        "covered_strategy_families": ["short_put"],
        "critical_replay_fixtures": {
            "put_release": True,
            "call_release": True,
            "capacity_and_invariant_reject": True,
            "partial_lifecycle": True,
            "stale_source": True,
            "authority_cas_conflict": True,
            "combo_decomposition": True,
        },
        "economic": {
            "modeled_daily_carry_uplift_base_cny": "10",
            "aggregate_net_carry_improvement_H_base_cny": "100",
            "pool_efficiencies": [
                {
                    "pool_key": "cash:one",
                    "before": "0.01",
                    "after": "0.02",
                    "resource_units_before": "100",
                    "resource_units_after": "100",
                }
            ],
        },
        "opportunities": opportunities,
    }


def _apply_first_use(
    base: Path,
    *,
    account: str = "lx",
    identity: str = IDENTITY,
    mode: str = "v1",
    promotion_evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    return apply_authority_change(
        base=base,
        normalized_account=account,
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=identity,
        target_mode=mode,
        expected_policy_hash="absent",
        actor="operator@example",
        requested_at=NOW,
        confirm=True,
        identity_binding_evidence=_binding(account=account, identity=identity),
        promotion_evidence=promotion_evidence,
    )


def _publish_promotion_artifacts(
    base: Path,
    evidence: dict[str, object],
) -> None:
    evidence_hash = canonical_sha256(evidence)
    gate = evaluate_promotion_gate(evidence)
    gate_payload = {
        **gate,
        "promotion_evidence_hash": evidence_hash,
    }
    gate_payload["artifact_hash"] = canonical_sha256(gate_payload)
    scope_dir = portfolio_scope_state_dir(base, scope_for("lx"))
    evidence_dir = scope_dir / "promotion_evidence"
    gate_dir = scope_dir / "promotion_gates"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    gate_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / f"{evidence_hash}.json").write_text(
        json.dumps(evidence),
        encoding="utf-8",
    )
    (gate_dir / f"{gate_payload['artifact_hash']}.json").write_text(
        json.dumps(gate_payload),
        encoding="utf-8",
    )


def test_first_use_defaults_v1_only_when_scope_has_no_history(tmp_path: Path) -> None:
    resolution = read_authority_resolution(
        base=tmp_path,
        normalized_account=" LX ",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
    )
    assert resolution.resolution_status == "first_use_default_v1"
    assert resolution.mode == "v1"

    scope_dir = portfolio_scope_state_dir(tmp_path, scope_for("lx"))
    (scope_dir / "authority_changes").mkdir(parents=True)
    (scope_dir / "authority_changes" / "orphan.json").write_text("{}")
    conflict = read_authority_resolution(
        base=tmp_path,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
    )
    assert conflict.resolution_status == "authority_conflict"
    assert conflict.reason_codes == ("authority_policy_missing_with_history",)


def test_authority_dry_run_has_no_write_and_apply_requires_confirm(tmp_path: Path) -> None:
    plan = plan_authority_change(
        base=tmp_path,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        target_mode="v1",
        expected_policy_hash="absent",
        actor="operator@example",
        requested_at=NOW,
        identity_binding_evidence=_binding(),
    )
    assert plan["status"] == "ready"
    assert plan["would_change"] is True
    assert not authority_policy_path(tmp_path, scope_for("lx")).exists()

    with pytest.raises(PositionAdviceAuthorityError, match="explicit confirm"):
        apply_authority_change(
            base=tmp_path,
            normalized_account="lx",
            normalized_portfolio_source="futu",
            portfolio_account_identity_hash=IDENTITY,
            target_mode="v1",
            expected_policy_hash="absent",
            actor="operator@example",
            requested_at=NOW,
            confirm=False,
            identity_binding_evidence=_binding(),
        )
    assert not authority_policy_path(tmp_path, scope_for("lx")).exists()


def test_apply_writes_receipt_before_valid_policy_and_identity_drift_conflicts(
    tmp_path: Path,
) -> None:
    applied = _apply_first_use(tmp_path)
    policy = applied["policy"]
    assert applied["status"] == "applied"
    receipt_path = Path(str(applied["change_receipt_path"]))
    binding_path = Path(str(applied["identity_binding_path"]))
    assert receipt_path.exists()
    assert receipt_path.stem == policy["change_receipt_hash"]
    assert binding_path.is_file()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert binding_path.parent == authority_identity_binding_dir(
        tmp_path,
        scope_for("lx"),
    )
    assert binding_path.stem == receipt["identity_binding_hash"]

    resolved = read_authority_resolution(
        base=tmp_path,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
    )
    assert resolved.resolution_status == "resolved"
    assert resolved.policy_hash == policy["policy_hash"]

    drifted = read_authority_resolution(
        base=tmp_path,
        normalized_account="lx",
        normalized_portfolio_source="external_holdings",
        portfolio_account_identity_hash="f" * 64,
    )
    assert drifted.resolution_status == "authority_conflict"
    assert {
        "portfolio_source_identity_conflict",
        "portfolio_account_identity_conflict",
    }.issubset(drifted.reason_codes)


def test_cas_and_cross_scope_identity_uniqueness_fail_without_write(tmp_path: Path) -> None:
    first = _apply_first_use(tmp_path)
    with pytest.raises(PositionAdviceAuthorityError, match="expected_hash_mismatch"):
        apply_authority_change(
            base=tmp_path,
            normalized_account="lx",
            normalized_portfolio_source="futu",
            portfolio_account_identity_hash=IDENTITY,
            target_mode="v2_shadow",
            expected_policy_hash="f" * 64,
            actor="operator@example",
            requested_at=NOW,
            confirm=True,
        )
    persisted = json.loads(
        authority_policy_path(tmp_path, scope_for("lx")).read_text()
    )
    assert persisted["policy_hash"] == first["policy"]["policy_hash"]

    plan = plan_authority_change(
        base=tmp_path,
        normalized_account="sy",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        target_mode="v1",
        expected_policy_hash="absent",
        actor="operator@example",
        requested_at=NOW,
        identity_binding_evidence=_binding(account="sy"),
    )
    assert plan["status"] == "blocked"
    assert "portfolio_identity_already_bound_to_other_scope" in plan["reason_codes"]
    assert not authority_policy_path(tmp_path, scope_for("sy")).exists()


def test_v2_requires_passing_promotion_and_unknown_delivery_blocks_promotion(
    tmp_path: Path,
) -> None:
    first = _apply_first_use(tmp_path)
    shadow = apply_authority_change(
        base=tmp_path,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        target_mode="v2_shadow",
        expected_policy_hash=str(first["policy"]["policy_hash"]),
        actor="operator@example",
        requested_at=NOW,
        confirm=True,
    )
    policy_hash = str(shadow["policy"]["policy_hash"])
    promotion = _promotion_evidence(
        generation=int(shadow["policy"]["generation"]),
        policy_hash=policy_hash,
    )
    unpublished = plan_authority_change(
        base=tmp_path,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        target_mode="v2",
        expected_policy_hash=policy_hash,
        actor="operator@example",
        requested_at=NOW,
        promotion_evidence=promotion,
    )
    assert (
        "promotion_evidence_not_published_or_conflicted"
        in unpublished["reason_codes"]
    )
    with pytest.raises(PositionAdviceAuthorityError, match="promotion"):
        apply_authority_change(
            base=tmp_path,
            normalized_account="lx",
            normalized_portfolio_source="futu",
            portfolio_account_identity_hash=IDENTITY,
            target_mode="v2",
            expected_policy_hash=policy_hash,
            actor="operator@example",
            requested_at=NOW,
            confirm=True,
            promotion_evidence={"schema_version": "wrong"},
        )
    _publish_promotion_artifacts(tmp_path, promotion)

    unknown_dir = (
        portfolio_scope_state_dir(tmp_path, scope_for("lx"))
        / "notification_authority"
        / "unknown"
    )
    unknown_dir.mkdir(parents=True)
    (unknown_dir / "delivery-1.json").write_text("{}")
    blocked = plan_authority_change(
        base=tmp_path,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        target_mode="v2",
        expected_policy_hash=policy_hash,
        actor="operator@example",
        requested_at=NOW,
        promotion_evidence=promotion,
    )
    assert "notification_authority_unknown_unresolved" in blocked["reason_codes"]

    resolution_dir = unknown_dir.parent / "resolutions"
    resolution_dir.mkdir()
    (resolution_dir / "delivery-1.json").write_text("{}")
    promoted = apply_authority_change(
        base=tmp_path,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        target_mode="v2",
        expected_policy_hash=policy_hash,
        actor="operator@example",
        requested_at=NOW,
        confirm=True,
        promotion_evidence=promotion,
    )
    assert promoted["policy"]["mode"] == "v2"
    assert promoted["policy"]["generation"] == 3
    assert promoted["policy"]["covered_strategy_families"] == ["short_put"]


def test_rollback_v1_records_outstanding_unknown_without_blocking(
    tmp_path: Path,
) -> None:
    first = _apply_first_use(tmp_path)
    shadow = apply_authority_change(
        base=tmp_path,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        target_mode="v2_shadow",
        expected_policy_hash=str(first["policy"]["policy_hash"]),
        actor="operator@example",
        requested_at=NOW,
        confirm=True,
    )
    receipt_id = "d" * 64
    unknown_dir = (
        portfolio_scope_state_dir(tmp_path, scope_for("lx"))
        / "notification_authority"
        / "unknown"
    )
    unknown_dir.mkdir(parents=True)
    (unknown_dir / f"{receipt_id}.json").write_text("{}", encoding="utf-8")

    rollback = apply_authority_change(
        base=tmp_path,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        target_mode="v1",
        expected_policy_hash=str(shadow["policy"]["policy_hash"]),
        actor="operator@example",
        requested_at=NOW,
        confirm=True,
    )

    assert rollback["policy"]["mode"] == "v1"
    assert rollback["outstanding_notification_receipt_ids"] == [receipt_id]
    receipt = json.loads(
        Path(str(rollback["change_receipt_path"])).read_text(encoding="utf-8")
    )
    assert receipt["outstanding_notification_receipt_ids"] == [receipt_id]


def test_malformed_existing_policy_blocks_other_scope_first_use(tmp_path: Path) -> None:
    malformed_scope = portfolio_scope_state_dir(tmp_path, scope_for("bad"))
    malformed_scope.mkdir(parents=True)
    (malformed_scope / "authority_policy.v1.json").write_text("{not-json")
    plan = plan_authority_change(
        base=tmp_path,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        target_mode="v1",
        expected_policy_hash="absent",
        actor="operator@example",
        requested_at=NOW,
        identity_binding_evidence=_binding(),
    )
    assert plan["status"] == "blocked"
    assert "existing_authority_policy_malformed" in plan["reason_codes"]


def test_concurrent_first_use_same_identity_creates_only_one_policy(
    tmp_path: Path,
) -> None:
    barrier = Barrier(2)

    def create(account: str) -> tuple[str, str]:
        barrier.wait()
        try:
            applied = _apply_first_use(
                tmp_path,
                account=account,
                identity=IDENTITY,
            )
        except PositionAdviceAuthorityError as exc:
            return ("blocked", str(exc))
        return ("applied", str(applied["policy"]["policy_hash"]))

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(create, ("lx", "sy")))

    assert [status for status, _detail in outcomes].count("applied") == 1
    blocked = [
        detail
        for status, detail in outcomes
        if status == "blocked"
    ]
    assert len(blocked) == 1
    assert "portfolio_identity_already_bound_to_other_scope" in blocked[0]
    policies = [
        authority_policy_path(tmp_path, scope_for(account))
        for account in ("lx", "sy")
    ]
    assert sum(path.is_file() for path in policies) == 1
