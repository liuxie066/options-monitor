from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from domain.domain.decision_state_fingerprint import (
    DECISION_STATE_FINGERPRINT_SCHEMA,
    DECISION_STATE_SNAPSHOT_SCHEMA,
    build_decision_state_fingerprint,
    canonical_sha256,
)
from src.application.ledger.projection_verify import compare_projection_lots
from src.application.ledger.event_codec import valid_void_target_event_id
from src.application.ledger.combo_membership import (
    resolve_account_combo_memberships,
    validate_combo_group_membership,
)
from src.application.ledger.lifecycle_overlay import (
    resolve_account_lifecycle_overlay,
    resolve_lifecycle_account_rows,
    validate_account_lifecycle_resolution,
)
from src.application.ledger.publisher import project_stored_trade_events_to_position_lots


POSITION_FACT_SNAPSHOT_CONTRACT = "position_fact_snapshot.v1"
_SNAPSHOT_FINGERPRINT_METADATA_FIELDS = frozenset(
    {
        "actionable",
        "decision_state_fingerprint",
        "error",
        "fingerprint_schema_version",
        "projection_comparison",
        "projection_diagnostics",
        "reason_codes",
        "snapshot_status",
        "source_observed_at",
    }
)


def decision_state_snapshot(
    repo: Any,
    *,
    account: str,
    portfolio_scope_id: str,
    source_observed_at: str | None = None,
) -> dict[str, Any]:
    observed_at_override = (
        str(source_observed_at) if source_observed_at is not None else None
    )
    observed_at = observed_at_override or datetime.now(timezone.utc).isoformat()
    candidate = getattr(repo, "primary_repo", repo)
    read_rows = getattr(candidate, "read_decision_state_rows", None)
    if not callable(read_rows):
        return _unavailable_snapshot(
            observed_at=observed_at,
            reason_code="coherent_ledger_snapshot_unavailable",
        )
    try:
        rows = read_rows(account=account)
        # A ledger observation is complete only after the coherent read
        # transaction has returned.  Injected timestamps remain available for
        # deterministic tests and historical replay.
        observed_at = (
            observed_at_override or datetime.now(timezone.utc).isoformat()
        )
        return decision_state_snapshot_from_rows(
            rows,
            account=account,
            portfolio_scope_id=portfolio_scope_id,
            source_observed_at=observed_at,
        )
    except Exception as exc:
        return _unavailable_snapshot(
            observed_at=observed_at,
            reason_code="coherent_ledger_snapshot_failed",
            error=exc,
        )


def decision_state_snapshot_from_rows(
    rows: Mapping[str, Any],
    *,
    account: str,
    portfolio_scope_id: str,
    source_observed_at: str,
) -> dict[str, Any]:
    """Build one account snapshot from an already-frozen ledger read."""

    observed_at = str(source_observed_at or "").strip()
    if not observed_at:
        raise ValueError("source_observed_at is required")
    try:
        events = list(rows["trade_events"])
        stored_lots = list(rows["stored_position_lots"])
        projection = project_stored_trade_events_to_position_lots(events)
        projected_lots = [item.to_dict() for item in projection.lots]
        comparison = compare_projection_lots(
            projected_lots=projected_lots,
            current_lots=stored_lots,
            diagnostics=projection.diagnostics,
        )
        error_count = sum(
            count
            for status, count in comparison["summary"].items()
            if status != "matched"
        )
        account_value = str(account or "").strip().lower()
        account_terminal_event_ids = {
            str(item.get("canonical_terminal_event_id") or "").strip()
            for item in rows["account_lifecycle_allocations"]
            if str(item.get("canonical_terminal_event_id") or "").strip()
        }
        effective_void_event_ids = sorted(
            {
                target
                for item in events
                for target in [valid_void_target_event_id(item)]
                if target and target in account_terminal_event_ids
            }
        )
        reprojected_account_lots = [
            row
            for row in projected_lots
            if str((row.get("fields") or {}).get("account") or "").strip().lower() == account_value
        ]
        lifecycle_resolution = resolve_lifecycle_account_rows(rows)
        combo_memberships = resolve_account_combo_memberships(
            account=account_value,
            trade_events=events,
            projected_position_lots=projected_lots,
            identities=rows["account_combo_identities"],
        )
        fingerprint_payload = {
            "schema_version": DECISION_STATE_SNAPSHOT_SCHEMA,
            "position_fact_contract_version": (
                POSITION_FACT_SNAPSHOT_CONTRACT
            ),
            "normalized_account": account_value,
            "portfolio_scope_id": str(portfolio_scope_id or "").strip(),
            "event_fingerprint": canonical_sha256(events),
            "stored_position_lots_fingerprint": canonical_sha256(stored_lots),
            "reprojected_position_lots_fingerprint": canonical_sha256(projected_lots),
            "account_position_lots": rows["account_position_lots"],
            "account_reprojected_position_lots": reprojected_account_lots,
            "account_lifecycle_cases": rows["account_lifecycle_cases"],
            "account_lifecycle_evidence": rows["account_lifecycle_evidence"],
            "account_lifecycle_evidence_received_at_ms_by_id": rows[
                "account_lifecycle_evidence_received_at_ms_by_id"
            ],
            "account_lifecycle_allocations": rows["account_lifecycle_allocations"],
            "account_lifecycle_source_consumptions": rows[
                "account_lifecycle_source_consumptions"
            ],
            "account_lifecycle_timing_policies": rows[
                "account_lifecycle_timing_policies"
            ],
            "account_lifecycle_resolution": lifecycle_resolution,
            "effective_void_event_ids": effective_void_event_ids,
            "account_assigned_stock_events": rows["account_assigned_stock_events"],
            "account_combo_identities": rows["account_combo_identities"],
            "account_combo_group_memberships": combo_memberships,
        }
        fingerprint = decision_state_snapshot_fingerprint(
            fingerprint_payload
        )
        trusted = error_count == 0
        return {
            **fingerprint_payload,
            "fingerprint_schema_version": DECISION_STATE_FINGERPRINT_SCHEMA,
            "snapshot_status": "trusted" if trusted else "projection_untrusted",
            "actionable": trusted,
            "reason_codes": [] if trusted else ["same_snapshot_projection_mismatch"],
            "decision_state_fingerprint": fingerprint,
            "source_observed_at": observed_at,
            "projection_comparison": comparison,
            "projection_diagnostics": [item.to_dict() for item in projection.diagnostics],
        }
    except Exception as exc:
        return _unavailable_snapshot(
            observed_at=observed_at,
            reason_code="coherent_ledger_snapshot_failed",
            error=exc,
        )


