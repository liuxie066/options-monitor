"""Shared advisory file-lock context managers for ``src/application``.

Consolidates the byte-identical ``_exclusive_lock`` copies (blocking
``LOCK_EX``, one holder at a time, waits for the current holder) and the
``_single_instance_lock`` copies (non-blocking ``LOCK_EX`` that reports a
``RESOURCE_BUSY`` :class:`AgentToolError` when another process already holds
the lock) that were re-implemented per module. Callers bind private aliases so
their call sites keep the local name they already use, e.g.::

    from src.application.file_locks import exclusive_lock as _exclusive_lock
    from src.application.file_locks import single_instance_lock
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from src.application.agent_tool_contracts import AgentToolError


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def single_instance_lock(
    lock_path: str | os.PathLike[str] | None,
    *,
    busy_message: str,
) -> Any:
    raw = str(lock_path or "").strip()
    if not raw:
        yield
        return
    path = Path(raw).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AgentToolError(
                code="RESOURCE_BUSY",
                message=busy_message,
                details={"lock_path": str(path)},
            ) from exc
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
