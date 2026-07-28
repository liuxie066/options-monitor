from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterable, Mapping

from domain.domain.combo_identity import (
    build_combo_identity,
    classify_combo_structure,
)
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.lifecycle_allocation import (
    allocation_id_for,
    terminal_event_id_for,
)
from domain.domain.option_lifecycle import (
    derive_lifecycle_read_model,
    expiration_observation_start_ms,
)
from domain.domain.position_advice_allocator import allocate_position_advice
from domain.domain.position_advice_promotion import (
    CRITICAL_REPLAY_SCHEMA,
    PROMOTION_CHECKS_SCHEMA,
    SAFETY_METRICS,
)
from src.application.position_advice_authority_service import (
    apply_authority_change,
    build_identity_binding_evidence,
    plan_authority_change,
)
from src.application.position_advice_plan_builder import (
    POSITION_ADVICE_LEG_PLAN_SCHEMA,
)
from src.application.positions.context_builder import (
    build_lifecycle_read_models_from_decision_snapshot,
)


_TERMINAL_LIFECYCLE_STATES = frozenset(
    {
        "assigned",
        "exercised",
        "expired_unassigned",
        "resolved_mixed",
    }
)
_BLOCKED_LIFECYCLE_STATES = frozenset(
    {
        "settlement_pending",
        "partially_resolved",
        "assigned",
        "exercised",
        "expired_unassigned",
        "resolved_mixed",
        "needs_review",
        "conflict",
    }
)
_PROMOTABLE_ROW_FAMILIES = frozenset(
    {"short_put", "funding_put", "covered_call"}
)


