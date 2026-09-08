from __future__ import annotations

from typing import Any, Mapping

from domain.domain.symbol_identity import symbol_market
from domain.domain.wheel import (
    project_wheel_branches,
    project_wheel_linkage_candidates,
    project_wheel_lifecycles,
)
from src.application.ledger.api import (
    project_assigned_stock_lifecycle_from_rows,
    project_position_lots_from_trade_facts,
)


WHEEL_READ_MODEL_SCHEMA = "wheel_read_model.v2"


def _candidate_rows(snapshot: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if not isinstance(snapshot, Mapping):
        return []
    rows = snapshot.get("batches") or snapshot.get("rows")
    if isinstance(rows, list):
        return [item for item in rows if isinstance(item, Mapping)]
    return [snapshot]


def _dict_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _event_time_ms(row: Mapping[str, Any], field: str) -> int:
    try:
        return int(row.get(field) or 0)
    except (TypeError, ValueError):
        return 0


def _branch_from_legacy_batch(batch: Mapping[str, Any]) -> dict[str, Any]:
    branch = dict(batch)
    stock_lot_id = str(branch.get("stock_lot_id") or "").strip()
    branch.setdefault("wheel_branch_id", stock_lot_id)
    branch.setdefault("parent_branch_id", None)
    branch.setdefault("direction", "call")
    return branch


def _legacy_call_batch(branch: Mapping[str, Any]) -> dict[str, Any]:
    batch = dict(branch)
    if not batch.get("legacy_call_adapter"):
        batch["phase"] = {
            "option_open": "call_open",
            "intent_pending": "call_pending",
            "residual_capacity": "residual_stock",
            "manual_ended": None,
        }.get(batch.get("phase"), batch.get("phase"))
    batch.setdefault("batch_generation_hash", batch.get("branch_generation_hash"))
    batch.setdefault("active_call_lot_ids", list(batch.get("active_option_lot_ids") or []))
    batch.setdefault("unresolved_call_lot_ids", [])
    batch.setdefault(
        "active_intent_reserved_shares",
        int(batch.get("active_intent_reserved_contracts") or 0)
        * int(batch.get("multiplier") or 0),
    )
    return batch


def _legacy_call_batches(branches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _legacy_call_batch(branch)
        for branch in branches
        if str(branch.get("direction") or "call").strip().lower() == "call"
        and str(branch.get("stock_lot_id") or "").strip()
    ]


def _split_lifecycle_projection(value: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if isinstance(value, Mapping):
        batches = _dict_rows(value.get("batches"))
        branches = _dict_rows(value.get("wheel_branches"))
        if not branches:
            branches = [_branch_from_legacy_batch(batch) for batch in batches]
        if not batches:
            batches = _legacy_call_batches(branches)
        return batches, branches

    rows = _dict_rows(value)
    if any("wheel_branch_id" in row or "direction" in row for row in rows):
        return _legacy_call_batches(rows), rows
    return rows, [_branch_from_legacy_batch(batch) for batch in rows]


def _includes_branch_projection(value: Any) -> bool:
    if isinstance(value, Mapping):
        return isinstance(value.get("wheel_branches"), list)
    return any(
        "wheel_branch_id" in row or "direction" in row
        for row in _dict_rows(value)
    )


def _attach_candidates(
    projections: list[dict[str, Any]],
    candidates: list[Mapping[str, Any]],
    *,
    account: str,
) -> None:
    for projection in projections:
        branch_id = str(projection.get("wheel_branch_id") or "").strip()
        stock_lot_id = str(projection.get("stock_lot_id") or "").strip()
        matches = [
            item
            for item in candidates
            if str(item.get("account") or "").strip().lower() == account
            and str(item.get("projection_hash") or "").strip()
            == str(projection.get("projection_hash") or "").strip()
            and (
                (
                    branch_id
                    and str(item.get("wheel_branch_id") or "").strip()
                    == branch_id
                )
                or (
                    stock_lot_id
                    and str(item.get("stock_lot_id") or "").strip()
                    == stock_lot_id
                )
            )
        ]
        if len(matches) == 1:
            candidate = matches[0].get("final_candidate")
            if isinstance(candidate, Mapping):
                projection["candidate"] = dict(candidate)


def build_wheel_read_model_from_rows(
    rows: Mapping[str, Any],
    *,
    account: str,
    as_of_ms: int,
    candidate_snapshot: Mapping[str, Any] | None = None,
    monitoring_readiness: Mapping[str, Any] | None = None,
    market: str | None = None,
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    if not account_value:
        raise ValueError("wheel read model requires account")
    instant = int(as_of_ms)
    if instant <= 0:
        raise ValueError("wheel read model requires as_of_ms > 0")
    trade_events = [
        dict(item)
        for item in rows.get("trade_events") or []
        if isinstance(item, Mapping)
        and _event_time_ms(item, "event_time_ms") <= instant
    ]
    wheel_events = [
        dict(item)
        for item in rows.get("account_wheel_events") or []
        if isinstance(item, Mapping)
        and _event_time_ms(item, "occurred_at_ms") <= instant
    ]
    projected = project_position_lots_from_trade_facts(trade_events)
    scoped_rows = {
        **dict(rows),
        "trade_events": trade_events,
        "account_wheel_events": wheel_events,
        "account_position_lots": [
            {"record_id": item.record_id, "fields": dict(item.fields)}
            for item in projected.lots
        ],
    }
    assigned_stock = build_assigned_stock_projection_from_rows(
        scoped_rows,
        account=account_value,
        as_of_ms=instant,
    )
    lifecycle_projection = project_wheel_lifecycles(
        wheel_events,
        trade_events,
        scoped_rows["account_position_lots"],
        assigned_stock,
        instant,
    )
    batches, wheel_branches = _split_lifecycle_projection(lifecycle_projection)
    if not _includes_branch_projection(lifecycle_projection):
        wheel_branches = project_wheel_branches(
            wheel_events,
            trade_events,
            scoped_rows["account_position_lots"],
            assigned_stock,
            instant,
        )
        batches = _legacy_call_batches(wheel_branches)
    market_value = str(market or "").strip().upper()
    if market_value:
        if market_value not in {"US", "HK"}:
            raise ValueError("wheel read model market must be us or hk")
        batches = [
            batch
            for batch in batches
            if symbol_market(batch.get("symbol")) == market_value
        ]
        wheel_branches = [
            branch
            for branch in wheel_branches
            if symbol_market(branch.get("symbol")) == market_value
        ]
    candidates = _candidate_rows(candidate_snapshot)
    _attach_candidates(batches, candidates, account=account_value)
    _attach_candidates(wheel_branches, candidates, account=account_value)
    readiness = dict(monitoring_readiness or {})
    monitoring_gate = str(readiness.get("monitoring_gate") or "disabled").strip().lower()
    if monitoring_gate not in {"enabled", "disabled", "config_mismatch"}:
        monitoring_gate = "disabled"
    for projection in [*batches, *wheel_branches]:
        projection["monitoring_gate"] = monitoring_gate
        if readiness.get("reason_code"):
            projection["monitoring_gate_reason"] = readiness["reason_code"]
    linkage_candidates = project_wheel_linkage_candidates(
        wheel_branches,
        scoped_rows["account_position_lots"],
        wheel_events,
    )
    unresolved_stock_lot_ids = {
        str(item.get("stock_lot_id") or "").strip()
        for item in linkage_candidates
        if str(item.get("stock_lot_id") or "").strip()
    }
    for projection in [*batches, *wheel_branches]:
        if (
            str(projection.get("stock_lot_id") or "").strip()
            in unresolved_stock_lot_ids
            and projection.get("lifecycle_status") == "active"
        ):
            projection["phase"] = "linkage_unresolved"
    return {
        "schema_version": WHEEL_READ_MODEL_SCHEMA,
        "account": account_value,
        "market": market_value or None,
        "as_of_ms": instant,
        "monitoring_gate": monitoring_gate,
        "monitoring_gate_reason": readiness.get("reason_code"),
        "batches": batches,
        "wheel_branches": wheel_branches,
        "linkage_candidates": linkage_candidates,
        "assigned_stock_projection": assigned_stock,
    }


def build_assigned_stock_projection_from_rows(
    rows: Mapping[str, Any],
    *,
    account: str,
    as_of_ms: int,
) -> dict[str, Any]:
    return project_assigned_stock_lifecycle_from_rows(
        rows,
        as_of_ms=int(as_of_ms),
        quote_snapshots=[],
        account=str(account or "").strip().lower(),
    )


def build_wheel_read_model(
    repo: Any,
    account: str,
    as_of_ms: int,
    candidate_snapshot: Mapping[str, Any] | None = None,
    monitoring_readiness: Mapping[str, Any] | None = None,
    market: str | None = None,
) -> dict[str, Any]:
    candidate = getattr(repo, "primary_repo", repo)
    reader = getattr(candidate, "read_lifecycle_account_rows", None)
    if not callable(reader):
        raise TypeError("wheel read model requires the coherent ledger reader")
    return build_wheel_read_model_from_rows(
        reader(account=str(account or "").strip().lower()),
        account=account,
        as_of_ms=as_of_ms,
        candidate_snapshot=candidate_snapshot,
        monitoring_readiness=monitoring_readiness,
        market=market,
    )


__all__ = [
    "WHEEL_READ_MODEL_SCHEMA",
    "build_assigned_stock_projection_from_rows",
    "build_wheel_read_model",
    "build_wheel_read_model_from_rows",
]
