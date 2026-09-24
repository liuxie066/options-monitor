from __future__ import annotations

import json
import fcntl
import os
import stat
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
AUDIT_SEGMENT_BYTES = 64 * 1024 * 1024


@contextmanager
def rotating_private_jsonl_lock(path: str | Path, *, incoming_bytes: int):
    """Serialize append and rename through a stable lock, never through the rotated inode."""
    if incoming_bytes > AUDIT_SEGMENT_BYTES:
        raise ValueError("audit record exceeds segment size limit")
    target = Path(path)
    _ensure_private_directory(target.parent)
    lock_path = Path(f"{target}.lock")
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(lock_path, flags, PRIVATE_FILE_MODE)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("audit lock is not a private regular file")
        os.fchmod(fd, PRIVATE_FILE_MODE)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            if target.exists() or target.is_symlink():
                current = target.lstat()
                if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                    raise OSError("audit target is not a regular file")
                today = datetime.now(timezone.utc).strftime("%Y%m%d")
                file_day = datetime.fromtimestamp(current.st_mtime, timezone.utc).strftime("%Y%m%d")
                readable = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    complete_tail = not current.st_size or os.pread(readable, 1, current.st_size - 1) == b"\n"
                finally:
                    os.close(readable)
                if current.st_size and complete_tail and (current.st_size + incoming_bytes > AUDIT_SEGMENT_BYTES or file_day != today):
                    for number in range(1, 1000000):
                        segment = target.with_name(f"{target.stem}.{file_day}.{number:06d}{target.suffix}")
                        if not segment.exists() and not segment.is_symlink():
                            target.rename(segment)
                            break
                    else:
                        raise OSError("audit segment sequence exhausted")
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def read_json(path: str | Path, default: Any = None, encoding: str = "utf-8") -> Any:
    p = Path(path)
    if not p.exists() or p.is_symlink():
        return default
    try:
        return json.loads(p.read_text(encoding=encoding))
    except Exception:
        return default


def atomic_write_text(path: str | Path, content: str, *, encoding: str = "utf-8") -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".tmp.{uuid.uuid4().hex[:12]}")
    try:
        tmp.write_text(content, encoding=encoding)
        tmp.replace(p)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_private_text(path: str | Path, content: str, *, encoding: str = "utf-8") -> None:
    p = Path(path).expanduser()
    _ensure_private_directory(p.parent)
    if p.is_symlink():
        raise OSError(f"sensitive file must not be a symlink: {p.name}")
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=p.parent)
    tmp = Path(temp_name)
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding=encoding) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, p)
        p.chmod(PRIVATE_FILE_MODE)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_json(
    path: str | Path,
    obj: Any,
    *,
    encoding: str = "utf-8",
    indent: int = 2,
) -> None:
    payload = json.dumps(obj, ensure_ascii=False, indent=indent) + "\n"
    atomic_write_text(path, payload, encoding=encoding)


def atomic_write_private_json(
    path: str | Path,
    obj: Any,
    *,
    encoding: str = "utf-8",
    indent: int = 2,
) -> None:
    payload = json.dumps(obj, ensure_ascii=False, indent=indent) + "\n"
    atomic_write_private_text(path, payload, encoding=encoding)


def append_private_text(path: str | Path, content: str, *, encoding: str = "utf-8") -> Path:
    target = Path(path).expanduser()
    _ensure_private_directory(target.parent)
    if target.is_symlink():
        raise OSError(f"sensitive file must not be a symlink: {target.name}")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, PRIVATE_FILE_MODE)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"sensitive file is not a regular file: {target.name}")
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


def _ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise OSError(f"sensitive directory must not be a symlink: {path.name}")
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    if path.is_symlink() or not path.is_dir():
        raise OSError(f"sensitive directory is not a regular directory: {path.name}")
    path.chmod(PRIVATE_DIRECTORY_MODE)
