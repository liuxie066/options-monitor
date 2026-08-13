from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Callable, Sequence

from domain.domain.ledger import (
    EMPTY_PROJECTION_DIAGNOSTIC_SHA256,
    ResumableProjectionState,
)
from src.application.ledger.event_codec import trade_event_application_payload
from src.application.ledger.position_projection_publication import (
    PositionProjectionPublication,
    publish_full_position_projection,
)
from src.application.ledger.projector_implementation import (
    loaded_projector_implementation_fingerprint,
)
from src.application.ledger.publisher import (
    ResumablePublicationState,
    ensure_projection_publishable,
    project_stored_trade_events_to_position_lots,
    project_stored_trade_events_to_resumable_position_lots,
)
from src.application.ledger.repository import (
    POSITION_PROJECTION_SCHEMA,
    SQLiteOptionPositionsRepository,
    require_position_projection_publication_repo,
)


POSITION_PROJECTION_CHECKPOINT_SCHEMA = "position_projection_checkpoint.v1"
CHECKPOINT_ROTATE_EVENT_COUNT = 100
CHECKPOINT_ROTATE_EVENT_BYTES = 1_048_576
MAX_CHECKPOINT_STATE_BYTES = 64 * 1_048_576
_EVENT_PREFIX_SEED = b"position_projection_event_prefix.v1"


@dataclass(frozen=True)
class ProjectionRuntimeResult:
    mode_requested: str
    mode_used: str
    fallback_reason: str | None
    checkpoint_id: str | None
    parent_checkpoint_id: str | None
    checkpoint_written: bool
    pruned_checkpoint_ids: tuple[str, ...]
    tail_event_count: int
    tail_event_bytes: int
    publication: PositionProjectionPublication

    @property
    def position_lot_count(self) -> int:
        return self.publication.position_lot_count


@dataclass(frozen=True)
class _DecodedCheckpoint:
    row: dict[str, Any]
    domain_state: ResumableProjectionState
    publication_state: ResumablePublicationState


def initial_event_prefix_chain() -> str:
    return hashlib.sha256(_EVENT_PREFIX_SEED).hexdigest()


def extend_event_prefix_chain(previous: str, event_json: str | bytes) -> str:
    try:
        prior = bytes.fromhex(str(previous))
    except ValueError as exc:
        raise ValueError("event prefix chain must be lowercase SHA-256 hex") from exc
    if len(prior) != 32 or str(previous) != str(previous).lower():
        raise ValueError("event prefix chain must be lowercase SHA-256 hex")
    payload = event_json.encode("utf-8") if isinstance(event_json, str) else bytes(event_json)
    digest = hashlib.sha256()
    digest.update(prior)
    digest.update(len(payload).to_bytes(8, byteorder="big", signed=False))
    digest.update(payload)
    return digest.hexdigest()


def run_position_projection_forced_full(
    repo: Any,
    events: Sequence[Any] = (),
    *,
    seed_checkpoint: bool = False,
    failure_hook: Callable[[str], None] | None = None,
) -> ProjectionRuntimeResult:
    return _run_runtime(
        repo,
        events,
        mode="forced_full",
        seed_checkpoint=seed_checkpoint,
        failure_hook=failure_hook,
    )


def run_position_projection_fast_if_safe(
    repo: Any,
    events: Sequence[Any] = (),
    *,
    failure_hook: Callable[[str], None] | None = None,
) -> ProjectionRuntimeResult:
    return _run_runtime(
        repo,
        events,
        mode="fast_if_safe",
        seed_checkpoint=False,
        failure_hook=failure_hook,
    )


