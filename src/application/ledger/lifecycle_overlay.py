from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.option_lifecycle import LIFECYCLE_CASE_SCHEMA
from src.application.ledger.event_codec import valid_void_target_event_id
from src.application.ledger.source_consumption import (
    SOURCE_CONSUMPTION_SCHEMA,
    SOURCE_PAYLOAD_SCHEMA,
    canonical_source_payload_hash,
)


ZERO_PRICE_OPTION_CLOSE_EVIDENCE = "option_zero_price_close"
NON_ALLOCATING_EVIDENCE_TYPES = frozenset({"migration_bridge"})
LIFECYCLE_ANCHOR_RESOLUTION_SCHEMA = (
    "lifecycle_option_close_anchor_resolution.v1"
)
ACCOUNT_LIFECYCLE_RESOLUTION_SCHEMA = (
    "account_lifecycle_reservation_resolution.v1"
)
LIFECYCLE_GENERATION_TOKEN_SCHEMA = "lifecycle_generation_token.v1"


class LifecycleOverlayContractError(ValueError):
    """Raised when a coherent lifecycle row bundle is structurally invalid."""


@dataclass(frozen=True)
class LifecycleEvidenceFacts:
    effective_allocations: tuple[dict[str, Any], ...]
    reservation_contracts_by_lot: dict[str, int]
    reservation_evidence_ids: tuple[str, ...]
    orphan_evidence_ids: tuple[str, ...]


def lifecycle_evidence_facts(
    *,
    evidence: Iterable[dict[str, Any]],
    allocations: Iterable[dict[str, Any]],
    void_event_ids: Iterable[str] = (),
) -> LifecycleEvidenceFacts:
    """Derive effective lifecycle allocations and pending close reservations."""

    allocation_rows = [
        dict(item)
        for item in allocations
        if isinstance(item, dict)
    ]
    voided = {
        str(item or "").strip()
        for item in void_event_ids
        if str(item or "").strip()
    }
    effective_allocations = tuple(
        item
        for item in allocation_rows
        if not bool(item.get("voided"))
        and str(item.get("canonical_terminal_event_id") or "").strip()
        not in voided
    )
    allocated_evidence_ids = {
        str(item.get("evidence_id") or "").strip()
        for item in allocation_rows
        if str(item.get("evidence_id") or "").strip()
    }
    effective_allocated_evidence_ids = {
        str(item.get("evidence_id") or "").strip()
        for item in effective_allocations
        if str(item.get("evidence_id") or "").strip()
    }
    observed_closes: dict[str, int] = {}
    zero_price_evidence_ids: set[str] = set()
    orphan_evidence_ids: set[str] = set()

    for item in evidence:
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("evidence_id") or "").strip()
        if not evidence_id:
            continue
        evidence_type = str(item.get("evidence_type") or "").strip().lower()
        if evidence_type != ZERO_PRICE_OPTION_CLOSE_EVIDENCE:
            if evidence_type in NON_ALLOCATING_EVIDENCE_TYPES:
                continue
            if evidence_id not in allocated_evidence_ids:
                orphan_evidence_ids.add(evidence_id)
            continue
        manifest = _explicit_reservation_manifest(item)
        if manifest is None:
            orphan_evidence_ids.add(evidence_id)
            continue
        if evidence_id not in effective_allocated_evidence_ids:
            zero_price_evidence_ids.add(evidence_id)
        for lot_id, contracts in manifest.items():
            observed_closes[lot_id] = observed_closes.get(lot_id, 0) + contracts

    effective_terminal_contracts_by_lot: dict[str, int] = {}
    for item in effective_allocations:
        lot_id = str(item.get("target_lot_id") or "").strip()
        contracts = _positive_integer(item.get("contracts_allocated"))
        if lot_id and contracts is not None:
            effective_terminal_contracts_by_lot[lot_id] = (
                effective_terminal_contracts_by_lot.get(lot_id, 0) + contracts
            )
    reservations = {
        lot_id: outstanding
        for lot_id, observed_contracts in observed_closes.items()
        for outstanding in [
            max(
                observed_contracts
                - effective_terminal_contracts_by_lot.get(lot_id, 0),
                0,
            )
        ]
        if outstanding
    }
    reservation_evidence_ids = (
        zero_price_evidence_ids if reservations else set()
    )
    return LifecycleEvidenceFacts(
        effective_allocations=effective_allocations,
        reservation_contracts_by_lot=dict(sorted(reservations.items())),
        reservation_evidence_ids=tuple(sorted(reservation_evidence_ids)),
        orphan_evidence_ids=tuple(sorted(orphan_evidence_ids)),
    )


def _explicit_reservation_manifest(
    evidence: dict[str, Any],
) -> dict[str, int] | None:
    raw_manifest = evidence.get("target_contracts_by_lot")
    if isinstance(raw_manifest, dict):
        if not raw_manifest:
            return None
        normalized: dict[str, int] = {}
        for raw_lot_id, raw_contracts in raw_manifest.items():
            lot_id = str(raw_lot_id or "").strip()
            contracts = _positive_integer(raw_contracts)
            if not lot_id or contracts is None or lot_id in normalized:
                return None
            normalized[lot_id] = contracts
        return normalized

    lot_id = str(evidence.get("target_lot_id") or "").strip()
    contracts = _positive_integer(evidence.get("contracts"))
    if not lot_id or contracts is None:
        return None
    return {lot_id: contracts}


def _positive_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = Decimal(str(value))
        parsed = int(numeric)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return None
    if not numeric.is_finite() or parsed <= 0 or numeric != parsed:
        return None
    return parsed


