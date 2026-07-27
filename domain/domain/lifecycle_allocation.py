from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from itertools import product
from typing import Any, Iterable


TERMINAL_TYPES = frozenset({"assignment", "exercise", "expire_close"})


@dataclass(frozen=True)
class AllocationResolution:
    status: str
    target_contracts_by_lot: dict[str, int]
    resolved_contracts_by_lot: dict[str, int]
    remaining_contracts_by_lot: dict[str, int]
    resolved_contracts_by_terminal_type: dict[str, int]
    reason_codes: tuple[str, ...] = ()

    @property
    def remaining_contracts(self) -> int:
        return sum(self.remaining_contracts_by_lot.values())

    @property
    def resolved_contracts(self) -> int:
        return sum(self.resolved_contracts_by_lot.values())


@dataclass(frozen=True)
class AllocationPlan:
    status: str
    allocations: tuple[dict[str, Any], ...] = ()
    reason_codes: tuple[str, ...] = ()


def _positive_contracts(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        numeric = Decimal(str(value))
        parsed = int(numeric)
    except (InvalidOperation, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if not numeric.is_finite() or parsed <= 0 or numeric != parsed:
        raise ValueError(f"{field} must be a positive integer")
    return parsed


def normalize_target_manifest(target_contracts_by_lot: dict[str, Any]) -> dict[str, int]:
    if not isinstance(target_contracts_by_lot, dict) or not target_contracts_by_lot:
        raise ValueError("target_contracts_by_lot must be a non-empty object")
    normalized: dict[str, int] = {}
    for raw_lot_id, raw_contracts in sorted(target_contracts_by_lot.items()):
        lot_id = str(raw_lot_id or "").strip()
        if not lot_id:
            raise ValueError("target lot id is required")
        if lot_id in normalized:
            raise ValueError(f"duplicate normalized target lot id: {lot_id}")
        normalized[lot_id] = _positive_contracts(raw_contracts, field=f"target contracts for {lot_id}")
    return normalized


def _normalize_remaining(remaining_contracts_by_lot: dict[str, Any]) -> dict[str, int]:
    if not isinstance(remaining_contracts_by_lot, dict) or not remaining_contracts_by_lot:
        raise ValueError("remaining_contracts_by_lot must be a non-empty object")
    normalized: dict[str, int] = {}
    for raw_lot_id, raw_contracts in sorted(remaining_contracts_by_lot.items()):
        lot_id = str(raw_lot_id or "").strip()
        if not lot_id or isinstance(raw_contracts, bool):
            raise ValueError("remaining lot id and quantity are invalid")
        try:
            numeric = Decimal(str(raw_contracts))
            contracts = int(numeric)
        except (InvalidOperation, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("remaining contracts must be a nonnegative integer") from exc
        if not numeric.is_finite() or contracts < 0 or numeric != contracts:
            raise ValueError("remaining contracts must be a nonnegative integer")
        if lot_id in normalized:
            raise ValueError(f"duplicate normalized remaining lot id: {lot_id}")
        normalized[lot_id] = contracts
    return normalized


def allocation_id_for(*, case_id: str, evidence_id: str, target_lot_id: str) -> str:
    case = str(case_id or "").strip()
    evidence = str(evidence_id or "").strip()
    lot = str(target_lot_id or "").strip()
    if not case or not evidence or not lot:
        raise ValueError("case_id, evidence_id and target_lot_id are required")
    raw = f"{case}{evidence}{lot}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def terminal_event_id_for(
    *,
    case_id: str,
    evidence_id: str,
    target_lot_id: str,
    terminal_type: str,
    contracts_allocated: int,
) -> str:
    case = str(case_id or "").strip()
    evidence = str(evidence_id or "").strip()
    lot = str(target_lot_id or "").strip()
    if not case or not evidence or not lot:
        raise ValueError("case_id, evidence_id and target_lot_id are required")
    terminal = str(terminal_type or "").strip().lower()
    if terminal not in TERMINAL_TYPES:
        raise ValueError(f"unsupported lifecycle terminal_type: {terminal_type}")
    contracts = _positive_contracts(contracts_allocated, field="contracts_allocated")
    raw = f"{case}{evidence}{lot}{terminal}{contracts}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def resolve_allocations(
    target_contracts_by_lot: dict[str, Any],
    allocations: Iterable[dict[str, Any]],
    *,
    void_event_ids: Iterable[str] = (),
) -> AllocationResolution:
    target = normalize_target_manifest(target_contracts_by_lot)
    voided = {str(item or "").strip() for item in void_event_ids if str(item or "").strip()}
    resolved = {lot_id: 0 for lot_id in target}
    by_type: dict[str, int] = {}
    reasons: set[str] = set()
    seen_allocations: dict[str, tuple[Any, ...]] = {}
    seen_bindings: dict[tuple[str, str, str], str] = {}

    for raw in allocations:
        row = dict(raw or {})
        allocation_id = str(row.get("allocation_id") or "").strip()
        lot_id = str(row.get("target_lot_id") or "").strip()
        terminal_type = str(row.get("terminal_type") or "").strip().lower()
        event_id = str(row.get("canonical_terminal_event_id") or "").strip()
        if event_id in voided or bool(row.get("voided")):
            continue
        case_id = str(row.get("case_id") or "").strip()
        evidence_id = str(row.get("evidence_id") or "").strip()
        if not allocation_id or lot_id not in target:
            reasons.add("allocation_target_unknown")
            continue
        if not case_id or not evidence_id or not event_id:
            reasons.add("allocation_identity_incomplete")
            continue
        if terminal_type not in TERMINAL_TYPES:
            reasons.add("allocation_terminal_type_invalid")
            continue
        try:
            contracts = _positive_contracts(row.get("contracts_allocated"), field="contracts_allocated")
        except ValueError:
            reasons.add("allocation_quantity_invalid")
            continue
        expected_allocation_id = allocation_id_for(
            case_id=case_id,
            evidence_id=evidence_id,
            target_lot_id=lot_id,
        )
        expected_event_id = terminal_event_id_for(
            case_id=case_id,
            evidence_id=evidence_id,
            target_lot_id=lot_id,
            terminal_type=terminal_type,
            contracts_allocated=contracts,
        )
        if allocation_id != expected_allocation_id:
            reasons.add("allocation_id_mismatch")
            continue
        if event_id != expected_event_id:
            reasons.add("terminal_event_id_mismatch")
            continue
        signature = (case_id, evidence_id, lot_id, terminal_type, contracts, event_id)
        previous = seen_allocations.get(allocation_id)
        if previous is not None:
            if previous != signature:
                reasons.add("allocation_id_conflict")
            continue
        seen_allocations[allocation_id] = signature
        binding = (case_id, evidence_id, lot_id)
        prior_binding = seen_bindings.get(binding)
        if prior_binding is not None and prior_binding != allocation_id:
            reasons.add("allocation_binding_conflict")
            continue
        seen_bindings[binding] = allocation_id
        resolved[lot_id] += contracts
        by_type[terminal_type] = by_type.get(terminal_type, 0) + contracts

    for lot_id, contracts in resolved.items():
        if contracts > target[lot_id]:
            reasons.add("allocation_exceeds_target")
    remaining = {lot_id: max(target[lot_id] - resolved[lot_id], 0) for lot_id in target}
    return AllocationResolution(
        status="conflict" if reasons else "ok",
        target_contracts_by_lot=target,
        resolved_contracts_by_lot=resolved,
        remaining_contracts_by_lot=remaining,
        resolved_contracts_by_terminal_type=dict(sorted(by_type.items())),
        reason_codes=tuple(sorted(reasons)),
    )


def _quantity_solutions(remaining: dict[str, int], quantity: int) -> list[dict[str, int]]:
    lot_ids = sorted(remaining)
    ranges = [range(0, remaining[lot_id] + 1) for lot_id in lot_ids]
    solutions: list[dict[str, int]] = []
    for values in product(*ranges):
        if sum(values) != quantity:
            continue
        solutions.append({lot_id: value for lot_id, value in zip(lot_ids, values) if value})
        if len(solutions) > 1:
            break
    return solutions


def plan_evidence_allocation(
    *,
    case_id: str,
    evidence_id: str,
    terminal_type: str,
    contracts: Any,
    remaining_contracts_by_lot: dict[str, Any],
    target_lot_id: str | None = None,
) -> AllocationPlan:
    case = str(case_id or "").strip()
    evidence = str(evidence_id or "").strip()
    if not case or not evidence:
        return AllocationPlan(status="conflict", reason_codes=("case_or_evidence_id_missing",))
    terminal = str(terminal_type or "").strip().lower()
    if terminal not in TERMINAL_TYPES:
        return AllocationPlan(status="conflict", reason_codes=("terminal_type_invalid",))
    try:
        quantity = _positive_contracts(contracts, field="evidence contracts")
        remaining = _normalize_remaining(remaining_contracts_by_lot)
    except ValueError as exc:
        return AllocationPlan(status="conflict", reason_codes=(str(exc),))
    if quantity > sum(remaining.values()):
        return AllocationPlan(status="conflict", reason_codes=("evidence_quantity_exceeds_remaining",))

    explicit_lot = str(target_lot_id or "").strip()
    if explicit_lot:
        if explicit_lot not in remaining or quantity > remaining[explicit_lot]:
            return AllocationPlan(status="conflict", reason_codes=("target_lot_quantity_mismatch",))
        selected = {explicit_lot: quantity}
    else:
        solutions = _quantity_solutions(remaining, quantity)
        if not solutions:
            return AllocationPlan(status="conflict", reason_codes=("evidence_quantity_unallocatable",))
        if len(solutions) != 1:
            return AllocationPlan(status="conflict", reason_codes=("ambiguous_quantity_binding",))
        selected = solutions[0]

    rows: list[dict[str, Any]] = []
    for lot_id, allocated in sorted(selected.items()):
        rows.append(
            {
                "allocation_id": allocation_id_for(
                    case_id=case,
                    evidence_id=evidence,
                    target_lot_id=lot_id,
                ),
                "case_id": case,
                "evidence_id": evidence,
                "target_lot_id": lot_id,
                "terminal_type": terminal,
                "contracts_allocated": allocated,
                "canonical_terminal_event_id": terminal_event_id_for(
                    case_id=case,
                    evidence_id=evidence,
                    target_lot_id=lot_id,
                    terminal_type=terminal,
                    contracts_allocated=allocated,
                ),
            }
        )
    return AllocationPlan(status="planned", allocations=tuple(rows))


__all__ = [
    "AllocationPlan",
    "AllocationResolution",
    "TERMINAL_TYPES",
    "allocation_id_for",
    "normalize_target_manifest",
    "plan_evidence_allocation",
    "resolve_allocations",
    "terminal_event_id_for",
]