def _run_runtime(
    repo: Any,
    events: Sequence[Any],
    *,
    mode: str,
    seed_checkpoint: bool,
    failure_hook: Callable[[str], None] | None,
) -> ProjectionRuntimeResult:
    candidate = require_position_projection_publication_repo(repo)
    if not isinstance(candidate, SQLiteOptionPositionsRepository):
        raise TypeError("position projection runtime requires SQLite transaction authority")
    implementation = loaded_projector_implementation_fingerprint()
    conn = candidate._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        created = 0
        for event in events:
            created += int(candidate.upsert_trade_event(event, conn=conn))
        _fail(failure_hook, "after_event_write")
        if mode == "fast_if_safe" and events and created == 0:
            result = _unchanged_runtime_result_if_trusted(
                candidate,
                conn=conn,
                mode_requested=mode,
                implementation=implementation,
            )
            if result is not None:
                _fail(failure_hook, "before_commit")
                conn.commit()
                return result
        result = _run_in_transaction(
            candidate,
            conn=conn,
            mode=mode,
            seed_checkpoint=seed_checkpoint,
            implementation=implementation,
            failure_hook=failure_hook,
        )
        _fail(failure_hook, "before_commit")
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _unchanged_runtime_result_if_trusted(
    repo: SQLiteOptionPositionsRepository,
    *,
    conn: Any,
    mode_requested: str,
    implementation: str,
) -> ProjectionRuntimeResult | None:
    source = repo.read_position_projection_source_state(conn=conn)
    if (
        str(source.get("projector_schema") or "") != POSITION_PROJECTION_SCHEMA
        or str(source.get("projector_implementation_fingerprint") or "")
        != implementation
        or _int_or_missing(source.get("sqlite_schema_cookie"))
        != repo.position_projection_schema_cookie(conn=conn)
    ):
        return None
    lot_count = 0
    accounts = repo.list_position_projection_accounts(conn=conn)
    if not accounts:
        return None
    for account in accounts:
        head = repo.read_position_projection_account_metadata(account, conn=conn)["head"]
        if (
            head is None
            or str(head.get("status") or "") != "trusted"
            or str(head.get("projector_schema") or "") != POSITION_PROJECTION_SCHEMA
            or str(head.get("projector_implementation_fingerprint") or "")
            != implementation
            or _int_or_missing(head.get("built_source_generation"))
            != int(source["source_generation"])
            or _int_or_missing(head.get("built_lots_generation"))
            != int(head.get("lots_generation") or 0)
        ):
            return None
        lot_count += int((head or {}).get("lot_count") or 0)
    return ProjectionRuntimeResult(
        mode_requested=mode_requested,
        mode_used="unchanged",
        fallback_reason=None,
        checkpoint_id=None,
        parent_checkpoint_id=None,
        checkpoint_written=False,
        pruned_checkpoint_ids=(),
        tail_event_count=0,
        tail_event_bytes=0,
        publication=PositionProjectionPublication(
            position_lot_count=lot_count,
            added=0,
            changed=0,
            removed=0,
            unchanged=lot_count,
            touched_accounts=(),
            heads_trusted=True,
            trust_reason=None,
        ),
    )


def _run_in_transaction(
    repo: SQLiteOptionPositionsRepository,
    *,
    conn: Any,
    mode: str,
    seed_checkpoint: bool,
    implementation: str,
    failure_hook: Callable[[str], None] | None,
) -> ProjectionRuntimeResult:
    source = repo.read_position_projection_source_state(conn=conn)
    checkpoint_mode = str(source.get("checkpoint_mode") or "disabled")
    fallback_reason: str | None = None
    decoded: _DecodedCheckpoint | None = None

    if mode == "forced_full":
        fallback_reason = "forced_full"
    elif checkpoint_mode != "enabled":
        fallback_reason = f"checkpoint_mode_{checkpoint_mode}"
    else:
        row = repo.read_newest_trusted_position_projection_checkpoint(conn=conn)
        if row is None:
            fallback_reason = "checkpoint_missing_or_invalidated"
        else:
            try:
                decoded = _decode_checkpoint(
                    row,
                    source=source,
                    schema_cookie=repo.position_projection_schema_cookie(conn=conn),
                    implementation=implementation,
                )
            except (TypeError, ValueError) as exc:
                repo.invalidate_position_projection_checkpoints(
                    reason="checkpoint_corrupt_or_incompatible",
                    checkpoint_ids=(str(row.get("checkpoint_id") or ""),),
                    mark_mode_untrusted=True,
                    conn=conn,
                )
                checkpoint_mode = "untrusted"
                fallback_reason = f"checkpoint_untrusted:{exc}"

    if decoded is not None:
        fast = _try_fast_path(
            repo,
            conn=conn,
            checkpoint=decoded,
            source=source,
            implementation=implementation,
            failure_hook=failure_hook,
        )
        if isinstance(fast, ProjectionRuntimeResult):
            return fast
        fallback_reason = fast

    return _run_full_path(
        repo,
        conn=conn,
        mode_requested=mode,
        fallback_reason=fallback_reason or "checkpoint_unavailable",
        seed_checkpoint=(
            seed_checkpoint
            or (checkpoint_mode == "enabled" and mode == "fast_if_safe")
        ),
        implementation=implementation,
        failure_hook=failure_hook,
    )


