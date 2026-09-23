"""Explicit, backed-up Wheel event/schema transition; ordinary opens cannot migrate."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Mapping

from src.infrastructure.private_storage import connect_private_sqlite, secure_sqlite_artifacts
from .position_projection_migration import _read_only_connection, _store_identity, _write_connection
from .repository_core import _create_wheel_events_v2_table, _create_wheel_events_v2_guards, _wheel_events_schema_is_v2
from .repository_schema import initialize_ledger_connection
from .position_projection_migration import _repository
from .position_projection_runtime import run_position_projection_in_transaction
from .projector_implementation import loaded_projector_implementation_fingerprint
from .repository_schema import ensure_trade_attribution_policy_schema, ensure_trade_attribution_writer_fence


def _inventory(path: Path, conn: Any) -> dict[str, Any]:
    # ponytail: one streamed store digest; add incremental inventory only if maintenance time warrants it.
    digest = hashlib.sha256()
    for statement in conn.iterdump():
        digest.update(statement.encode("utf-8"))
        digest.update(b"\n")
    schema = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='wheel_events'").fetchone()
    return {"schema_version": "trade_attribution_migration.v1", "store_identity": _store_identity(path),
            "content_hash": digest.hexdigest(), "projector_implementation": loaded_projector_implementation_fingerprint(), "wheel_schema_present": bool(schema),
            "wheel_schema_current": bool(schema and _wheel_events_schema_is_v2(conn)),
            "policy_schema_present": bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trade_attribution_policy_enablings'").fetchone())}


def preview_trade_attribution_migration(sqlite_path: str | Path) -> dict[str, Any]:
    path = Path(sqlite_path).resolve(strict=True)
    with _read_only_connection(path) as conn:
        conn.execute("BEGIN")
        return _inventory(path, conn)


def apply_trade_attribution_migration(sqlite_path: str | Path, *, manifest: Mapping[str, Any],
                                     backup_path: str | Path, writers_stopped: bool) -> dict[str, Any]:
    if writers_stopped is not True:
        raise ValueError("stop and drain all ingress and persistent writers before migration")
    path = Path(sqlite_path).resolve(strict=True)
    backup = Path(backup_path).resolve()
    if backup == path or backup.exists() or not backup.parent.is_dir():
        raise ValueError("migration requires a new backup file in an existing private directory")
    with _write_connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            before = _inventory(path, conn)
            if dict(manifest) != before or not before["wheel_schema_present"]:
                raise ValueError("migration manifest is stale, incomplete or belongs to another store")
            # The writer lock and SQLite reservation remain held through backup and commit.
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            dest = initialize_ledger_connection(connect_private_sqlite(backup))
            try:
                with _read_only_connection(path) as source:
                    source.backup(dest)
                if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("migration backup integrity check failed")
                if _inventory(backup, dest)["content_hash"] != before["content_hash"]:
                    raise ValueError("migration backup readback differs from source")
            finally:
                dest.close()
                secure_sqlite_artifacts(backup)
            old_rows = [tuple(row) for row in conn.execute("SELECT rowid, * FROM wheel_events ORDER BY rowid")]
            if not before["wheel_schema_current"]:
                guards = conn.execute("SELECT name, sql FROM sqlite_master WHERE tbl_name='wheel_events' AND type IN ('index','trigger') AND sql IS NOT NULL").fetchall()
                columns = [row["name"] for row in conn.execute("PRAGMA table_info(wheel_events)")]
                _create_wheel_events_v2_table(conn, "wheel_events_attribution_upgrade")
                target_columns = [row["name"] for row in conn.execute("PRAGMA table_info(wheel_events_attribution_upgrade)")]
                if columns != target_columns:
                    raise ValueError("complete the existing lot-identity migration before attribution migration")
                names = ",".join('"' + str(name).replace('"', '""') + '"' for name in columns)
                conn.execute(f"INSERT INTO wheel_events_attribution_upgrade(rowid,{names}) SELECT rowid,{names} FROM wheel_events")
                conn.execute("DROP TABLE wheel_events")
                conn.execute("ALTER TABLE wheel_events_attribution_upgrade RENAME TO wheel_events")
                _create_wheel_events_v2_guards(conn)
                for guard in guards:
                    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (guard["name"],)).fetchone():
                        conn.execute(guard["sql"])
            ensure_trade_attribution_policy_schema(conn)
            ensure_trade_attribution_writer_fence(conn)
            if old_rows != [tuple(row) for row in conn.execute("SELECT rowid, * FROM wheel_events ORDER BY rowid")]:
                raise ValueError("migration changed historical Wheel event facts")
            repo = _repository(path)
            repo.invalidate_position_projection_checkpoints(reason="trade_attribution_migration", conn=conn)
            projection = run_position_projection_in_transaction(repo, (), conn=conn, mode="forced_full", seed_checkpoint=True)
            if not projection.publication.heads_trusted or not projection.checkpoint_written:
                raise ValueError("migration could not publish a trusted current projection")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("migration foreign key check failed")
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("migration integrity check failed")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    after = preview_trade_attribution_migration(path)
    if not after["wheel_schema_current"] or not after["policy_schema_present"]:
        raise RuntimeError("committed migration requires readback recovery; do not run an old writer")
    return {"status": "applied", "before": before, "after": after, "backup_path": str(backup),
            "rules_enabled": False, "old_binary_rollback_allowed": False}
