from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from domain.domain.position_advice import decimal_value


ALLOCATOR_VERSION = "typed_deterministic_greedy.v2"
RESOURCE_POOL_CONTRACTS = {
    "cash_base_cny": ("cash:", "CNY"),
    "covered_shares": ("shares:", "shares"),
}


@dataclass(frozen=True)
class AllocatorResult:
    selected: tuple[dict[str, Any], ...]
    alternatives: tuple[dict[str, Any], ...]
    resource_pools_before: dict[str, dict[str, str]]
    resource_pools_after: dict[str, dict[str, str]]
    candidate_quantity_before: dict[str, int]
    candidate_quantity_after: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "allocator_version": ALLOCATOR_VERSION,
            "selected": [dict(item) for item in self.selected],
            "alternatives": [dict(item) for item in self.alternatives],
            "resource_pools_before": self.resource_pools_before,
            "resource_pools_after": self.resource_pools_after,
            "candidate_quantity_before": self.candidate_quantity_before,
            "candidate_quantity_after": self.candidate_quantity_after,
        }


def _decimal_text(value: Decimal) -> str:
    rendered = format(value.normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _normalize_pools(raw_pools: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    pools: dict[str, dict[str, Any]] = {}
    for pool_key, raw in sorted((raw_pools or {}).items()):
        key = str(pool_key or "").strip()
        item = dict(raw or {})
        kind = str(item.get("resource_kind") or "").strip()
        unit = str(item.get("unit") or "").strip()
        if not key or not kind or not unit:
            raise ValueError("resource pool requires pool_key, resource_kind and unit")
        contract = RESOURCE_POOL_CONTRACTS.get(kind)
        if contract is None or not key.startswith(contract[0]) or unit != contract[1]:
            raise ValueError(f"resource pool contract is invalid: {key}")
        available = decimal_value(item.get("available"), field=f"{key}.available", nonnegative=True)
        pools[key] = {
            "resource_kind": kind,
            "unit": unit,
            "available": available,
        }
    return pools


def _normalize_candidate_quantities(raw: dict[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for candidate_id, quantity in sorted((raw or {}).items()):
        key = str(candidate_id or "").strip()
        if not key or isinstance(quantity, bool):
            raise ValueError("candidate quantity requires candidate id and nonnegative integer")
        try:
            numeric = decimal_value(quantity, field="candidate quantity", nonnegative=True)
            parsed = int(numeric)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("candidate quantity must be a nonnegative integer") from exc
        if numeric != parsed:
            raise ValueError("candidate quantity must be a nonnegative integer")
        result[key] = parsed
    return result


def _proposal_primary_pool(proposal: dict[str, Any]) -> tuple[str, str]:
    deltas = proposal.get("resource_deltas")
    rows = [dict(item or {}) for item in deltas] if isinstance(deltas, list) else []
    keys = sorted(
        (
            str(row.get("resource_kind") or "").strip(),
            str(row.get("pool_key") or "").strip(),
        )
        for row in rows
    )
    return keys[0] if keys else ("", "")


def _proposal_sort_key(proposal: dict[str, Any]) -> tuple[Any, ...]:
    kind, pool_key = _proposal_primary_pool(proposal)
    efficiency = decimal_value(
        proposal.get("pool_efficiency_improvement"),
        field="pool_efficiency_improvement",
    )
    improvement_field = "net_carry_improvement_H"
    if (
        kind == "cash_base_cny"
        and proposal.get("net_carry_improvement_H_base_cny") is not None
    ):
        improvement_field = "net_carry_improvement_H_base_cny"
    improvement = decimal_value(
        proposal.get(improvement_field),
        field=improvement_field,
    )
    return (
        kind,
        pool_key,
        -efficiency,
        -improvement,
        int(proposal.get("allocation_rank") or 0),
        ",".join(sorted(str(item) for item in proposal.get("source_position_ids") or [])),
        str(proposal.get("candidate_id") or ""),
        str(proposal.get("proposal_id") or ""),
    )


def _rejected(proposal: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        **proposal,
        "selected": False,
        "actionable": False,
        "allocator_reason": reason,
        "depends_on": [],
    }


def _positive_integer(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("quantity must be a positive integer")
    numeric = decimal_value(value, field="quantity", positive=True)
    parsed = int(numeric)
    if numeric != parsed:
        raise ValueError("quantity must be a positive integer")
    return parsed


def allocate_position_advice(
    *,
    proposals: list[dict[str, Any]],
    resource_pools: dict[str, dict[str, Any]],
    candidate_quantities: dict[str, Any],
) -> AllocatorResult:
    pools = _normalize_pools(resource_pools)
    quantities = _normalize_candidate_quantities(candidate_quantities)
    pools_before = {
        key: {
            "resource_kind": item["resource_kind"],
            "unit": item["unit"],
            "available": _decimal_text(item["available"]),
        }
        for key, item in pools.items()
    }
    quantities_before = dict(quantities)
    selected: list[dict[str, Any]] = []
    alternatives: list[dict[str, Any]] = []
    used_sources: set[str] = set()
    release_dependencies: dict[str, list[str]] = {key: [] for key in pools}
    proposal_id_counts: dict[str, int] = {}
    for raw in proposals or []:
        proposal_id = str(dict(raw or {}).get("proposal_id") or "").strip()
        if proposal_id:
            proposal_id_counts[proposal_id] = proposal_id_counts.get(proposal_id, 0) + 1

    sortable: list[dict[str, Any]] = []
    for raw in proposals or []:
        proposal = dict(raw or {})
        if proposal.get("replacement_eligibility") not in {
            "accepted_opening",
            "capacity_deferred_to_allocator",
        }:
            alternatives.append(_rejected(proposal, "replacement_ineligible"))
            continue
        try:
            sort_key = _proposal_sort_key(proposal)
        except (ValueError, TypeError):
            alternatives.append(_rejected(proposal, "proposal_economics_invalid"))
            continue
        if sort_key[2] >= 0 or sort_key[3] >= 0:
            alternatives.append(_rejected(proposal, "proposal_economics_nonpositive"))
            continue
        sortable.append(proposal)

    for proposal in sorted(sortable, key=_proposal_sort_key):
        proposal_id = str(proposal.get("proposal_id") or "").strip()
        candidate_id = str(proposal.get("candidate_id") or "").strip()
        sources = {str(item or "").strip() for item in proposal.get("source_position_ids") or []}
        sources.discard("")
        try:
            proposed_contracts = _positive_integer(proposal.get("candidate_contracts"))
        except (TypeError, ValueError, OverflowError):
            proposed_contracts = 0
        if not proposal_id or not candidate_id or not sources or proposed_contracts <= 0:
            alternatives.append(_rejected(proposal, "proposal_identity_invalid"))
            continue
        if proposal_id_counts.get(proposal_id) != 1:
            alternatives.append(_rejected(proposal, "proposal_id_conflict"))
            continue
        if used_sources.intersection(sources):
            alternatives.append(_rejected(proposal, "source_position_already_allocated"))
            continue
        if quantities.get(candidate_id, 0) < proposed_contracts:
            alternatives.append(_rejected(proposal, "candidate_quantity_exhausted"))
            continue

        raw_deltas = proposal.get("resource_deltas")
        deltas = [dict(item or {}) for item in raw_deltas] if isinstance(raw_deltas, list) else []
        if not deltas:
            alternatives.append(_rejected(proposal, "resource_delta_missing"))
            continue
        simulated: dict[str, Decimal] = {}
        normalized_deltas: list[dict[str, Any]] = []
        dependencies: set[str] = set()
        seen_delta_pools: set[str] = set()
        failure: str | None = None
        for delta in sorted(
            deltas,
            key=lambda item: (
                str(item.get("resource_kind") or ""),
                str(item.get("pool_key") or ""),
            ),
        ):
            pool_key = str(delta.get("pool_key") or "").strip()
            kind = str(delta.get("resource_kind") or "").strip()
            unit = str(delta.get("unit") or "").strip()
            pool = pools.get(pool_key)
            if pool is None or pool["resource_kind"] != kind or pool["unit"] != unit:
                failure = "resource_pool_unknown_or_mismatched"
                break
            if pool_key in seen_delta_pools:
                failure = "duplicate_resource_delta"
                break
            seen_delta_pools.add(pool_key)
            try:
                released = decimal_value(delta.get("released", 0), field="released", nonnegative=True)
                required = decimal_value(delta.get("required", 0), field="required", nonnegative=True)
            except ValueError:
                failure = "resource_delta_invalid"
                break
            if required <= 0:
                failure = "resource_delta_invalid"
                break
            before = simulated.get(pool_key, pool["available"])
            after = before + released - required
            if after < 0:
                failure = "portfolio_capacity_conflict"
                break
            # If initial free capacity plus this proposal's own release was insufficient,
            # this action is contingent on earlier selected releases in the same typed pool.
            if pools_before[pool_key]["available"] != "":
                initial = decimal_value(pools_before[pool_key]["available"], field="initial pool", nonnegative=True)
                if initial + released < required:
                    dependencies.update(release_dependencies.get(pool_key, ()))
            simulated[pool_key] = after
            normalized_deltas.append(
                {
                    "resource_kind": kind,
                    "pool_key": pool_key,
                    "unit": unit,
                    "released": _decimal_text(released),
                    "required": _decimal_text(required),
                    "net_after": _decimal_text(after),
                }
            )
        if failure:
            alternatives.append(_rejected(proposal, failure))
            continue

        for pool_key, after in simulated.items():
            before = pools[pool_key]["available"]
            pools[pool_key]["available"] = after
            if after > before:
                release_dependencies[pool_key].append(proposal_id)
        quantities[candidate_id] -= proposed_contracts
        used_sources.update(sources)
        selected.append(
            {
                **proposal,
                "selected": True,
                "actionable": True,
                "allocator_reason": "selected",
                "execution_order": len(selected) + 1,
                "depends_on": sorted(dependencies),
                "resource_deltas": normalized_deltas,
                "candidate_quantity_before": quantities[candidate_id] + proposed_contracts,
                "candidate_quantity_after": quantities[candidate_id],
            }
        )

    pools_after = {
        key: {
            "resource_kind": item["resource_kind"],
            "unit": item["unit"],
            "available": _decimal_text(item["available"]),
        }
        for key, item in pools.items()
    }
    return AllocatorResult(
        selected=tuple(selected),
        alternatives=tuple(alternatives),
        resource_pools_before=pools_before,
        resource_pools_after=pools_after,
        candidate_quantity_before=quantities_before,
        candidate_quantity_after=quantities,
    )


__all__ = [
    "ALLOCATOR_VERSION",
    "RESOURCE_POOL_CONTRACTS",
    "AllocatorResult",
    "allocate_position_advice",
]