def _try_fast_path(
    repo: SQLiteOptionPositionsRepository,
    *,
    conn: Any,
    checkpoint: _DecodedCheckpoint,
    source: dict[str, Any],
    implementation: str,
    failure_hook: Callable[[str], None] | None,
) -> ProjectionRuntimeResult | str:
    row = checkpoint.row
    tail_rows = repo.list_position_projection_event_rows(
        after=(int(row["prefix_end_trade_time_ms"]), str(row["prefix_end_event_id"])),
        conn=conn,
    )
    if int(source["source_generation"]) != int(row["source_generation"]) + len(tail_rows):
        return "source_generation_not_strict_append"
    tail_events = [_application_event(item["event_json"]) for item in tail_rows]
    for tail_row, event in zip(tail_rows, tail_events, strict=True):
        stored_account = str(tail_row.get("account") or "").strip()
        event_account = str((event.get("contract_key") or {}).get("account") or "").strip()
        if (
            not stored_account
            or stored_account != stored_account.lower()
            or stored_account != event_account
        ):
            return "normalized_columns_incomplete"
    tail_accounts = {
        str(item.get("account") or "").strip()
        for item in tail_rows
        if str(item.get("account") or "").strip()
    }
    head_problem = _head_invariant_problem(
        repo,
        conn=conn,
        source=source,
        checkpoint=checkpoint,
        tail_accounts=tail_accounts,
        implementation=implementation,
    )
    if head_problem is not None:
        return head_problem
    projection = project_stored_trade_events_to_resumable_position_lots(
        tail_events,
        domain_state=checkpoint.domain_state,
        publication_state=checkpoint.publication_state,
        entry_mode="tail",
    )
    if not projection.eligible:
        return projection.full_replay_reason or "tail_projection_ineligible"
    _fail(failure_hook, "after_tail_projection")

    tail_open_events: dict[str, str] = {}
    for event in tail_events:
        if event.get("event_type") != "open":
            continue
        lot_id = str(event.get("lot_id") or f"lot_{event.get('event_id') or ''}")
        if lot_id in tail_open_events:
            return "new_lot_id_collision"
        tail_open_events[lot_id] = str(event.get("event_id") or "")
    if set(tail_open_events) & set(checkpoint.publication_state.fields_by_lot_id):
        return "new_lot_id_collision"
    stored_open_ids = {
        str(item["record_id"]): str((item.get("fields") or {}).get("source_event_id") or "")
        for item in repo.get_position_lots_by_ids(tuple(tail_open_events), conn=conn)
    }
    tail_event_ids = {str(item["event_id"]) for item in tail_rows}
    if any(source_event_id not in tail_event_ids for source_event_id in stored_open_ids.values()):
        return "new_lot_id_collision"

    publication = _publish_touched_projection(
        repo,
        projection.touched_lots,
        conn=conn,
        implementation=implementation,
    )
    if not publication.heads_trusted:
        raise RuntimeError(f"tail head publication is untrusted: {publication.trust_reason}")
    _fail(failure_hook, "after_head_publication")

    tail_bytes = sum(len(str(item["event_json"]).encode("utf-8")) for item in tail_rows)
    checkpoint_id = str(row["checkpoint_id"])
    wrote = False
    pruned: tuple[str, ...] = ()
    if tail_rows and (
        len(tail_rows) >= CHECKPOINT_ROTATE_EVENT_COUNT
        or tail_bytes >= CHECKPOINT_ROTATE_EVENT_BYTES
    ):
        if projection.domain_state is None or projection.publication_state is None:
            raise RuntimeError("eligible tail projection lacks resumable state")
        chain = str(row["prefix_chain_sha256"])
        for item in tail_rows:
            chain = extend_event_prefix_chain(chain, str(item["event_json"]))
        checkpoint_row = _build_checkpoint_row(
            domain_state=projection.domain_state,
            publication_state=projection.publication_state,
            prefix_event_count=int(row["prefix_event_count"]) + len(tail_rows),
            prefix_end_trade_time_ms=int(tail_rows[-1]["trade_time_ms"]),
            prefix_end_event_id=str(tail_rows[-1]["event_id"]),
            prefix_chain_sha256=chain,
            source_generation=int(source["source_generation"]),
            sqlite_schema_cookie=repo.position_projection_schema_cookie(conn=conn),
            implementation=implementation,
            verification_kind="derived",
            parent_checkpoint_id=checkpoint_id,
        )
        if checkpoint_row is None:
            repo.invalidate_position_projection_checkpoints(
                reason="checkpoint_state_too_large",
                mark_mode_untrusted=True,
                conn=conn,
            )
            checkpoint_id = None
        else:
            _fail(failure_hook, "before_checkpoint_insert")
            repo.insert_position_projection_checkpoint(checkpoint_row, conn=conn)
            checkpoint_id = str(checkpoint_row["checkpoint_id"])
            wrote = True
            _fail(failure_hook, "after_checkpoint_insert")
            pruned = repo.prune_position_projection_checkpoints(conn=conn)
            _fail(failure_hook, "after_checkpoint_prune")

    return ProjectionRuntimeResult(
        mode_requested="fast_if_safe",
        mode_used="fast_tail",
        fallback_reason=None,
        checkpoint_id=checkpoint_id,
        parent_checkpoint_id=str(row["checkpoint_id"]),
        checkpoint_written=wrote,
        pruned_checkpoint_ids=pruned,
        tail_event_count=len(tail_rows),
        tail_event_bytes=tail_bytes,
        publication=publication,
    )


