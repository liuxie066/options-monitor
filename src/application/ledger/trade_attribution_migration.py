"""Explicit, backed-up Wheel event/schema transition; ordinary opens cannot migrate."""
from __future__ import annotations

import hashlib
import json
import time
import os
from pathlib import Path
from typing import Any, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.symbol_identity import symbol_market

from src.infrastructure.private_storage import connect_private_sqlite, secure_sqlite_artifacts
from .position_projection_migration import _read_only_connection, _store_identity, _write_connection
from .repository_core import _create_wheel_events_v2_table, _create_wheel_events_v2_guards, _wheel_events_schema_is_v2
from .repository_schema import initialize_ledger_connection
from .position_projection_migration import _repository
from .position_projection_runtime import run_position_projection_in_transaction
from .projector_implementation import loaded_projector_implementation_fingerprint
from .repository_schema import ensure_trade_attribution_policy_schema, ensure_trade_attribution_writer_fence
from .trade_attribution import ATTRIBUTION_POLICY_VERSION


_SCOPE_FIELDS = ("broker", "physical_account_id", "environment", "account", "market")


def _cutover_inventory(conn: Any, scope: Mapping[str, Any], effective_from_ms: int) -> dict[str, Any]:
    scope = {key: scope.get(key) for key in _SCOPE_FIELDS}
    if (scope["broker"] != "futu" or not isinstance(scope["physical_account_id"], str)
            or not scope["physical_account_id"] or scope["environment"] not in {"REAL", "SIMULATE"}
            or not isinstance(scope["account"], str) or scope["account"] != scope["account"].lower()
            or not scope["account"] or scope["market"] not in {"us", "hk"}
            or type(effective_from_ms) is not int or effective_from_ms <= 0):
        raise ValueError("invalid attribution cutover scope or time")
    enabled = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trade_attribution_policy_enablings'").fetchone()
    policies = (conn.execute("SELECT policy_version, effective_from_ms FROM trade_attribution_policy_enablings WHERE "
        + " AND ".join(f"{key} = ?" for key in _SCOPE_FIELDS), tuple(scope.values())).fetchall() if enabled else [])
    t0 = next((int(row["effective_from_ms"]) for row in policies if row["policy_version"] == "trade_attribution.v1"), None)
    if t0 is not None and effective_from_ms < t0:
        raise ValueError("attribution v2 cutover cannot precede v1 activation")
    already_v2 = next((int(row["effective_from_ms"]) for row in policies if row["policy_version"] == ATTRIBUTION_POLICY_VERSION), None)
    counts = {"before_t2": 0, "at_or_after_t2": 0, "v1_window": 0}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trade_events'").fetchone():
        for (encoded,) in conn.execute("SELECT event_json FROM trade_events"):
            event = json.loads(encoded)
            if event.get("event_type") != "open":
                continue
            contract = event.get("contract_key") or {}
            ref = (((event.get("raw_payload") or {}).get("execution_input") or {}).get("broker_account_ref") or {})
            if (contract.get("account") != scope["account"]
                    or str(symbol_market(contract.get("underlying_symbol") or "") or "").lower() != scope["market"]
                    or any(ref.get(source) != scope[target] for source, target in (
                        ("broker_id", "broker"), ("external_account_id", "physical_account_id"),
                        ("environment", "environment")))):
                continue
            event_time_ms = int(event.get("event_time_ms") or 0)
            counts["at_or_after_t2" if event_time_ms >= effective_from_ms else "before_t2"] += 1
            if t0 is not None and t0 <= event_time_ms < effective_from_ms:
                counts["v1_window"] += 1
    return {"cutover_scope": scope, "t0_effective_from_ms": t0,
            "t2_effective_from_ms": effective_from_ms, "v2_existing_effective_from_ms": already_v2,
            "source_open_counts": counts}


def _inventory(path: Path, conn: Any, *, scope: Mapping[str, Any] | None = None,
               effective_from_ms: int | None = None) -> dict[str, Any]:
    # ponytail: one streamed store digest; add incremental inventory only if maintenance time warrants it.
    digest = hashlib.sha256()
    for statement in conn.iterdump():
        digest.update(statement.encode("utf-8"))
        digest.update(b"\n")
    schema = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='wheel_events'").fetchone()
    result = {"schema_version": "trade_attribution_migration.v2", "store_identity": _store_identity(path),
            "content_hash": digest.hexdigest(), "projector_implementation": loaded_projector_implementation_fingerprint(), "wheel_schema_present": bool(schema),
            "wheel_schema_current": bool(schema and _wheel_events_schema_is_v2(conn)),
            "policy_schema_present": bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trade_attribution_policy_enablings'").fetchone())}
    if scope is not None:
        if effective_from_ms is None:
            raise ValueError("cutover preview requires effective_from_ms")
        result.update(_cutover_inventory(conn, scope, effective_from_ms))
    return result


def preview_trade_attribution_migration(sqlite_path: str | Path, *,
        scope: Mapping[str, Any] | None = None, effective_from_ms: int | None = None) -> dict[str, Any]:
    path = Path(sqlite_path).resolve(strict=True)
    with _read_only_connection(path) as conn:
        conn.execute("BEGIN")
        return _inventory(path, conn, scope=scope, effective_from_ms=effective_from_ms)


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
            scope = manifest.get("cutover_scope")
            t2 = manifest.get("t2_effective_from_ms")
            before = _inventory(path, conn, scope=scope, effective_from_ms=t2)
            if dict(manifest) != before or not before["wheel_schema_present"]:
                raise ValueError("migration manifest is stale, incomplete or belongs to another store")
            if scope is not None:
                if before["v2_existing_effective_from_ms"] is not None:
                    raise ValueError("attribution v2 cutover already exists for source")
                if t2 < int(time.time() * 1000):
                    raise ValueError("attribution cutover time passed; create a new preview")
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
            if scope is not None:
                now_ms = int(time.time() * 1000)
                if t2 < now_ms:
                    raise ValueError("attribution cutover time passed; create a new preview")
                request = {**scope, "policy_version": ATTRIBUTION_POLICY_VERSION,
                           "effective_from_ms": t2, "created_at_ms": now_ms,
                           "actor": "trade_attribution_migration",
                           "request_id": "migration:v2:" + canonical_sha256({"scope": scope, "t2": t2})[:24]}
                request["request_hash"] = canonical_sha256(request)
                columns = list(request)
                conn.execute("INSERT INTO trade_attribution_policy_enablings (" + ",".join(columns)
                    + ") VALUES (" + ",".join("?" for _ in columns) + ")", tuple(request.values()))
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
    after = preview_trade_attribution_migration(path, scope=scope, effective_from_ms=t2)
    if (not after["wheel_schema_current"] or not after["policy_schema_present"]
            or scope is not None and after["v2_existing_effective_from_ms"] != t2):
        raise RuntimeError("committed migration requires readback recovery; do not run an old writer")
    return {"status": "applied", "before": before, "after": after, "backup_path": str(backup),
            "rules_enabled": scope is not None, "old_binary_rollback_allowed": False}
