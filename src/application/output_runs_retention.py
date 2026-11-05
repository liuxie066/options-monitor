"""Age-based retention confined to direct, timestamp-named output_runs children."""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import stat
import sys
from contextlib import contextmanager, ExitStack
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from domain.storage.repositories import state_repo
from src.application.agent_tool_contracts import AgentToolError
from src.application.config_defaults import output_runs_keep_days


_RUN_NAME = re.compile(r"(?P<timestamp>\d{8}T\d{6}Z)(?:-[0-9a-f]{6})?\Z")


def parse_run_timestamp(name: str) -> datetime | None:
    match = _RUN_NAME.fullmatch(name)
    if match is None:
        return None
    try:
        return datetime.strptime(match["timestamp"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _directory_bytes(path: Path) -> tuple[int, int]:
    """Count logical bytes and allocated file bytes without following symlinks."""
    logical = allocated = 0
    for directory, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            info = (Path(directory) / name).lstat()
            logical += info.st_size
            # Hard-linked bytes remain allocated through the other name.
            if info.st_nlink == 1:
                allocated += getattr(info, "st_blocks", 0) * 512
    return logical, allocated


def _audit(root: Path, *, action: str, status: str, receipt: dict[str, Any]) -> None:
    state_repo.append_audit_event(root, {
        "event_type": "output_runs_retention",
        "action": action,
        "status": status,
        "extra": receipt,
    })


@contextmanager
def run_lifecycle_lock(runtime_root: Path, *, exclusive: bool = False, create: bool = True):
    """Writers/readers share the lease; deletion skips rather than waiting for them."""
    path = runtime_root / "locks" / "output_runs.lock"
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as locks:
        descriptors = []
        if create or path.exists():
            descriptor = os.open(path, os.O_RDWR | (os.O_CREAT if create else 0) | os.O_NOFOLLOW, 0o600)
            locks.callback(os.close, descriptor)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("output_runs lifecycle lock must be a regular file")
            descriptors.append(descriptor)
        # A read-only query before the first writer/cleanup needs no new file:
        # a shared lease on the existing runs root also excludes the cleaner.
        runs_root = runtime_root / "output_runs"
        if runs_root.is_dir() and not runs_root.is_symlink():
            descriptor = os.open(runs_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            locks.callback(os.close, descriptor)
            descriptors.append(descriptor)
        operation = fcntl.LOCK_EX | fcntl.LOCK_NB if exclusive else fcntl.LOCK_SH
        for descriptor in descriptors:
            try:
                fcntl.flock(descriptor, operation)
            except BlockingIOError as exc:
                raise AgentToolError(code="RESOURCE_BUSY", message="output_runs is in use by a run or reader") from exc
            locks.callback(fcntl.flock, descriptor, fcntl.LOCK_UN)
        yield


def run_reader_lease(root_for: Callable[..., Path | None]):
    """Keep a logical multi-file read alive under the existing lifecycle lease."""
    def decorate(function):
        @wraps(function)
        def leased(*args, **kwargs):
            selected = root_for(*args, **kwargs)
            if selected is None:
                return function(*args, **kwargs)
            root = Path(selected)
            with run_lifecycle_lock(root, create=False):
                return function(*args, **kwargs)
        return leased
    return decorate


def configured_keep_days(root: Path, *, config_path: Path | None = None) -> int:
    """Markets share one runs directory; the longer configured retention wins."""
    paths = {root / "config.us.json", root / "config.hk.json"}
    if config_path is not None:
        paths.add(config_path)
    values = []
    for path in sorted(paths):
        if path.exists():
            cfg = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(cfg, dict):
                raise ValueError(f"runtime config must be an object: {path}")
            values.append(output_runs_keep_days(cfg))
    return max(values) if values else output_runs_keep_days({})


@contextmanager
def tick_run_lifecycle(*, runtime_root: Path, config_path: Path, automatic: bool = True):
    """Cover all account workers, post-delivery sidecars, and completion writes."""
    try:
        with run_lifecycle_lock(runtime_root):
            yield
    finally:
        if automatic:
            try:
                days = configured_keep_days(runtime_root, config_path=config_path)
                result = prune_output_runs(runtime_root=runtime_root, keep_days=days, confirm=True, trigger="tick_end")
                if not result["ok"]:
                    print("<3>OUTPUT_RUNS_RETENTION_DEGRADED " + json.dumps(result), file=sys.stderr)
            except Exception as exc:
                # Maintenance must not change the scan/delivery result; failure stays visible.
                print(f"<3>OUTPUT_RUNS_RETENTION_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)


def _run_target(raw: str, *, root: Path, parent: Path) -> str | None:
    value = Path(raw).expanduser()
    options = [value] if value.is_absolute() else [root / value, parent / value]
    for candidate in options:
        try:
            parts = candidate.resolve().relative_to(root / "output_runs").parts
        except (ValueError, OSError, RuntimeError):
            continue
        if parts and parse_run_timestamp(parts[0]) is not None:
            return parts[0]
    return None


def _protected_runs(root: Path, runs: list[Path], retained: set[str]) -> dict[str, str]:
    """Protect path dependencies, not historical IDs in independent audit copies."""
    protected: dict[str, str] = {}
    pointer = root / "output_shared" / "state" / "last_run_dir.txt"
    if pointer.exists():
        target = _run_target(pointer.read_text(encoding="utf-8").strip(), root=root, parent=root)
        if target:
            protected[target] = "last_run_pointer"
    # Older processes may predate the lifecycle lease. Their live claims still pin
    # the run; completed/stale history is only provenance.
    claims = root / "output_shared" / "state" / "idempotency" / "tick_execution"
    if claims.exists():
        for path in claims.glob("*.json"):
            claim = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(claim, dict) or claim.get("status") != "in_progress":
                continue
            pid = claim.get("pid")
            if type(pid) is int and pid > 0:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                except PermissionError:
                    pass
                run_id = str(claim.get("run_id") or "")
                if parse_run_timestamp(run_id):
                    protected[run_id] = "active_run"

    # Latest candidate readers search by account/market even when the newest run
    # was only a scheduler skip. Keep one terminal scan per scope.
    latest_scopes: set[tuple[str, str]] = set()
    for run in sorted(runs, key=lambda item: item.name, reverse=True):
        accounts = run / "accounts"
        if not accounts.is_dir() or accounts.is_symlink():
            continue
        for account in accounts.iterdir():
            if not account.is_dir() or account.is_symlink() or (account / "state").is_symlink():
                continue
            manifests = list((account / "state").glob("candidate_snapshot_manifest*.json"))
            if not manifests:
                continue
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            for market in manifest.get("markets", []) if isinstance(manifest, dict) else []:
                scope = (account.name, str(market))
                if scope not in latest_scopes:
                    latest_scopes.add(scope)
                    protected[run.name] = "latest_account_market_scan"

    sources: list[Path] = []
    for directory in [root / "output_accounts", root / "output_shared", root / "research", root / "output"]:
        if directory.is_symlink():
            target = _run_target(str(directory.resolve()), root=root, parent=root)
            if target:
                protected[target] = "referenced_alias"
            # output is an alias of output_accounts, already traversed separately.
            continue
        if directory.exists():
            sources.append(directory)
    sources.extend(path for path in runs if path.name in retained or path.name in protected)
    pending = list(sources)
    seen: set[Path] = set()
    while pending:
        directory = pending.pop()
        if directory in seen:
            continue
        seen.add(directory)
        for current, dirs, files in os.walk(directory, followlinks=False):
            here = Path(current)
            # Archive copies have their own run namespace; they do not pin live originals.
            dirs[:] = [name for name in dirs if name not in {"remote_archive", "run_logs", "idempotency"}]
            for name in [*dirs, *files]:
                path = here / name
                if path.is_symlink():
                    target = _run_target(str(path.resolve()), root=root, parent=here)
                    if target:
                        protected[target] = "referenced_alias"
            for name in files:
                path = here / name
                if path.is_symlink() or path.suffix != ".json":
                    continue
                # Only manifests and current read models own live dependencies; large
                # candidate payloads and event/history copies are independent evidence.
                if not ("manifest" in name or name.startswith("inventory") or name in {"last_run.json", "scheduler_decision.json"}
                        or (name.endswith(".current.json") and not name.startswith("audit_event"))):
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, (dict, list)):
                    continue

                reference_roots = [path.parent]
                if isinstance(payload, dict):
                    reference_roots.extend(path.parent / raw for key, raw in payload.items()
                                           if str(key).endswith("_root_relpath") and isinstance(raw, str))

                def visit(value: Any, key: str = "") -> None:
                    if isinstance(value, dict):
                        for child_key, child in value.items():
                            if child_key in {"history", "events", "audit"}:
                                continue
                            visit(child, str(child_key))
                            if "/" in str(child_key):
                                visit(str(child_key), "path")
                    elif isinstance(value, list):
                        for child in value:
                            visit(child, key)
                    elif isinstance(value, str) and (
                        key in {"path", "run_dir", "relpath", "root"} or key.endswith(("_path", "_relpath", "_dir"))
                    ):
                        for reference_root in reference_roots:
                            target = _run_target(value, root=root, parent=reference_root)
                            if target:
                                protected[target] = "referenced_path"
                visit(payload)
        # A protected run can itself reference another run. Follow only that live closure.
        for run in runs:
            if run.name in protected and run not in seen:
                pending.append(run)
    return protected


def _locked_run(stack: ExitStack, path: Path) -> bool:
    """Respect existing per-artifact .lock files without creating or removing any."""
    for current, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
        for name in files:
            if not name.endswith(".lock"):
                continue
            lock = Path(current) / name
            if lock.is_symlink():
                return True
            handle = stack.enter_context(lock.open("r"))
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    return False


def prune_output_runs(*, runtime_root: Path, keep_days: int = 7, confirm: bool = False,
                      now: datetime | None = None, trigger: str = "manual",
                      candidate_ids: set[str] | None = None) -> dict[str, Any]:
    days = output_runs_keep_days({"runtime": {"output_runs_keep_days": keep_days}})
    root = Path(runtime_root).expanduser().resolve()
    runs_root = root / "output_runs"
    if runs_root.is_symlink():
        raise ValueError("output_runs must be a real directory, not a symlink")
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError("retention clock must include a timezone")
    cutoff = clock.astimezone(timezone.utc) - timedelta(days=days)
    result: dict[str, Any] = {
        "ok": True, "dry_run": not confirm, "trigger": trigger, "root": str(runs_root),
        "keep_days": days, "cutoff_utc": cutoff.isoformat(), "candidates": [], "deleted": [],
        "skipped": [], "errors": [], "freed_bytes": 0, "freed_allocated_bytes": 0,
    }
    if not runs_root.exists():
        return result
    try:
        with run_lifecycle_lock(root, exclusive=True, create=confirm):
            return _prune_locked(root, runs_root, cutoff, result, confirm=confirm, candidate_ids=candidate_ids)
    except AgentToolError as exc:
        if exc.code != "RESOURCE_BUSY":
            raise
        result["skipped"].append({"path": str(runs_root), "reason": "runs_locked"})
        if confirm:
            _audit(root, action="skipped", status="skipped", receipt=result)
        return result


def _prune_locked(root: Path, runs_root: Path, cutoff: datetime, result: dict[str, Any], *,
                  confirm: bool, candidate_ids: set[str] | None) -> dict[str, Any]:
    with ExitStack() as artifact_locks:
        return _prune_with_artifact_locks(root, runs_root, cutoff, result, confirm=confirm,
                                         candidate_ids=candidate_ids, artifact_locks=artifact_locks)


def _prune_with_artifact_locks(root: Path, runs_root: Path, cutoff: datetime, result: dict[str, Any], *,
                              confirm: bool, candidate_ids: set[str] | None, artifact_locks: ExitStack) -> dict[str, Any]:
    entries = sorted(runs_root.iterdir(), key=lambda path: path.name)
    runs = [path for path in entries if not path.is_symlink() and path.is_dir() and parse_run_timestamp(path.name)]
    latest = max(runs, key=lambda path: parse_run_timestamp(path.name), default=None)
    retained = {path.name for path in runs if parse_run_timestamp(path.name) >= cutoff}
    if latest:
        retained.add(latest.name)
    preserved: dict[str, str] = {}
    if candidate_ids is not None:
        preserved.update({path.name: "outside_requested_set" for path in runs if path.name not in candidate_ids})
    # Decide every retained root before resolving dependencies. Otherwise a locked
    # expired run (or service keep-count run) could survive while its source is deleted.
    for path in runs:
        if path.name in retained or path.name in preserved:
            continue
        try:
            if _locked_run(artifact_locks, path):
                preserved[path.name] = "run_locked"
        except OSError as exc:
            preserved[path.name] = "lock_inventory_failed"
            result["ok"] = False
            result["errors"].append({"run_id": path.name, "reason": "lock_inventory_failed", "error": str(exc)})
    retained.update(preserved)
    try:
        protected = _protected_runs(root, runs, retained)
        protected.update(preserved)
    except (OSError, ValueError) as exc:
        result["ok"] = False
        result["errors"].append({"reason": "reference_inventory_failed", "error": str(exc)})
        if confirm:
            _audit(root, action="blocked", status="error", receipt=result)
        return result
    for path in entries:
        row = {"run_id": path.name, "path": str(path)}
        timestamp = parse_run_timestamp(path.name)
        reason = None
        if path.is_symlink():
            reason = "symlink"
        elif not path.is_dir():
            reason = "not_directory"
        elif timestamp is None:
            reason = "invalid_run_name"
        elif path == latest:
            reason = "latest_run"
        elif timestamp >= cutoff:
            reason = "within_retention"
        elif path.name in protected:
            reason = protected[path.name]
        elif candidate_ids is not None and path.name not in candidate_ids:
            reason = "outside_requested_set"
        if reason:
            result["skipped"].append({**row, "reason": reason})
            continue
        try:
            logical, allocated = _directory_bytes(path)
            row.update(size_bytes=logical, allocated_bytes=allocated)
            result["candidates"].append(row)
            if not confirm:
                continue
            # Failure to persist intent prevents deletion, including on a full disk.
            _audit(root, action="delete_started", status="started", receipt={**row, "trigger": result["trigger"]})
            shutil.rmtree(path)
            result["deleted"].append(row)
            result["freed_bytes"] += logical
            result["freed_allocated_bytes"] += allocated
            _audit(root, action="deleted", status="ok", receipt=row)
        except OSError as exc:
            result["ok"] = False
            result["errors"].append({**row, "reason": "delete_failed" if confirm else "inventory_failed", "error": str(exc)})
    if confirm:
        _audit(root, action="completed", status="ok" if result["ok"] else "error", receipt=result)
    return result