def _publish_touched_projection(
    repo: SQLiteOptionPositionsRepository,
    records: Sequence[Any],
    *,
    conn: Any,
    implementation: str,
) -> PositionProjectionPublication:
    diff = repo.apply_position_lot_diff(
        records,
        remove_missing=False,
        conn=conn,
    )
    lot_count, trusted, reason = repo.publish_full_position_projection_heads(
        implementation_fingerprint=implementation,
        known_accounts=diff.accounts,
        changed_accounts=diff.touched_accounts,
        full_verified=False,
        publish_source_implementation=False,
        readiness_prevalidated=True,
        conn=conn,
    )
    if trusted and int(lot_count) != diff.lot_count:
        raise RuntimeError("position projection head count differs from published lot diff")
    return PositionProjectionPublication(
        position_lot_count=diff.lot_count,
        added=diff.added,
        changed=diff.changed,
        removed=diff.removed,
        unchanged=diff.unchanged,
        touched_accounts=diff.touched_accounts,
        heads_trusted=bool(trusted),
        trust_reason=reason,
    )


def _run_full_path(
    repo: SQLiteOptionPositionsRepository,
    *,
    conn: Any,
    mode_requested: str,
    fallback_reason: str,
    seed_checkpoint: bool,
    implementation: str,
    failure_hook: Callable[[str], None] | None,
) -> ProjectionRuntimeResult:
    rows = repo.list_position_projection_event_rows(conn=conn)
    events = [_application_event(item["event_json"]) for item in rows]
    projection = project_stored_trade_events_to_position_lots(events)
    ensure_projection_publishable(projection, operation="position projection runtime full replay")
    if projection.resumable_state is None or projection.resumable_publication_state is None:
        raise RuntimeError("publishable full projection lacks resumable state")
    _fail(failure_hook, "after_full_projection")
    publication = publish_full_position_projection(repo, projection.lots, conn=conn)
    if not publication.heads_trusted:
        seed_checkpoint = False
    _fail(failure_hook, "after_head_publication")

    checkpoint_id: str | None = None
    pruned: tuple[str, ...] = ()
    if seed_checkpoint:
        chain = initial_event_prefix_chain()
        for item in rows:
            chain = extend_event_prefix_chain(chain, str(item["event_json"]))
        checkpoint_row = _build_checkpoint_row(
            domain_state=projection.resumable_state,
            publication_state=projection.resumable_publication_state,
            prefix_event_count=len(rows),
            prefix_end_trade_time_ms=int(rows[-1]["trade_time_ms"]) if rows else 0,
            prefix_end_event_id=str(rows[-1]["event_id"]) if rows else "",
            prefix_chain_sha256=chain,
            source_generation=int(repo.read_position_projection_source_state(conn=conn)["source_generation"]),
            sqlite_schema_cookie=repo.position_projection_schema_cookie(conn=conn),
            implementation=implementation,
            verification_kind="full_oracle",
            parent_checkpoint_id=None,
        )
        if checkpoint_row is None:
            repo.invalidate_position_projection_checkpoints(
                reason="checkpoint_state_too_large",
                mark_mode_untrusted=True,
                conn=conn,
            )
        else:
            _fail(failure_hook, "before_checkpoint_insert")
            repo.insert_position_projection_checkpoint(checkpoint_row, conn=conn)
            checkpoint_id = str(checkpoint_row["checkpoint_id"])
            _fail(failure_hook, "after_checkpoint_insert")
            pruned = repo.prune_position_projection_checkpoints(conn=conn)
            _fail(failure_hook, "after_checkpoint_prune")

    return ProjectionRuntimeResult(
        mode_requested=mode_requested,
        mode_used="full",
        fallback_reason=fallback_reason,
        checkpoint_id=checkpoint_id,
        parent_checkpoint_id=None,
        checkpoint_written=checkpoint_id is not None,
        pruned_checkpoint_ids=pruned,
        tail_event_count=0,
        tail_event_bytes=0,
        publication=publication,
    )


