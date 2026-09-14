"""Offline legacy Pi-store migration only. The Bot uses no Node process."""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping
from src.infrastructure.private_storage import ensure_private_directory, ensure_private_file, private_path, secure_sqlite_artifacts
MIN_NODE_VERSION = (22, 19, 0)
MAX_SAFE_MESSAGE_CHARS = 240
_SESSION_ID_PATTERN = re.compile(r"^om_[0-9a-f]{64}$")


@contextmanager
def pi_session_locks(
    database: str | Path, session_id: str | None = None,
) -> Iterator[tuple[Path, tuple[int, ...]]]:
    """Hold independent inherited flock descriptions; None fences offline conversion."""
    if session_id is not None and not _SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError("invalid session identity")
    target = private_path(database)
    ensure_private_directory(target.parent)
    if target.is_symlink():
        raise OSError("session database must not be a symlink")
    if target.exists():
        if target.stat().st_nlink != 1:
            raise OSError("session database must not have hard-link aliases")
        secure_sqlite_artifacts(target)
    target = target.resolve()
    paths = [(Path(str(target) + ".om-pi.lock"), fcntl.LOCK_EX if session_id is None else fcntl.LOCK_SH)]
    if session_id is not None:
        paths.append((Path(str(target) + "." + session_id + ".lock"), fcntl.LOCK_EX))
    descriptors: list[int] = []
    try:
        for path, operation in paths:
            ensure_private_file(path)
            fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
            descriptors.append(fd)
            identity = os.fstat(fd)
            named = path.lstat()
            if (not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1
                    or (identity.st_dev, identity.st_ino) != (named.st_dev, named.st_ino)):
                raise OSError("session lock identity changed")
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
        yield target, tuple(descriptors)
    finally:
        # LOCK_UN would also unlock a surviving child's inherited description.
        # Close only: the kernel releases exclusion when the final holder exits.
        for fd in reversed(descriptors):
            os.close(fd)

def _runtime_command(
    runtime_entry: Path | None, environ: Mapping[str, str] | None,
    *, deadline_monotonic: float | None = None,
) -> tuple[list[str], Path]:
    source = os.environ if environ is None else environ
    node = shutil.which("node", path=source.get("PATH"))
    if node is None:
        raise LookupError("node executable not found")
    try:
        version_out = subprocess.run(
            [node, "--version"],
            capture_output=True,
            text=True,
            timeout=min(2, max(0.001, deadline_monotonic - time.monotonic())) if deadline_monotonic is not None else 2,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        raise LookupError("node version probe failed")
    if not version_out.startswith("v"):
        raise LookupError("node version output is unparseable")
    try:
        parts = version_out[1:].split(".")
        numeric = tuple(int(part) for part in parts[:3])
    except ValueError:
        raise LookupError("node version output is unparseable")
    if numeric < MIN_NODE_VERSION:
        raise LookupError("node is older than 22.19.0")

    if runtime_entry is None:
        repo_root = Path(__file__).resolve().parent.parent.parent
        entry = repo_root / "agent-runtime" / "main.ts"
    else:
        entry = runtime_entry
    if not entry.is_file():
        raise LookupError("runtime entry is missing")
    return [node, "--no-warnings", str(entry)], entry

def run_pi_migration_bridge(
    command: str,
    runtime: Path,
    arguments: Mapping[str, str | Path],
    *,
    timeout: float = 120,
    maintenance_descriptors: tuple[int, ...] = (),
) -> dict[str, Any]:
    """Run the offline Pi converter with the same Node floor as the Agent."""
    if command not in {"identity", "probe", "export", "import"}:
        raise ValueError("unsupported Pi migration bridge command")
    entry = Path(__file__).resolve().parents[2] / "agent-runtime" / "pi_migration.mjs"
    environment = {"PATH": os.environ.get("PATH", "")}
    try:
        argv, _ = _runtime_command(entry, environment)
        argv.insert(1, "--experimental-import-meta-resolve")
        argv.extend((command, "--runtime", str(runtime)))
        for name, argument in arguments.items():
            argv.extend(("--" + name.replace("_", "-"), str(argument)))
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            check=False, env=environment, pass_fds=maintenance_descriptors,
        )
    except (LookupError, OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Pi migration Node runtime is unavailable or timed out") from exc
    if completed.returncode:
        reason = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "offline bridge failed"
        raise ValueError(f"Pi migration bridge rejected the store: {reason[:MAX_SAFE_MESSAGE_CHARS]}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("Pi migration bridge returned invalid output") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise ValueError("Pi migration bridge failed")
    return result


def derive_pi_session_id(
    channel: str,
    sender: str,
    conversation: str,
    authority_scope: str,
) -> str:
    parts = (channel, sender, conversation, authority_scope)
    if any(not _is_nonempty_str(part) or "\0" in part for part in parts):
        raise ValueError("session identity parts must be non-empty and contain no NUL")
    material = "om-pi-session-v1\0" + "\0".join(parts)
    return "om_" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def derive_pi_local_session_id(authority_scope: str, session_key: str) -> str:
    parts = (authority_scope, session_key)
    if any(not _is_nonempty_str(part) or "\0" in part for part in parts):
        raise ValueError("local session identity parts must be non-empty and contain no NUL")
    material = "local\0" + authority_scope + "\0" + session_key
    return "om_" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and len(value) > 0
