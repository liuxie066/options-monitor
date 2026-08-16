from __future__ import annotations

import errno
import os
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, TextIO


PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
_SQLITE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")


def private_path(path: str | Path) -> Path:
    """Return an absolute path without resolving a possibly hostile symlink."""
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def ensure_private_directory(path: str | Path) -> Path:
    target = private_path(path)
    if target.is_symlink():
        raise OSError(f"sensitive directory must not be a symlink: {target.name}")
    target.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    if target.is_symlink() or not target.is_dir():
        raise OSError(f"sensitive directory is not a regular directory: {target.name}")
    target.chmod(PRIVATE_DIRECTORY_MODE)
    return target


def ensure_private_file(path: str | Path) -> Path:
    target = private_path(path)
    ensure_private_directory(target.parent)
    if target.is_symlink():
        raise OSError(f"sensitive file must not be a symlink: {target.name}")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, PRIVATE_FILE_MODE)
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError(f"sensitive file is not a regular file: {target.name}")
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
    finally:
        os.close(descriptor)
    if target.is_symlink():
        raise OSError(f"sensitive file must not be a symlink: {target.name}")
    return target


def atomic_write_private_text(path: str | Path, content: str, *, encoding: str = "utf-8") -> Path:
    target = private_path(path)
    ensure_private_directory(target.parent)
    if target.is_symlink():
        raise OSError(f"sensitive file must not be a symlink: {target.name}")
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding=encoding) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        target.chmod(PRIVATE_FILE_MODE)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise
    return target


def atomic_write_private_bytes(path: str | Path, content: bytes) -> Path:
    target = private_path(path)
    ensure_private_directory(target.parent)
    if target.is_symlink():
        raise OSError(f"sensitive file must not be a symlink: {target.name}")
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        target.chmod(PRIVATE_FILE_MODE)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise
    return target


def append_private_text(path: str | Path, content: str, *, encoding: str = "utf-8") -> Path:
    target = ensure_private_file(path)
    flags = os.O_WRONLY | os.O_APPEND
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags)
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "a", encoding=encoding) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    return target


@contextmanager
def open_private_text(path: str | Path, *, encoding: str = "utf-8") -> Iterator[TextIO]:
    """Open an existing sensitive regular file without following a final symlink."""

    target = private_path(path)
    if target.is_symlink():
        raise OSError(f"sensitive file must not be a symlink: {target.name}")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags)
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError(f"sensitive file is not a regular file: {target.name}")
        with os.fdopen(descriptor, "r", encoding=encoding) as handle:
            descriptor = -1
            yield handle
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def connect_private_sqlite(path: str | Path, **kwargs: Any) -> sqlite3.Connection:
    target = ensure_private_file(path)
    connection = sqlite3.connect(str(target), **kwargs)
    secure_sqlite_artifacts(target)
    return connection


def secure_sqlite_artifacts(path: str | Path) -> None:
    target = private_path(path)
    for candidate in (target, *(Path(f"{target}{suffix}") for suffix in _SQLITE_SIDECAR_SUFFIXES)):
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(candidate, flags)
        except FileNotFoundError:
            if candidate == target:
                raise OSError(f"sensitive SQLite artifact is missing: {candidate.name}") from None
            continue
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise OSError(f"sensitive SQLite artifact must not be a symlink: {candidate.name}") from exc
            raise
        try:
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise OSError(f"sensitive SQLite artifact is not a regular file: {candidate.name}")
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
        finally:
            os.close(descriptor)


__all__ = [
    "PRIVATE_DIRECTORY_MODE",
    "PRIVATE_FILE_MODE",
    "append_private_text",
    "atomic_write_private_bytes",
    "atomic_write_private_text",
    "connect_private_sqlite",
    "ensure_private_directory",
    "ensure_private_file",
    "open_private_text",
    "private_path",
    "secure_sqlite_artifacts",
]