def evaluate_position_advice_plan_safety(
    plan_bindings: Iterable[
        tuple[Mapping[str, Any], Mapping[str, Any]]
    ],
) -> dict[str, Any]:
    """Count structural safety violations from exact immutable shadow plans."""

    counts = {metric: 0 for metric in SAFETY_METRICS}
    violations: list[dict[str, str]] = []
    plan_hashes: list[str] = []
    for raw_plan, raw_input in plan_bindings:
        plan = dict(raw_plan or {})
        immutable_input = dict(raw_input or {})
        plan_hash = str(plan.get("artifact_hash") or "")
        plan_hashes.append(plan_hash)
        rows = {
            str(item.get("position_id") or ""): dict(item)
            for item in plan.get("rows") or []
            if isinstance(item, Mapping)
            and str(item.get("position_id") or "")
        }
        selected = [
            dict(item)
            for item in plan.get("selected_proposals") or []
            if isinstance(item, Mapping)
        ]
        selected_source_ids = {
            str(source_id or "")
            for proposal in selected
            for source_id in proposal.get("source_position_ids") or []
        }
        checked_at = _parse_timestamp(plan.get("advice_checked_at"))
        snapshot = dict(
            immutable_input.get("decision_state_snapshot") or {}
        )
        snapshot_replayable = (
            snapshot.get("snapshot_status") == "trusted"
            and snapshot.get("actionable") is True
            and snapshot.get("decision_state_fingerprint")
            == plan.get("decision_state_fingerprint")
        )
        snapshot_position_ids = {
            str(item.get("record_id") or "")
            for item in snapshot.get("account_position_lots") or []
            if isinstance(item, Mapping)
            and str(item.get("record_id") or "")
        }
        lifecycle_models: dict[str, dict[str, Any]] = {}
        if snapshot_replayable:
            try:
                lifecycle_models = {
                    str(key): dict(value)
                    for key, value in (
                        build_lifecycle_read_models_from_decision_snapshot(
                            snapshot,
                            now_ms=int(checked_at.timestamp() * 1000),
                        )
                    ).items()
                }
            except (TypeError, ValueError):
                _add_violation(
                    counts,
                    violations,
                    metric="lifecycle_or_identity_conflict_actionable",
                    plan_hash=plan_hash,
                    code="lifecycle_replay_failed",
                )

        for position_id, row in rows.items():
            lifecycle = str(row.get("lifecycle_state") or "")
            if (
                snapshot_replayable
                and position_id not in snapshot_position_ids
            ):
                _add_violation(
                    counts,
                    violations,
                    metric="lifecycle_or_identity_conflict_actionable",
                    plan_hash=plan_hash,
                    code=f"row_absent_from_snapshot:{position_id}",
                )
            expected_lifecycle = str(
                lifecycle_models.get(position_id, {}).get(
                    "lifecycle_state"
                )
                or ""
            )
            if (
                lifecycle in _TERMINAL_LIFECYCLE_STATES
                and expected_lifecycle != lifecycle
            ):
                _add_violation(
                    counts,
                    violations,
                    metric="false_assignment_confirmation",
                    plan_hash=plan_hash,
                    code=f"terminal_state_unreplayed:{position_id}",
                )
            if (
                expected_lifecycle
                and expected_lifecycle != lifecycle
                and (
                    row.get("model_actionable") is True
                    or position_id in selected_source_ids
                )
            ):
                _add_violation(
                    counts,
                    violations,
                    metric="lifecycle_or_identity_conflict_actionable",
                    plan_hash=plan_hash,
                    code=f"actionable_lifecycle_replay_mismatch:{position_id}",
                )
            family = str(row.get("strategy_family") or "")
            if (
                family in _PROMOTABLE_ROW_FAMILIES
                and (
                    row.get("actionable") is True
                    or row.get("promotion_scope_status")
                    != "shadow_evaluation"
                )
            ):
                _add_violation(
                    counts,
                    violations,
                    metric="authority_mixed_exposure",
                    plan_hash=plan_hash,
                    code=f"shadow_row_authority_mixed:{position_id}",
                )
            if (
                row.get("model_actionable") is True
                and (
                    lifecycle in _BLOCKED_LIFECYCLE_STATES
                    or str(row.get("group_structure_state") or "")
                    in {
                        "identity_unverified",
                        "review_required",
                        "opening_incomplete",
                        "partially_decomposed",
                    }
                )
            ):
                _add_violation(
                    counts,
                    violations,
                    metric="lifecycle_or_identity_conflict_actionable",
                    plan_hash=plan_hash,
                    code=f"blocked_row_model_actionable:{position_id}",
                )

        for source_id in sorted(selected_source_ids):
            row = rows.get(source_id, {})
            if str(row.get("lifecycle_state") or "") != "open":
                _add_violation(
                    counts,
                    violations,
                    metric="lifecycle_or_identity_conflict_actionable",
                    plan_hash=plan_hash,
                    code=f"selected_source_not_open:{source_id}",
                )
            if _combo_selection_invalid(row):
                _add_violation(
                    counts,
                    violations,
                    metric="invalid_combo_continuation",
                    plan_hash=plan_hash,
                    code=f"selected_combo_plan_invalid:{source_id}",
                )

        if selected and not _source_manifest_fresh_and_complete(
            plan.get("source_manifest"),
            checked_at=checked_at,
        ):
            _add_violation(
                counts,
                violations,
                metric="stale_or_incomplete_actionable_exposure",
                plan_hash=plan_hash,
                code="selected_plan_source_stale_or_incomplete",
            )
        if selected and not snapshot_replayable:
            _add_violation(
                counts,
                violations,
                metric="stale_or_incomplete_actionable_exposure",
                plan_hash=plan_hash,
                code="selected_plan_snapshot_not_replayable",
            )
        if _allocator_replay_mismatch(plan):
            _add_violation(
                counts,
                violations,
                metric="allocator_invariant_violation",
                plan_hash=plan_hash,
                code="allocator_replay_mismatch",
            )

    payload = {
        "schema_version": PROMOTION_CHECKS_SCHEMA,
        "evaluator_version": PROMOTION_CHECKS_SCHEMA,
        "source_plan_hashes": sorted(set(plan_hashes)),
        "safety": counts,
        "violations": sorted(
            violations,
            key=lambda item: (
                item["metric"],
                item["plan_hash"],
                item["code"],
            ),
        ),
    }
    return {**payload, "artifact_hash": canonical_sha256(payload)}


