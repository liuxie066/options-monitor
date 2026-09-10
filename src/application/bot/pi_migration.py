"""Bounded offline conversion between OM's Pi 0.84.2 and 0.85.1 stores."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping

from src.infrastructure.pi_agent_process import pi_session_locks, run_pi_migration_bridge
from src.infrastructure.private_storage import atomic_write_private_text, ensure_private_directory, private_path


PI_MIGRATION_RECEIPT_VERSION = "om-pi-migration.v1"
_EXPORT_VERSION = "om-pi-export.v1"
_SUPPORTED = {"0.84.2": "legacy", "0.85.1": "target"}
_PHASES = {"prepared", "validated", "published"}
_PACKAGES = {
    "@earendil-works/pi-agent-core",
    "@earendil-works/pi-ai",
    "@earendil-works/pi-session-backend-sqlite-node",
}


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def pi_migration_receipt_path(pi_db: str | Path) -> Path:
    path = _canonical_database(pi_db)
    return path.with_name(path.name + ".om-pi-migration.json")


def _canonical_database(pi_db: str | Path) -> Path:
    path = private_path(pi_db)
    if path.is_symlink():
        raise ValueError("Pi database must not be a symlink")
    return path.parent.resolve() / path.name


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("migration artifact must be an existing regular file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError("migration artifact must be an existing regular file")
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_runtime_identity(value: Any) -> dict[str, Any]:
    if (not isinstance(value, dict) or set(value) != {"ok", "version", "lockSha256", "packages"}
            or value.get("ok") is not True or value.get("version") not in _SUPPORTED):
        raise ValueError("Pi runtime identity is invalid")
    if not _is_sha256(value.get("lockSha256")):
        raise ValueError("Pi runtime lock identity is invalid")
    packages = value.get("packages")
    if not isinstance(packages, dict) or set(packages) != _PACKAGES:
        raise ValueError("Pi runtime package identity is invalid")
    for package in packages.values():
        if (not isinstance(package, dict) or set(package) != {"version", "manifestSha256", "entrySha256"}
                or package.get("version") != value["version"]):
            raise ValueError("Pi runtime package version is invalid")
        if any(not _is_sha256(package.get(key)) for key in ("manifestSha256", "entrySha256")):
            raise ValueError("Pi runtime package fingerprint is invalid")
    return value


def _validate_receipt(value: Any, database: Path) -> dict[str, Any]:
    required = {"version", "database", "phase", "rollbackRequired", "source", "target", "backup"}
    optional = {"readback", "published", "prior", "conversion"}
    if (not isinstance(value, dict) or not required.issubset(value) or
            not set(value).issubset(required | optional) or
            value.get("version") != PI_MIGRATION_RECEIPT_VERSION or value.get("rollbackRequired") is not True):
        raise ValueError("Pi migration receipt version is invalid")
    if value.get("database") != str(database):
        raise ValueError("Pi migration receipt database identity mismatch")
    if value.get("phase") not in _PHASES:
        raise ValueError("Pi migration receipt phase is invalid")
    source = value.get("source")
    target = value.get("target")
    if not isinstance(source, dict) or not isinstance(target, dict):
        raise ValueError("Pi migration receipt direction is invalid")
    if set(source) != {"runtimePath", "runtime", "database"}:
        raise ValueError("Pi migration receipt source is invalid")
    target_keys = {"runtimePath", "runtime"} if value["phase"] == "prepared" else {"runtimePath", "runtime", "database"}
    if set(target) != target_keys:
        raise ValueError("Pi migration receipt target is invalid")
    source_runtime = _validate_runtime_identity(source.get("runtime"))
    target_runtime = _validate_runtime_identity(target.get("runtime"))
    if source_runtime["version"] == target_runtime["version"] or {
        source_runtime["version"], target_runtime["version"],
    } != set(_SUPPORTED):
        raise ValueError("Pi migration receipt direction is unsupported")
    for owner in (source, target):
        runtime_path = owner.get("runtimePath")
        if not isinstance(runtime_path, str) or not Path(runtime_path).is_absolute():
            raise ValueError("Pi migration receipt runtime path is invalid")
    backup = value.get("backup")
    if (not isinstance(backup, dict) or set(backup) != {"path", "sha256"} or
            not isinstance(backup.get("path"), str)):
        raise ValueError("Pi migration receipt backup identity is invalid")
    _validate_backup_path(Path(backup["path"]), database, source_runtime["version"], target_runtime["version"])
    for item in (source.get("database"), backup):
        if not isinstance(item, dict) or not _is_sha256(item.get("sha256")):
            raise ValueError("Pi migration receipt database fingerprint is invalid")
    source_db = source["database"]
    if set(source_db) != {"sha256", "format"} or source_db["format"] != _SUPPORTED[source_runtime["version"]]:
        raise ValueError("Pi migration receipt source format is invalid")
    if value["phase"] in {"validated", "published"}:
        target_db = target.get("database")
        readback = value.get("readback")
        if (not isinstance(target_db, dict) or set(target_db) != {"sha256", "format"}
                or not _is_sha256(target_db.get("sha256"))
                or target_db["format"] != _SUPPORTED[target_runtime["version"]]):
            raise ValueError("Pi migration target identity is missing")
        if (not isinstance(readback, dict) or set(readback) != {"equivalent", "contextSha256"}
                or readback.get("equivalent") is not True or not _is_sha256(readback.get("contextSha256"))):
            raise ValueError("Pi migration readback is missing")
    if value["phase"] == "published":
        published = value.get("published")
        if not isinstance(published, dict) or set(published) != {"databaseSha256"} or not _is_sha256(published.get("databaseSha256")):
            raise ValueError("Pi migration publication identity is missing")
    elif "published" in value:
        raise ValueError("Pi migration publication phase is inconsistent")
    conversion = value.get("conversion")
    if conversion is not None and (
        not isinstance(conversion, dict) or set(conversion) != {
            "sessionCount", "entryCount", "excludedTailCount", "sourceCanonicalSha256", "targetCanonicalSha256",
        } or any(not isinstance(conversion[key], int) or isinstance(conversion[key], bool) or conversion[key] < 0
                 for key in ("sessionCount", "entryCount", "excludedTailCount"))
        or not _is_sha256(conversion["sourceCanonicalSha256"])
        or not _is_sha256(conversion["targetCanonicalSha256"])
    ):
        raise ValueError("Pi migration conversion summary is invalid")
    prior = value.get("prior")
    if prior is not None and _validate_receipt(prior, database)["phase"] != "published":
        raise ValueError("prior Pi migration receipt is unresolved")
    return value


def read_pi_migration_receipt(pi_db: str | Path) -> dict[str, Any] | None:
    database = _canonical_database(pi_db)
    path = pi_migration_receipt_path(database)
    if not path.exists() and not path.is_symlink():
        return None
    return _validate_receipt(_load_json(path), database)


def retained_pi_runtime_paths(receipt: Mapping[str, Any]) -> tuple[Path, ...]:
    database = private_path(str(receipt.get("database", "/invalid")))
    checked = _validate_receipt(dict(receipt), database)
    paths = [] if checked.get("rollbackRequired", True) is not True else [Path(checked["source"]["runtimePath"])]
    prior = checked.get("prior")
    if isinstance(prior, dict):
        paths.extend(retained_pi_runtime_paths(prior))
    return tuple(dict.fromkeys(paths))


def _assert_recovery_dependencies(receipt: dict[str, Any]) -> None:
    try:
        source_path, source_identity = _runtime_identity(receipt["source"]["runtimePath"])
    except (OSError, ValueError) as exc:
        raise ValueError("retained Pi source runtime is unavailable") from exc
    if source_identity != receipt["source"]["runtime"]:
        raise ValueError("retained Pi source runtime identity changed")
    if source_path != Path(receipt["source"]["runtimePath"]):
        raise ValueError("retained Pi source runtime path changed")
    backup = Path(receipt["backup"]["path"])
    try:
        backup_sha256 = _sha256(backup)
    except ValueError as exc:
        raise ValueError("retained Pi recovery backup is unavailable") from exc
    if backup_sha256 != receipt["backup"]["sha256"]:
        raise ValueError("retained Pi recovery backup identity changed")
    if backup.stat().st_mode & 0o222 or any(path.exists() or path.is_symlink() for path in _sidecars(backup)):
        raise ValueError("retained Pi recovery backup is not sealed")
    with backup.open("rb") as stream:
        header = stream.read(20)
    if len(header) < 20 or header[18:20] != b"\x01\x01":
        raise ValueError("retained Pi recovery backup is not sealed")
    prior = receipt.get("prior")
    if isinstance(prior, dict):
        _assert_recovery_dependencies(prior)


def _bridge(
    command: str,
    runtime: Path,
    *,
    maintenance_descriptors: tuple[int, ...] = (),
    **paths: Path | str,
) -> dict[str, Any]:
    return run_pi_migration_bridge(
        command,
        runtime,
        {name.replace("_", "-"): value for name, value in paths.items()},
        maintenance_descriptors=maintenance_descriptors,
    )


def _runtime_identity(runtime: str | Path) -> tuple[Path, dict[str, Any]]:
    path = private_path(runtime)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("Pi runtime must be an existing directory")
    resolved = path.resolve()
    identity = _validate_runtime_identity(_bridge("identity", resolved))
    return resolved, identity


def _target_runtime(source: tuple[Path, dict[str, Any]], target: tuple[Path, dict[str, Any]]) -> Path:
    return source[0] if source[1]["version"] == "0.85.1" else target[0]


def _controller_runtime() -> Path:
    runtime = Path(__file__).resolve().parents[3] / "agent-runtime"
    resolved, identity = _runtime_identity(runtime)
    if identity["version"] != "0.85.1":
        raise ValueError("the current Pi migration controller is not exact 0.85.1")
    return resolved


def _probe_store(
    database: Path,
    runtime_0851: Path,
    *,
    immutable: bool = False,
    maintenance_descriptors: tuple[int, ...] = (),
) -> dict[str, Any]:
    arguments: dict[str, Path | str] = {"database": database}
    if immutable:
        arguments["immutable"] = "true"
    result = _bridge("probe", runtime_0851, maintenance_descriptors=maintenance_descriptors, **arguments)
    if result.get("format") not in {"legacy", "target", "unknown", "corrupt", "missing"}:
        raise ValueError("Pi store probe returned an invalid format")
    return result


def _backup_path(database: Path, source_version: str, target_version: str, generation: str) -> Path:
    return database.with_name(
        database.name + f".om-pi-recovery-{source_version}-to-{target_version}-{generation}.sqlite3"
    )


def _validate_backup_path(
    backup: Path,
    database: Path,
    source_version: str,
    target_version: str,
) -> Path:
    prefix = database.name + f".om-pi-recovery-{source_version}-to-{target_version}-"
    suffix = ".sqlite3"
    generation = (
        backup.name[len(prefix):-len(suffix)]
        if backup.name.startswith(prefix) and backup.name.endswith(suffix)
        else ""
    )
    if (backup.parent != database.parent or len(generation) != 32 or
            any(character not in "0123456789abcdef" for character in generation)):
        raise ValueError("Pi migration receipt backup identity is invalid")
    return backup


def _derived_artifacts(backup: Path) -> tuple[Path, Path, Path, Path]:
    return tuple(Path(str(backup) + suffix) for suffix in (
        ".export.sqlite3", ".staging.sqlite3", ".source.json", ".readback.json",
    ))  # type: ignore[return-value]


def _new_backup_path(database: Path, source_version: str, target_version: str) -> Path:
    while True:
        backup = _backup_path(database, source_version, target_version, secrets.token_hex(16))
        artifacts = (backup, *_derived_artifacts(backup))
        if not any(
            path.exists() or path.is_symlink() or any(
                sidecar.exists() or sidecar.is_symlink() for sidecar in _sidecars(path)
            )
            for path in artifacts
        ):
            return backup


def _sidecars(database: Path) -> tuple[Path, Path, Path]:
    return tuple(Path(str(database) + suffix) for suffix in ("-journal", "-wal", "-shm"))  # type: ignore[return-value]


def _artifact_inventory(database: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for label, path in (("database", database), ("journal", _sidecars(database)[0]),
                        ("wal", _sidecars(database)[1]), ("shm", _sidecars(database)[2])):
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError("Pi database artifact is unsafe")
        state = path.stat()
        result[label] = {"size": state.st_size, "sha256": _sha256(path)}
    return result


def _assert_previewable(database: Path) -> dict[str, dict[str, Any]]:
    before = _artifact_inventory(database)
    if not database.is_file() or database.is_symlink():
        raise ValueError("Pi database must be an existing regular file")
    if (("wal" in before and before["wal"]["size"] > 0) or
            ("journal" in before and before["journal"]["size"] > 0)):
        raise ValueError("Pi preview needs a quiescent WAL checkpoint before inspection")
    return before


def _logical_sha256(database: Path) -> str:
    digest = hashlib.sha256()
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Pi database integrity check failed")
        for statement in connection.iterdump():
            digest.update(statement.encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def _remove_work(database: Path) -> None:
    for path in (database, *_sidecars(database)):
        if path.is_symlink():
            raise ValueError("Pi migration work artifact is unsafe")
        if path.exists():
            path.unlink()


def _sqlite_backup(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise ValueError("Pi recovery backup already exists without a matching receipt")
    ensure_private_directory(destination.parent)
    with closing(sqlite3.connect(str(source), timeout=5)) as origin, closing(sqlite3.connect(str(destination), timeout=5)) as saved:
        origin.backup(saved)
        if str(saved.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower() != "delete":
            raise ValueError("Pi recovery backup could not be sealed in DELETE journal mode")
        if saved.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Pi recovery backup integrity check failed")
        saved.commit()
    with destination.open("rb") as stream:
        header = stream.read(20)
    if len(header) < 20 or header[18:20] != b"\x01\x01":
        raise ValueError("Pi recovery backup is not sealed in rollback journal mode")
    for sidecar in _sidecars(destination):
        if sidecar.exists():
            if sidecar.is_symlink() or (sidecar.name.endswith(("-journal", "-wal")) and sidecar.stat().st_size):
                raise ValueError("Pi recovery backup is not self-contained")
            sidecar.unlink()
    destination.chmod(0o400)
    _fsync_file(destination)


def _checkpoint_source(database: Path) -> None:
    with closing(sqlite3.connect(str(database), timeout=5)) as connection:
        row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is not None and row[0] != 0:
            raise ValueError("Pi source WAL checkpoint is busy")
        if str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower() != "delete":
            raise ValueError("Pi source could not enter quiescent rollback journal mode")
        connection.commit()
    for sidecar in _sidecars(database):
        if sidecar.exists():
            if sidecar.is_symlink() or (sidecar.name.endswith(("-journal", "-wal")) and sidecar.stat().st_size):
                raise ValueError("Pi source sidecar remains after quiescent checkpoint")
            sidecar.unlink()


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_receipt(database: Path, receipt: dict[str, Any]) -> None:
    _validate_receipt(receipt, database)
    atomic_write_private_text(
        pi_migration_receipt_path(database),
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    descriptor = os.open(database.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _active_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("format") != _EXPORT_VERSION or not isinstance(payload.get("sessions"), list):
        raise ValueError("canonical Pi export is invalid")
    sessions = []
    for session in payload["sessions"]:
        if not isinstance(session, dict):
            raise ValueError("canonical Pi session is invalid")
        sessions.append({key: session[key] for key in ("id", "createdAt", "entries", "contextSha256", "contentSha256")})
    return {"format": _EXPORT_VERSION, "sessions": sessions}


def _export(
    runtime: Path,
    version: str,
    database: Path,
    output: Path,
    *,
    maintenance_descriptors: tuple[int, ...] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    report = _bridge(
        "export", runtime, maintenance_descriptors=maintenance_descriptors,
        expected_version=version, database=database, output=output,
    )
    return report, _active_payload(_load_json(output))


def _same_payload(left: dict[str, Any], right: dict[str, Any]) -> bool:
    encoded = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return encoded(left) == encoded(right)


def _sanitized_report(*, mode: str, source: dict[str, Any], target: dict[str, Any],
                      store: dict[str, Any], receipt_phase: str | None = None,
                      write_applied: bool = False, already_applied: bool = False) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "ready" if mode == "preview" else "migrated",
        "mode": mode,
        "direction": f"{source['version']}->{target['version']}",
        "runtime": {
            "source": {"version": source["version"], "package_lock_sha256": source["lockSha256"]},
            "target": {"version": target["version"], "package_lock_sha256": target["lockSha256"]},
        },
        "database_format": store["format"],
        "session_ids": list(store.get("sessionIds", [])),
        "receipt_phase": receipt_phase,
        "write_applied": write_applied,
        "already_applied": already_applied,
    }


def assert_pi_storage_ready(
    pi_db: str | Path,
    runtime_dir: str | Path,
    *,
    maintenance_descriptors: tuple[int, ...] = (),
) -> dict[str, Any]:
    database = _canonical_database(pi_db)
    runtime, identity = _runtime_identity(runtime_dir)
    receipt = read_pi_migration_receipt(database)
    if receipt is not None and receipt["phase"] != "published":
        raise ValueError("Pi migration receipt is unresolved; run migrate-pi apply to reconcile")
    if not database.exists():
        if receipt is not None:
            raise ValueError("published Pi migration database is missing")
        return {
            "ok": True, "status": "ready", "runtime": {"version": identity["version"]},
            "database_format": "missing", "receipt_phase": None,
        }
    if receipt is not None:
        if receipt["target"]["runtime"] != identity:
            raise ValueError("published Pi migration target runtime identity changed")
        _assert_recovery_dependencies(receipt)
    if identity["version"] == "0.85.1":
        probe_runtime = runtime
    elif receipt is not None:
        owner = "source" if receipt["source"]["runtime"]["version"] == "0.85.1" else "target"
        probe_runtime, probe_identity = _runtime_identity(receipt[owner]["runtimePath"])
        if probe_identity != receipt[owner]["runtime"]:
            raise ValueError("retained Pi probe runtime identity changed")
    else:
        probe_runtime = _controller_runtime()
    probe = _probe_store(database, probe_runtime, maintenance_descriptors=maintenance_descriptors)
    expected = _SUPPORTED[identity["version"]]
    if probe["format"] != expected:
        raise ValueError("Pi store format is incompatible with the selected runtime")
    if receipt is not None:
        retained_pi_runtime_paths(receipt)
    return {
        "ok": True, "status": "ready", "runtime": {"version": identity["version"]},
        "database_format": probe["format"], "receipt_phase": receipt["phase"] if receipt else None,
    }


def migrate_pi(*, pi_db: str | Path, source_runtime: str | Path, target_runtime: str | Path,
               apply: bool = False, writers_stopped: bool = False) -> dict[str, Any]:
    database = _canonical_database(pi_db)
    source = _runtime_identity(source_runtime)
    target = _runtime_identity(target_runtime)
    if source[1]["version"] == target[1]["version"] or {source[1]["version"], target[1]["version"]} != set(_SUPPORTED):
        raise ValueError("Pi migration supports only exact 0.84.2 and 0.85.1 directions")
    runtime_0851 = _target_runtime(source, target)
    receipt = None
    if not apply:
        before = _assert_previewable(database)
        receipt = read_pi_migration_receipt(database)
        if receipt is not None:
            _assert_recovery_dependencies(receipt)
            if receipt["phase"] == "published":
                if receipt["target"]["runtime"] != source[1]:
                    raise ValueError("published Pi migration receipt does not match this source")
            elif (source != (Path(receipt["source"]["runtimePath"]), receipt["source"]["runtime"]) or
                  target != (Path(receipt["target"]["runtimePath"]), receipt["target"]["runtime"])):
                raise ValueError("Pi migration receipt runtime identity or path changed")
        store = _probe_store(database, runtime_0851, immutable=True)
        if store["format"] != _SUPPORTED[source[1]["version"]]:
            raise ValueError("Pi store format does not match the selected source runtime")
        if _artifact_inventory(database) != before:
            raise ValueError("Pi database changed during read-only preview")
    else:
        if database.is_symlink() or not database.is_file():
            raise ValueError("Pi database must be an existing regular file")
        store = {"format": "unknown", "sessionIds": []}
    free = shutil.disk_usage(database.parent).free
    if free < max(database.stat().st_size * 4, 1024 * 1024):
        raise ValueError("insufficient disk space for Pi migration copies")
    report = _sanitized_report(mode="preview", source=source[1], target=target[1], store=store,
                               receipt_phase=receipt["phase"] if receipt else None)
    if not apply:
        return report
    if not writers_stopped:
        raise ValueError("--apply requires --writers-stopped")

    with pi_session_locks(database) as (canonical, maintenance_descriptors):
        database = canonical
        receipt = read_pi_migration_receipt(database)
        if receipt is not None:
            _assert_recovery_dependencies(receipt)
        current = _probe_store(database, runtime_0851, maintenance_descriptors=maintenance_descriptors)
        if (receipt and receipt["phase"] == "published" and
                receipt["source"]["runtime"] == source[1] and receipt["target"]["runtime"] == target[1] and
                receipt["source"]["runtimePath"] == str(source[0]) and
                receipt["target"]["runtimePath"] == str(target[0])):
            ready = assert_pi_storage_ready(
                database, target[0], maintenance_descriptors=maintenance_descriptors,
            )
            return _sanitized_report(mode="apply", source=source[1], target=target[1], store=current,
                                     receipt_phase=ready["receipt_phase"], already_applied=True)
        prior_receipt = None
        if receipt and receipt["phase"] == "published":
            if (receipt["target"]["runtime"] != source[1] or
                    current["format"] != _SUPPORTED[source[1]["version"]]):
                raise ValueError("published Pi migration receipt does not match this source")
            prior_receipt = receipt
            receipt = None
        if receipt:
            if receipt["source"]["runtime"] != source[1] or receipt["target"]["runtime"] != target[1]:
                raise ValueError("Pi migration receipt runtime identity changed")
            if receipt["source"]["runtimePath"] != str(source[0]) or receipt["target"]["runtimePath"] != str(target[0]):
                raise ValueError("Pi migration receipt runtime path changed")

        backup = (
            Path(receipt["backup"]["path"])
            if receipt is not None
            else _new_backup_path(database, source[1]["version"], target[1]["version"])
        )
        work, stage, canonical_source, canonical_readback = _derived_artifacts(backup)

        if receipt and receipt["phase"] == "validated" and current["format"] == _SUPPORTED[target[1]["version"]]:
            if _sha256(database) != receipt["target"]["database"]["sha256"]:
                raise ValueError("published target differs from validated staging identity")
            receipt["phase"] = "published"
            receipt["published"] = {"databaseSha256": _sha256(database)}
            _write_receipt(database, receipt)
            return _sanitized_report(mode="apply", source=source[1], target=target[1], store=current,
                                     receipt_phase="published", already_applied=True)
        if receipt and receipt["phase"] == "validated":
            if current["format"] != _SUPPORTED[source[1]["version"]]:
                raise ValueError("Pi validated migration cannot identify the active store")
            _checkpoint_source(database)
            if _logical_sha256(database) != receipt["source"]["database"]["sha256"]:
                raise ValueError("Pi source database changed before publication retry")
            if not stage.is_file() or _sha256(stage) != receipt["target"]["database"]["sha256"]:
                raise ValueError("Pi validated staging database identity changed")
            stage_probe = _probe_store(
                stage, runtime_0851, maintenance_descriptors=maintenance_descriptors,
            )
            if stage_probe["format"] != _SUPPORTED[target[1]["version"]]:
                raise ValueError("Pi validated staging store format changed")
            if any(path.exists() for path in _sidecars(database)):
                raise ValueError("Pi source sidecar exists before publication retry")
            os.replace(stage, database)
            directory_fd = os.open(database.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            receipt["phase"] = "published"
            receipt["published"] = {"databaseSha256": _sha256(database)}
            _write_receipt(database, receipt)
            return _sanitized_report(mode="apply", source=source[1], target=target[1],
                                     store=_probe_store(
                                         database, runtime_0851,
                                         maintenance_descriptors=maintenance_descriptors,
                                     ), receipt_phase="published",
                                     write_applied=True)

        if receipt is None:
            if current["format"] != _SUPPORTED[source[1]["version"]]:
                raise ValueError("Pi active store does not match the selected source runtime")
            if int(current.get("writerLeaseCount", 0)):
                raise ValueError("outstanding Pi source writer lease prevents migration")
            _sqlite_backup(database, backup)
            _checkpoint_source(database)
            source_logical = _logical_sha256(backup)
            if _logical_sha256(database) != source_logical:
                raise ValueError("Pi recovery backup does not match the active source")
            receipt = {
                "version": PI_MIGRATION_RECEIPT_VERSION,
                "database": str(database),
                "phase": "prepared",
                "rollbackRequired": True,
                "source": {"runtimePath": str(source[0]), "runtime": source[1],
                           "database": {"sha256": source_logical, "format": current["format"]}},
                "target": {"runtimePath": str(target[0]), "runtime": target[1]},
                "backup": {"path": str(backup), "sha256": _sha256(backup)},
            }
            if prior_receipt is not None:
                receipt["prior"] = prior_receipt
            _write_receipt(database, receipt)
        else:
            if current["format"] != _SUPPORTED[source[1]["version"]]:
                raise ValueError("Pi active store cannot be reconciled safely")
            _checkpoint_source(database)
            if _logical_sha256(database) != receipt["source"]["database"]["sha256"]:
                raise ValueError("Pi source database changed since migration preparation")

        _remove_work(work)
        shutil.copyfile(backup, work)
        work.chmod(0o600)
        _remove_work(stage)
        for output in (canonical_source, canonical_readback):
            if output.exists() or output.is_symlink():
                if output.is_symlink():
                    raise ValueError("Pi canonical work artifact is unsafe")
                output.unlink()
        export_report, source_payload = _export(
            source[0], source[1]["version"], work, canonical_source,
            maintenance_descriptors=maintenance_descriptors,
        )
        if _sha256(backup) != receipt["backup"]["sha256"]:
            raise ValueError("Pi recovery backup changed during export")
        _bridge(
            "import", target[0], maintenance_descriptors=maintenance_descriptors,
            expected_version=target[1]["version"], database=stage, input=canonical_source,
        )
        _checkpoint_source(stage)
        stage_probe = _probe_store(stage, runtime_0851, maintenance_descriptors=maintenance_descriptors)
        if stage_probe["format"] != _SUPPORTED[target[1]["version"]]:
            raise ValueError("Pi staging store format does not match the selected target runtime")
        readback_report, target_payload = _export(
            target[0], target[1]["version"], stage, canonical_readback,
            maintenance_descriptors=maintenance_descriptors,
        )
        if not _same_payload(source_payload, target_payload):
            raise ValueError("Pi target readback differs from committed source history")
        _checkpoint_source(stage)
        _fsync_file(stage)
        if _logical_sha256(database) != receipt["source"]["database"]["sha256"]:
            raise ValueError("Pi source database changed before publication")
        if any(path.exists() for path in _sidecars(database)):
            raise ValueError("Pi source sidecar exists before publication")
        receipt["phase"] = "validated"
        receipt["target"]["database"] = {"sha256": _sha256(stage), "format": _SUPPORTED[target[1]["version"]]}
        receipt["conversion"] = {
            "sessionCount": export_report["sessionCount"], "entryCount": export_report["entryCount"],
            "excludedTailCount": export_report["excludedTailCount"],
            "sourceCanonicalSha256": export_report["canonicalSha256"],
            "targetCanonicalSha256": readback_report["canonicalSha256"],
        }
        receipt["readback"] = {"equivalent": True, "contextSha256": readback_report["contextSha256"]}
        _write_receipt(database, receipt)
        os.replace(stage, database)
        directory_fd = os.open(database.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        receipt["phase"] = "published"
        receipt["published"] = {"databaseSha256": _sha256(database)}
        _write_receipt(database, receipt)
        final_probe = _probe_store(database, runtime_0851, maintenance_descriptors=maintenance_descriptors)
        if final_probe["format"] != _SUPPORTED[target[1]["version"]]:
            raise ValueError("published Pi target format readback failed")
        return _sanitized_report(mode="apply", source=source[1], target=target[1], store=final_probe,
                                 receipt_phase="published", write_applied=True)