def _head_invariant_problem(
    repo: SQLiteOptionPositionsRepository,
    *,
    conn: Any,
    source: dict[str, Any],
    checkpoint: _DecodedCheckpoint,
    tail_accounts: set[str],
    implementation: str,
) -> str | None:
    if repo.position_projection_column_contract(conn=conn) != {
        "trade_events": {"missing": (), "unclassified": ()},
        "position_lots": {"missing": (), "unclassified": ()},
    }:
        return "column_contract_open"
    if not repo.position_projection_indexes_ready(conn=conn):
        return "normalized_indexes_missing"
    checkpoint_accounts = set(checkpoint.domain_state.accounts)
    if any(account != account.lower() for account in tail_accounts):
        return "normalized_columns_incomplete"
    for account in repo.list_position_projection_accounts(conn=conn):
        head = repo.read_position_projection_account_metadata(account, conn=conn)["head"]
        if head is None:
            return "projection_head_missing"
        if str(head.get("status") or "") == "uninitialized":
            if (
                account in tail_accounts
                and account not in checkpoint_accounts
                and int(head.get("lots_generation") or 0) == 0
                and int(head.get("lot_count") or 0) == 0
            ):
                continue
            return "projection_head_stale"
        built_source = _int_or_missing(head.get("built_source_generation"))
        checks = (
            str(head.get("status") or "") == "trusted",
            str(head.get("projector_schema") or "") == POSITION_PROJECTION_SCHEMA,
            str(head.get("projector_implementation_fingerprint") or "") == implementation,
            int(checkpoint.row["source_generation"])
            <= built_source
            <= int(source["source_generation"]),
            _int_or_missing(head.get("built_lots_generation"))
            == int(head["lots_generation"]),
        )
        if not all(checks):
            return "projection_head_stale"
    return None


