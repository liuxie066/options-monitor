"""Private bounded binary transfer primitives for the archive owner.

No discovery, retry, archive publication or production target selection lives here.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Sequence


class TransferError(RuntimeError):
    """A transfer did not satisfy its frozen input or execution budget."""


@dataclass
class WriteBudget:
    remaining: int
    written: int = 0

    def record(self, count: int) -> None:
        if count < 0 or count > self.remaining:
            raise TransferError("payload budget exceeded")
        self.remaining -= count
        self.written += count


def safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(p in {"", ".", ".."} for p in value.split("/")):
        raise TransferError("unsafe relative path")
    return str(path)


# Arguments are data, not interpolated shell source. Parent paths are opened by
# directory descriptors so a replaced symlink cannot redirect the file open.
READ_FILE_SCRIPT = r"""
import hashlib, os, stat, sys
root, relative, size_text, expected = sys.argv[1:5]
frozen_identity = __import__('json').loads(sys.argv[5]) if len(sys.argv)>5 else None
size = int(size_text)
parts = relative.split('/')
if size < 0 or not parts or any(p in ('', '.', '..') for p in parts):
    raise ValueError('invalid frozen file')
flags = os.O_RDONLY | os.O_NOFOLLOW
fd = os.open(root, flags | os.O_DIRECTORY)
try:
    for component in parts[:-1]:
        child = os.open(component, flags | os.O_DIRECTORY, dir_fd=fd)
        os.close(fd)
        fd = child
    source = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=fd)
finally:
    os.close(fd)
try:
    before = os.fstat(source)
    if frozen_identity is not None and [before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns] != frozen_identity:
        raise ValueError('frozen file identity changed')
    if not stat.S_ISREG(before.st_mode) or before.st_size != size:
        raise ValueError('frozen file type or size changed')
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        chunk = os.read(source, min(65536, remaining))
        if not chunk:
            raise ValueError('short source')
        digest.update(chunk)
        remaining -= len(chunk)
        view = memoryview(chunk)
        while view:
            written = os.write(1, view)
            if written <= 0:
                raise ValueError('stdout made no progress')
            view = view[written:]
    extra = os.read(source, 1)
    after = os.fstat(source)
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if extra or identity(before) != identity(after) or digest.hexdigest() != expected:
        raise ValueError('frozen source changed')
finally:
    os.close(source)
