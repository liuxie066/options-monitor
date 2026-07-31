from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable


ZERO_PRICE_OPTION_CLOSE_EVIDENCE = "option_zero_price_close"


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


__all__ = [
    "LifecycleEvidenceFacts",
    "ZERO_PRICE_OPTION_CLOSE_EVIDENCE",
    "lifecycle_evidence_facts",
]
