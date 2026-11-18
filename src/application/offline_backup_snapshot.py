"""Private staging and verification for an explicitly stopped runtime tree."""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Mapping

from src.infrastructure.private_storage import atomic_write_private_bytes


_SCHEMA = "om_offline_backup_snapshot.v1"
_MANIFEST = "manifest.json"
_KINDS = {"file", "sqlite"}
_MANIFEST_LIMIT = 1024 * 1024
_SQLITE_SIGNATURE = b"SQLite format 3\0"


class OfflineBackupError(ValueError):
    """The snapshot was incomplete, inconsistent, or unsafe."""


def _directory(path: Path) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise OfflineBackupError("backup root must be a real directory")
    return path.resolve(strict=True)


def _relative(name: str) -> Path:
    path = Path(name)
    if (
        path.is_absolute() or not path.parts or str(path) != name
        or ".." in path.parts or "\\" in name or path.parts[0] == _MANIFEST
    ):
        raise OfflineBackupError("backup path must be relative and canonical")
    return path


def _regular(root: Path, relative: Path) -> Path:
    path = root / relative
    if (
        path.resolve(strict=True) != path
        or not stat.S_ISREG(path.lstat().st_mode)
    ):
        raise OfflineBackupError("backup entry must be a regular file")
    return path


def _identity(path: Path) -> tuple[int, int, int, int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _standalone(path: Path) -> None:
    if any(os.path.lexists(f"{path}{suffix}") for suffix in ("-wal", "-shm", "-journal")):
        raise OfflineBackupError("SQLite input must be stopped and checkpointed")


def _verify_sqlite(path: Path) -> None:
    _standalone(path)
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise OfflineBackupError("SQLite integrity check failed")


def _walk_failed(error: OSError) -> None:
    raise OfflineBackupError("backup directory cannot be inventoried") from error


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def stage_offline_backup(
    *, source: Path, destination: Path, files: Mapping[str, str], max_bytes: int,
) -> str:
    """Stage a supplied stopped tree. Return the manifest digest after readback.

    The caller owns quiescing all writers, including across databases. This core
    does not stop services, discover secrets, encrypt, transfer, or rotate packs.
    An interrupted directory without a manifest is not a valid snapshot.
    """
    source = _directory(source)
    destination = _directory(destination.parent) / destination.name
    if source == destination or source in destination.parents or destination in source.parents:
        raise OfflineBackupError("source and destination must be separate")
    if type(max_bytes) is not int or max_bytes <= 0 or not files:
        raise OfflineBackupError("backup needs a positive budget and explicit files")

    inputs = {}
    for name, kind in sorted(files.items()):
        if kind not in _KINDS:
            raise OfflineBackupError("unknown backup entry kind")
        relative = _relative(name)
        origin = _regular(source, relative)
        if kind == "sqlite":
            _standalone(origin)
        else:
            with origin.open("rb") as sample:
                if sample.read(len(_SQLITE_SIGNATURE)) == _SQLITE_SIGNATURE:
                    raise OfflineBackupError("SQLite input must be declared as sqlite")
        inputs[relative] = origin, kind, _identity(origin)
    if sum(item[2][2] for item in inputs.values()) > max_bytes:
        raise OfflineBackupError("backup exceeds source byte budget")

    destination.mkdir(mode=0o700)
    entries = []
    total = 0
    for relative, (origin, kind, identity) in inputs.items():
        target = destination / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with os.fdopen(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as handle:
            if kind == "file":
                with origin.open("rb") as original:
                    remaining = identity[2]
                    while remaining:
                        block = original.read(min(1024 * 1024, remaining))
                        if not block:
                            raise OfflineBackupError("source file shortened")
                        handle.write(block)
                        remaining -= len(block)
                    if original.read(1):
                        raise OfflineBackupError("source file grew")
                    handle.flush()
            else:
                with (
                    closing(sqlite3.connect(f"{origin.as_uri()}?mode=ro&immutable=1", uri=True)) as original,
                    closing(sqlite3.connect(target)) as copy,
                ):
                    original.backup(copy)
            os.fsync(handle.fileno())
        if kind == "sqlite":
            _verify_sqlite(target)
        size = target.stat().st_size
        total += size
        if total > max_bytes:
            raise OfflineBackupError("backup exceeds staged byte budget")
        with target.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        entries.append({"path": str(relative), "kind": kind, "bytes": size, "sha256": digest})

    for origin, kind, identity in inputs.values():
        if _identity(origin) != identity:
            raise OfflineBackupError("source changed during backup")
        if kind == "sqlite":
            _standalone(origin)
    for directory, _, _ in os.walk(destination, topdown=False, onerror=_walk_failed):
        _sync_directory(Path(directory))
    encoded = json.dumps({"schema": _SCHEMA, "files": entries}, separators=(",", ":")).encode()
    if len(encoded) > _MANIFEST_LIMIT:
        raise OfflineBackupError("backup manifest exceeds size limit")
    digest = hashlib.sha256(encoded).hexdigest()
    atomic_write_private_bytes(destination / _MANIFEST, encoded)
    _sync_directory(destination)
    _sync_directory(destination.parent)
    if (destination / _MANIFEST).read_bytes() != encoded:
        raise OfflineBackupError("snapshot manifest readback mismatch")
    return digest


def verify_offline_backup(*, root: Path, manifest_sha256: str) -> None:
    """Verify the package against the manifest digest from a trusted receipt."""
    root = _directory(root)
    with _regular(root, Path(_MANIFEST)).open("rb") as handle:
        encoded = handle.read(_MANIFEST_LIMIT + 1)
    if len(encoded) > _MANIFEST_LIMIT or hashlib.sha256(encoded).hexdigest() != manifest_sha256:
        raise OfflineBackupError("backup manifest identity mismatch")
    payload = json.loads(encoded)
    if not isinstance(payload, dict) or payload.get("schema") != _SCHEMA:
        raise OfflineBackupError("backup manifest schema mismatch")
    entries = payload.get("files")
    if not isinstance(entries, list) or not entries:
        raise OfflineBackupError("backup manifest entries are missing")
    seen = set()
    for row in entries:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str) or row.get("kind") not in _KINDS:
            raise OfflineBackupError("backup manifest entry is invalid")
        relative = _relative(row["path"])
        if relative in seen:
            raise OfflineBackupError("duplicate backup entry")
        seen.add(relative)
        file = _regular(root, relative)
        if file.stat().st_size != row.get("bytes"):
            raise OfflineBackupError("backup file size mismatch")
        with file.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != row.get("sha256"):
                raise OfflineBackupError("backup file hash mismatch")
        if row["kind"] == "sqlite":
            _verify_sqlite(file)
    for directory, directories, files in os.walk(root, onerror=_walk_failed):
        folder = Path(directory)
        if any((folder / name).is_symlink() for name in directories):
            raise OfflineBackupError("backup contains a directory alias")
        for name in files:
            relative = (folder / name).relative_to(root)
            if relative != Path(_MANIFEST) and relative not in seen:
                raise OfflineBackupError("backup contains an undeclared file")