"""


def process_table(timeout: float = 1.0) -> dict[int, dict]:
    """Read only OS process metadata; no optional Python dependency.

    Failure to observe is an error, never evidence that children have exited.
    """
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,rss=,lstart="],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=max(0.001, timeout),
        check=True,
    )
    if len(result.stdout) > 1024 * 1024:
        raise TransferError("process observation exceeds metadata limit")
    rows = {}
    for line in result.stdout.decode("ascii", errors="strict").splitlines():
        parts = line.split(None, 4)
        if len(parts) != 5:
            raise TransferError("invalid process observation")
        pid, parent, group, rss = map(int, parts[:4])
        rows[pid] = {"pid": pid, "parent": parent, "group": group, "rss": rss * 1024, "started": parts[4]}
    return rows


def stop_group(process: subprocess.Popen, grace: float) -> None:
    """Stop this transfer's session and confirm its whole group disappeared."""
    group = process.pid
    process.poll()
    rows = process_table()
    members = [row for row in rows.values() if row["group"] == group]
    if not members:
        process.wait(timeout=max(grace, 0.1))
        return
    try:
        os.killpg(group, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        process.poll()
        if any(row["group"] == group for row in process_table().values()):
            raise TransferError("cannot signal transfer group; exit unknown")
    end = time.monotonic() + grace
    while time.monotonic() < end:
        process.poll()
        if not any(row["group"] == group for row in process_table().values()):
            process.wait(timeout=max(grace, 0.1))
            return
        time.sleep(0.01)
    try:
        os.killpg(group, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.poll()
        if any(row["group"] == group for row in process_table().values()):
            raise TransferError("cannot signal transfer group; exit unknown")
    process.wait(timeout=max(grace, 0.1))
    end = time.monotonic() + grace
    while time.monotonic() < end:
        if not any(row["group"] == group for row in process_table().values()):
            return
        time.sleep(0.01)
    raise TransferError("transfer group exit unknown")


def receive_file(
    command: Sequence[str],
    *,
    destination: Path,
    size: int,
    sha256: str,
    budget: WriteBudget,
    deadline: float,
    grace: float,
    stderr_limit: int,
    reserve_bytes: int,
) -> dict:
    """Stream one file once; no stdout payload is captured in a result/log.

    destination must have a private, already-created parent owned by the caller.
    Failed partial files are left for that owner's exact cleanup policy.
    """
    if size < 0 or size > budget.remaining or grace <= 0 or stderr_limit < 0 or reserve_bytes < 0:
        raise TransferError("invalid transfer budget")
    if time.monotonic() >= deadline:
        raise TransferError("deadline expired before transfer")
    destination = Path(destination)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    process = None
    selector = selectors.DefaultSelector()
    received = 0
    digest = hashlib.sha256()
    stderr_count = 0
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            list(command), stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                raise TransferError("transfer deadline exceeded")
            for key, _ in selector.select(min(remaining_time, 0.05)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.fileobj is process.stderr:
                    stderr_count += len(chunk)
                    if stderr_count > stderr_limit:
                        raise TransferError("stderr budget exceeded")
                    # Never retain source exception text or business bytes.
                    continue
                if len(chunk) > size - received or len(chunk) > budget.remaining:
                    raise TransferError("sender exceeded frozen length")
                space = os.statvfs(destination.parent)
                if space.f_bavail * space.f_frsize < reserve_bytes + len(chunk):
                    raise TransferError("target free space below reserve")
                view = memoryview(chunk)
                while view:
                    if time.monotonic() >= deadline:
                        raise TransferError("transfer deadline exceeded")
                    count = os.write(fd, view)
                    if count <= 0:
                        raise TransferError("target write made no progress")
                    budget.record(count)
                    received += count
                    digest.update(view[:count])
                    view = view[count:]
        code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        if code != 0 or received != size or digest.hexdigest() != sha256:
            raise TransferError("source exit, length or hash mismatch")
        if time.monotonic() >= deadline:
            raise TransferError("transfer deadline exceeded")
        return {
            "returncode": code,
            "written_bytes": received,
            "stderr_bytes": stderr_count,
            "elapsed_seconds": time.monotonic() - started,
            "sha256": digest.hexdigest(),
        }
    finally:
        selector.close()
        os.close(fd)
        if process is not None:
            stop_group(process, grace)
            for stream in (process.stdout, process.stderr):
                stream.close()


def publish_batch(staging: Path, target: Path, *, deadline: float) -> None:
    """Durably publish one validated private tree, never replace a target.

    Caller writes a validated-only receipt before entering this function. Any
    failure after rename leaves target intact and must be reported as unknown.
    """
    import ctypes
    import errno
    import sys

    staging, target = Path(staging), Path(target)
    if staging.parent.resolve() != target.parent.resolve():
        raise TransferError("batch publication requires the same parent")
    if staging.is_symlink() or not staging.is_dir():
        raise TransferError("invalid staging directory")

    def sync(path: Path, directory: bool = False) -> None:
        if time.monotonic() >= deadline:
            raise TransferError("publication deadline exceeded; state unknown")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    # No rename has occurred here; errors must not remove validated evidence.
    for root, dirs, files in os.walk(staging, topdown=False, followlinks=False):
        for name in files:
            sync(Path(root) / name)
        for name in dirs:
            if (Path(root) / name).is_symlink():
                raise TransferError("staging symlink rejected")
        sync(Path(root), True)
    sync(staging.parent, True)
    if time.monotonic() >= deadline:
        raise TransferError("publication deadline exceeded")
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        status = rename(os.fsencode(staging), os.fsencode(target), 4)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        rename = library.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        status = rename(-100, os.fsencode(staging), -100, os.fsencode(target), 1)
    else:
        raise TransferError("atomic no-replace rename unavailable")
    if status:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise FileExistsError(code, "batch destination exists", target)
        raise OSError(code, "batch publication failed")
    # This sync is intentionally AFTER rename and BEFORE success/readback.
    sync(target.parent, True)
    if not target.is_dir() or staging.exists() or time.monotonic() >= deadline:
        raise TransferError("publication readback failed; state unknown")


# Frozen lengths are the framing: concatenation has no on-disk format change.
READ_FILES_SCRIPT = (
    "import json,sys\nroot=sys.argv[1]\nrows=json.load(sys.stdin)\nreader=compile("
    + repr(READ_FILE_SCRIPT)
    + ",'<frozen_file>','exec')\nfor row in rows:\n sys.argv=['read',root,row['path'],str(row['size']),row['sha256'],json.dumps(row['identity'])]\n exec(reader)\n"
)


def receive_files(command, *, root, rows, budget, deadline, grace, stderr_limit, reserve_bytes, evidence_limit):
    """One stream, frozen order/length framing; fail the whole batch on any error."""
    import json
    import tempfile

    encoded = json.dumps(rows, separators=(",", ":")).encode()
    if len(encoded) > evidence_limit or sum(row["size"] for row in rows) > budget.remaining:
        raise TransferError("stream request budget")
    if len({row["path"] for row in rows}) != len(rows):
        raise TransferError("duplicate stream path")
    stderr_limit = min(stderr_limit, evidence_limit - len(encoded))
    selector = selectors.DefaultSelector()
    process = None
    fd = None
    index = written = stderr_bytes = 0
    digest = hashlib.sha256()
    results = []
    started = time.monotonic()

    def advance():
        nonlocal fd, index, written, digest
        while index < len(rows):
            if time.monotonic() >= deadline:
                raise TransferError("stream deadline")
            row = rows[index]
            if fd is None:
                path = Path(root) / safe_relative(row["path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            if written != row["size"]:
                return
            if digest.hexdigest() != row["sha256"]:
                raise TransferError("stream file hash mismatch")
            os.close(fd)
            fd = None
            results.append({"written_bytes": written, "sha256": digest.hexdigest()})
            index += 1
            written = 0
            digest = hashlib.sha256()

    try:
        with tempfile.TemporaryFile() as request:
            request.write(encoded)
            request.seek(0)
            process = subprocess.Popen(
                command, stdin=request, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
            )
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        advance()
        while selector.get_map():
            if time.monotonic() >= deadline:
                raise TransferError("stream deadline")
            for key, _ in selector.select(min(0.05, max(0, deadline - time.monotonic()))):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.fileobj is process.stderr:
                    stderr_bytes += len(chunk)
                    if stderr_bytes > stderr_limit:
                        raise TransferError("stream stderr budget")
                    continue
                if len(chunk) > budget.remaining:
                    raise TransferError("stream total budget")
                view = memoryview(chunk)
                while view:
                    advance()
                    if index == len(rows):
                        raise TransferError("stream extra bytes")
                    if time.monotonic() >= deadline:
                        raise TransferError("stream deadline")
                    count = min(len(view), rows[index]["size"] - written)
                    space = os.statvfs(root)
                    if space.f_bavail * space.f_frsize < reserve_bytes + count:
                        raise TransferError("stream space reserve")
                    count = os.write(fd, view[:count])
                    if count <= 0:
                        raise TransferError("stream write stalled")
                    budget.record(count)
                    written += count
                    digest.update(view[:count])
                    view = view[count:]
                advance()
        code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        if code or index != len(rows):
            raise TransferError("stream short input or source failure")
        return {
            "returncode": code,
            "files": results,
            "written_bytes": sum(r["written_bytes"] for r in results),
            "stderr_bytes": stderr_bytes,
            "request_bytes": len(encoded),
            "elapsed_seconds": time.monotonic() - started,
        }
    finally:
        selector.close()
        if fd is not None:
            os.close(fd)
        if process is not None:
            stop_group(process, grace)
            process.stdout.close()
            process.stderr.close()
