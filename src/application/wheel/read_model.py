from __future__ import annotations

from typing import Any, Mapping

from src.application.ledger.api import (
    WHEEL_READ_MODEL_SCHEMA as WHEEL_READ_MODEL_SCHEMA,
    build_assigned_stock_projection_from_rows as build_assigned_stock_projection_from_rows,
    build_wheel_read_model_from_rows as build_wheel_read_model_from_rows,
    build_wheel_read_model_with_capacity_from_rows as build_wheel_read_model_with_capacity_from_rows,
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
