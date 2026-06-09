from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from src.application.shadow_replay.common import first_float, instrument_key, normal_status, text


DECISION_INSTANCE_SCHEMA_VERSION = "strategy_lab_decision_instance.v1"


def build_decision_instances(candidate_snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    single_leg: list[dict[str, Any]] = []
    combo_rows: list[dict[str, Any]] = []
    for idx, row in enumerate(candidate_snapshots, start=1):
        family = strategy_family(row)
        if family == "combo_yield":
            combo_rows.append(_candidate_ref(row, source_index=idx, strategy_family=family))
            continue
        if family in {"sell_put", "covered_call"}:
            single_leg.append(_single_leg_decision(row, source_index=idx, strategy_family=family))
    return _dedupe_decisions(single_leg + _combo_decisions(combo_rows))


def strategy_family(row: dict[str, Any]) -> str:
    raw = text(
        row.get("strategy_family")
        or row.get("function")
        or row.get("strategy_name")
        or row.get("strategy")
    ).lower().replace("-", "_")
    if raw in {"sell_put", "put"}:
        return "sell_put"
    if raw in {"sell_call", "covered_call", "call"}:
        return "covered_call"
    if raw in {"combo_yield", "yield_enhancement", "income_upside_enhancement", "vol_convexity_enhancement"}:
        return "combo_yield"
    mode = text(row.get("option_type") or row.get("mode")).lower()
    if mode == "put":
        return "sell_put"
    if mode == "call":
        return "covered_call"
    return "unknown"


def summarize_decision_instances(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    family_counts = Counter(text(row.get("strategy_family")) or "unknown" for row in decisions)
    status_counts = Counter(text(row.get("decision_status")) or "unknown" for row in decisions)
    blocker_counts: Counter[str] = Counter()
    family_blockers: dict[str, Counter[str]] = defaultdict(Counter)
    for row in decisions:
        family = text(row.get("strategy_family")) or "unknown"
        for blocker in row.get("blockers") or []:
            blocker_text = text(blocker)
            if not blocker_text:
                continue
            blocker_counts[blocker_text] += 1
            family_blockers[family][blocker_text] += 1
    return {
        "decision_instance_count": len(decisions),
        "strategy_family_counts": dict(sorted(family_counts.items())),
        "decision_status_counts": dict(sorted(status_counts.items())),
        "blocker_counts": dict(blocker_counts.most_common(20)),
        "blockers_by_strategy_family": {
            family: dict(counts.most_common(20))
            for family, counts in sorted(family_blockers.items())
        },
    }


def _single_leg_decision(row: dict[str, Any], *, source_index: int, strategy_family: str) -> dict[str, Any]:
    key = instrument_key(row)
    blockers: list[str] = []
    if not key:
        blockers.append("instrument_identity_missing")
    if strategy_family == "covered_call":
        blockers.extend(_covered_call_context_blockers(row))
    return {
        "schema_version": DECISION_INSTANCE_SCHEMA_VERSION,
        "decision_id": f"{strategy_family}:{key or source_index}",
        "strategy_family": strategy_family,
        "strategy_profile": text(row.get("strategy_profile") or row.get("profile") or row.get("strategy_mode")) or None,
        "strategy_group_id": None,
        "decision_status": normal_status(row.get("status")),
        "candidate_ids": [key] if key else [],
        "legs": [_candidate_ref(row, source_index=source_index, strategy_family=strategy_family)],
        "blockers": blockers,
        "ready_for_single_leg_experiment": not blockers,
        "ready_for_group_experiment": False,
    }


def _covered_call_context_blockers(row: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if not _has_covered_call_coverage_context(row):
        blockers.append("covered_call_coverage_context_missing")
    if not _has_covered_call_cost_context(row):
        blockers.append("covered_call_cost_basis_context_missing")
    return blockers


def _has_covered_call_coverage_context(row: dict[str, Any]) -> bool:
    if first_float(
        row,
        "covered_quantity",
        "covered_shares",
        "covered_share_quantity",
        "shares_available_for_cover",
        "covered_contracts_available",
        "covered_notional_cny",
        "underlying_share_quantity",
        "holding_quantity",
        "holding_qty",
        "position_shares",
    ) is not None:
        return True
    return bool(text(row.get("coverage_status") or row.get("covered_status") or row.get("holding_coverage_status")))


def _has_covered_call_cost_context(row: dict[str, Any]) -> bool:
    if first_float(
        row,
        "cost_basis",
        "underlying_cost_basis",
        "avg_cost",
        "average_cost",
        "cost_basis_floor",
        "min_strike_cost_multiplier",
        "strike_cost_multiplier",
    ) is not None:
        return True
    return bool(text(row.get("cost_basis_status") or row.get("covered_cost_basis_status")))


def _combo_decisions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    missing_group: list[dict[str, Any]] = []
    for row in rows:
        group_id = text(row.get("strategy_group_id"))
        if group_id:
            grouped[group_id].append(row)
        else:
            missing_group.append(row)

    out: list[dict[str, Any]] = []
    for group_id, legs in sorted(grouped.items()):
        blockers: list[str] = []
        leg_roles = {text(leg.get("leg_role")) for leg in legs if text(leg.get("leg_role"))}
        if not leg_roles:
            blockers.append("combo_yield_leg_role_missing")
        if len(legs) < 2:
            blockers.append("combo_yield_group_has_too_few_legs")
        candidate_ids = [text(leg.get("instrument_key")) for leg in legs if text(leg.get("instrument_key"))]
        out.append(
            {
                "schema_version": DECISION_INSTANCE_SCHEMA_VERSION,
                "decision_id": f"combo_yield:{group_id}",
                "strategy_family": "combo_yield",
                "strategy_profile": _first_text(leg.get("strategy_profile") for leg in legs),
                "strategy_group_id": group_id,
                "decision_status": _group_status(legs),
                "candidate_ids": candidate_ids,
                "legs": legs,
                "blockers": blockers,
                "ready_for_single_leg_experiment": False,
                "ready_for_group_experiment": not blockers,
            }
        )
    for idx, leg in enumerate(missing_group, start=1):
        key = text(leg.get("instrument_key")) or str(idx)
        out.append(
            {
                "schema_version": DECISION_INSTANCE_SCHEMA_VERSION,
                "decision_id": f"combo_yield:missing_group:{key}",
                "strategy_family": "combo_yield",
                "strategy_profile": leg.get("strategy_profile"),
                "strategy_group_id": None,
                "decision_status": normal_status(leg.get("status")),
                "candidate_ids": [key] if key else [],
                "legs": [leg],
                "blockers": ["combo_yield_group_identity_missing"],
                "ready_for_single_leg_experiment": False,
                "ready_for_group_experiment": False,
            }
        )
    return out


def _candidate_ref(row: dict[str, Any], *, source_index: int, strategy_family: str) -> dict[str, Any]:
    key = instrument_key(row)
    return {
        "source_candidate_index": source_index,
        "instrument_key": key or None,
        "strategy_family": strategy_family,
        "strategy_profile": text(row.get("strategy_profile") or row.get("profile") or row.get("strategy_mode")) or None,
        "strategy_group_id": text(row.get("strategy_group_id")) or None,
        "leg_role": text(row.get("leg_role")) or None,
        "status": normal_status(row.get("status")),
        "account": text(row.get("account")).lower() or None,
        "symbol": text(row.get("symbol") or row.get("underlying_symbol")).upper() or None,
        "mode": text(row.get("option_type") or row.get("mode")).lower() or None,
        "contract_symbol": text(row.get("contract_symbol") or row.get("option_symbol")) or None,
        "expiration": text(row.get("expiration") or row.get("exp")) or None,
        "strike": row.get("strike"),
    }


def _group_status(legs: list[dict[str, Any]]) -> str:
    statuses = {normal_status(row.get("status")) for row in legs}
    if not statuses:
        return "unknown"
    if "accepted" in statuses or "notified" in statuses:
        return "accepted"
    if "rejected" in statuses:
        return "rejected"
    if len(statuses) == 1:
        return next(iter(statuses))
    return "mixed"


def _first_text(values: Any) -> str | None:
    for value in values:
        value_text = text(value)
        if value_text:
            return value_text
    return None


def _dedupe_decisions(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in decisions:
        key = text(row.get("decision_id"))
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(row)
    return out
