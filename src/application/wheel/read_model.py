from __future__ import annotations

from typing import Any, Mapping

from domain.domain.wheel import (
    project_wheel_call_linkage_candidates,
    project_wheel_lifecycles,
)
from src.application.ledger.api import project_assigned_stock_lifecycle_from_rows


WHEEL_READ_MODEL_SCHEMA = "wheel_read_model.v1"


def _candidate_rows(snapshot: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if not isinstance(snapshot, Mapping):
        return []
    rows = snapshot.get("batches") or snapshot.get("rows")
    if isinstance(rows, list):
        return [item for item in rows if isinstance(item, Mapping)]
    return [snapshot]


def build_wheel_read_model_from_rows(
    rows: Mapping[str, Any],
    *,
    account: str,
    as_of_ms: int,
    candidate_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    if not account_value:
        raise ValueError("wheel read model requires account")
    instant = int(as_of_ms)
    if instant <= 0:
        raise ValueError("wheel read model requires as_of_ms > 0")
    assigned_stock = build_assigned_stock_projection_from_rows(
        rows,
        account=account_value,
        as_of_ms=instant,
    )
    batches = project_wheel_lifecycles(
        rows.get("account_wheel_events") or [],
        rows.get("trade_events") or [],
        rows.get("account_position_lots") or [],
        assigned_stock,
        instant,
    )
    candidates = _candidate_rows(candidate_snapshot)
    for batch in batches:
        matches = [
            item
            for item in candidates
            if str(item.get("account") or "").strip().lower() == account_value
            and str(item.get("stock_lot_id") or "").strip() == batch["stock_lot_id"]
            and str(item.get("projection_hash") or "").strip() == batch["projection_hash"]
        ]
        if len(matches) == 1:
            candidate = matches[0].get("final_candidate")
            if isinstance(candidate, Mapping):
                batch["candidate"] = dict(candidate)
    linkage_candidates = project_wheel_call_linkage_candidates(
        batches,
        rows.get("account_position_lots") or [],
        rows.get("account_wheel_events") or [],
    )
    return {
        "schema_version": WHEEL_READ_MODEL_SCHEMA,
        "account": account_value,
        "as_of_ms": instant,
        "batches": batches,
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
    )


__all__ = [
    "WHEEL_READ_MODEL_SCHEMA",
    "build_assigned_stock_projection_from_rows",
    "build_wheel_read_model",
    "build_wheel_read_model_from_rows",
]