def run_critical_promotion_replay() -> dict[str, Any]:
    """Run bounded deterministic fixtures through production domain functions."""

    checks = {
        "put_release": _fixture_put_release,
        "call_release": _fixture_call_release,
        "capacity_and_invariant_reject": (
            _fixture_capacity_and_invariant_reject
        ),
        "partial_lifecycle": _fixture_partial_lifecycle,
        "stale_source": _fixture_stale_source,
        "authority_cas_conflict": _fixture_authority_cas_conflict,
        "combo_decomposition": _fixture_combo_decomposition,
    }
    results: dict[str, bool] = {}
    details: dict[str, str] = {}
    for name, check in sorted(checks.items()):
        try:
            passed, detail = check()
        except Exception as exc:  # fail closed; details remain non-sensitive
            passed = False
            detail = f"{type(exc).__name__}:{exc}"
        results[name] = passed is True
        details[name] = detail
    payload = {
        "schema_version": CRITICAL_REPLAY_SCHEMA,
        "fixture_results": results,
        "details": details,
    }
    return {**payload, "artifact_hash": canonical_sha256(payload)}


def _allocator_replay_mismatch(plan: Mapping[str, Any]) -> bool:
    selected = [
        dict(item)
        for item in plan.get("selected_proposals") or []
        if isinstance(item, Mapping)
    ]
    alternatives = [
        dict(item)
        for item in plan.get("alternative_proposals") or []
        if isinstance(item, Mapping)
    ]
    if not selected and not alternatives:
        return False
    pools_before = plan.get("resource_pools_before")
    quantities_before = plan.get("candidate_quantity_before")
    if not isinstance(pools_before, dict) or not isinstance(
        quantities_before, dict
    ):
        return True
    proposals = [
        _allocator_input(item) for item in [*selected, *alternatives]
    ]
    try:
        replay = allocate_position_advice(
            proposals=proposals,
            resource_pools=dict(pools_before),
            candidate_quantities=dict(quantities_before),
        )
    except (TypeError, ValueError):
        return True
    expected_ids = [
        str(item.get("proposal_id") or "") for item in selected
    ]
    actual_ids = [
        str(item.get("proposal_id") or "") for item in replay.selected
    ]
    return (
        expected_ids != actual_ids
        or replay.resource_pools_after
        != dict(plan.get("resource_pools_after") or {})
        or replay.candidate_quantity_after
        != dict(plan.get("candidate_quantity_after") or {})
    )


def _allocator_input(raw: Mapping[str, Any]) -> dict[str, Any]:
    ignored = {
        "selected",
        "actionable",
        "allocator_reason",
        "execution_order",
        "depends_on",
        "candidate_quantity_before",
        "candidate_quantity_after",
    }
    proposal = {
        key: value for key, value in dict(raw).items() if key not in ignored
    }
    proposal["resource_deltas"] = [
        {
            key: value
            for key, value in dict(item).items()
            if key != "net_after"
        }
        for item in proposal.get("resource_deltas") or []
        if isinstance(item, Mapping)
    ]
    return proposal


def _combo_selection_invalid(row: Mapping[str, Any]) -> bool:
    state = str(row.get("group_structure_state") or "")
    if state != "active_combo":
        return str(row.get("action_scope") or "") == "combo_group"
    leg_plan = row.get("leg_plan")
    if not isinstance(leg_plan, Mapping):
        return True
    operations = [
        dict(item)
        for item in leg_plan.get("operations") or []
        if isinstance(item, Mapping)
    ]
    return not (
        leg_plan.get("schema_version") == POSITION_ADVICE_LEG_PLAN_SCHEMA
        and leg_plan.get("decomposes_group") is True
        and leg_plan.get("action_scope") == "combo_group"
        and [item.get("sequence") for item in operations] == [1, 2, 3]
        and len(operations) == 3
        and operations[1].get("strategy_group_after") is None
    )


def _source_manifest_fresh_and_complete(
    raw_manifest: Any,
    *,
    checked_at: datetime,
) -> bool:
    entries = [
        dict(item)
        for item in raw_manifest or []
        if isinstance(item, Mapping)
    ]
    required = {
        "quotes",
        "candidate_decisions",
        "portfolio",
        "ledger_decision_state",
        "cash_capacity",
        "share_coverage",
        "fx",
    }
    if {
        str(item.get("source_kind") or "") for item in entries
    } < required:
        return False
    for item in entries:
        if any(
            len(str(item.get(field) or "")) != 64
            for field in ("receipt_hash", "snapshot_id", "payload_sha256")
        ):
            return False
        try:
            expires_at = _parse_timestamp(item.get("expires_at"))
        except (TypeError, ValueError):
            return False
        if expires_at <= checked_at:
            return False
    return True