def _build_checkpoint_row(
    *,
    domain_state: ResumableProjectionState,
    publication_state: ResumablePublicationState,
    prefix_event_count: int,
    prefix_end_trade_time_ms: int,
    prefix_end_event_id: str,
    prefix_chain_sha256: str,
    source_generation: int,
    sqlite_schema_cookie: int,
    implementation: str,
    verification_kind: str,
    parent_checkpoint_id: str | None,
) -> dict[str, Any] | None:
    payload = _encode_accumulator(domain_state, publication_state)
    if len(payload) > MAX_CHECKPOINT_STATE_BYTES:
        return None
    accumulator_sha256 = hashlib.sha256(payload).hexdigest()
    checkpoint_id = _checkpoint_id(
        implementation=implementation,
        sqlite_schema_cookie=sqlite_schema_cookie,
        prefix_event_count=prefix_event_count,
        prefix_end_trade_time_ms=prefix_end_trade_time_ms,
        prefix_end_event_id=prefix_end_event_id,
        prefix_chain_sha256=prefix_chain_sha256,
        source_generation=source_generation,
        accumulator_sha256=accumulator_sha256,
    )
    from domain.domain.ledger.position_fields import now_ms

    ts = int(now_ms())
    return {
        "checkpoint_id": checkpoint_id,
        "projector_schema": POSITION_PROJECTION_SCHEMA,
        "projector_implementation_fingerprint": implementation,
        "prefix_event_count": int(prefix_event_count),
        "prefix_end_trade_time_ms": int(prefix_end_trade_time_ms),
        "prefix_end_event_id": str(prefix_end_event_id),
        "prefix_chain_sha256": str(prefix_chain_sha256),
        "source_generation": int(source_generation),
        "sqlite_schema_cookie": int(sqlite_schema_cookie),
        "accumulator_json": payload,
        "accumulator_sha256": accumulator_sha256,
        "diagnostic_count": 0,
        "diagnostic_sha256": EMPTY_PROJECTION_DIAGNOSTIC_SHA256,
        "state_bytes": len(payload),
        "trust_status": "trusted",
        "verification_kind": verification_kind,
        "parent_checkpoint_id": parent_checkpoint_id,
        "created_at_ms": ts,
        "verified_at_ms": ts,
        "invalidated_at_ms": None,
        "invalidation_reason": None,
    }


def _decode_checkpoint(
    row: dict[str, Any],
    *,
    source: dict[str, Any],
    schema_cookie: int,
    implementation: str,
) -> _DecodedCheckpoint:
    if str(row.get("trust_status") or "") != "trusted":
        raise ValueError("checkpoint is not trusted")
    if str(row.get("projector_schema") or "") != POSITION_PROJECTION_SCHEMA:
        raise ValueError("checkpoint projector schema mismatch")
    if str(row.get("projector_implementation_fingerprint") or "") != implementation:
        raise ValueError("checkpoint implementation mismatch")
    if str(source.get("projector_implementation_fingerprint") or "") != implementation:
        raise ValueError("source implementation mismatch")
    if int(row.get("sqlite_schema_cookie") or -1) != int(schema_cookie):
        raise ValueError("checkpoint SQLite schema cookie mismatch")
    if int(source.get("sqlite_schema_cookie") or -1) != int(schema_cookie):
        raise ValueError("source SQLite schema cookie mismatch")
    payload = bytes(row.get("accumulator_json") or b"")
    if not payload or len(payload) > MAX_CHECKPOINT_STATE_BYTES:
        raise ValueError("checkpoint state size is impossible")
    if int(row.get("state_bytes") or -1) != len(payload):
        raise ValueError("checkpoint state length mismatch")
    if hashlib.sha256(payload).hexdigest() != str(row.get("accumulator_sha256") or ""):
        raise ValueError("checkpoint accumulator hash mismatch")
    if int(row.get("diagnostic_count") if row.get("diagnostic_count") is not None else -1) != 0 or str(
        row.get("diagnostic_sha256") or ""
    ) != EMPTY_PROJECTION_DIAGNOSTIC_SHA256:
        raise ValueError("checkpoint diagnostic sentinel mismatch")
    domain_state, publication_state = _decode_accumulator(payload)
    if len(domain_state.active_lots) > int(row.get("prefix_event_count") or 0):
        raise ValueError("checkpoint active lot count is impossible")
    expected_id = _checkpoint_id(
        implementation=implementation,
        sqlite_schema_cookie=int(row["sqlite_schema_cookie"]),
        prefix_event_count=int(row["prefix_event_count"]),
        prefix_end_trade_time_ms=int(row["prefix_end_trade_time_ms"]),
        prefix_end_event_id=str(row["prefix_end_event_id"]),
        prefix_chain_sha256=str(row["prefix_chain_sha256"]),
        source_generation=int(row["source_generation"]),
        accumulator_sha256=str(row["accumulator_sha256"]),
    )
    if expected_id != str(row.get("checkpoint_id") or ""):
        raise ValueError("checkpoint identity mismatch")
    return _DecodedCheckpoint(
        row=row,
        domain_state=domain_state,
        publication_state=publication_state,
    )