def resolve_account_lifecycle_overlay(
    *,
    account: str,
    cases: Iterable[Mapping[str, Any]],
    evidence: Iterable[Mapping[str, Any]],
    allocations: Iterable[Mapping[str, Any]],
    source_claims: Iterable[Mapping[str, Any]],
    timing_policies: Iterable[Mapping[str, Any]],
    position_lots: Iterable[Mapping[str, Any]],
    trade_events: Iterable[Mapping[str, Any]] = (),
    effective_void_event_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Resolve every active lifecycle case from one coherent account bundle."""

    account_value = str(account or "").strip().lower()
    if not account_value:
        raise LifecycleOverlayContractError("lifecycle overlay account is required")

    case_rows = _unique_rows(cases, id_field="case_id", kind="lifecycle case")
    evidence_rows = _unique_rows(
        evidence,
        id_field="evidence_id",
        kind="lifecycle evidence",
    )
    allocation_rows = _unique_rows(
        allocations,
        id_field="allocation_id",
        kind="lifecycle allocation",
    )
    claim_rows = _unique_rows(
        source_claims,
        id_field="source_key",
        kind="lifecycle source claim",
    )
    timing_rows = _unique_rows(
        timing_policies,
        id_field="case_id",
        kind="lifecycle timing policy",
    )
    lot_rows = _unique_rows(
        position_lots,
        id_field="record_id",
        kind="position lot",
    )
    event_rows = _unique_rows(
        trade_events,
        id_field="event_id",
        kind="trade event",
    )
    voided = tuple(
        sorted(
            {
                str(item or "").strip()
                for item in effective_void_event_ids
                if str(item or "").strip()
            }
        )
    )

    cases_by_id = {str(item["case_id"]): item for item in case_rows}
    evidence_by_id = {
        str(item["evidence_id"]): item for item in evidence_rows
    }
    lots_by_id = {str(item["record_id"]): item for item in lot_rows}
    evidence_by_case = _group_rows(evidence_rows, "case_id")
    allocations_by_case = _group_rows(allocation_rows, "case_id")
    claims_by_case = _group_rows(claim_rows, "case_id")
    claims_by_evidence = _group_rows(claim_rows, "owner_evidence_id")
    timing_by_case = {str(item["case_id"]): item for item in timing_rows}

    active_cases: list[dict[str, Any]] = []
    for item in case_rows:
        row_account = str(item.get("account") or "").strip().lower()
        if row_account != account_value:
            continue
        if str(item.get("status") or "").strip().lower() == "superseded":
            continue
        active_cases.append(item)

    local_resolutions: dict[str, dict[str, Any]] = {}
    target_manifests: dict[str, dict[str, int]] = {}
    for lifecycle_case in sorted(
        active_cases,
        key=lambda item: str(item.get("case_id") or ""),
    ):
        case_id = str(lifecycle_case["case_id"])
        manifest = _case_target_manifest(lifecycle_case)
        target_manifests[case_id] = manifest or {}
        local_resolutions[case_id] = _resolve_case_anchor(
            lifecycle_case=lifecycle_case,
            cases_by_id=cases_by_id,
            evidence_by_id=evidence_by_id,
            evidence_rows=evidence_by_case.get(case_id, ()),
            allocation_rows=allocations_by_case.get(case_id, ()),
            case_claims=claims_by_case.get(case_id, ()),
            claims_by_evidence=claims_by_evidence,
            timing_policy=timing_by_case.get(case_id),
            lots_by_id=lots_by_id,
            effective_void_event_ids=set(voided),
        )

    arbitration = arbitrate_lifecycle_case_resolutions(
        account=account_value,
        case_resolutions=local_resolutions,
    )
    local_resolutions = {
        str(item["case_id"]): item
        for item in arbitration["case_resolutions"]
    }
    case_resolutions = arbitration["case_resolutions"]
    arbitration_payload = {
        "schema_version": arbitration["schema_version"],
        "account": arbitration["account"],
        "case_resolutions": case_resolutions,
        "contested_components": arbitration["contested_components"],
    }
    arbitration_hash = str(arbitration["arbitration_hash"])
    generation_tokens = _case_generation_tokens(
        account=account_value,
        case_rows=case_rows,
        active_cases=active_cases,
        target_manifests=target_manifests,
        case_resolutions=local_resolutions,
        evidence_rows=evidence_rows,
        allocation_rows=allocation_rows,
        claim_rows=claim_rows,
        timing_rows=timing_rows,
        lot_rows=lot_rows,
        event_rows=event_rows,
        effective_void_event_ids=voided,
    )
    return {
        **arbitration_payload,
        "arbitration_hash": arbitration_hash,
        "generation_tokens": generation_tokens,
    }


def arbitrate_lifecycle_case_resolutions(
    *,
    account: str,
    case_resolutions: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Apply the canonical cross-case reservation rule to compact facts."""

    account_value = str(account or "").strip().lower()
    if not account_value:
        raise LifecycleOverlayContractError("lifecycle arbitration account is required")
    resolutions: dict[str, dict[str, Any]] = {}
    for raw_case_id, raw_resolution in case_resolutions.items():
        case_id = str(raw_case_id or "").strip()
        item = dict(raw_resolution)
        if not case_id or str(item.get("case_id") or "").strip() != case_id:
            raise LifecycleOverlayContractError("lifecycle arbitration case id mismatch")
        resolutions[case_id] = item

    contested_components = _reservation_conflict_components(resolutions)
    conflict_case_ids = {
        case_id
        for component in contested_components
        for case_id in component["case_ids"]
    }
    for case_id, resolution in resolutions.items():
        if case_id in conflict_case_ids:
            resolution["status"] = "conflict"
            resolution["reason_codes"] = sorted(
                {
                    *resolution.get("reason_codes", ()),
                    "reservation_target_overlap",
                }
            )
            resolution["effective_reservations_by_lot"] = {}
        elif resolution.get("status") != "conflict":
            resolution["effective_reservations_by_lot"] = dict(
                resolution.get("requested_reservations_by_lot") or {}
            )
        resolution["resolution_hash"] = _hash_without(
            resolution,
            "resolution_hash",
        )

    ordered = [resolutions[case_id] for case_id in sorted(resolutions)]
    arbitration_payload = {
        "schema_version": ACCOUNT_LIFECYCLE_RESOLUTION_SCHEMA,
        "account": account_value,
        "case_resolutions": ordered,
        "contested_components": contested_components,
    }
    arbitration_hash = canonical_sha256(arbitration_payload)
    return {
        **arbitration_payload,
        "arbitration_hash": arbitration_hash,
    }


def lifecycle_case_resolution(
    account_resolution: Mapping[str, Any],
    *,
    case_id: str,
) -> dict[str, Any] | None:
    case_value = str(case_id or "").strip()
    for item in account_resolution.get("case_resolutions") or ():
        if (
            isinstance(item, Mapping)
            and str(item.get("case_id") or "").strip() == case_value
        ):
            return dict(item)
    return None


def lifecycle_case_generation_token(
    account_resolution: Mapping[str, Any],
    *,
    case_id: str,
) -> dict[str, Any] | None:
    case_value = str(case_id or "").strip()
    for item in account_resolution.get("generation_tokens") or ():
        if (
            isinstance(item, Mapping)
            and str(item.get("case_id") or "").strip() == case_value
        ):
            return dict(item)
    return None


def validate_account_lifecycle_resolution(
    payload: Mapping[str, Any],
) -> tuple[str, ...]:
    """Validate the self-contained hashes and canonical shape of a resolution."""

    item = dict(payload or {})
    reasons: set[str] = set()
    if item.get("schema_version") != ACCOUNT_LIFECYCLE_RESOLUTION_SCHEMA:
        reasons.add("lifecycle_resolution_schema_invalid")
    case_resolutions = item.get("case_resolutions")
    contested = item.get("contested_components")
    generation_tokens = item.get("generation_tokens")
    if not isinstance(case_resolutions, list) or any(
        not isinstance(row, Mapping) for row in case_resolutions
    ):
        reasons.add("lifecycle_case_resolutions_invalid")
        case_resolutions = []
    if not isinstance(contested, list) or any(
        not isinstance(row, Mapping) for row in contested
    ):
        reasons.add("lifecycle_contested_components_invalid")
        contested = []
    if not isinstance(generation_tokens, list) or any(
        not isinstance(row, Mapping) for row in generation_tokens
    ):
        reasons.add("lifecycle_generation_tokens_invalid")
        generation_tokens = []
    case_ids = [
        str(row.get("case_id") or "").strip()
        for row in case_resolutions
    ]
    token_case_ids = [
        str(row.get("case_id") or "").strip()
        for row in generation_tokens
    ]
    if (
        not all(case_ids)
        or case_ids != sorted(set(case_ids))
        or token_case_ids != case_ids
    ):
        reasons.add("lifecycle_resolution_case_order_invalid")
    for row in case_resolutions:
        if str(row.get("resolution_hash") or "") != _hash_without(
            row,
            "resolution_hash",
        ):
            reasons.add("lifecycle_case_resolution_hash_mismatch")
    for row in generation_tokens:
        if (
            row.get("schema_version") != LIFECYCLE_GENERATION_TOKEN_SCHEMA
            or len(str(row.get("generation_token") or "")) != 64
        ):
            reasons.add("lifecycle_generation_token_invalid")
    arbitration_payload = {
        "schema_version": item.get("schema_version"),
        "account": item.get("account"),
        "case_resolutions": case_resolutions,
        "contested_components": contested,
    }
    if str(item.get("arbitration_hash") or "") != canonical_sha256(
        arbitration_payload
    ):
        reasons.add("lifecycle_arbitration_hash_mismatch")
    return tuple(sorted(reasons))


def resolve_lifecycle_account_rows(
    rows: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve a coherent repository bundle without performing more reads."""

    required_lists = (
        "trade_events",
        "account_position_lots",
        "account_lifecycle_cases",
        "account_lifecycle_evidence",
        "account_lifecycle_allocations",
        "account_lifecycle_source_consumptions",
        "account_lifecycle_timing_policies",
    )
    normalized: dict[str, list[Mapping[str, Any]]] = {}
    for field in required_lists:
        value = rows.get(field)
        if not isinstance(value, list) or any(
            not isinstance(item, Mapping) for item in value
        ):
            raise LifecycleOverlayContractError(
                f"coherent lifecycle rows require object list: {field}"
            )
        normalized[field] = value
    received_at_by_id = rows.get(
        "account_lifecycle_evidence_received_at_ms_by_id"
    )
    if not isinstance(received_at_by_id, Mapping):
        raise LifecycleOverlayContractError(
            "coherent lifecycle rows require evidence receive-time map"
        )
    evidence = []
    for raw in normalized["account_lifecycle_evidence"]:
        item = dict(raw)
        evidence_id = str(item.get("evidence_id") or "").strip()
        if evidence_id in received_at_by_id:
            item["_ledger_created_at_ms"] = received_at_by_id[evidence_id]
        evidence.append(item)
    events = normalized["trade_events"]
    effective_void_event_ids = sorted(
        {
            target
            for event in events
            for target in [valid_void_target_event_id(dict(event))]
            if target
        }
    )
    return resolve_account_lifecycle_overlay(
        account=str(rows.get("account") or ""),
        cases=normalized["account_lifecycle_cases"],
        evidence=evidence,
        allocations=normalized["account_lifecycle_allocations"],
        source_claims=normalized[
            "account_lifecycle_source_consumptions"
        ],
        timing_policies=normalized[
            "account_lifecycle_timing_policies"
        ],
        position_lots=normalized["account_position_lots"],
        trade_events=events,
        effective_void_event_ids=effective_void_event_ids,
    )


def _resolve_case_anchor(
    *,
    lifecycle_case: dict[str, Any],
    cases_by_id: Mapping[str, dict[str, Any]],
    evidence_by_id: Mapping[str, dict[str, Any]],
    evidence_rows: Sequence[dict[str, Any]],
    allocation_rows: Sequence[dict[str, Any]],
    case_claims: Sequence[dict[str, Any]],
    claims_by_evidence: Mapping[str, Sequence[dict[str, Any]]],
    timing_policy: dict[str, Any] | None,
    lots_by_id: Mapping[str, dict[str, Any]],
    effective_void_event_ids: set[str],
) -> dict[str, Any]:
    case_id = str(lifecycle_case.get("case_id") or "").strip()
    base = {
        "resolver_schema_version": LIFECYCLE_ANCHOR_RESOLUTION_SCHEMA,
        "case_id": case_id,
        "status": "missing",
        "anchor_facts": [],
        "requested_reservations_by_lot": {},
        "effective_reservations_by_lot": {},
        "reason_codes": [],
        "timing_policy_hash": (
            canonical_sha256(timing_policy) if timing_policy else None
        ),
    }
    if str(lifecycle_case.get("schema_version") or "").strip() != LIFECYCLE_CASE_SCHEMA:
        return _conflicted_resolution(base, "lifecycle_case_schema_invalid")
    manifest = _case_target_manifest(lifecycle_case)
    if not manifest:
        return _conflicted_resolution(base, "lifecycle_target_manifest_invalid")
    for lot_id, contracts in manifest.items():
        lot = lots_by_id.get(lot_id)
        if lot is None:
            return _conflicted_resolution(base, "lifecycle_target_lot_missing")
        capacity = _lot_contract_capacity(lot)
        if capacity is None or contracts > capacity:
            return _conflicted_resolution(base, "lifecycle_target_quantity_invalid")

    direct_rows = [
        item
        for item in evidence_rows
        if _evidence_type(item) == ZERO_PRICE_OPTION_CLOSE_EVIDENCE
    ]
    bridge_rows = [
        item
        for item in evidence_rows
        if _evidence_type(item) == "migration_bridge"
    ]
    if direct_rows and bridge_rows:
        return _conflicted_resolution(base, "direct_and_migration_bridge_conflict")
    if not direct_rows and not bridge_rows:
        base["resolution_hash"] = _hash_without(base, "resolution_hash")
        return base

    anchor_facts: list[dict[str, Any]] = []
    reasons: set[str] = set()
    if direct_rows:
        direct_ids = {
            str(item.get("evidence_id") or "").strip()
            for item in direct_rows
        }
        extra_claims = [
            item
            for item in case_claims
            if str(item.get("source_role") or "").strip().lower()
            == "option_anchor"
            and str(item.get("owner_evidence_id") or "").strip()
            not in direct_ids
        ]
        if extra_claims:
            reasons.add("direct_anchor_claim_unbound")
        seen_source_keys: dict[str, tuple[str, str]] = {}
        claimed_lots: set[str] = set()
        for evidence_row in sorted(
            direct_rows,
            key=lambda item: str(item.get("evidence_id") or ""),
        ):
            evidence_id = str(evidence_row.get("evidence_id") or "").strip()
            owner_claims = [
                item
                for item in claims_by_evidence.get(evidence_id, ())
                if str(item.get("source_role") or "").strip().lower()
                == "option_anchor"
            ]
            fact, fact_reasons = _validated_anchor_fact(
                lifecycle_case=lifecycle_case,
                evidence=evidence_row,
                owner_claims=owner_claims,
                manifest=_evidence_manifest(evidence_row),
                anchor_kind="direct",
                bridge_evidence_id=None,
            )
            reasons.update(fact_reasons)
            if fact is None:
                continue
            source_key = str(fact["source_key"])
            identity = (
                str(fact["source_owner_evidence_id"]),
                str(fact["source_payload_hash"]),
            )
            if source_key in seen_source_keys and seen_source_keys[source_key] != identity:
                reasons.add("direct_anchor_source_collision")
                continue
            seen_source_keys[source_key] = identity
            fact_lots = set(fact["target_contracts_by_lot"])
            if claimed_lots & fact_lots:
                reasons.add("direct_anchor_manifest_overlap")
                continue
            claimed_lots.update(fact_lots)
            anchor_facts.append(fact)
    else:
        if len(bridge_rows) != 1:
            reasons.add("migration_bridge_option_anchor_ambiguous")
        else:
            bridge = bridge_rows[0]
            bridge_id = str(bridge.get("evidence_id") or "").strip()
            legacy_case_id = str(
                bridge.get("referenced_legacy_case_id") or ""
            ).strip()
            legacy_evidence_id = str(
                bridge.get("referenced_legacy_evidence_id") or ""
            ).strip()
            legacy_case = cases_by_id.get(legacy_case_id)
            legacy_evidence = evidence_by_id.get(legacy_evidence_id)
            if not _valid_bridge_binding(
                lifecycle_case=lifecycle_case,
                bridge=bridge,
                legacy_case=legacy_case,
                legacy_evidence=legacy_evidence,
            ):
                reasons.add("migration_bridge_invalid")
            elif legacy_evidence is not None:
                owner_claims = [
                    item
                    for item in claims_by_evidence.get(legacy_evidence_id, ())
                    if str(item.get("source_role") or "").strip().lower()
                    == "option_anchor"
                ]
                fact, fact_reasons = _validated_anchor_fact(
                    lifecycle_case=lifecycle_case,
                    evidence=legacy_evidence,
                    owner_claims=owner_claims,
                    manifest=manifest,
                    anchor_kind="migration_bridge",
                    bridge_evidence_id=bridge_id,
                    expected_owner_case_id=legacy_case_id,
                )
                reasons.update(fact_reasons)
                if fact is not None:
                    anchor_facts.append(fact)
            target_option_claims = [
                item
                for item in case_claims
                if str(item.get("source_role") or "").strip().lower()
                == "option_anchor"
            ]
            if target_option_claims:
                reasons.add("migration_bridge_competing_owner_claim")

    if reasons or not anchor_facts:
        return _conflicted_resolution(
            base,
            *(sorted(reasons) or ["lifecycle_anchor_invalid"]),
        )

    requested: dict[str, int] = {}
    for fact in anchor_facts:
        for lot_id, contracts in fact["target_contracts_by_lot"].items():
            if lot_id not in manifest or contracts > manifest[lot_id]:
                reasons.add("lifecycle_anchor_target_quantity_invalid")
                continue
            requested[lot_id] = requested.get(lot_id, 0) + contracts
    allocated: dict[str, int] = {}
    for item in allocation_rows:
        if bool(item.get("voided")):
            continue
        terminal_event_id = str(
            item.get("canonical_terminal_event_id") or ""
        ).strip()
        if terminal_event_id in effective_void_event_ids:
            continue
        lot_id = str(item.get("target_lot_id") or "").strip()
        contracts = _positive_integer(item.get("contracts_allocated"))
        if lot_id not in manifest or contracts is None:
            reasons.add("lifecycle_allocation_invalid")
            continue
        allocated[lot_id] = allocated.get(lot_id, 0) + contracts
    reservations: dict[str, int] = {}
    for lot_id, observed in requested.items():
        assigned = allocated.get(lot_id, 0)
        if assigned > observed:
            reasons.add("lifecycle_allocation_exceeds_anchor")
            continue
        outstanding = observed - assigned
        if outstanding:
            reservations[lot_id] = outstanding
    if reasons:
        return _conflicted_resolution(base, *sorted(reasons))

    base.update(
        {
            "status": "direct" if direct_rows else "bridged",
            "anchor_facts": sorted(
                anchor_facts,
                key=lambda item: str(item.get("anchor_fact_id") or ""),
            ),
            "requested_reservations_by_lot": dict(sorted(reservations.items())),
        }
    )
    base["resolution_hash"] = _hash_without(base, "resolution_hash")
    return base


def advance_direct_lifecycle_anchor_resolution(
    *,
    lifecycle_case: Mapping[str, Any],
    prior_resolution: Mapping[str, Any],
    evidence: Mapping[str, Any],
    source_claim: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and add one direct anchor using only compact prior facts."""

    case = dict(lifecycle_case)
    case_id = str(case.get("case_id") or "").strip()
    prior = dict(prior_resolution)
    prior_status = str(prior.get("status") or "missing").strip().lower()
    if prior_status not in {"missing", "direct"}:
        raise LifecycleOverlayContractError(
            "incremental direct anchor requires missing or direct prior state"
        )
    manifest = _evidence_manifest(evidence)
    target = _case_target_manifest(case)
    fact, reasons = _validated_anchor_fact(
        lifecycle_case=case,
        evidence=evidence,
        owner_claims=(source_claim,),
        manifest=manifest,
        anchor_kind="direct",
        bridge_evidence_id=None,
    )
    if fact is None or reasons:
        raise LifecycleOverlayContractError(
            ",".join(sorted(reasons)) or "direct anchor is invalid"
        )
    if target is None:
        raise LifecycleOverlayContractError(
            "lifecycle target manifest is invalid"
        )
    anchors = [
        dict(item)
        for item in prior.get("anchor_facts") or []
        if isinstance(item, Mapping)
    ]
    if any(item.get("anchor_fact_id") == fact["anchor_fact_id"] for item in anchors):
        result = {
            "resolver_schema_version": LIFECYCLE_ANCHOR_RESOLUTION_SCHEMA,
            "case_id": case_id,
            "status": prior_status,
            "anchor_facts": anchors,
            "requested_reservations_by_lot": dict(
                prior.get("requested_reservations_by_lot") or {}
            ),
            "effective_reservations_by_lot": dict(
                prior.get("effective_reservations_by_lot") or {}
            ),
            "reason_codes": list(
                prior.get("reason_codes")
                or prior.get("contested_reason_codes")
                or []
            ),
            "timing_policy_hash": None,
        }
        result["resolution_hash"] = _hash_without(result, "resolution_hash")
        return result
    base = {
        "resolver_schema_version": LIFECYCLE_ANCHOR_RESOLUTION_SCHEMA,
        "case_id": case_id,
        "status": "direct",
        "anchor_facts": [],
        "requested_reservations_by_lot": {},
        "effective_reservations_by_lot": {},
        "reason_codes": [],
        "timing_policy_hash": None,
    }
    if any(
        str(item.get("source_key") or "") == str(fact["source_key"])
        for item in anchors
    ):
        return _conflicted_resolution(base, "direct_anchor_source_collision")
    if any(
        set(item.get("target_contracts_by_lot") or {})
        & set(fact["target_contracts_by_lot"])
        for item in anchors
    ):
        return _conflicted_resolution(
            base,
            "direct_anchor_manifest_overlap",
        )
    requested = dict(prior.get("requested_reservations_by_lot") or {})
    for lot_id, contracts in fact["target_contracts_by_lot"].items():
        requested[lot_id] = int(requested.get(lot_id, 0)) + int(contracts)
        if lot_id not in target or requested[lot_id] > int(target[lot_id]):
            return _conflicted_resolution(
                base,
                "lifecycle_anchor_target_quantity_invalid",
            )
    base.update(
        {
            "anchor_facts": sorted(
                [*anchors, fact],
                key=lambda item: str(item.get("anchor_fact_id") or ""),
            ),
            "requested_reservations_by_lot": dict(sorted(requested.items())),
            "effective_reservations_by_lot": dict(sorted(requested.items())),
        }
    )
    base["resolution_hash"] = _hash_without(base, "resolution_hash")
    return base


def _validated_anchor_fact(
    *,
    lifecycle_case: Mapping[str, Any],
    evidence: Mapping[str, Any],
    owner_claims: Sequence[Mapping[str, Any]],
    manifest: dict[str, int] | None,
    anchor_kind: str,
    bridge_evidence_id: str | None,
    expected_owner_case_id: str | None = None,
) -> tuple[dict[str, Any] | None, set[str]]:
    reasons: set[str] = set()
    case_id = str(lifecycle_case.get("case_id") or "").strip()
    evidence_id = str(evidence.get("evidence_id") or "").strip()
    owner_case_id = str(
        expected_owner_case_id
        if expected_owner_case_id is not None
        else case_id
    ).strip()
    if (
        not evidence_id
        or _evidence_type(evidence) != ZERO_PRICE_OPTION_CLOSE_EVIDENCE
        or str(evidence.get("case_id") or "").strip() != owner_case_id
        or not _decimal_equal(_evidence_value(evidence, "price"), 0)
        or manifest is None
    ):
        reasons.add(f"{anchor_kind}_anchor_evidence_invalid")
        return None, reasons
    if len(owner_claims) != 1:
        reasons.add(f"{anchor_kind}_anchor_source_claim_not_unique")
        return None, reasons
    claim = dict(owner_claims[0])
    payload = (
        dict(claim.get("source_payload") or {})
        if isinstance(claim.get("source_payload"), Mapping)
        else {}
    )
    source_key = str(claim.get("source_key") or "").strip()
    if (
        str(claim.get("schema_version") or "").strip()
        != SOURCE_CONSUMPTION_SCHEMA
        or str(claim.get("case_id") or "").strip() != owner_case_id
        or str(claim.get("owner_evidence_id") or "").strip() != evidence_id
        or str(claim.get("source_role") or "").strip().lower()
        != "option_anchor"
        or str(payload.get("schema_version") or "").strip()
        != SOURCE_PAYLOAD_SCHEMA
        or str(payload.get("source_key") or "").strip() != source_key
        or str(claim.get("source_payload_hash") or "").strip()
        != canonical_source_payload_hash(payload)
        or not _claim_matches_case(payload, lifecycle_case)
    ):
        reasons.add(f"{anchor_kind}_anchor_source_claim_invalid")
        return None, reasons
    evidence_source = str(evidence.get("source_event_id") or "").strip()
    if evidence_source.startswith("futu:") and evidence_source != source_key:
        reasons.add(f"{anchor_kind}_anchor_source_key_mismatch")
        return None, reasons
    quantity = _positive_integer(payload.get("quantity"))
    execution_time_ms = _positive_integer(payload.get("execution_time_ms"))
    received_at_ms = _positive_integer(
        evidence.get("received_at_ms")
        or evidence.get("_ledger_created_at_ms")
    )
    if (
        quantity is None
        or quantity != sum(manifest.values())
        or execution_time_ms is None
        or received_at_ms is None
    ):
        reasons.add(f"{anchor_kind}_anchor_quantity_or_time_invalid")
        return None, reasons
    if evidence.get("contracts") not in (None, ""):
        evidence_contracts = _positive_integer(evidence.get("contracts"))
        if evidence_contracts != quantity:
            reasons.add(f"{anchor_kind}_anchor_quantity_mismatch")
            return None, reasons

    fact = {
        "anchor_kind": anchor_kind,
        "canonical_case_id": case_id,
        "bridge_evidence_id": bridge_evidence_id,
        "source_owner_case_id": owner_case_id,
        "source_owner_evidence_id": evidence_id,
        "source_key": source_key,
        "source_payload_hash": str(claim["source_payload_hash"]),
        "futu_account_id": str(payload.get("futu_account_id") or "").strip(),
        "execution_time_ms": execution_time_ms,
        "received_at_ms": received_at_ms,
        "quantity": quantity,
        "target_contracts_by_lot": dict(sorted(manifest.items())),
    }
    fact["anchor_fact_id"] = canonical_sha256(
        {
            "schema_version": LIFECYCLE_ANCHOR_RESOLUTION_SCHEMA,
            **fact,
        }
    )
    fact["anchor_fact_hash"] = _hash_without(fact, "anchor_fact_hash")
    return fact, reasons


def _claim_matches_case(
    payload: Mapping[str, Any],
    lifecycle_case: Mapping[str, Any],
) -> bool:
    return (
        str(payload.get("account") or "").strip().lower()
        == str(lifecycle_case.get("account") or "").strip().lower()
        and str(payload.get("futu_account_id") or "").strip()
        == str(lifecycle_case.get("futu_account_id") or "").strip()
        and str(payload.get("symbol") or "").strip().upper()
        == str(lifecycle_case.get("symbol") or "").strip().upper()
        and str(payload.get("option_type") or "").strip().lower()
        == str(lifecycle_case.get("option_type") or "").strip().lower()
        and str(payload.get("position_side") or "").strip().lower()
        == str(lifecycle_case.get("position_side") or "").strip().lower()
        and str(payload.get("expiration_ymd") or "").strip()
        == str(lifecycle_case.get("expiration_ymd") or "").strip()
        and _decimal_equal(payload.get("strike"), lifecycle_case.get("strike"))
        and _decimal_equal(payload.get("price"), 0)
    )


def _valid_bridge_binding(
    *,
    lifecycle_case: Mapping[str, Any],
    bridge: Mapping[str, Any],
    legacy_case: Mapping[str, Any] | None,
    legacy_evidence: Mapping[str, Any] | None,
) -> bool:
    case_id = str(lifecycle_case.get("case_id") or "").strip()
    legacy_case_id = str(
        bridge.get("referenced_legacy_case_id") or ""
    ).strip()
    legacy_evidence_id = str(
        bridge.get("referenced_legacy_evidence_id") or ""
    ).strip()
    return bool(
        str(bridge.get("schema_version") or "").strip()
        == "migration_bridge_evidence.v1"
        and bridge.get("allocating") is False
        and str(bridge.get("case_id") or "").strip() == case_id
        and str(bridge.get("account") or "").strip().lower()
        == str(lifecycle_case.get("account") or "").strip().lower()
        and str(bridge.get("symbol") or "").strip().upper()
        == str(lifecycle_case.get("symbol") or "").strip().upper()
        and isinstance(legacy_case, Mapping)
        and isinstance(legacy_evidence, Mapping)
        and str(legacy_case.get("case_id") or "").strip() == legacy_case_id
        and str(legacy_case.get("status") or "").strip().lower()
        == "superseded"
        and str(legacy_case.get("superseded_by_case_id") or "").strip()
        == case_id
        and str(legacy_evidence.get("evidence_id") or "").strip()
        == legacy_evidence_id
        and str(legacy_evidence.get("case_id") or "").strip()
        == legacy_case_id
        and _evidence_type(legacy_evidence)
        == ZERO_PRICE_OPTION_CLOSE_EVIDENCE
    )


def _case_target_manifest(
    lifecycle_case: Mapping[str, Any],
) -> dict[str, int] | None:
    raw = lifecycle_case.get("target_contracts_by_lot")
    return _normalize_manifest(raw)


def _evidence_manifest(
    evidence: Mapping[str, Any],
) -> dict[str, int] | None:
    raw = evidence.get("target_contracts_by_lot")
    canonical = _normalize_manifest(raw) if isinstance(raw, Mapping) else None
    single_lot = str(evidence.get("target_lot_id") or "").strip()
    single_contracts = _positive_integer(evidence.get("contracts"))
    legacy = (
        {single_lot: single_contracts}
        if single_lot and single_contracts is not None
        else None
    )
    if canonical is not None and legacy is not None and canonical != legacy:
        return None
    return canonical if canonical is not None else legacy


def _normalize_manifest(value: Any) -> dict[str, int] | None:
    if not isinstance(value, Mapping) or not value:
        return None
    normalized: dict[str, int] = {}
    for raw_lot_id, raw_contracts in value.items():
        lot_id = str(raw_lot_id or "").strip()
        contracts = _positive_integer(raw_contracts)
        if not lot_id or contracts is None or lot_id in normalized:
            return None
        normalized[lot_id] = contracts
    return dict(sorted(normalized.items()))


def _lot_contract_capacity(lot: Mapping[str, Any]) -> int | None:
    fields = (
        dict(lot.get("fields") or {})
        if isinstance(lot.get("fields"), Mapping)
        else dict(lot)
    )
    for key in ("original_contracts", "contracts"):
        value = _positive_integer(fields.get(key))
        if value is not None:
            return value
    return None


def _reservation_conflict_components(
    resolutions: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    case_to_lots = {
        case_id: set(
            str(lot_id)
            for lot_id in (
                resolution.get("requested_reservations_by_lot") or {}
            )
        )
        for case_id, resolution in resolutions.items()
        if resolution.get("status") != "conflict"
        and resolution.get("requested_reservations_by_lot")
    }
    lot_to_cases: dict[str, set[str]] = {}
    for case_id, lot_ids in case_to_lots.items():
        for lot_id in lot_ids:
            lot_to_cases.setdefault(lot_id, set()).add(case_id)

    visited: set[str] = set()
    components: list[dict[str, Any]] = []
    for start in sorted(case_to_lots):
        if start in visited:
            continue
        pending = [start]
        component_cases: set[str] = set()
        component_lots: set[str] = set()
        while pending:
            case_id = pending.pop()
            if case_id in component_cases:
                continue
            component_cases.add(case_id)
            visited.add(case_id)
            for lot_id in case_to_lots.get(case_id, set()):
                component_lots.add(lot_id)
                pending.extend(lot_to_cases.get(lot_id, set()) - component_cases)
        contested = sorted(
            lot_id
            for lot_id in component_lots
            if len(lot_to_cases.get(lot_id, set())) > 1
        )
        if contested:
            components.append(
                {
                    "case_ids": sorted(component_cases),
                    "lot_ids": sorted(component_lots),
                    "contested_lot_ids": contested,
                }
            )
    return sorted(components, key=lambda item: tuple(item["case_ids"]))


def _case_generation_tokens(
    *,
    account: str,
    case_rows: Sequence[Mapping[str, Any]],
    active_cases: Sequence[Mapping[str, Any]],
    target_manifests: Mapping[str, Mapping[str, int]],
    case_resolutions: Mapping[str, Mapping[str, Any]],
    evidence_rows: Sequence[Mapping[str, Any]],
    allocation_rows: Sequence[Mapping[str, Any]],
    claim_rows: Sequence[Mapping[str, Any]],
    timing_rows: Sequence[Mapping[str, Any]],
    lot_rows: Sequence[Mapping[str, Any]],
    event_rows: Sequence[Mapping[str, Any]],
    effective_void_event_ids: Sequence[str],
) -> list[dict[str, Any]]:
    active_cases_by_id = {
        str(item.get("case_id") or "").strip(): dict(item)
        for item in active_cases
    }
    all_cases_by_id = {
        str(item.get("case_id") or "").strip(): dict(item)
        for item in case_rows
    }
    lots_by_id = {
        str(item.get("record_id") or "").strip(): dict(item)
        for item in lot_rows
    }
    out: list[dict[str, Any]] = []
    for case_id in sorted(active_cases_by_id):
        dependency_ids = _target_dependency_case_ids(
            case_id=case_id,
            target_manifests=target_manifests,
        )
        target_lots = {
            lot_id
            for dependency_case_id in dependency_ids
            for lot_id in target_manifests.get(dependency_case_id, {})
        }
        dependency_set = set(dependency_ids)
        referenced_legacy_case_ids = {
            str(item.get("referenced_legacy_case_id") or "").strip()
            for item in evidence_rows
            if str(item.get("case_id") or "").strip() in dependency_set
            and _evidence_type(item) == "migration_bridge"
            and str(item.get("referenced_legacy_case_id") or "").strip()
        }
        raw_case_ids = dependency_set | referenced_legacy_case_ids
        relevant_events = _events_for_lots(event_rows, target_lots)
        relevant_event_ids = {
            str(item.get("event_id") or "").strip()
            for item in relevant_events
        }
        relevant_voids = sorted(
            event_id
            for event_id in effective_void_event_ids
            if event_id in relevant_event_ids
        )
        preimage = {
            "schema_version": LIFECYCLE_GENERATION_TOKEN_SCHEMA,
            "account": account,
            "case_id": case_id,
            "dependency_case_ids": dependency_ids,
            "target_lot_ids": sorted(target_lots),
            "cases": [
                all_cases_by_id[item]
                for item in sorted(raw_case_ids)
                if item in all_cases_by_id
            ],
            "case_resolution_hashes": [
                {
                    "case_id": item,
                    "resolution_hash": case_resolutions[item].get(
                        "resolution_hash"
                    ),
                }
                for item in dependency_ids
            ],
            "evidence": sorted(
                (
                    dict(item)
                    for item in evidence_rows
                    if str(item.get("case_id") or "").strip()
                    in raw_case_ids
                ),
                key=lambda item: str(item.get("evidence_id") or ""),
            ),
            "allocations": sorted(
                (
                    dict(item)
                    for item in allocation_rows
                    if str(item.get("case_id") or "").strip()
                    in raw_case_ids
                ),
                key=lambda item: str(item.get("allocation_id") or ""),
            ),
            "source_claims": sorted(
                (
                    dict(item)
                    for item in claim_rows
                    if str(item.get("case_id") or "").strip()
                    in raw_case_ids
                ),
                key=lambda item: str(item.get("source_key") or ""),
            ),
            "timing_policies": sorted(
                (
                    dict(item)
                    for item in timing_rows
                    if str(item.get("case_id") or "").strip()
                    in raw_case_ids
                ),
                key=lambda item: str(item.get("case_id") or ""),
            ),
            "position_lots": [
                lots_by_id[lot_id]
                for lot_id in sorted(target_lots)
                if lot_id in lots_by_id
            ],
            "trade_events": relevant_events,
            "effective_void_event_ids": relevant_voids,
        }
        out.append(
            {
                "schema_version": LIFECYCLE_GENERATION_TOKEN_SCHEMA,
                "case_id": case_id,
                "dependency_case_ids": dependency_ids,
                "target_lot_ids": sorted(target_lots),
                "generation_token": canonical_sha256(preimage),
            }
        )
    return out


def _target_dependency_case_ids(
    *,
    case_id: str,
    target_manifests: Mapping[str, Mapping[str, int]],
) -> list[str]:
    """Return the transitive potential-overlap component for one case."""

    selected: set[str] = {case_id}
    selected_lots = set(target_manifests.get(case_id, {}))
    changed = True
    while changed:
        changed = False
        for other_case_id, manifest in target_manifests.items():
            if other_case_id in selected or not selected_lots.intersection(manifest):
                continue
            selected.add(other_case_id)
            selected_lots.update(manifest)
            changed = True
    return sorted(selected)


def _events_for_lots(
    event_rows: Sequence[Mapping[str, Any]],
    lot_ids: set[str],
) -> list[dict[str, Any]]:
    event_by_id = {
        str(item.get("event_id") or "").strip(): dict(item)
        for item in event_rows
    }
    selected_ids = {
        event_id
        for event_id, item in event_by_id.items()
        if lot_ids
        & {
            str(item.get("lot_id") or "").strip(),
            str(item.get("target_lot_id") or "").strip(),
        }
    }
    changed = True
    while changed:
        changed = False
        for event_id, item in event_by_id.items():
            target_event_id = str(item.get("target_event_id") or "").strip()
            if target_event_id in selected_ids and event_id not in selected_ids:
                selected_ids.add(event_id)
                changed = True
    return [event_by_id[event_id] for event_id in sorted(selected_ids)]


def _conflicted_resolution(
    base: Mapping[str, Any],
    *reason_codes: str,
) -> dict[str, Any]:
    result = {
        **dict(base),
        "status": "conflict",
        "anchor_facts": [],
        "requested_reservations_by_lot": {},
        "effective_reservations_by_lot": {},
        "reason_codes": sorted(
            {
                str(item or "").strip()
                for item in reason_codes
                if str(item or "").strip()
            }
        ),
    }
    result["resolution_hash"] = _hash_without(result, "resolution_hash")
    return result


def _unique_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    id_field: str,
    kind: str,
    allow_empty: bool = False,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise LifecycleOverlayContractError(f"{kind} row must be an object")
        item = dict(raw)
        identity = str(item.get(id_field) or "").strip()
        if not identity:
            if allow_empty:
                continue
            raise LifecycleOverlayContractError(f"{kind} {id_field} is required")
        if identity in seen:
            raise LifecycleOverlayContractError(f"duplicate {kind} {id_field}: {identity}")
        seen.add(identity)
        out.append(item)
    return out


def _group_rows(
    rows: Iterable[dict[str, Any]],
    field: str,
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for item in rows:
        key = str(item.get(field) or "").strip()
        if key:
            out.setdefault(key, []).append(item)
    return out


def _hash_without(payload: Mapping[str, Any], field: str) -> str:
    return canonical_sha256(
        {key: value for key, value in payload.items() if key != field}
    )


def _evidence_type(item: Mapping[str, Any]) -> str:
    return str(item.get("evidence_type") or "").strip().lower()


def _evidence_value(item: Mapping[str, Any], field: str) -> Any:
    if item.get(field) not in (None, ""):
        return item.get(field)
    raw = item.get("raw")
    if isinstance(raw, Mapping):
        if raw.get(field) not in (None, ""):
            return raw.get(field)
        option_deal = raw.get("option_deal")
        if isinstance(option_deal, Mapping):
            return option_deal.get(field)
    return None


def _decimal_equal(left: Any, right: Any) -> bool:
    try:
        left_value = Decimal(str(left))
        right_value = Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return (
        left_value.is_finite()
        and right_value.is_finite()
        and left_value == right_value
    )


__all__ = [
    "ACCOUNT_LIFECYCLE_RESOLUTION_SCHEMA",
    "LifecycleEvidenceFacts",
    "LifecycleOverlayContractError",
    "LIFECYCLE_ANCHOR_RESOLUTION_SCHEMA",
    "LIFECYCLE_GENERATION_TOKEN_SCHEMA",
    "NON_ALLOCATING_EVIDENCE_TYPES",
    "ZERO_PRICE_OPTION_CLOSE_EVIDENCE",
    "advance_direct_lifecycle_anchor_resolution",
    "lifecycle_case_generation_token",
    "lifecycle_case_resolution",
    "lifecycle_evidence_facts",
    "arbitrate_lifecycle_case_resolutions",
    "resolve_account_lifecycle_overlay",
    "resolve_lifecycle_account_rows",
]