def _add_violation(
    counts: dict[str, int],
    violations: list[dict[str, str]],
    *,
    metric: str,
    plan_hash: str,
    code: str,
) -> None:
    counts[metric] += 1
    violations.append(
        {"metric": metric, "plan_hash": plan_hash, "code": code}
    )


def _fixture_put_release() -> tuple[bool, str]:
    pool_key = "cash:scope:authority"
    result = allocate_position_advice(
        proposals=[_fixture_proposal("put", "cash_base_cny", pool_key, "CNY")],
        resource_pools={
            pool_key: {
                "resource_kind": "cash_base_cny",
                "unit": "CNY",
                "available": "0",
            }
        },
        candidate_quantities={"candidate-put": 1},
    )
    passed = (
        len(result.selected) == 1
        and result.resource_pools_after[pool_key]["available"] == "10"
    )
    return passed, "selected_with_exact_cash_release"


def _fixture_call_release() -> tuple[bool, str]:
    pool_key = "shares:scope:NVDA"
    result = allocate_position_advice(
        proposals=[
            _fixture_proposal(
                "call", "covered_shares", pool_key, "shares"
            )
        ],
        resource_pools={
            pool_key: {
                "resource_kind": "covered_shares",
                "unit": "shares",
                "available": "0",
            }
        },
        candidate_quantities={"candidate-call": 1},
    )
    passed = (
        len(result.selected) == 1
        and result.resource_pools_after[pool_key]["available"] == "10"
    )
    return passed, "selected_with_exact_share_release"


def _fixture_capacity_and_invariant_reject() -> tuple[bool, str]:
    pool_key = "cash:scope:authority"
    capacity = _fixture_proposal(
        "capacity", "cash_base_cny", pool_key, "CNY"
    )
    capacity["resource_deltas"][0].update(
        {"released": "0", "required": "110"}
    )
    invariant = _fixture_proposal(
        "invariant", "cash_base_cny", pool_key, "CNY"
    )
    invariant["replacement_eligibility"] = "rejected_invariant"
    result = allocate_position_advice(
        proposals=[capacity, invariant],
        resource_pools={
            pool_key: {
                "resource_kind": "cash_base_cny",
                "unit": "CNY",
                "available": "100",
            }
        },
        candidate_quantities={
            "candidate-capacity": 1,
            "candidate-invariant": 1,
        },
    )
    reasons = {str(item.get("allocator_reason")) for item in result.alternatives}
    return (
        reasons
        == {"portfolio_capacity_conflict", "replacement_ineligible"},
        "capacity_and_invariant_fail_closed",
    )


def _fixture_partial_lifecycle() -> tuple[bool, str]:
    expiration = "2026-07-17"
    observation = expiration_observation_start_ms(expiration, "US")
    if observation is None:
        return False, "expiration_boundary_missing"
    case_id = "case-partial"
    evidence_id = "evidence-partial"
    lot_id = "lot-partial"
    allocation = {
        "case_id": case_id,
        "evidence_id": evidence_id,
        "target_lot_id": lot_id,
        "terminal_type": "assignment",
        "contracts_allocated": 1,
        "allocation_id": allocation_id_for(
            case_id=case_id,
            evidence_id=evidence_id,
            target_lot_id=lot_id,
        ),
        "canonical_terminal_event_id": terminal_event_id_for(
            case_id=case_id,
            evidence_id=evidence_id,
            target_lot_id=lot_id,
            terminal_type="assignment",
            contracts_allocated=1,
        ),
    }
    model = derive_lifecycle_read_model(
        expiration_ymd=expiration,
        market="US",
        target_contracts_by_lot={lot_id: 2},
        allocations=[allocation],
        now_ms=observation + 1,
    )
    return (
        model.lifecycle_state == "partially_resolved"
        and model.remaining_contracts_by_lot == {lot_id: 1}
        and model.actionable is False,
        "partial_terminal_evidence_is_nonactionable",
    )


def _fixture_stale_source() -> tuple[bool, str]:
    checked = datetime(2026, 7, 20, tzinfo=timezone.utc)
    manifest = [
        {
            "source_kind": kind,
            "snapshot_id": "a" * 64,
            "receipt_hash": "b" * 64,
            "payload_sha256": "c" * 64,
            "expires_at": "2026-07-19T00:00:00Z",
        }
        for kind in (
            "quotes",
            "candidate_decisions",
            "portfolio",
            "ledger_decision_state",
            "cash_capacity",
            "share_coverage",
            "fx",
        )
    ]
    return (
        not _source_manifest_fresh_and_complete(
            manifest,
            checked_at=checked,
        ),
        "expired_source_is_rejected",
    )