def read_decision_state_rows_many(
    repo: Any,
    *,
    accounts: list[str] | tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    """Expose the repository's one-transaction multi-account read."""

    candidate = getattr(repo, "primary_repo", repo)
    read_rows = getattr(candidate, "read_decision_state_rows_many", None)
    if not callable(read_rows):
        raise TypeError("coherent multi-account ledger snapshot is unavailable")
    rows = read_rows(accounts=accounts)
    if not isinstance(rows, dict):
        raise TypeError("coherent multi-account ledger snapshot is invalid")
    return {
        str(account or "").strip().lower(): dict(payload)
        for account, payload in rows.items()
        if str(account or "").strip() and isinstance(payload, Mapping)
    }


def _unavailable_snapshot(
    *,
    observed_at: str,
    reason_code: str,
    error: Exception | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": DECISION_STATE_SNAPSHOT_SCHEMA,
        "fingerprint_schema_version": DECISION_STATE_FINGERPRINT_SCHEMA,
        "snapshot_status": "snapshot_unavailable",
        "actionable": False,
        "reason_codes": [str(reason_code)],
        "decision_state_fingerprint": None,
        "source_observed_at": str(observed_at),
    }
    if error is not None:
        payload["error"] = str(error)
    return payload


def validate_position_fact_snapshot_contract(
    payload: Mapping[str, Any],
) -> tuple[str, ...]:
    """Validate the lifecycle/combo capability required by action readers."""

    snapshot = dict(payload or {})
    reasons: set[str] = set()
    if snapshot.get("schema_version") != DECISION_STATE_SNAPSHOT_SCHEMA:
        reasons.add("position_fact_snapshot_schema_invalid")
    if (
        snapshot.get("fingerprint_schema_version")
        != DECISION_STATE_FINGERPRINT_SCHEMA
    ):
        reasons.add("position_fact_fingerprint_schema_invalid")
    if (
        snapshot.get("position_fact_contract_version")
        != POSITION_FACT_SNAPSHOT_CONTRACT
    ):
        reasons.add("position_fact_contract_version_invalid")
    account = str(snapshot.get("normalized_account") or "").strip().lower()
    if not account:
        reasons.add("position_fact_account_missing")

    lifecycle_rows: dict[str, list[Mapping[str, Any]]] = {}
    lifecycle_rows_valid = True
    for field in (
        "account_position_lots",
        "account_lifecycle_cases",
        "account_lifecycle_evidence",
        "account_lifecycle_allocations",
        "account_lifecycle_source_consumptions",
        "account_lifecycle_timing_policies",
        "account_combo_identities",
    ):
        rows = snapshot.get(field)
        if not isinstance(rows, list) or any(
            not isinstance(item, Mapping) for item in rows
        ):
            reasons.add(f"position_fact_{field}_invalid")
            lifecycle_rows_valid = False
        elif field != "account_combo_identities":
            lifecycle_rows[field] = rows

    evidence_received_at = snapshot.get(
        "account_lifecycle_evidence_received_at_ms_by_id"
    )
    receive_times_valid = isinstance(evidence_received_at, Mapping)
    if receive_times_valid:
        receive_times_valid = all(
            isinstance(evidence_id, str)
            and bool(evidence_id)
            and isinstance(received_at_ms, int)
            and not isinstance(received_at_ms, bool)
            and received_at_ms > 0
            for evidence_id, received_at_ms in evidence_received_at.items()
        )
    evidence_rows = lifecycle_rows.get("account_lifecycle_evidence")
    if receive_times_valid and evidence_rows is not None:
        evidence_ids = [
            str(item.get("evidence_id") or "").strip()
            for item in evidence_rows
        ]
        receive_times_valid = (
            all(evidence_ids)
            and len(evidence_ids) == len(set(evidence_ids))
            and set(evidence_received_at) == set(evidence_ids)
        ) or (not evidence_ids and not evidence_received_at)
    if not receive_times_valid:
        reasons.add("position_fact_lifecycle_evidence_receive_times_invalid")

    lifecycle_resolution = snapshot.get("account_lifecycle_resolution")
    lifecycle_resolution_valid = isinstance(lifecycle_resolution, Mapping)
    if not lifecycle_resolution_valid:
        reasons.add("position_fact_lifecycle_resolution_missing")
    else:
        resolution_reasons = validate_account_lifecycle_resolution(
            lifecycle_resolution
        )
        reasons.update(resolution_reasons)
        lifecycle_resolution_valid = not resolution_reasons
        if str(lifecycle_resolution.get("account") or "").strip().lower() != account:
            reasons.add("position_fact_lifecycle_account_mismatch")
            lifecycle_resolution_valid = False

    void_event_ids = snapshot.get("effective_void_event_ids")
    if (
        not isinstance(void_event_ids, list)
        or any(
            not isinstance(item, str) or not item
            for item in void_event_ids
        )
        or void_event_ids != sorted(set(void_event_ids))
    ):
        reasons.add("position_fact_void_event_ids_invalid")
        void_event_ids_valid = False
    else:
        void_event_ids_valid = True

    if (
        account
        and lifecycle_rows_valid
        and receive_times_valid
        and lifecycle_resolution_valid
        and void_event_ids_valid
    ):
        frozen_evidence = []
        for raw in lifecycle_rows["account_lifecycle_evidence"]:
            item = dict(raw)
            item["_ledger_created_at_ms"] = evidence_received_at[
                str(item.get("evidence_id") or "").strip()
            ]
            frozen_evidence.append(item)
        try:
            expected_resolution = resolve_account_lifecycle_overlay(
                account=account,
                cases=lifecycle_rows["account_lifecycle_cases"],
                evidence=frozen_evidence,
                allocations=lifecycle_rows["account_lifecycle_allocations"],
                source_claims=lifecycle_rows[
                    "account_lifecycle_source_consumptions"
                ],
                timing_policies=lifecycle_rows[
                    "account_lifecycle_timing_policies"
                ],
                position_lots=lifecycle_rows["account_position_lots"],
                effective_void_event_ids=void_event_ids,
            )
        except (TypeError, ValueError):
            reasons.add("position_fact_lifecycle_resolution_inputs_invalid")
        else:
            if _lifecycle_resolution_fact_view(
                lifecycle_resolution
            ) != _lifecycle_resolution_fact_view(expected_resolution):
                reasons.add(
                    "position_fact_lifecycle_resolution_facts_mismatch"
                )

    memberships = snapshot.get("account_combo_group_memberships")
    if not isinstance(memberships, list) or any(
        not isinstance(item, Mapping) for item in memberships
    ):
        reasons.add("position_fact_combo_memberships_invalid")
    else:
        group_ids = [
            str(item.get("group_id") or "").strip()
            for item in memberships
        ]
        if (
            any(not group_id for group_id in group_ids)
            or group_ids != sorted(set(group_ids))
        ):
            reasons.add("position_fact_combo_membership_order_invalid")
        if any(
            validate_combo_group_membership(item).status != "valid"
            for item in memberships
        ):
            reasons.add("position_fact_combo_membership_invalid")
    supplied_fingerprint = str(
        snapshot.get("decision_state_fingerprint") or ""
    ).strip().lower()
    if supplied_fingerprint != decision_state_snapshot_fingerprint(
        snapshot
    ):
        reasons.add("position_fact_decision_fingerprint_mismatch")
    return tuple(sorted(reasons))


def decision_state_snapshot_fingerprint(
    payload: Mapping[str, Any],
) -> str:
    business_payload = {
        key: value
        for key, value in dict(payload or {}).items()
        if key not in _SNAPSHOT_FINGERPRINT_METADATA_FIELDS
    }
    return build_decision_state_fingerprint(business_payload)


def _lifecycle_resolution_fact_view(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the lifecycle decision fields derivable from frozen position facts."""

    return {
        "schema_version": payload.get("schema_version"),
        "account": payload.get("account"),
        "case_resolutions": payload.get("case_resolutions"),
        "contested_components": payload.get("contested_components"),
        "arbitration_hash": payload.get("arbitration_hash"),
        "generation_token_bindings": [
            {
                "schema_version": item.get("schema_version"),
                "case_id": item.get("case_id"),
                "dependency_case_ids": item.get("dependency_case_ids"),
                "target_lot_ids": item.get("target_lot_ids"),
            }
            for item in payload.get("generation_tokens") or []
            if isinstance(item, Mapping)
        ],
    }


__all__ = [
    "POSITION_FACT_SNAPSHOT_CONTRACT",
    "decision_state_snapshot",
    "decision_state_snapshot_from_rows",
    "decision_state_snapshot_fingerprint",
    "read_decision_state_rows_many",
    "validate_position_fact_snapshot_contract",
]
