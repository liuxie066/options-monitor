from __future__ import annotations

import errno
import fcntl
import os
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator, Literal


LockMode = Literal["shared", "exclusive"]


class PositionAdviceLockError(RuntimeError):
    """Raised when a Position Advice manifest lock cannot be acquired safely."""


def position_advice_state_root(base: Path) -> Path:
    return Path(base).resolve() / "output_shared" / "state" / "position_advice"


def portfolio_scope_state_dir(base: Path, portfolio_scope_id: str) -> Path:
    scope_id = str(portfolio_scope_id or "").strip()
    if not scope_id or "/" in scope_id or "\\" in scope_id or scope_id in {".", ".."}:
        raise ValueError("portfolio_scope_id is invalid")
    return position_advice_state_root(base) / scope_id


def _lock_operation(mode: LockMode) -> int:
    if mode == "shared":
        return fcntl.LOCK_SH
    if mode == "exclusive":
        return fcntl.LOCK_EX
    raise ValueError(f"unsupported lock mode: {mode}")


@contextmanager
def manifest_file_lock(
    path: Path,
    *,
    mode: LockMode,
    timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.05,
) -> Iterator[None]:
    """Acquire one bounded POSIX file lock and fail closed on timeout."""

    timeout = float(timeout_seconds)
    poll_interval = float(poll_interval_seconds)
    if timeout < 0:
        raise ValueError("timeout_seconds must be non-negative")
    if poll_interval <= 0:
        raise ValueError("poll_interval_seconds must be positive")

    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(descriptor, "a+")
    deadline = time.monotonic() + timeout
    operation = _lock_operation(mode) | fcntl.LOCK_NB
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), operation)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise PositionAdviceLockError(f"manifest lock failed: {lock_path}") from exc
                if time.monotonic() >= deadline:
                    raise PositionAdviceLockError(f"manifest lock timed out: {lock_path}") from exc
                time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
        yield
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def position_advice_manifest_locks(
    *,
    base: Path,
    portfolio_scope_id: str | None = None,
    global_mode: LockMode = "shared",
    scope_mode: LockMode | None = None,
    timeout_seconds: float = 5.0,
) -> Iterator[None]:
    """Acquire Position Advice locks in the only allowed order: global then scope."""

    root = position_advice_state_root(base)
    if scope_mode is not None and portfolio_scope_id is None:
        raise ValueError("scope lock requires portfolio_scope_id")
    with ExitStack() as stack:
        stack.enter_context(
            manifest_file_lock(
                root / ".manifest.lock",
                mode=global_mode,
                timeout_seconds=timeout_seconds,
            )
        )
        if scope_mode is not None:
            scope_dir = portfolio_scope_state_dir(base, str(portfolio_scope_id))
            stack.enter_context(
                manifest_file_lock(
                    scope_dir / ".current.lock",
                    mode=scope_mode,
                    timeout_seconds=timeout_seconds,
                )
            )
        yield


__all__ = [
    "LockMode",
    "PositionAdviceLockError",
    "manifest_file_lock",
    "portfolio_scope_state_dir",
    "position_advice_manifest_locks",
    "position_advice_state_root",
]