def _encode_accumulator(
    domain_state: ResumableProjectionState,
    publication_state: ResumablePublicationState,
) -> bytes:
    if {item.lot_id for item in domain_state.active_lots} != set(
        publication_state.fields_by_lot_id
    ):
        raise ValueError("checkpoint domain/publication lot ids differ")
    return _canonical_json_bytes(
        {
            "schema_version": POSITION_PROJECTION_CHECKPOINT_SCHEMA,
            "rotation_event_count": CHECKPOINT_ROTATE_EVENT_COUNT,
            "rotation_event_bytes": CHECKPOINT_ROTATE_EVENT_BYTES,
            "domain_state": domain_state.to_dict(),
            "publication_state": publication_state.to_dict(),
        }
    )


def _decode_accumulator(
    payload: bytes,
) -> tuple[ResumableProjectionState, ResumablePublicationState]:
    decoded = _strict_json_loads(payload)
    expected_keys = {
        "schema_version",
        "rotation_event_count",
        "rotation_event_bytes",
        "domain_state",
        "publication_state",
    }
    if not isinstance(decoded, dict) or set(decoded) != expected_keys:
        raise ValueError("checkpoint accumulator fields differ from v1 schema")
    if decoded["schema_version"] != POSITION_PROJECTION_CHECKPOINT_SCHEMA:
        raise ValueError("checkpoint accumulator schema mismatch")
    if decoded["rotation_event_count"] != CHECKPOINT_ROTATE_EVENT_COUNT or decoded[
        "rotation_event_bytes"
    ] != CHECKPOINT_ROTATE_EVENT_BYTES:
        raise ValueError("checkpoint rotation contract mismatch")
    if _canonical_json_bytes(decoded) != bytes(payload):
        raise ValueError("checkpoint accumulator JSON is not canonical")
    domain_payload = decoded.pop("domain_state")
    publication_payload = decoded.pop("publication_state")
    del decoded
    domain_state = ResumableProjectionState.from_dict(domain_payload)
    del domain_payload
    publication_state = ResumablePublicationState.from_dict(publication_payload)
    del publication_payload
    if {item.lot_id for item in domain_state.active_lots} != set(
        publication_state.fields_by_lot_id
    ):
        raise ValueError("checkpoint domain/publication lot ids differ")
    return domain_state, publication_state


def _checkpoint_id(
    *,
    implementation: str,
    sqlite_schema_cookie: int,
    prefix_event_count: int,
    prefix_end_trade_time_ms: int,
    prefix_end_event_id: str,
    prefix_chain_sha256: str,
    source_generation: int,
    accumulator_sha256: str,
) -> str:
    identity = {
        "projector_schema": POSITION_PROJECTION_SCHEMA,
        "projector_implementation_fingerprint": implementation,
        "sqlite_schema_cookie": int(sqlite_schema_cookie),
        "prefix_event_count": int(prefix_event_count),
        "prefix_end_trade_time_ms": int(prefix_end_trade_time_ms),
        "prefix_end_event_id": str(prefix_end_event_id),
        "prefix_chain_sha256": str(prefix_chain_sha256),
        "source_generation": int(source_generation),
        "accumulator_sha256": str(accumulator_sha256),
    }
    return hashlib.sha256(_canonical_json_bytes(identity)).hexdigest()


def _application_event(event_json: str) -> dict[str, Any]:
    payload = _strict_json_loads(str(event_json).encode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("stored trade event is not a JSON object")
    return trade_event_application_payload(payload)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _strict_json_loads(payload: bytes) -> Any:
    def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in items:
            if key in out:
                raise ValueError(f"duplicate JSON key: {key}")
            out[key] = value
        return out

    def _constant(value: str) -> Any:
        raise ValueError(f"non-finite JSON number: {value}")

    try:
        return json.loads(
            bytes(payload).decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("payload is not valid UTF-8 JSON") from exc


def _fail(hook: Callable[[str], None] | None, stage: str) -> None:
    if hook is not None:
        hook(stage)


def _int_or_missing(value: Any) -> int:
    return -1 if value is None else int(value)


__all__ = [
    "CHECKPOINT_ROTATE_EVENT_BYTES",
    "CHECKPOINT_ROTATE_EVENT_COUNT",
    "POSITION_PROJECTION_CHECKPOINT_SCHEMA",
    "ProjectionRuntimeResult",
    "extend_event_prefix_chain",
    "initial_event_prefix_chain",
    "run_position_projection_fast_if_safe",
    "run_position_projection_forced_full",
]
