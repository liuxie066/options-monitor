from __future__ import annotations

from typing import Any, Mapping

from domain.domain.wheel import project_wheel_lifecycles
from src.application.performance.adapters import (
    ledger_performance_inputs_from_rows,
    load_assigned_stock_projection,
)


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
    inputs = ledger_performance_inputs_from_rows(rows)
    assigned_stock = load_assigned_stock_projection(
        inputs,
        as_of_ms=instant,
        quote_snapshots=[],
        account=account_value,
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
    return {
        "schema_version": WHEEL_READ_MODEL_SCHEMA,
        "account": account_value,
        "as_of_ms": instant,
        "batches": batches,
        "linkage_candidates": [],
        "assigned_stock_projection": assigned_stock,
    }


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
    "build_wheel_read_model",
    "build_wheel_read_model_from_rows",
]
