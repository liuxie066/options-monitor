"""Offline, explicit one-time migration. Never imported as an old-name alias."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Sequence

from src.infrastructure.private_storage import (
    atomic_write_private_text, connect_private_sqlite, exclusive_private_file_lock, private_path,
)

LEGACY_TABLES = (
    "copilot_sessions", "copilot_session_runs", "copilot_runs",
    "copilot_reply_outbox", "copilot_lane_leases",
)
MARKER = "bot_schema_migrations"
VERSION = "bot-name-v1"


@contextmanager
def _read_db(path: Path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Bot database must be an existing regular file")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        yield conn
    finally:
        conn.close()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _manifest_path(path: Path) -> Path:
    return path.parent / "bot-migration.json"


def _manifest(path: Path) -> dict[str, Any] | None:
    target = _manifest_path(path)
    if not target.exists():
        return None
    if target.is_symlink():
        raise ValueError("migration manifest must not be a symlink")
    value = json.loads(target.read_text())
    if not isinstance(value, dict) or value.get("version") != VERSION or value.get("database") != str(path):
        raise ValueError("Bot migration manifest identity mismatch")
    if value.get("backup") != str(path.with_name(path.name + ".pre-bot.sqlite3")):
        raise ValueError("Bot migration backup path mismatch")
    return value


def assert_bot_ready(path: str | Path) -> None:
    """Run before schema ensure; missing fresh installs are handled by the caller."""
    target = private_path(path)
    manifest = _manifest(target)
    if manifest and manifest.get("status") != "complete":
        raise ValueError("Bot migration incomplete; run ./om bot migrate --dry-run")
    if not target.exists():
        if manifest:
            raise ValueError("Bot migration database is missing")
        return
    with _read_db(target) as conn:
        names = _tables(conn)
        if names.intersection(LEGACY_TABLES):
            raise ValueError("Legacy Copilot data requires ./om bot migrate --dry-run")
        if MARKER in names and not manifest:
            raise ValueError("Bot migration manifest missing; operator recovery required")
        if manifest:
            if MARKER not in names:
                raise ValueError("Bot migration completion marker missing")
            row = conn.execute(f"SELECT database_id FROM {MARKER} WHERE version=?", (VERSION,)).fetchone()
            if not row or row[0] != manifest.get("database_id"):
                raise ValueError("Bot migration database identity mismatch")


def _config(path: Path) -> tuple[dict[str, Any], str, bool]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("configuration must be an existing regular file")
    raw = path.read_bytes().decode("utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        import yaml
        value = yaml.safe_load(raw)
    else:
        value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("configuration must be an object")
    assistant = value.get("assistant", {})
    if not isinstance(assistant, dict):
        raise ValueError("assistant must be an object")
    if "copilot" in assistant and "bot" in assistant:
        raise ValueError("conflicting assistant.copilot and assistant.bot; no data was overwritten")
    changed = "copilot" in assistant
    if changed:
        assistant["bot"] = assistant.pop("copilot")
    if path.suffix.lower() in {".yaml", ".yml"}:
        import yaml
        encoded = yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
    else:
        encoded = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    return value, encoded if changed else raw, changed


def _idle(conn: sqlite3.Connection, names: set[str]) -> None:
    for prefix in ("copilot", "bot"):
        table = f"{prefix}_runs"
        if table in names and conn.execute(f"SELECT 1 FROM {table} WHERE status IN ('running','waiting_model','waiting_tool') LIMIT 1").fetchone():
            raise ValueError("active Bot run prevents migration; drain all writers first")
        table = f"{prefix}_reply_outbox"
        if table in names and conn.execute(f"SELECT 1 FROM {table} WHERE status='delivering' LIMIT 1").fetchone():
            raise ValueError("unresolved reply delivery prevents migration")
        for suffix in ("session_runs", "lane_leases"):
            table = f"{prefix}_{suffix}"
            if table in names and conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                raise ValueError("outstanding Bot lease prevents migration")


def _rename_envelope(value: Any) -> Any:
    # Only mutable protocol keys/identifiers; user text and historical events stay byte-for-byte.
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            name = {"copilot": "bot", "copilot_result": "bot_result", "copilot_request": "bot_request"}.get(key, key)
            if name in result:
                raise ValueError("conflicting pending reply keys")
            if key in {"tool", "tool_name", "schema_version", "schema", "decision"} and isinstance(item, str):
                if item == "copilot" or item.startswith(("copilot_", "copilot.")):
                    item = "bot" + item[len("copilot"): ]
            result[name] = _rename_envelope(item) if isinstance(item, (dict, list)) else item
        return result
    if isinstance(value, list):
        return [_rename_envelope(item) for item in value]
    return value



def _file_hash(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("migration resource must be an existing regular file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _database_hash(conn: sqlite3.Connection) -> str:
    # A logical snapshot includes committed WAL data without copying live sidecars.
    digest = hashlib.sha256()
    for statement in conn.iterdump():
        digest.update(statement.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _check_config_inventory(current: dict[str, Any], *, create_backups: bool = False) -> None:
    records = current.get("config_records")
    if not isinstance(records, list) or {row.get("path") for row in records} != set(current["configs"]):
        raise ValueError("migration config fingerprints missing; operator inspection required")
    for row in records:
        path = Path(row["path"])
        digest = _file_hash(path)
        if digest not in {row["original_sha256"], row["new_sha256"]}:
            raise ValueError("configuration changed outside migration")
        if digest == row["original_sha256"]:
            _value, encoded, _changed = _config(path)
            if hashlib.sha256(encoded.encode()).hexdigest() != row["new_sha256"]:
                raise ValueError("configuration migration plan changed")
        if not row["changed"]:
            continue
        backup = Path(row["backup"])
        if backup != path.with_name(path.name + ".pre-bot"):
            raise ValueError("configuration backup path mismatch")
        if backup.exists() or backup.is_symlink():
            if _file_hash(backup) != row["backup_sha256"]:
                raise ValueError("configuration backup identity changed")
        elif digest == row["new_sha256"]:
            raise ValueError("migrated configuration backup is missing")
        elif create_backups:
            raw = path.read_bytes().decode("utf-8")
            if hashlib.sha256(raw.encode()).hexdigest() != row["original_sha256"]:
                raise ValueError("configuration changed during backup")
            atomic_write_private_text(backup, raw)
            if _file_hash(backup) != row["backup_sha256"]:
                raise ValueError("configuration backup verification failed")

def migrate_bot(*, host_db: str | Path, config_paths: Sequence[str | Path] = (), pi_paths: Sequence[str | Path] = (), apply: bool = False, writers_stopped: bool = False) -> dict[str, Any]:
    path = private_path(host_db)
    configs = sorted({private_path(p) for p in config_paths}, key=str)
    pi_dbs = sorted({private_path(p) for p in pi_paths}, key=str)
    if path in configs or path in pi_dbs or set(configs).intersection(pi_dbs):
        raise ValueError("migration resources must have distinct identities")
    current = _manifest(path)
    prepared = [(p, *_config(p)) for p in configs]
    for p in pi_dbs:
        with _read_db(p) as conn:
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Pi database integrity check failed")
    with _read_db(path) as conn:
        conn.execute("BEGIN")
        names = _tables(conn)
        legacy = sorted(names.intersection(LEGACY_TABLES))
        for table in legacy:
            if table.replace("copilot", "bot", 1) in names:
                raise ValueError("old and new Bot tables coexist; refusing overwrite")
        _idle(conn, names)
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Host database integrity check failed")
        source_sha256 = _database_hash(conn)
        counts = {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in legacy}
    report = {"ok": True, "dry_run": not apply, "tables": counts, "config_count": len(configs), "pi_database_count": len(pi_dbs), "config_changes": sum(item[3] for item in prepared), "write_applied": False}
    if current and current.get("status") != "complete":
        _check_config_inventory(current)
        if current.get("pi_databases") != [str(p) for p in pi_dbs]:
            raise ValueError("migration Pi database inventory changed")
        for p in pi_dbs:
            with _read_db(p) as conn:
                if _database_hash(conn) != current.get("pi_sha256", {}).get(str(p)):
                    raise ValueError("Pi database changed during migration")
        expected = {current.get("source_sha256"), current.get("database_after_sha256")}
        if source_sha256 not in expected:
            raise ValueError("Host database changed outside migration")
    if not apply:
        return report
    if not writers_stopped:
        raise ValueError("--apply requires --writers-stopped after all shared DB/config writers exit")
    with exclusive_private_file_lock(path.parent / ".bot-migration.lock", blocking=False):
        current = _manifest(path)
        identities = [str(p) for p in configs]
        if current and current.get("configs") != identities:
            raise ValueError("migration configuration inventory changed")
        if current and current.get("status") == "complete":
            assert_bot_ready(path)
            if legacy or any(item[3] for item in prepared):
                raise ValueError("completed migration contains new legacy resources")
            return {**report, "already_complete": True}
        if not legacy and not current and not any(item[3] for item in prepared):
            return {**report, "already_current": True}
        if not current:
            from uuid import uuid4
            backup = path.with_name(path.name + ".pre-bot.sqlite3")
            if backup.exists():
                raise ValueError("unowned migration backup already exists")
            config_records = []
            for p, _value, encoded, changed in prepared:
                backup_config = p.with_name(p.name + ".pre-bot")
                if changed and (backup_config.exists() or backup_config.is_symlink()):
                    raise ValueError("unowned configuration backup already exists")
                original = _file_hash(p)
                config_records.append({"path": str(p), "changed": changed, "original_sha256": original,
                    "new_sha256": hashlib.sha256(encoded.encode()).hexdigest(), "backup": str(backup_config),
                    "backup_sha256": original if changed else None})
            pi_sha256 = {}
            for p in pi_dbs:
                with _read_db(p) as conn:
                    pi_sha256[str(p)] = _database_hash(conn)
            current = {"source_sha256": source_sha256, "config_records": config_records,
                       "pi_databases": [str(p) for p in pi_dbs], "pi_sha256": pi_sha256, "version": VERSION, "database": str(path), "database_id": uuid4().hex, "configs": identities, "status": "pending", "database_done": False, "config_done": [], "backup": str(backup), "counts": counts}
            atomic_write_private_text(_manifest_path(path), json.dumps(current, ensure_ascii=False, indent=2))
        _check_config_inventory(current, create_backups=True)
        backup = Path(current["backup"])
        if not current.get("backup_sha256"):
            # All writers are stopped by the explicit maintenance precondition.
            if backup.exists():
                with _read_db(backup) as saved:
                    if saved.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise ValueError("incomplete backup requires operator inspection")
            else:
                with _read_db(path) as source, closing(connect_private_sqlite(backup)) as dest:
                    source.backup(dest)
                    dest.execute("PRAGMA journal_mode=DELETE")
            with _read_db(backup) as saved:
                if _database_hash(saved) != current["source_sha256"]:
                    raise ValueError("migration backup content differs from the frozen source")
                saved_names = _tables(saved)
                for table, expected in current["counts"].items():
                    if table not in saved_names or saved.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] != expected:
                        raise ValueError("migration backup does not preserve the source inventory")
            current["backup_sha256"] = _file_hash(backup)
            atomic_write_private_text(_manifest_path(path), json.dumps(current, ensure_ascii=False, indent=2))
        if _file_hash(backup) != current["backup_sha256"]:
            raise ValueError("migration backup identity changed")
        with closing(connect_private_sqlite(path, timeout=1)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            names = _tables(conn)
            if _database_hash(conn) not in {current["source_sha256"], current.get("database_after_sha256")}:
                raise ValueError("Host database changed before migration write")
            _idle(conn, names)
            conn.execute(f"CREATE TABLE IF NOT EXISTS {MARKER} (version TEXT PRIMARY KEY, database_id TEXT NOT NULL)")
            marker = conn.execute(f"SELECT database_id FROM {MARKER} WHERE version=?", (VERSION,)).fetchone()
            if marker and marker[0] != current["database_id"]:
                raise ValueError("migration marker identity conflict")
            if not marker:
                for old in LEGACY_TABLES:
                    if old not in names:
                        continue
                    new = old.replace("copilot", "bot", 1)
                    if new in names:
                        raise ValueError("new table conflicts with legacy data")
                    conn.execute(f'ALTER TABLE "{old}" RENAME TO "{new}"')
                for index_name, index_sql in list(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")):
                    if str(index_name).startswith("copilot_"):
                        escaped = str(index_name).replace('"', '""')
                        conn.execute(f'DROP INDEX "{escaped}"')
                        conn.execute(index_sql.replace("copilot_", "bot_"))
                if "copilot_reply_outbox" in names:
                    for key, raw in conn.execute("SELECT delivery_key,payload_json FROM bot_reply_outbox WHERE status NOT IN ('delivered','terminal_failed','expired')"):
                        payload = _rename_envelope(json.loads(raw))
                        conn.execute("UPDATE bot_reply_outbox SET payload_json=? WHERE delivery_key=?", (json.dumps(payload, ensure_ascii=False), key))
                conn.execute(f"INSERT INTO {MARKER} VALUES (?,?)", (VERSION, current["database_id"]))
            for old, count in current["counts"].items():
                new = old.replace("copilot", "bot", 1)
                if conn.execute(f'SELECT COUNT(*) FROM "{new}"').fetchone()[0] != count:
                    raise ValueError("migration row count changed")
            current["database_after_sha256"] = _database_hash(conn)
            atomic_write_private_text(_manifest_path(path), json.dumps(current, ensure_ascii=False, indent=2))
        current["database_done"] = True
        atomic_write_private_text(_manifest_path(path), json.dumps(current, ensure_ascii=False, indent=2))
        for record in current["config_records"]:
            _check_config_inventory(current)
            p = Path(record["path"])
            digest = _file_hash(p)
            if digest != record["new_sha256"]:
                _value, encoded, changed = _config(p)
                if not changed or hashlib.sha256(encoded.encode()).hexdigest() != record["new_sha256"]:
                    raise ValueError("configuration migration result changed")
                if _file_hash(p) != record["original_sha256"]:
                    raise ValueError("configuration changed before migration write")
                atomic_write_private_text(p, encoded)
            if _file_hash(p) != record["new_sha256"] or _config(p)[2]:
                raise ValueError("configuration migration verification failed")
            if str(p) not in current["config_done"]:
                current["config_done"].append(str(p))
                atomic_write_private_text(_manifest_path(path), json.dumps(current, ensure_ascii=False, indent=2))
        current["status"] = "complete"
        atomic_write_private_text(_manifest_path(path), json.dumps(current, ensure_ascii=False, indent=2))
        assert_bot_ready(path)
        return {**report, "write_applied": True}
