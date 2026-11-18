"""Controlled removal of scan blobs after all referencing roots have retired."""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import stat
from typing import Any, Callable

from domain.storage.no_follow import open_directory_chain
from src.application.agent_tool_contracts import AgentToolError
from src.application.research.storage_baseline import preview_scan_blob_gc, scan_blob_file_identity


def retire_scan_blobs(
    *, runtime_root: str | Path, expected_plan_sha256: str, receipt: Path,
    writers_stopped: bool, confirm: bool, max_candidates: int,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Apply a strict preview on a quiesced tree; never infer writer shutdown."""
    def fail(message: str) -> None:
        raise AgentToolError(code="SCAN_BLOB_RETIREMENT_BLOCKED", message=message)

    if not confirm or not writers_stopped:
        fail("scan blob retirement requires confirmation and stopped writers")
    if type(max_candidates) is not int or max_candidates <= 0:
        fail("scan blob retirement requires a positive candidate limit")
    root, receipt = Path(runtime_root).expanduser().absolute(), receipt.expanduser().absolute()
    if (
        root != root.resolve(strict=True) or receipt != receipt.resolve()
        or receipt.is_relative_to(root) or not receipt.parent.is_dir()
    ):
        fail("runtime must be canonical and receipt must be outside runtime")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = preview_scan_blob_gc(runtime_root=root, now_fn=now_fn, retired_only=True)
        root_info = os.fstat(root_fd)
        if (
            not plan["deletion_allowed"] or plan["plan_sha256"] != expected_plan_sha256
            or plan["runtime_identity"] != [root_info.st_dev, root_info.st_ino]
        ):
            fail("scan blob retirement requires the exact current strict preview")
        candidates = plan["candidates"]
        if not 0 < len(candidates) <= max_candidates:
            fail("scan blob retirement candidate count is empty or exceeds the approved limit")

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        with os.fdopen(os.open(receipt, flags, 0o600), "w", encoding="utf-8") as journal:
            def write(status: str, **fields: Any) -> None:
                journal.write(json.dumps({"status": status, "at_utc": datetime.now(timezone.utc).isoformat(), **fields}) + "\n")
                journal.flush()
                os.fsync(journal.fileno())

            write("started", plan_sha256=expected_plan_sha256, runtime_root=str(root),
                  candidates=[item["blob_relpath"] for item in candidates])
            receipt_dir = os.open(receipt.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(receipt_dir)
            finally:
                os.close(receipt_dir)
            for item in candidates:
                current = root.lstat()
                if not stat.S_ISDIR(current.st_mode) or [current.st_dev, current.st_ino] != plan["runtime_identity"]:
                    fail("scan runtime changed; check the progress receipt")
                relative = Path(item["blob_relpath"])
                parent = open_directory_chain(base=root, components=relative.parts[:-1], create=False)
                try:
                    info = os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or scan_blob_file_identity(info) != item["file_identity"]:
                        fail("scan blob changed during retirement; check the progress receipt")
                    write("deleting", path=relative.as_posix(), file_identity=item["file_identity"])
                    os.unlink(relative.name, dir_fd=parent)
                    os.fsync(parent)
                    try:
                        os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        fail("scan blob removal could not be confirmed")
                    write("removed", path=relative.as_posix())
                finally:
                    os.close(parent)
            write("complete", removed_count=len(candidates))
    finally:
        os.close(root_fd)
    return {"status": "complete", "removed_count": len(candidates), "receipt": str(receipt),
            "plan_sha256": expected_plan_sha256}
