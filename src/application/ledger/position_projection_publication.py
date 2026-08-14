from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from domain.domain.ledger.position_fingerprint import (
    POSITION_LOTS_FINGERPRINT_SCHEMA,
)
from src.application.ledger.position_records import PositionLotRecord
from src.application.ledger.projector_implementation import (
    ProjectorImplementationUnavailable,
    loaded_projector_implementation_fingerprint,
)
from src.application.ledger.repository import (
    POSITION_PROJECTION_SCHEMA,
    PositionLotDiff,
    SQLiteOptionPositionsRepository,
    require_position_projection_publication_repo,
    require_option_positions_read_repo,
)


CURRENT_POSITION_PROJECTION_SCHEMA = "current_position_projection.v1"


@dataclass(frozen=True)
class PositionProjectionPublication:
    position_lot_count: int
    added: int
    changed: int
    removed: int
    unchanged: int
    touched_accounts: tuple[str, ...]
    heads_trusted: bool
    trust_reason: str | None


def _required_int(value: Any, *, missing: int = -1) -> int:
    return missing if value is None else int(value)


def publish_full_position_projection(
    repo: Any,
    records: Sequence[PositionLotRecord],
    *,
    conn: Any | None = None,
) -> PositionProjectionPublication:
    """Publish the unchanged full oracle through one diff/head boundary."""

    candidate = require_position_projection_publication_repo(repo)
    if isinstance(candidate, SQLiteOptionPositionsRepository) and conn is None:
        active_conn = candidate._connect()
        try:
            active_conn.execute("BEGIN IMMEDIATE")
            result = publish_full_position_projection(
                candidate,
                records,
                conn=active_conn,
            )
            active_conn.commit()
            return result
        except Exception:
            active_conn.rollback()
            raise
        finally:
            active_conn.close()
    diff: PositionLotDiff = candidate.apply_position_lot_diff(records, conn=conn)
    try:
        implementation_fingerprint = loaded_projector_implementation_fingerprint()
    except ProjectorImplementationUnavailable:
        # The full-oracle public rows are still authoritative. Heads remain
        # unavailable so no fast/trusted reader can cross an unknown binary.
        return PositionProjectionPublication(
            position_lot_count=diff.lot_count,
            added=diff.added,
            changed=diff.changed,
            removed=diff.removed,
            unchanged=diff.unchanged,
            touched_accounts=diff.touched_accounts,
            heads_trusted=False,
            trust_reason="projector_implementation_unavailable",
        )

    lot_count, heads_trusted, trust_reason = candidate.publish_full_position_projection_heads(
        implementation_fingerprint=implementation_fingerprint,
        known_accounts=diff.accounts,
        changed_accounts=diff.touched_accounts,
        conn=conn,
    )
    if heads_trusted and int(lot_count) != diff.lot_count:
        raise RuntimeError("position projection head count differs from published lot diff")
    return PositionProjectionPublication(
        position_lot_count=diff.lot_count,
        added=diff.added,
        changed=diff.changed,
        removed=diff.removed,
        unchanged=diff.unchanged,
        touched_accounts=diff.touched_accounts,
        heads_trusted=bool(heads_trusted),
        trust_reason=trust_reason,
    )


def read_current_position_projection(repo: Any, *, account: str) -> dict[str, Any]:
    account_value = str(account or "").strip()
    if not account_value or account_value != account_value.lower():
        raise ValueError("position projection account must be lowercase")
    candidate = require_option_positions_read_repo(repo)
    if not isinstance(candidate, SQLiteOptionPositionsRepository):
        return _unavailable(account_value, "not_initialized", "sqlite_repository_required")
    try:
        implementation_fingerprint = loaded_projector_implementation_fingerprint()
    except ProjectorImplementationUnavailable:
        return _unavailable(
            account_value,
            "data_unavailable",
            "projector_implementation_unavailable",
        )

    conn = candidate._connect()
    try:
        conn.execute("BEGIN")
        state = candidate.read_position_projection_account_metadata(
            account_value,
            conn=conn,
        )
        source = state["source"]
        head = state["head"]
        if source is None or head is None:
            return _unavailable(account_value, "not_initialized", "projection_head_missing")
        checks = (
            (
                str(source.get("projector_schema") or "") == POSITION_PROJECTION_SCHEMA,
                "source_projector_schema_mismatch",
            ),
            (
                str(head.get("projector_schema") or "") == POSITION_PROJECTION_SCHEMA,
                "head_projector_schema_mismatch",
            ),
            (
                str(source.get("projector_implementation_fingerprint") or "") == implementation_fingerprint,
                "source_implementation_mismatch",
            ),
            (
                str(head.get("projector_implementation_fingerprint") or "") == implementation_fingerprint,
                "head_implementation_mismatch",
            ),
            (str(head.get("status") or "") == "trusted", "head_not_trusted"),
            (
                int(source.get("source_generation") or 0) == _required_int(head.get("built_source_generation")),
                "source_generation_mismatch",
            ),
            (
                int(head.get("lots_generation") or 0) == _required_int(head.get("built_lots_generation")),
                "lots_generation_mismatch",
            ),
            (
                int(source.get("sqlite_schema_cookie") or -1) == int(state["schema_cookie"]),
                "sqlite_schema_cookie_mismatch",
            ),
        )
        for passed, reason in checks:
            if not passed:
                return _unavailable(account_value, "data_unavailable", reason)
        snapshot = candidate.position_projection_account_snapshot(
            account_value,
            include_records=True,
            conn=conn,
        )
        if int(head.get("lot_count") or 0) != int(snapshot.lot_count):
            return _unavailable(account_value, "data_unavailable", "lot_count_mismatch")
        if str(head.get("projection_fingerprint") or "") != snapshot.fingerprint:
            return _unavailable(
                account_value,
                "data_unavailable",
                "projection_fingerprint_mismatch",
            )
        return {
            "schema_version": CURRENT_POSITION_PROJECTION_SCHEMA,
            "status": "trusted",
            "account": account_value,
            "position_lots_fingerprint_schema": POSITION_LOTS_FINGERPRINT_SCHEMA,
            "projection_fingerprint": snapshot.fingerprint,
            "source_generation": int(source["source_generation"]),
            "lots_generation": int(head["lots_generation"]),
            "lot_count": int(snapshot.lot_count),
            "position_lots": list(snapshot.records),
            "reason": None,
        }
    finally:
        conn.rollback()
        conn.close()


def _unavailable(account: str, status: str, reason: str) -> dict[str, Any]:
    return {
        "schema_version": CURRENT_POSITION_PROJECTION_SCHEMA,
        "status": status,
        "account": account,
        "position_lots": [],
        "lot_count": 0,
        "reason": reason,
    }


__all__ = [
    "CURRENT_POSITION_PROJECTION_SCHEMA",
    "PositionProjectionPublication",
    "publish_full_position_projection",
    "read_current_position_projection",
]
