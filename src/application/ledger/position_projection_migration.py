from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import time
from typing import Any, Callable, Iterator, Mapping

from domain.domain.ledger.position_fingerprint import (
    ordered_position_lots_fingerprint,
)
from src.application.ledger.event_codec import trade_event_application_payload
from src.application.ledger.position_projection_runtime import (
    MAX_CHECKPOINT_STATE_BYTES,
    _decode_checkpoint,
    extend_event_prefix_chain,
    initial_event_prefix_chain,
    position_projection_runtime_telemetry,
    run_position_projection_in_transaction,
)
from src.application.ledger.projector_implementation import (
    loaded_projector_implementation_fingerprint,
)
from src.application.ledger.projection_verify import compare_projection_lots
from src.application.ledger.publisher import (
    project_stored_trade_events_to_position_lots,
    project_stored_trade_events_to_resumable_position_lots,
)
from src.application.ledger.repository import (
    POSITION_LOTS_COLUMN_CLASSIFICATION,
    POSITION_PROJECTION_SCHEMA,
    TRADE_EVENTS_COLUMN_CLASSIFICATION,
    SQLiteOptionPositionsRepository,
    _ensure_position_projection_schema,
    _position_lot_contract_scalars,
    initialize_ledger_connection,
)
from src.application.ledger.sqlite_row_codec import position_lot_row_to_record
from src.infrastructure.private_storage import (
    connect_private_sqlite,
    private_path,
    secure_sqlite_artifacts,
)


INVENTORY_SCHEMA = "position_projection_migration_inventory.v1"
VERIFY_SCHEMA = "position_projection_migration_verify.v1"
ACCEPTANCE_SCHEMA = "data_storage_projection_phase3a_acceptance.v1"

