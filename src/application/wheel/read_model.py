from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from domain.domain.trade_contract_identity import contract_share_quantity
from domain.domain.symbol_identity import symbol_market
from domain.domain.wheel import (
    project_wheel_branches,
    project_wheel_coverage,
    project_wheel_linkage_candidates,
)
from src.application.ledger.api import (
    project_assigned_stock_lifecycle_from_rows,
    project_position_lots_and_assigned_stock_from_rows,
)


WHEEL_READ_MODEL_SCHEMA = "wheel_read_model.v2"


def _candidate_rows(snapshot: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if not isinstance(snapshot, Mapping):
        return []
    rows = snapshot.get("batches") or snapshot.get("rows")
    if isinstance(rows, list):
        return [item for item in rows if isinstance(item, Mapping)]
    return [snapshot]


def _event_time_ms(row: Mapping[str, Any], field: str) -> int:
    try:
        return int(row.get(field) or 0)
    except (TypeError, ValueError):
        return 0


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
    if "active_intent_reserved_shares" not in batch:
        try:
            batch["active_intent_reserved_shares"] = contract_share_quantity(
                batch.get("active_intent_reserved_contracts") or 0, batch.get("multiplier"),
            )
        except ValueError:
            batch["active_intent_reserved_shares"] = None
    return batch


def _legacy_call_batches(branches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _legacy_call_batch(branch)
        for branch in branches
        if str(branch.get("direction") or "call").strip().lower() == "call"
        and str(branch.get("stock_lot_id") or "").strip()
    ]


def _attach_candidates(
    projections: list[dict[str, Any]],
    candidates: list[Mapping[str, Any]],
    *,
    account: str,
) -> None:
    for projection in projections:
        branch_id = str(projection.get("wheel_branch_id") or "").strip()
        lot_id = str(projection.get("stock_lot_id") or "").strip()
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
                    lot_id
                    and str(item.get("stock_lot_id") or "").strip()
                    == lot_id
                )
            )
        ]
        if len(matches) == 1:
            candidate = matches[0].get("final_candidate")
            if isinstance(candidate, Mapping):
                projection["candidate"] = dict(candidate)


def _build_wheel_read_model_base(
    rows: Mapping[str, Any], *, account: str, as_of_ms: int,
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
    scoped_rows = {
        **dict(rows),
        "trade_events": trade_events,
        "account_wheel_events": wheel_events,
    }
    position_lots, assigned_stock = project_position_lots_and_assigned_stock_from_rows(
        scoped_rows, account=account_value, as_of_ms=instant,
    )
    scoped_rows["account_position_lots"] = position_lots
    wheel_branches = project_wheel_branches(
        wheel_events, trade_events, position_lots, assigned_stock, instant,
    )
    batches = _legacy_call_batches(wheel_branches)
    return {
        "account": account_value, "as_of_ms": instant,
        "position_lots": position_lots, "assigned_stock": assigned_stock,
        "wheel_events": wheel_events, "wheel_branches": wheel_branches,
        "batches": batches,
    }


def _render_wheel_read_model(
    base: dict[str, Any], *, candidate_snapshot: Mapping[str, Any] | None = None,
    monitoring_readiness: Mapping[str, Any] | None = None, market: str | None = None,
) -> dict[str, Any]:
    account_value, instant = base["account"], base["as_of_ms"]
    position_lots, assigned_stock = base["position_lots"], base["assigned_stock"]
    wheel_events, wheel_branches = base["wheel_events"], base["wheel_branches"]
    batches = base["batches"]
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
        position_lots,
        wheel_events,
    )
    unresolved_branch_ids = {
        str(item.get("wheel_branch_id") or "").strip()
        for item in linkage_candidates
        if str(item.get("wheel_branch_id") or "").strip()
    }
    for projection in [*batches, *wheel_branches]:
        if (
            str(projection.get("wheel_branch_id") or "").strip()
            in unresolved_branch_ids
            and projection.get("lifecycle_status") == "active"
        ):
            projection["phase"] = "linkage_unresolved"
    lots_by_id = {str(row["record_id"]): row["fields"] for row in position_lots}
    for projection in [*batches, *wheel_branches]:
        projection["coverage"] = project_wheel_coverage(projection)
        # Use only the lot identities already linked by the Wheel projection.
        active_ids = projection.get("active_option_lot_ids") or projection.get("active_call_lot_ids") or []
        projection["active_option_contracts"] = [
            {"lot_id": lot_id, **{
                key: (lots_by_id.get(lot_id, {}).get("contract_key") or {}).get(key)
                for key in ("underlying_symbol", "option_type", "strike", "expiration_ymd")
            }}
            for lot_id in active_ids
        ]
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


def build_wheel_read_model_from_rows(
    rows: Mapping[str, Any],
    *,
    account: str,
    as_of_ms: int,
    candidate_snapshot: Mapping[str, Any] | None = None,
    monitoring_readiness: Mapping[str, Any] | None = None,
    market: str | None = None,
) -> dict[str, Any]:
    return _render_wheel_read_model(
        _build_wheel_read_model_base(rows, account=account, as_of_ms=as_of_ms),
        candidate_snapshot=candidate_snapshot, monitoring_readiness=monitoring_readiness,
        market=market,
    )


def build_wheel_read_model_with_capacity_from_rows(
    rows: Mapping[str, Any], *, account: str, as_of_ms: int, market: str | None,
    monitoring_readiness: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive independent market and account-wide views from one observation."""
    base = _build_wheel_read_model_base(rows, account=account, as_of_ms=as_of_ms)
    market_model = _render_wheel_read_model(
        deepcopy(base), market=market, monitoring_readiness=monitoring_readiness,
    )
    capacity_model = _render_wheel_read_model(base)
    return market_model, capacity_model


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
    "build_wheel_read_model_with_capacity_from_rows",
]