def _fixture_authority_cas_conflict() -> tuple[bool, str]:
    identity_hash = "a" * 64
    binding = build_identity_binding_evidence(
        normalized_account="fixture",
        normalized_portfolio_source="fixture_broker",
        portfolio_account_identity_hash=identity_hash,
        authoring_config_hash="b" * 64,
        market_bindings=[
            {
                "market": "US",
                "generated_config_hash": "c" * 64,
                "source_receipt_hash": "d" * 64,
                "normalized_account": "fixture",
                "normalized_portfolio_source": "fixture_broker",
                "portfolio_account_identity_hash": identity_hash,
                "source_receipt_fresh": True,
            }
        ],
    )
    with TemporaryDirectory(prefix="om-promotion-cas-") as raw:
        base = Path(raw)
        apply_authority_change(
            base=base,
            normalized_account="fixture",
            normalized_portfolio_source="fixture_broker",
            portfolio_account_identity_hash=identity_hash,
            target_mode="v1",
            expected_policy_hash="absent",
            actor="promotion-fixture",
            requested_at="2026-07-20T00:00:00Z",
            confirm=True,
            identity_binding_evidence=binding,
        )
        result = plan_authority_change(
            base=base,
            normalized_account="fixture",
            normalized_portfolio_source="fixture_broker",
            portfolio_account_identity_hash=identity_hash,
            target_mode="v2_shadow",
            expected_policy_hash="e" * 64,
            actor="promotion-fixture",
            requested_at="2026-07-20T00:01:00Z",
        )
    return (
        result["status"] == "blocked"
        and "authority_expected_hash_mismatch"
        in result["reason_codes"],
        "stale_expected_hash_is_blocked",
    )


def _fixture_combo_decomposition() -> tuple[bool, str]:
    identity = build_combo_identity(
        {
            "group_id": "combo-fixture",
            "strategy": "combo_yield",
            "account": "fixture",
            "symbol": "NVDA",
            "funding_put_record_id": "lot-put",
            "funding_put_open_event_id": "event-put",
            "funding_put_contract_key": {"option_type": "put"},
            "participation_call_record_id": "lot-call",
            "participation_call_open_event_id": "event-call",
            "participation_call_contract_key": {"option_type": "call"},
            "original_contracts": 1,
        }
    )
    active = classify_combo_structure(
        identity=identity,
        funding_put_contracts_open=1,
        participation_call_contracts_open=1,
        funding_put_terminal_allocated=0,
        participation_call_terminal_allocated=0,
        assigned_stock_contracts=0,
        evidence_conflict=False,
    )
    decomposed = classify_combo_structure(
        identity=identity,
        funding_put_contracts_open=0,
        participation_call_contracts_open=1,
        funding_put_terminal_allocated=1,
        participation_call_terminal_allocated=0,
        assigned_stock_contracts=0,
        evidence_conflict=False,
    )
    return (
        active == "active_combo" and decomposed == "residual_call",
        "terminal_leg_decomposes_combo",
    )


def _fixture_proposal(
    suffix: str,
    resource_kind: str,
    pool_key: str,
    unit: str,
) -> dict[str, Any]:
    return {
        "proposal_id": f"proposal-{suffix}",
        "source_position_ids": [f"lot-{suffix}"],
        "candidate_id": f"candidate-{suffix}",
        "candidate_contracts": 1,
        "replacement_eligibility": "capacity_deferred_to_allocator",
        "pool_efficiency_improvement": "0.1",
        "net_carry_improvement_H": "10",
        "net_carry_improvement_H_base_cny": "10",
        "allocation_rank": 1,
        "resource_deltas": [
            {
                "resource_kind": resource_kind,
                "pool_key": pool_key,
                "unit": unit,
                "released": "100",
                "required": "90",
            }
        ],
    }


def _parse_timestamp(value: Any) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone aware")
    return parsed.astimezone(timezone.utc)


__all__ = [
    "CRITICAL_REPLAY_SCHEMA",
    "PROMOTION_CHECKS_SCHEMA",
    "evaluate_position_advice_plan_safety",
    "run_critical_promotion_replay",
]