REQUIRED_INDEXES = (
    "idx_trade_events_trade_time",
    "idx_trade_events_account_time",
    "idx_position_lots_account_expiration",
    "idx_position_lots_account_record",
)
REQUIRED_TRIGGERS = (
    "trg_trade_events_account_insert_guard",
    "trg_trade_events_account_update_guard",
    "trg_trade_events_source_insert",
    "trg_trade_events_source_update",
    "trg_trade_events_source_delete",
    "trg_position_lots_account_insert_guard",
    "trg_position_lots_account_update_guard",
    "trg_position_lots_generation_insert",
    "trg_position_lots_generation_delete",
    "trg_position_lots_generation_update_same",
    "trg_position_lots_generation_update_old",
    "trg_position_lots_generation_update_new",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _manifest(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "manifest_hash": _sha256(payload)}


def _validate_manifest(payload: Mapping[str, Any], *, schema: str) -> dict[str, Any]:
    value = dict(payload)
    if value.get("schema_version") != schema:
        raise ValueError(f"manifest schema must be {schema}")
    supplied = str(value.pop("manifest_hash", "") or "")
    if len(supplied) != 64 or supplied != _sha256(value):
        raise ValueError("manifest hash mismatch")
    return {**value, "manifest_hash": supplied}


def _store_path(value: str | Path) -> Path:
    path = private_path(value)
    if path.is_symlink() or not path.exists():
        raise ValueError("projection migration requires an existing non-symlink SQLite store")
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("projection migration store must be a regular file")
    return path


def _store_identity(path: Path) -> dict[str, Any]:
    info = path.stat(follow_symlinks=False)
    fields = {
        "resolved_path": str(path),
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
    }
    return {**fields, "identity_sha256": _sha256(fields)}


@contextmanager
def _read_only_connection(path: Path) -> Iterator[sqlite3.Connection]:
    uri = f"{path.as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        yield conn
    finally:
        conn.close()


def _write_connection(path: Path) -> sqlite3.Connection:
    conn = connect_private_sqlite(path)
    initialize_ledger_connection(conn)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _repository(path: Path) -> SQLiteOptionPositionsRepository:
    """Bind existing repository methods without running startup DDL."""

    repo = object.__new__(SQLiteOptionPositionsRepository)
    repo.db_path = path
    repo.data_config_path = None
    repo.bootstrap_status = "migration_existing_store"
    repo.bootstrap_message = None
    return repo


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def _column_names(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    if not _table_exists(conn, table):
        return ()
    return tuple(str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})"))


def _schema_cookie(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA schema_version").fetchone()
    return int(row[0]) if row is not None else 0


def _int_or_missing(value: Any) -> int:
    return -1 if value is None else int(value)


def _object_names(conn: sqlite3.Connection, kind: str) -> set[str]:
    return {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type=? ORDER BY name",
            (kind,),
        )
    }


def _row_fingerprint(
    conn: sqlite3.Connection,
    *,
    table: str,
    columns: tuple[str, ...],
    order_by: str,
) -> tuple[str, int, int]:
    if not _table_exists(conn, table):
        return _sha256([]), 0, 0
    digest = hashlib.sha256()
    digest.update(_canonical_bytes({"table": table, "columns": columns}))
    count = 0
    payload_bytes = 0
    cursor = conn.execute(
        f"SELECT {','.join(columns)} FROM {table} ORDER BY {order_by}"
    )
    for row in cursor:
        payload = _canonical_bytes([row[column] for column in columns])
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        count += 1
        payload_bytes += len(payload)
    return digest.hexdigest(), count, payload_bytes


def _json_object(raw: Any) -> dict[str, Any] | None:
    try:
        value = json.loads(str(raw or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _account_inventory(conn: sqlite3.Connection) -> dict[str, Any]:
    mappings: dict[str, int] = {}
    event_null = event_conflict = event_invalid = 0
    if _table_exists(conn, "trade_events"):
        columns = set(_column_names(conn, "trade_events"))
        account_expr = "account" if "account" in columns else "NULL AS account"
        for row in conn.execute(
            f"SELECT event_id, {account_expr}, event_json FROM trade_events ORDER BY event_id"
        ):
            payload = _json_object(row["event_json"])
            contract_key = payload.get("contract_key") if isinstance(payload, dict) else None
            canonical = str(
                (contract_key.get("account") if isinstance(contract_key, dict) else None)
                or (payload.get("account") if isinstance(payload, dict) else None)
                or ""
            ).strip()
            stored = str(row["account"] or "").strip()
            if payload is None or not canonical or canonical != canonical.lower():
                event_invalid += 1
            else:
                mappings[canonical] = mappings.get(canonical, 0) + 1
            if not stored:
                event_null += 1
            elif stored != canonical:
                event_conflict += 1

    lot_null = lot_conflict = lot_invalid = scalar_null = scalar_conflict = 0
    if _table_exists(conn, "position_lots"):
        columns = set(_column_names(conn, "position_lots"))
        selected = ["record_id", "fields_json"]
        for name in ("account", "expiration", "strike", "multiplier"):
            selected.append(name if name in columns else f"NULL AS {name}")
        for row in conn.execute(
            f"SELECT {','.join(selected)} FROM position_lots ORDER BY record_id"
        ):
            fields = _json_object(row["fields_json"])
            canonical = str(fields.get("account") if isinstance(fields, dict) else "").strip()
            stored = str(row["account"] or "").strip()
            if fields is None or not canonical or canonical != canonical.lower():
                lot_invalid += 1
            else:
                mappings[canonical] = mappings.get(canonical, 0) + 1
            if not stored:
                lot_null += 1
            elif stored != canonical:
                lot_conflict += 1
            if not isinstance(fields, dict):
                continue
            expiration, strike_value, multiplier_value = _position_lot_contract_scalars(fields)
            expected = (expiration, strike_value, multiplier_value)
            actual = (row["expiration"], row["strike"], row["multiplier"])
            if str(fields.get("option_type") or "").lower() in {"put", "call"} and any(
                item is None for item in actual
            ):
                scalar_null += 1
            if any(
                left is not None
                and right is not None
                and abs(float(left) - float(right)) > 1e-9
                for left, right in zip(actual, expected, strict=True)
            ):
                scalar_conflict += 1
    return {
        "canonical_account_mappings": [
            {"account": account, "row_count": mappings[account]}
            for account in sorted(mappings)
        ],
        "trade_events": {
            "normalized_account_null_count": event_null,
            "account_conflict_count": event_conflict,
            "canonical_account_invalid_count": event_invalid,
        },
        "position_lots": {
            "normalized_account_null_count": lot_null,
            "account_conflict_count": lot_conflict,
            "canonical_account_invalid_count": lot_invalid,
            "contract_scalar_null_count": scalar_null,
            "contract_scalar_conflict_count": scalar_conflict,
        },
    }


def _column_contract(conn: sqlite3.Connection) -> dict[str, dict[str, list[str]]]:
    result: dict[str, dict[str, list[str]]] = {}
    for table, expected in (
        ("trade_events", TRADE_EVENTS_COLUMN_CLASSIFICATION),
        ("position_lots", POSITION_LOTS_COLUMN_CLASSIFICATION),
    ):
        actual = set(_column_names(conn, table))
        result[table] = {
            "missing": sorted(set(expected) - actual),
            "unclassified": sorted(actual - set(expected)),
        }
    return result


def _source_state(conn: sqlite3.Connection) -> dict[str, Any] | None:
    if not _table_exists(conn, "position_projection_source_state"):
        return None
    row = conn.execute(
        "SELECT * FROM position_projection_source_state WHERE singleton_id=1"
    ).fetchone()
    return dict(row) if row is not None else None


def _estimated_checkpoint_bytes(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "position_lots"):
        return 0
    row = conn.execute(
        """
        SELECT coalesce(sum(length(fields_json)), 0), count(*)
        FROM position_lots
        WHERE json_valid(fields_json)=1
          AND json_extract(fields_json, '$.status')='open'
        """
    ).fetchone()
    payload = int(row[0] or 0) if row is not None else 0
    count = int(row[1] or 0) if row is not None else 0
    return payload * 2 + count * 256 + 4_096


def _inventory_from_conn(
    path: Path,
    conn: sqlite3.Connection,
    *,
    implementation: str,
    implementation_timing: Mapping[str, int],
) -> dict[str, Any]:
    event_columns = set(_column_names(conn, "trade_events"))
    lot_columns = set(_column_names(conn, "position_lots"))
    event_fp_columns = tuple(
        name
        for name in ("event_id", "account", "event_json", "trade_time_ms")
        if name in event_columns
    )
    lot_fp_columns = tuple(
        name
        for name in (
            "record_id",
            "account",
            "fields_json",
            "source_event_id",
            "expiration",
            "strike",
            "multiplier",
        )
        if name in lot_columns
    )
    event_fp, event_count, event_bytes = _row_fingerprint(
        conn,
        table="trade_events",
        columns=event_fp_columns or ("rowid",),
        order_by="trade_time_ms,event_id" if {"trade_time_ms", "event_id"}.issubset(event_columns) else "rowid",
    )
    lot_fp, lot_count, lot_bytes = _row_fingerprint(
        conn,
        table="position_lots",
        columns=lot_fp_columns or ("rowid",),
        order_by="record_id" if "record_id" in lot_columns else "rowid",
    )
    indexes = _object_names(conn, "index")
    triggers = _object_names(conn, "trigger")
    columns = _column_contract(conn)
    accounts = _account_inventory(conn)
    source = _source_state(conn)
    reasons: list[str] = []
    if not event_columns or not lot_columns:
        reasons.append("base_tables_missing")
    if any(details["missing"] or details["unclassified"] for details in columns.values()):
        reasons.append("column_contract_open")
    if any(
        int(value) > 0
        for section in (accounts["trade_events"], accounts["position_lots"])
        for value in section.values()
    ):
        reasons.append("normalized_columns_incomplete")
    missing_indexes = sorted(set(REQUIRED_INDEXES) - indexes)
    missing_triggers = sorted(set(REQUIRED_TRIGGERS) - triggers)
    if missing_indexes:
        reasons.append("required_indexes_missing")
    if missing_triggers:
        reasons.append("required_triggers_missing")
    stable = {
        "store_identity": _store_identity(path),
        "projector_schema": POSITION_PROJECTION_SCHEMA,
        "loaded_projector_implementation_fingerprint": implementation,
        "sqlite_schema_cookie": _schema_cookie(conn),
        "counts": {
            "trade_events": event_count,
            "position_lots": lot_count,
        },
        "fingerprints": {
            "trade_events": event_fp,
            "position_lots": lot_fp,
            "trade_event_stream_bytes": event_bytes,
            "position_lot_stream_bytes": lot_bytes,
        },
        "accounts": accounts,
        "column_contract": columns,
        "required_indexes": {
            "present": sorted(set(REQUIRED_INDEXES) & indexes),
            "missing": missing_indexes,
        },
        "required_triggers": {
            "present": sorted(set(REQUIRED_TRIGGERS) & triggers),
            "missing": missing_triggers,
        },
        "source_state": source,
        "estimated_checkpoint_bytes": _estimated_checkpoint_bytes(conn),
    }
    return {
        **stable,
        "inventory_fingerprint": _sha256(stable),
        "readiness": "ready" if not reasons else "not_ready",
        "readiness_reasons": reasons,
        "loaded_projector_fingerprint_timing": dict(implementation_timing),
    }


def _loaded_implementation() -> tuple[str, dict[str, int]]:
    wall_start = time.perf_counter_ns()
    cpu_start = time.process_time_ns()
    implementation = loaded_projector_implementation_fingerprint()
    return implementation, {
        "wall_ns": time.perf_counter_ns() - wall_start,
        "cpu_ns": time.process_time_ns() - cpu_start,
    }


def build_position_projection_migration_inventory(
    sqlite_path: str | Path,
) -> dict[str, Any]:
    path = _store_path(sqlite_path)
    implementation, timing = _loaded_implementation()
    before = _file_sizes(path)
    with _read_only_connection(path) as conn:
        inventory = _inventory_from_conn(
            path,
            conn,
            implementation=implementation,
            implementation_timing=timing,
        )
    after = _file_sizes(path)
    _assert_read_only_persistent_sizes(before, after, operation="inventory")
    return _manifest(
        {
            "schema_version": INVENTORY_SCHEMA,
            "generated_at_utc": _now_iso(),
            "operation": "inventory",
            "read_only": True,
            **inventory,
        }
    )


def apply_position_projection_migration(
    sqlite_path: str | Path,
    manifest: Mapping[str, Any],
    *,
    failure_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    supplied = _validate_manifest(manifest, schema=INVENTORY_SCHEMA)
    path = _store_path(sqlite_path)
    implementation, timing = _loaded_implementation()
    conn = _write_connection(path)
    repo = _repository(path)
    before_sizes = _file_sizes(path)
    wall_start = time.perf_counter_ns()
    cpu_start = time.process_time_ns()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = _inventory_from_conn(
            path,
            conn,
            implementation=implementation,
            implementation_timing=timing,
        )
        if current["inventory_fingerprint"] != supplied.get("inventory_fingerprint"):
            raise ValueError("migration manifest is stale or belongs to another store")
        if current["store_identity"] != supplied.get("store_identity"):
            raise ValueError("migration manifest store identity mismatch")
        if current["loaded_projector_implementation_fingerprint"] != supplied.get(
            "loaded_projector_implementation_fingerprint"
        ):
            raise ValueError("migration manifest projector implementation mismatch")
        if "base_tables_missing" in current["readiness_reasons"]:
            raise ValueError("migration requires existing trade_events and position_lots tables")
        _fail(failure_hook, "after_manifest_recheck")

        _ensure_position_projection_schema(conn)
        _fail(failure_hook, "after_schema")
        account_updates = repo.backfill_position_projection_accounts(conn=conn)
        scalar_updates = repo.backfill_position_lot_contract_columns(conn=conn)
        _fail(failure_hook, "after_backfill")
        index_wall_start = time.perf_counter_ns()
        index_cpu_start = time.process_time_ns()
        indexes_created = repo.build_position_projection_indexes(conn=conn)
        index_timing = {
            "wall_ns": time.perf_counter_ns() - index_wall_start,
            "cpu_ns": time.process_time_ns() - index_cpu_start,
        }
        _fail(failure_hook, "after_indexes")
        runtime = run_position_projection_in_transaction(
            repo,
            (),
            conn=conn,
            mode="forced_full",
            seed_checkpoint=True,
            failure_hook=(
                (lambda stage: _fail(failure_hook, f"projection:{stage}"))
                if failure_hook is not None
                else None
            ),
        )
        if not runtime.publication.heads_trusted or not runtime.checkpoint_written:
            raise RuntimeError("migration full oracle did not publish trusted heads and checkpoint")
        repo.set_position_projection_checkpoint_mode("disabled", conn=conn)
        if len(repo.list_position_projection_checkpoints(conn=conn)) > 3:
            raise RuntimeError("migration checkpoint retention exceeds K=3")
        _fail(failure_hook, "before_commit")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        secure_sqlite_artifacts(path)
    result = {
        "schema_version": "position_projection_migration_apply.v1",
        "generated_at_utc": _now_iso(),
        "operation": "apply",
        "write_applied": True,
        "checkpoint_mode": "disabled",
        "store_identity": _store_identity(path),
        "source_manifest_hash": supplied["manifest_hash"],
        "accounts_backfilled": account_updates,
        "contract_scalars_backfilled": int(scalar_updates),
        "indexes_created": list(indexes_created),
        "index_timing": index_timing,
        "projection": {
            "mode_used": runtime.mode_used,
            "position_lot_count": runtime.position_lot_count,
            "checkpoint_id": runtime.checkpoint_id,
            "checkpoint_written": runtime.checkpoint_written,
        },
        "timing": {
            "wall_ns": time.perf_counter_ns() - wall_start,
            "cpu_ns": time.process_time_ns() - cpu_start,
        },
        "sqlite_bytes": {
            "before": before_sizes,
            "after": _file_sizes(path),
        },
    }
    return _manifest(result)


def _load_event_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {
            "event_id": str(row["event_id"]),
            "account": str(row["account"] or ""),
            "event_json": str(row["event_json"]),
            "trade_time_ms": int(row["trade_time_ms"]),
        }
        for row in conn.execute(
            """
            SELECT event_id, account, event_json, trade_time_ms
            FROM trade_events ORDER BY trade_time_ms,event_id
            """
        )
    ]


def _events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        trade_event_application_payload(json.loads(str(row["event_json"])))
        for row in rows
    ]


def _load_lots(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        position_lot_row_to_record(row)
        for row in conn.execute(
            """
            SELECT record_id, fields_json, expiration, strike, multiplier
            FROM position_lots ORDER BY record_id
            """
        )
    ]


def _source_commit() -> str | None:
    root = Path(__file__).resolve().parents[3]
    try:
        source_status = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                "domain",
                "src",
                "scripts",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if source_status.stdout.strip():
            return None
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _verify_from_conn(
    path: Path,
    conn: sqlite3.Connection,
    *,
    shadow: bool,
    implementation: str,
    source_commit: str | None,
) -> dict[str, Any]:
    inventory = _inventory_from_conn(
        path,
        conn,
        implementation=implementation,
        implementation_timing={"wall_ns": 0, "cpu_ns": 0},
    )
    reasons = list(inventory["readiness_reasons"])
    if not source_commit:
        reasons.append("source_commit_unavailable")
    parity: dict[str, Any] = {"status": "not_run"}
    shadow_result: dict[str, Any] = {"status": "not_requested"}
    checkpoint_summary: dict[str, Any] = {
        "count": 0,
        "trusted_count": 0,
        "state_bytes": 0,
        "max_state_bytes": 0,
        "integrity": "not_checked",
    }
    head_results: list[dict[str, Any]] = []
    source = _source_state(conn)
    required_ready = not any(
        reason
        in {
            "base_tables_missing",
            "column_contract_open",
            "normalized_columns_incomplete",
            "required_indexes_missing",
            "required_triggers_missing",
        }
        for reason in reasons
    )
    if required_ready and source is not None:
        event_rows = _load_event_rows(conn)
        events = _events(event_rows)
        current_lots = _load_lots(conn)
        projection = project_stored_trade_events_to_position_lots(events)
        comparison = compare_projection_lots(
            projected_lots=list(projection.lots),
            current_lots=current_lots,
            diagnostics=list(projection.diagnostics),
        )
        mismatches = sum(
            count for key, count in comparison["summary"].items() if key != "matched"
        )
        parity = {
            "status": "pass" if mismatches == 0 else "fail",
            "mismatch_count": mismatches,
            "summary": comparison["summary"],
            "mismatch_items": [
                item for item in comparison["items"] if item.get("status") != "matched"
            ][:10],
        }
        if mismatches:
            reasons.append("full_oracle_parity_mismatch")

        checkpoint_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM position_projection_checkpoints
                ORDER BY prefix_event_count DESC,created_at_ms DESC,checkpoint_id DESC
                """
            )
        ]
        trusted = [row for row in checkpoint_rows if row.get("trust_status") == "trusted"]
        state_bytes = sum(int(row.get("state_bytes") or 0) for row in checkpoint_rows)
        max_state = max((int(row.get("state_bytes") or 0) for row in checkpoint_rows), default=0)
        integrity_failures: list[str] = []
        decoded = None
        for row in trusted:
            try:
                candidate = _decode_checkpoint(
                    row,
                    source=source,
                    schema_cookie=_schema_cookie(conn),
                    implementation=implementation,
                )
                prefix_count = int(row["prefix_event_count"])
                if prefix_count > len(event_rows):
                    raise ValueError("checkpoint prefix exceeds source event count")
                prefix = event_rows[:prefix_count]
                chain = initial_event_prefix_chain()
                for item in prefix:
                    chain = extend_event_prefix_chain(chain, item["event_json"])
                expected_end = (
                    (prefix[-1]["trade_time_ms"], prefix[-1]["event_id"])
                    if prefix
                    else (0, "")
                )
                if chain != row["prefix_chain_sha256"] or expected_end != (
                    int(row["prefix_end_trade_time_ms"]),
                    str(row["prefix_end_event_id"]),
                ):
                    raise ValueError("checkpoint prefix binding mismatch")
                if decoded is None:
                    decoded = candidate
            except (TypeError, ValueError) as exc:
                integrity_failures.append(f"{row.get('checkpoint_id')}:{exc}")
        checkpoint_summary = {
            "count": len(checkpoint_rows),
            "trusted_count": len(trusted),
            "state_bytes": state_bytes,
            "max_state_bytes": max_state,
            "integrity": "pass" if trusted and not integrity_failures else "fail",
            "integrity_failures": integrity_failures[:10],
            "k_within_bound": len(checkpoint_rows) <= 3,
            "space_within_bound": state_bytes <= int(max_state * 3 * 1.1) if max_state else False,
        }
        if not trusted:
            reasons.append("trusted_checkpoint_missing")
        if integrity_failures:
            reasons.append("checkpoint_integrity_failure")
        if len(checkpoint_rows) > 3:
            reasons.append("checkpoint_k_exceeded")
        if max_state > MAX_CHECKPOINT_STATE_BYTES:
            reasons.append("checkpoint_state_size_exceeded")
        if max_state and state_bytes > int(max_state * 3 * 1.1):
            reasons.append("checkpoint_steady_state_space_exceeded")

        repo = _repository(path)
        heads = {
            str(row["account"]): dict(row)
            for row in conn.execute(
                "SELECT * FROM position_projection_heads ORDER BY account"
            )
        }
        projected_accounts = {
            str((item.get("fields") or {}).get("account") or "")
            for item in current_lots
            if str((item.get("fields") or {}).get("account") or "")
        }
        for account in sorted(set(heads) | projected_accounts):
            head = heads.get(account)
            snapshot = repo.position_projection_account_snapshot(account, conn=conn)
            checks = {
                "present": head is not None,
                "trusted": bool(head and head.get("status") == "trusted"),
                "source_generation": bool(
                    head
                    and _int_or_missing(head.get("built_source_generation"))
                    == int(source.get("source_generation") or 0)
                ),
                "lots_generation": bool(
                    head
                    and _int_or_missing(head.get("built_lots_generation"))
                    == int(head.get("lots_generation") or 0)
                ),
                "fingerprint": bool(
                    head and str(head.get("projection_fingerprint") or "") == snapshot.fingerprint
                ),
                "lot_count": bool(head and int(head.get("lot_count") or 0) == snapshot.lot_count),
                "implementation": bool(
                    head
                    and head.get("projector_implementation_fingerprint") == implementation
                ),
            }
            head_results.append({"account": account, "checks": checks})
            if not all(checks.values()):
                reasons.append(f"projection_head_mismatch:{account}")

        source_checks = {
            "projector_schema": source.get("projector_schema") == POSITION_PROJECTION_SCHEMA,
            "implementation": source.get("projector_implementation_fingerprint") == implementation,
            "schema_cookie": _int_or_missing(source.get("sqlite_schema_cookie"))
            == _schema_cookie(conn),
            "last_full_verified_generation": _int_or_missing(
                source.get("last_full_verified_source_generation")
            )
            == int(source.get("source_generation") or 0),
        }
        if not all(source_checks.values()):
            reasons.append("source_state_mismatch")

        if shadow:
            if decoded is None:
                shadow_result = {"status": "fail", "reason": "trusted_checkpoint_missing"}
            else:
                prefix_count = int(decoded.row["prefix_event_count"])
                resumed = project_stored_trade_events_to_resumable_position_lots(
                    _events(event_rows[prefix_count:]),
                    domain_state=decoded.domain_state,
                    publication_state=decoded.publication_state,
                    entry_mode="tail",
                )
                full_state = projection.resumable_publication_state
                full_active = (
                    dict(full_state.fields_by_lot_id) if full_state is not None else {}
                )
                resumed_active = (
                    dict(resumed.publication_state.fields_by_lot_id)
                    if resumed.publication_state is not None
                    else {}
                )
                mismatched = sorted(
                    lot_id
                    for lot_id in set(full_active) | set(resumed_active)
                    if full_active.get(lot_id) != resumed_active.get(lot_id)
                )
                shadow_result = {
                    "status": "pass" if resumed.eligible and not mismatched else "fail",
                    "eligible": bool(resumed.eligible),
                    "diagnostic_count": len(resumed.diagnostics),
                    "full_active_lot_count": len(full_active),
                    "resumed_active_lot_count": len(resumed_active),
                    "mismatch_count": len(mismatched),
                    "mismatch_record_ids": mismatched[:10],
                }
                if shadow_result["status"] != "pass":
                    reasons.append("runtime_shadow_mismatch")
    else:
        if source is None:
            reasons.append("source_state_missing")

    reasons = sorted(set(reasons))
    binding = {
        "store_identity": inventory["store_identity"],
        "projector_schema": POSITION_PROJECTION_SCHEMA,
        "loaded_projector_implementation_fingerprint": implementation,
        "source_commit": source_commit,
        "sqlite_schema_cookie": inventory["sqlite_schema_cookie"],
        "source_generation": (
            int(source.get("source_generation") or 0) if source is not None else None
        ),
        "trade_events_fingerprint": inventory["fingerprints"]["trade_events"],
        "position_lots_fingerprint": inventory["fingerprints"]["position_lots"],
    }
    return {
        "schema_version": VERIFY_SCHEMA,
        "generated_at_utc": _now_iso(),
        "operation": "verify",
        "mode": "shadow" if shadow else "full",
        "read_only": True,
        "status": "pass" if not reasons else "fail",
        "readiness": "ready" if not reasons else "not_ready",
        "reasons": reasons,
        "store_binding": binding,
        "inventory": inventory,
        "full_oracle_parity": parity,
        "runtime_shadow": shadow_result,
        "checkpoint": checkpoint_summary,
        "heads": head_results,
        "resource_failures": [
            reason
            for reason in reasons
            if reason.startswith("checkpoint_")
        ],
        "parity_failures": [
            reason for reason in reasons if "parity" in reason or "shadow" in reason
        ],
    }


def verify_position_projection_migration(
    sqlite_path: str | Path,
    *,
    shadow: bool = False,
) -> dict[str, Any]:
    path = _store_path(sqlite_path)
    implementation, _timing = _loaded_implementation()
    source_commit = _source_commit()
    before = _file_sizes(path)
    wall_start = time.perf_counter_ns()
    cpu_start = time.process_time_ns()
    with _read_only_connection(path) as conn:
        result = _verify_from_conn(
            path,
            conn,
            shadow=shadow,
            implementation=implementation,
            source_commit=source_commit,
        )
    after = _file_sizes(path)
    _assert_read_only_persistent_sizes(before, after, operation="verification")
    result["timing"] = {
        "wall_ns": time.perf_counter_ns() - wall_start,
        "cpu_ns": time.process_time_ns() - cpu_start,
    }
    return _manifest(result)


def activate_position_projection_checkpoints(
    sqlite_path: str | Path,
    *,
    acceptance_manifest: Mapping[str, Any],
    shadow_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    acceptance = _validate_manifest(acceptance_manifest, schema=ACCEPTANCE_SCHEMA)
    shadow = _validate_manifest(shadow_manifest, schema=VERIFY_SCHEMA)
    if acceptance.get("status") != "pass" or acceptance.get("readiness") != "ready":
        raise ValueError("Phase 3A acceptance manifest is not pass/ready")
    if (
        shadow.get("status") != "pass"
        or shadow.get("readiness") != "ready"
        or shadow.get("mode") != "shadow"
    ):
        raise ValueError("runtime shadow manifest is not a passing shadow verification")
    if acceptance.get("resource_failures") or acceptance.get("parity_failures"):
        raise ValueError("Phase 3A acceptance manifest has unresolved failures")
    components = acceptance.get("components")
    component_requirements = {
        "lot_diff_publication": "pass",
        "checkpoint_tail": "pass",
        "combined": "ready",
    }
    if not isinstance(components, Mapping) or any(
        not isinstance(components.get(name), Mapping)
        or components[name].get("status") != expected
        for name, expected in component_requirements.items()
    ):
        raise ValueError("Phase 3A acceptance component gates are not pass/ready")
    reference_host = acceptance.get("reference_host")
    current_host = (
        str(reference_host.get("current_fingerprint") or "")
        if isinstance(reference_host, Mapping)
        else ""
    )
    expected_host = (
        str(reference_host.get("expected_fingerprint") or "")
        if isinstance(reference_host, Mapping)
        else ""
    )
    if (
        not isinstance(reference_host, Mapping)
        or reference_host.get("comparable") is not True
        or len(current_host) != 64
        or current_host != expected_host
    ):
        raise ValueError("Phase 3A acceptance reference host is not comparable")
    if acceptance.get("retained_lots_10x_guarantee") is not False:
        raise ValueError("Phase 3A retained-lots diagnostic boundary is missing")
    if shadow.get("resource_failures") or shadow.get("parity_failures"):
        raise ValueError("runtime shadow manifest has unresolved failures")
    shadow_parity = shadow.get("full_oracle_parity")
    runtime_shadow = shadow.get("runtime_shadow")
    if (
        not isinstance(shadow_parity, Mapping)
        or shadow_parity.get("status") != "pass"
        or not isinstance(runtime_shadow, Mapping)
        or runtime_shadow.get("status") != "pass"
    ):
        raise ValueError("runtime shadow manifest lacks passing parity evidence")
    acceptance_binding = acceptance.get("store_binding")
    shadow_binding = shadow.get("store_binding")
    if (
        not isinstance(acceptance_binding, Mapping)
        or not isinstance(shadow_binding, Mapping)
        or dict(acceptance_binding) != dict(shadow_binding)
    ):
        raise ValueError("acceptance and shadow store bindings differ")

    path = _store_path(sqlite_path)
    implementation, _timing = _loaded_implementation()
    source_commit = str(acceptance_binding.get("source_commit") or "")
    if not source_commit:
        raise ValueError("acceptance source commit is missing")
    current_source_commit = _source_commit()
    if current_source_commit != source_commit:
        raise ValueError("loaded source commit differs from acceptance evidence")
    conn = _write_connection(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = _verify_from_conn(
            path,
            conn,
            shadow=True,
            implementation=implementation,
            source_commit=current_source_commit,
        )
        if current["status"] != "pass":
            raise ValueError("current store verification is not pass")
        if current["store_binding"] != dict(acceptance_binding):
            raise ValueError("activation evidence is stale or belongs to another store")
        cursor = conn.execute(
            """
            UPDATE position_projection_source_state
            SET checkpoint_mode='enabled', updated_at_ms=?
            WHERE singleton_id=1 AND checkpoint_mode='disabled'
            """,
            (int(time.time() * 1000),),
        )
        if int(cursor.rowcount) != 1:
            raise ValueError("activation requires checkpoint mode disabled")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        secure_sqlite_artifacts(path)
    return _manifest(
        {
            "schema_version": "position_projection_activation.v1",
            "generated_at_utc": _now_iso(),
            "operation": "activate",
            "write_applied": True,
            "checkpoint_mode": "enabled",
            "store_binding": dict(acceptance_binding),
            "acceptance_manifest_hash": acceptance["manifest_hash"],
            "shadow_manifest_hash": shadow["manifest_hash"],
        }
    )


def deactivate_position_projection_checkpoints(
    sqlite_path: str | Path,
) -> dict[str, Any]:
    path = _store_path(sqlite_path)
    conn = _write_connection(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        source = _source_state(conn)
        if source is None:
            raise ValueError("position projection source state is missing")
        previous = str(source.get("checkpoint_mode") or "")
        cursor = conn.execute(
            """
            UPDATE position_projection_source_state
            SET checkpoint_mode='disabled', updated_at_ms=?
            WHERE singleton_id=1 AND checkpoint_mode='enabled'
            """,
            (int(time.time() * 1000),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        secure_sqlite_artifacts(path)
    return _manifest(
        {
            "schema_version": "position_projection_deactivation.v1",
            "generated_at_utc": _now_iso(),
            "operation": "deactivate",
            "write_applied": int(cursor.rowcount) == 1,
            "previous_checkpoint_mode": previous,
            "checkpoint_mode": "disabled" if previous in {"enabled", "disabled"} else previous,
            "preserved": ["trade_events", "position_lots", "heads", "checkpoints"],
        }
    )


def position_projection_migration_status(sqlite_path: str | Path) -> dict[str, Any]:
    path = _store_path(sqlite_path)
    implementation, timing = _loaded_implementation()
    with _read_only_connection(path) as conn:
        source = _source_state(conn)
        checkpoints = (
            [dict(row) for row in conn.execute("SELECT * FROM position_projection_checkpoints")]
            if _table_exists(conn, "position_projection_checkpoints")
            else []
        )
        heads = (
            [dict(row) for row in conn.execute("SELECT * FROM position_projection_heads")]
            if _table_exists(conn, "position_projection_heads")
            else []
        )
        reasons: list[str] = []
        lot_columns = set(_column_names(conn, "position_lots"))
        if not lot_columns:
            reasons.append("position_lots_table_missing")
        elif "account" not in lot_columns:
            reasons.append("position_lots_account_column_missing")
        if source is None:
            reasons.append("source_state_missing")
        else:
            if source.get("projector_schema") != POSITION_PROJECTION_SCHEMA:
                reasons.append("source_projector_schema_mismatch")
            if source.get("projector_implementation_fingerprint") != implementation:
                reasons.append("projector_implementation_mismatch")
            if _int_or_missing(source.get("sqlite_schema_cookie")) != _schema_cookie(conn):
                reasons.append("sqlite_schema_cookie_mismatch")
            if _int_or_missing(source.get("last_full_verified_source_generation")) != int(
                source.get("source_generation") or 0
            ):
                reasons.append("last_full_verification_stale")
        if len(checkpoints) > 3:
            reasons.append("checkpoint_k_exceeded")
        trusted_checkpoints = [
            row for row in checkpoints if row.get("trust_status") == "trusted"
        ]
        if not trusted_checkpoints:
            reasons.append("trusted_checkpoint_missing")
        if len(trusted_checkpoints) != len(checkpoints):
            reasons.append("untrusted_checkpoint")
        max_checkpoint_state_bytes = max(
            (int(row.get("state_bytes") or 0) for row in checkpoints),
            default=0,
        )
        checkpoint_state_bytes = sum(
            int(row.get("state_bytes") or 0) for row in checkpoints
        )
        if max_checkpoint_state_bytes > MAX_CHECKPOINT_STATE_BYTES:
            reasons.append("checkpoint_state_size_exceeded")
        if max_checkpoint_state_bytes and checkpoint_state_bytes > int(
            max_checkpoint_state_bytes * 3 * 1.1
        ):
            reasons.append("checkpoint_steady_state_space_exceeded")
        projected_accounts = (
            {
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT account FROM position_lots "
                    "WHERE account IS NOT NULL AND account<>''"
                )
            }
            if "account" in lot_columns
            else set()
        )
        head_accounts = {str(row.get("account") or "") for row in heads}
        if projected_accounts != head_accounts:
            reasons.append("projection_head_account_set_mismatch")
        for head in heads:
            account = str(head.get("account") or "")
            if head.get("status") != "trusted":
                reasons.append(f"untrusted_projection_head:{account}")
            if source is not None and _int_or_missing(head.get("built_source_generation")) != int(
                source.get("source_generation") or 0
            ):
                reasons.append(f"source_generation_mismatch:{account}")
            if _int_or_missing(head.get("built_lots_generation")) != int(
                head.get("lots_generation") or 0
            ):
                reasons.append(f"lots_generation_mismatch:{account}")
            if head.get("projector_schema") != POSITION_PROJECTION_SCHEMA:
                reasons.append(f"head_projector_schema_mismatch:{account}")
            if head.get("projector_implementation_fingerprint") != implementation:
                reasons.append(f"head_implementation_mismatch:{account}")
        fingerprint_scope = (
            conn.execute(
                "SELECT count(*),coalesce(sum(length(fields_json)),0) FROM position_lots"
            ).fetchone()
            if "fields_json" in lot_columns
            else (0, 0)
        )
        return _manifest(
            {
                "schema_version": "position_projection_migration_status.v1",
                "generated_at_utc": _now_iso(),
                "operation": "status",
                "read_only": True,
                "store_identity": _store_identity(path),
                "checkpoint_mode": source.get("checkpoint_mode") if source else None,
                "source_generation": source.get("source_generation") if source else None,
                "loaded_projector_implementation_fingerprint": implementation,
                "loaded_projector_fingerprint_timing": timing,
                "head_count": len(heads),
                "trusted_head_count": sum(row.get("status") == "trusted" for row in heads),
                "checkpoint_count": len(checkpoints),
                "trusted_checkpoint_count": sum(
                    row.get("trust_status") == "trusted" for row in checkpoints
                ),
                "checkpoint_state_bytes": checkpoint_state_bytes,
                "checkpoint_max_state_bytes": max_checkpoint_state_bytes,
                "checkpoint_k_within_bound": len(checkpoints) <= 3,
                "checkpoint_space_within_bound": bool(
                    max_checkpoint_state_bytes
                    and checkpoint_state_bytes
                    <= int(max_checkpoint_state_bytes * 3 * 1.1)
                ),
                "last_full_verified_source_generation": (
                    source.get("last_full_verified_source_generation") if source else None
                ),
                "fingerprint_scope": {
                    "rows": int(fingerprint_scope[0] or 0),
                    "fields_json_bytes": int(fingerprint_scope[1] or 0),
                },
                "runtime_telemetry": position_projection_runtime_telemetry(),
                "readiness": "ready" if not reasons else "not_ready",
                "reasons": sorted(set(reasons)),
            }
        )


def _file_sizes(path: Path) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for name, suffix in (("db", ""), ("wal", "-wal"), ("shm", "-shm")):
        candidate = Path(f"{path}{suffix}")
        try:
            sizes[f"{name}_bytes"] = int(candidate.stat().st_size)
        except FileNotFoundError:
            sizes[f"{name}_bytes"] = 0
    sizes["total_bytes"] = sum(sizes.values())
    return sizes


def _assert_read_only_persistent_sizes(
    before: Mapping[str, int],
    after: Mapping[str, int],
    *,
    operation: str,
) -> None:
    # SQLite may resize its ephemeral WAL shared-memory coordination file on a
    # read-only connection. The database and WAL payload are the durable state.
    if any(before.get(key) != after.get(key) for key in ("db_bytes", "wal_bytes")):
        raise RuntimeError(f"read-only migration {operation} changed persistent SQLite sizes")


def _fail(hook: Callable[[str], None] | None, stage: str) -> None:
    if hook is not None:
        hook(stage)


__all__ = [
    "ACCEPTANCE_SCHEMA",
    "INVENTORY_SCHEMA",
    "VERIFY_SCHEMA",
    "activate_position_projection_checkpoints",
    "apply_position_projection_migration",
    "build_position_projection_migration_inventory",
    "deactivate_position_projection_checkpoints",
    "position_projection_migration_status",
    "verify_position_projection_migration",
]
