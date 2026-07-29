from __future__ import annotations

import os
import hashlib
import json
from datetime import datetime, timedelta, timezone
from functools import cmp_to_key
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from src.application.position_advice_current_repository import (
    PositionAdviceCurrentError,
    collect_protected_current_runs_under_global_lock,
)
from src.application.version_check import compare_versions
from src.infrastructure.position_advice_manifest_lock import (
    PositionAdviceLockError,
    position_advice_manifest_locks,
)


def _default_releases_root(repo_root: Path) -> Path:
    repo = Path(repo_root).expanduser()
    return (repo.parent / "releases").resolve()


def _path_size(path: Path) -> int:
    if not path.exists() and not path.is_symlink():
        return 0
    if path.is_file() or path.is_symlink():
        try:
            return int(path.lstat().st_size)
        except OSError:
            return 0
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        for name in files:
            try:
                total += int((root_path / name).lstat().st_size)
            except OSError:
                pass
        for name in dirs:
            item = root_path / name
            if item.is_symlink():
                try:
                    total += int(item.lstat().st_size)
                except OSError:
                    pass
    return total


def _release_version(path: Path) -> str:
    version = path.name
    version_path = path / "VERSION"
    if version_path.exists():
        try:
            version = version_path.read_text(encoding="utf-8").strip() or path.name
        except OSError:
            version = path.name
    return version


def _compare_release_dirs_desc(left: Path, right: Path) -> int:
    left_version = _release_version(left)
    right_version = _release_version(right)
    try:
        return -compare_versions(left_version, right_version)
    except Exception:
        if left.name == right.name:
            return 0
        return -1 if left.name > right.name else 1


def _release_dirs(releases_root: Path) -> list[Path]:
    if not releases_root.exists():
        return []
    dirs = [
        path
        for path in releases_root.iterdir()
        if path.is_dir() and not path.is_symlink() and (path / "VERSION").is_file()
    ]
    return sorted(dirs, key=cmp_to_key(_compare_release_dirs_desc))


def _safe_child(path: Path, *, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _delete_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    shutil.rmtree(path)


def _delete_path_result(path: Path, *, kind: str) -> dict[str, Any]:
    before_bytes = _path_size(path)
    try:
        _delete_path(path)
    except Exception as exc:
        return {
            "path": str(path),
            "kind": kind,
            "ok": False,
            "reason": "delete_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "before_bytes": before_bytes,
            "after_bytes": _path_size(path),
            "freed_bytes": 0,
        }
    after_bytes = _path_size(path)
    return {
        "path": str(path),
        "kind": kind,
        "ok": True,
        "before_bytes": before_bytes,
        "after_bytes": after_bytes,
        "freed_bytes": max(0, before_bytes - after_bytes),
    }


def _mtime_utc(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _set_result_item(
    *,
    kind: str,
    path: Path,
    root: Path,
    estimated_bytes: int | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": kind,
        "path": str(path),
        "exists": path.exists(),
        "estimated_bytes": _path_size(path) if estimated_bytes is None else int(estimated_bytes),
    }
    try:
        out["mtime_utc"] = _mtime_utc(path).isoformat()
    except OSError:
        out["mtime_utc"] = None
    try:
        out["relative_path"] = str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        out["relative_path"] = None
    if reason:
        out["reason"] = reason
    return out


def _latest_run_pointer(runtime: Path, runs_root: Path) -> Path | None:
    pointer = runtime / "output_shared" / "state" / "last_run_dir.txt"
    if not pointer.exists() or not pointer.is_file():
        return None
    try:
        target = Path(pointer.read_text(encoding="utf-8").strip()).expanduser().resolve()
    except OSError:
        return None
    if target.exists() and target.is_dir() and _safe_child(target, parent=runs_root):
        return target
    return None


def _output_run_cleanup_plan(
    *,
    runtime_root: Path,
    keep_days: int,
    keep_count: int,
    now: datetime,
    position_advice_protected_runs: set[Path] | None = None,
) -> dict[str, Any]:
    runs_root = runtime_root / "output_runs"
    keep_days_int = max(0, int(keep_days))
    keep_count_int = max(1, int(keep_count))
    cutoff = now - timedelta(days=keep_days_int)
    base = {
        "enabled": True,
        "root": str(runs_root),
        "root_exists": runs_root.exists() and runs_root.is_dir(),
        "keep_days": keep_days_int,
        "keep_count": keep_count_int,
        "cutoff_utc": cutoff.isoformat(),
        "scanned_count": 0,
        "protected_runs": [],
        "delete_runs": [],
        "estimated_bytes": 0,
    }
    if not runs_root.exists() or not runs_root.is_dir():
        return base

    run_dirs = sorted(
        [item.resolve() for item in runs_root.iterdir() if item.is_dir() and not item.is_symlink()],
        key=lambda item: (_mtime_utc(item), item.name),
        reverse=True,
    )
    latest_protected = run_dirs[:keep_count_int]
    protected: dict[Path, str] = {path: "latest_keep_count" for path in latest_protected}
    pointer = _latest_run_pointer(runtime_root, runs_root)
    if pointer is not None:
        protected[pointer.resolve()] = "last_run_dir_pointer"
    for current_run in position_advice_protected_runs or set():
        protected[current_run.resolve()] = "position_advice_current"

    delete_runs: list[dict[str, Any]] = []
    protected_runs: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        if run_dir in protected:
            protected_runs.append(
                _set_result_item(kind="output_run", path=run_dir, root=runs_root, reason=protected[run_dir])
            )
            continue
        try:
            mtime = _mtime_utc(run_dir)
        except OSError:
            protected_runs.append(
                _set_result_item(kind="output_run", path=run_dir, root=runs_root, reason="mtime_unavailable")
            )
            continue
        if mtime >= cutoff:
            protected_runs.append(
                _set_result_item(kind="output_run", path=run_dir, root=runs_root, reason="within_keep_days")
            )
            continue
        if run_dir.parent != runs_root.resolve() or not _safe_child(run_dir, parent=runs_root):
            protected_runs.append(
                _set_result_item(kind="output_run", path=run_dir, root=runs_root, reason="unsafe_path")
            )
            continue
        delete_runs.append(_set_result_item(kind="output_run", path=run_dir, root=runs_root, reason="expired"))

    estimated = sum(int(item.get("estimated_bytes") or 0) for item in delete_runs)
    return {
        **base,
        "scanned_count": len(run_dirs),
        "protected_runs": protected_runs,
        "delete_runs": delete_runs,
        "estimated_bytes": estimated,
    }


def _runtime_log_cleanup_plan(
    *,
    runtime_root: Path,
    keep_days: int,
    now: datetime,
) -> dict[str, Any]:
    logs_root = runtime_root / "logs"
    keep_days_int = max(0, int(keep_days))
    cutoff = now - timedelta(days=keep_days_int)
    base = {
        "enabled": True,
        "root": str(logs_root),
        "root_exists": logs_root.exists() and logs_root.is_dir(),
        "keep_days": keep_days_int,
        "cutoff_utc": cutoff.isoformat(),
        "scanned_count": 0,
        "protected_logs": [],
        "delete_logs": [],
        "estimated_bytes": 0,
    }
    if not logs_root.exists() or not logs_root.is_dir():
        return base

    files = sorted(
        [item.resolve() for item in logs_root.iterdir() if item.is_file() and not item.is_symlink()],
        key=lambda item: (_mtime_utc(item), item.name),
        reverse=True,
    )
    delete_logs: list[dict[str, Any]] = []
    protected_logs: list[dict[str, Any]] = []
    for item in files:
        if item.suffix != ".log":
            protected_logs.append(_set_result_item(kind="runtime_log", path=item, root=logs_root, reason="non_log_file"))
            continue
        try:
            mtime = _mtime_utc(item)
        except OSError:
            protected_logs.append(
                _set_result_item(kind="runtime_log", path=item, root=logs_root, reason="mtime_unavailable")
            )
            continue
        if mtime >= cutoff:
            protected_logs.append(
                _set_result_item(kind="runtime_log", path=item, root=logs_root, reason="within_keep_days")
            )
            continue
        if item.parent != logs_root.resolve() or not _safe_child(item, parent=logs_root):
            protected_logs.append(_set_result_item(kind="runtime_log", path=item, root=logs_root, reason="unsafe_path"))
            continue
        delete_logs.append(_set_result_item(kind="runtime_log", path=item, root=logs_root, reason="expired"))

    estimated = sum(int(item.get("estimated_bytes") or 0) for item in delete_logs)
    return {
        **base,
        "scanned_count": len(files),
        "protected_logs": protected_logs,
        "delete_logs": delete_logs,
        "estimated_bytes": estimated,
    }


def _output_runs_plan_sha256(payload: dict[str, Any]) -> str:
    rows = payload.get("delete_runs") if isinstance(payload, dict) else []
    normalized = [
        {
            "path": str(item.get("path") or ""),
            "estimated_bytes": int(item.get("estimated_bytes") or 0),
            "mtime_utc": item.get("mtime_utc"),
        }
        for item in rows or []
        if isinstance(item, dict)
    ]
    encoded = json.dumps(
        sorted(normalized, key=lambda item: item["path"]),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cache_candidates(
    *,
    apps_root: Path,
    cleanup_downloads: bool,
    cleanup_pip_cache: bool,
    include_apt_cache: bool,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if cleanup_downloads:
        out.append({"kind": "downloads", "path": apps_root / "_downloads", "delete_mode": "tree"})
    if cleanup_pip_cache:
        out.append({"kind": "pip_cache", "path": Path.home() / ".cache" / "pip", "delete_mode": "tree"})
    if include_apt_cache:
        out.append({"kind": "apt_cache", "path": Path("/var/cache/apt/archives"), "delete_mode": "command"})
    return out


def _run_journal_vacuum(
    *,
    size: str,
    run_cmd: Callable[..., Any],
) -> dict[str, Any]:
    command = ["journalctl", f"--vacuum-size={size}"]
    try:
        proc = run_cmd(command, capture_output=True, text=True, timeout=120, check=False)
    except Exception as exc:
        return {"command": command, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "command": command,
        "ok": int(getattr(proc, "returncode", 1)) == 0,
        "returncode": int(getattr(proc, "returncode", 1)),
        "stdout": str(getattr(proc, "stdout", "") or "")[-2000:],
        "stderr": str(getattr(proc, "stderr", "") or "")[-2000:],
    }


def service_cleanup(
    *,
    repo_root: str | Path,
    releases_root: str | Path | None = None,
    runtime_root: str | Path | None = None,
    keep_releases: int = 2,
    include_apt_cache: bool = False,
    journal_vacuum_size: str | None = None,
    cleanup_downloads: bool = False,
    cleanup_pip_cache: bool = False,
    cleanup_output_runs: bool = False,
    output_runs_keep_days: int = 14,
    output_runs_keep_count: int = 200,
    cleanup_runtime_logs: bool = False,
    runtime_logs_keep_days: int = 14,
    expected_output_runs_plan_sha256: str | None = None,
    confirm: bool = False,
    run_cmd: Callable[..., Any] = subprocess.run,
    now: datetime | None = None,
) -> dict[str, Any]:
    repo_link = Path(repo_root).expanduser()
    releases = Path(releases_root).expanduser().resolve() if releases_root else _default_releases_root(repo_link)
    runtime = Path(runtime_root).expanduser().resolve() if runtime_root else None
    keep_count = max(2, int(keep_releases or 2))
    base = {
        "schema_version": 1,
        "confirmed": bool(confirm),
        "repo_root": str(repo_link),
        "releases_root": str(releases),
        "runtime_root": str(runtime) if runtime else None,
        "keep_releases": keep_count,
    }
    if not repo_link.is_symlink():
        return {
            **base,
            "ok": False,
            "status": "repo_root_not_symlink",
            "reason": "repo_root must be the current symlink path so the active release can be protected",
            "changed": False,
        }

    active_release = repo_link.resolve()
    releases_list = _release_dirs(releases)
    active_in_releases = any(path.resolve() == active_release for path in releases_list)
    if not active_in_releases:
        return {
            **base,
            "ok": False,
            "status": "active_release_not_under_releases_root",
            "active_release": str(active_release),
            "changed": False,
        }

    kept: list[Path] = [active_release]
    for release in releases_list:
        if release.resolve() == active_release:
            continue
        if len(kept) >= keep_count:
            break
        kept.append(release.resolve())
    kept_set = {path.resolve() for path in kept}
    delete_releases = [path for path in releases_list if path.resolve() not in kept_set]

    apps_root = repo_link.parent.resolve()
    cache_candidates = _cache_candidates(
        apps_root=apps_root,
        cleanup_downloads=cleanup_downloads,
        cleanup_pip_cache=cleanup_pip_cache,
        include_apt_cache=include_apt_cache,
    )
    cache_items: list[dict[str, Any]] = []
    for item in cache_candidates:
        path = Path(item["path"])
        cache_items.append(
            {
                **item,
                "path": str(path),
                "exists": path.exists(),
                "estimated_bytes": _path_size(path),
            }
        )

    release_items = [
        {"path": str(path), "version": path.name, "estimated_bytes": _path_size(path)}
        for path in delete_releases
    ]
    runtime_now = now or datetime.now(timezone.utc)
    output_runs_cleanup = {"enabled": False}
    output_runs_cleanup_error: str | None = None
    runtime_cleanup_root = runtime or repo_link.parent.resolve()
    if cleanup_output_runs:
        try:
            with position_advice_manifest_locks(
                base=runtime_cleanup_root,
                global_mode="exclusive",
            ):
                current_runs = collect_protected_current_runs_under_global_lock(
                    base=runtime_cleanup_root,
                )
                output_runs_cleanup = _output_run_cleanup_plan(
                    runtime_root=runtime_cleanup_root,
                    keep_days=output_runs_keep_days,
                    keep_count=output_runs_keep_count,
                    now=runtime_now,
                    position_advice_protected_runs=current_runs,
                )
        except (PositionAdviceCurrentError, PositionAdviceLockError, OSError) as exc:
            output_runs_cleanup_error = f"{type(exc).__name__}: {exc}"
            output_runs_cleanup = {
                "enabled": True,
                "status": "position_advice_manifest_invalid",
                "root": str(runtime_cleanup_root / "output_runs"),
                "protected_runs": [],
                "delete_runs": [],
                "estimated_bytes": 0,
                "error": output_runs_cleanup_error,
            }
    runtime_logs_cleanup = {"enabled": False}
    if cleanup_runtime_logs:
        runtime_logs_cleanup = _runtime_log_cleanup_plan(
            runtime_root=runtime or repo_link.parent.resolve(),
            keep_days=runtime_logs_keep_days,
            now=runtime_now,
        )

    estimated = (
        sum(int(item["estimated_bytes"]) for item in release_items)
        + sum(int(item["estimated_bytes"]) for item in cache_items)
        + int(output_runs_cleanup.get("estimated_bytes") or 0)
        + int(runtime_logs_cleanup.get("estimated_bytes") or 0)
    )
    deleted_paths: list[str] = []
    operations: list[dict[str, Any]] = []

    if output_runs_cleanup_error is not None:
        return {
            **base,
            "ok": False,
            "status": "position_advice_manifest_invalid",
            "reason": output_runs_cleanup_error,
            "changed": False,
            "active_release": str(active_release),
            "kept_releases": [{"path": str(path), "version": path.name} for path in kept],
            "delete_releases": release_items,
            "cache_dirs": cache_items,
            "output_runs_cleanup": output_runs_cleanup,
            "runtime_logs_cleanup": runtime_logs_cleanup,
            "journal_vacuum_size": journal_vacuum_size,
            "estimated_freed_bytes": estimated,
            "freed_bytes": 0,
            "deleted_paths": [],
            "operations": [],
            "protected": [
                "output_shared",
                "output_accounts",
                "option_positions.sqlite3",
                "trade_events",
                "audit",
                "locks",
                "runtime config",
                "user overlay config",
                "active release",
                "rollback release",
            ],
        }

    output_runs_plan_sha256 = _output_runs_plan_sha256(output_runs_cleanup)
    expected_plan_sha256 = str(expected_output_runs_plan_sha256 or "").strip().lower()
    if confirm and expected_plan_sha256 and expected_plan_sha256 != output_runs_plan_sha256:
        return {
            **base,
            "ok": False,
            "status": "output_runs_plan_changed",
            "reason": "current output-runs deletion plan does not match the confirmed preview",
            "changed": False,
            "output_runs_cleanup": output_runs_cleanup,
            "output_runs_plan_sha256": output_runs_plan_sha256,
            "expected_output_runs_plan_sha256": expected_plan_sha256,
            "deleted_paths": [],
            "operations": [],
        }

    if confirm:
        for path in delete_releases:
            if path.resolve() == active_release or path.resolve() in kept_set or not _safe_child(path, parent=releases):
                operations.append({"path": str(path), "ok": False, "skipped": True, "reason": "unsafe_release_path"})
                continue
            operation = _delete_path_result(path, kind="release")
            operations.append(operation)
            if operation.get("ok"):
                deleted_paths.append(str(path))
        for item in cache_items:
            path = Path(str(item["path"]))
            if not item.get("exists"):
                continue
            if item.get("kind") == "apt_cache":
                before_bytes = _path_size(path)
                command = ["apt-get", "clean"]
                try:
                    result = run_cmd(command, capture_output=True, text=True, timeout=120, check=False)
                except Exception as exc:
                    operations.append(
                        {
                            "kind": "apt_cache",
                            "command": command,
                            "ok": False,
                            "error": f"{type(exc).__name__}: {exc}",
                            "before_bytes": before_bytes,
                            "after_bytes": _path_size(path),
                            "freed_bytes": 0,
                        }
                    )
                    continue
                after_bytes = _path_size(path)
                operations.append(
                    {
                        "kind": "apt_cache",
                        "command": command,
                        "ok": int(getattr(result, "returncode", 1)) == 0,
                        "returncode": int(getattr(result, "returncode", 1)),
                        "stdout": str(getattr(result, "stdout", "") or "")[-2000:],
                        "stderr": str(getattr(result, "stderr", "") or "")[-2000:],
                        "before_bytes": before_bytes,
                        "after_bytes": after_bytes,
                        "freed_bytes": max(0, before_bytes - after_bytes),
                    }
                )
                if int(getattr(result, "returncode", 1)) == 0:
                    deleted_paths.append(str(path))
                continue
            if item.get("kind") == "downloads" and not _safe_child(path, parent=apps_root):
                operations.append({"path": str(path), "ok": False, "skipped": True, "reason": "unsafe_cache_path"})
                continue
            operation = _delete_path_result(path, kind=str(item.get("kind") or "cache"))
            operations.append(operation)
            if operation.get("ok"):
                deleted_paths.append(str(path))
        if cleanup_output_runs:
            try:
                with position_advice_manifest_locks(
                    base=runtime_cleanup_root,
                    global_mode="exclusive",
                ):
                    current_runs = collect_protected_current_runs_under_global_lock(
                        base=runtime_cleanup_root,
                    )
                    output_runs_cleanup = _output_run_cleanup_plan(
                        runtime_root=runtime_cleanup_root,
                        keep_days=output_runs_keep_days,
                        keep_count=output_runs_keep_count,
                        now=runtime_now,
                        position_advice_protected_runs=current_runs,
                    )
                    current_plan_sha256 = _output_runs_plan_sha256(output_runs_cleanup)
                    if (
                        expected_plan_sha256
                        and current_plan_sha256 != expected_plan_sha256
                    ):
                        operations.append(
                            {
                                "ok": False,
                                "skipped": True,
                                "reason": "output_runs_plan_changed",
                                "kind": "output_run_cleanup",
                                "expected_output_runs_plan_sha256": expected_plan_sha256,
                                "output_runs_plan_sha256": current_plan_sha256,
                            }
                        )
                        output_runs_cleanup["plan_sha256"] = current_plan_sha256
                        raise _OutputRunsPlanChanged
                    for item in output_runs_cleanup.get("delete_runs") or []:
                        path = Path(str(item.get("path") or ""))
                        runs_root = Path(str(output_runs_cleanup.get("root") or ""))
                        if not path.exists():
                            operations.append(
                                {
                                    "path": str(path),
                                    "ok": False,
                                    "skipped": True,
                                    "reason": "missing",
                                    "kind": "output_run",
                                }
                            )
                            continue
                        if (
                            path.parent != runs_root.resolve()
                            or not _safe_child(path, parent=runs_root)
                        ):
                            operations.append(
                                {
                                    "path": str(path),
                                    "ok": False,
                                    "skipped": True,
                                    "reason": "unsafe_output_run_path",
                                    "kind": "output_run",
                                }
                            )
                            continue
                        operation = _delete_path_result(path, kind="output_run")
                        operations.append(operation)
                        if operation.get("ok"):
                            deleted_paths.append(str(path))
            except _OutputRunsPlanChanged:
                pass
            except (PositionAdviceCurrentError, PositionAdviceLockError, OSError) as exc:
                operations.append(
                    {
                        "ok": False,
                        "skipped": True,
                        "reason": "position_advice_manifest_invalid",
                        "error": f"{type(exc).__name__}: {exc}",
                        "kind": "output_run_cleanup",
                    }
                )
        for item in runtime_logs_cleanup.get("delete_logs") or []:
            path = Path(str(item.get("path") or ""))
            logs_root = Path(str(runtime_logs_cleanup.get("root") or ""))
            if not path.exists():
                operations.append({"path": str(path), "ok": False, "skipped": True, "reason": "missing", "kind": "runtime_log"})
                continue
            if path.parent != logs_root.resolve() or path.suffix != ".log" or not _safe_child(path, parent=logs_root):
                operations.append(
                    {"path": str(path), "ok": False, "skipped": True, "reason": "unsafe_runtime_log_path", "kind": "runtime_log"}
                )
                continue
            operation = _delete_path_result(path, kind="runtime_log")
            operations.append(operation)
            if operation.get("ok"):
                deleted_paths.append(str(path))
        if journal_vacuum_size:
            operations.append({"kind": "journal", **_run_journal_vacuum(size=journal_vacuum_size, run_cmd=run_cmd)})

    unsafe_roots = [
        "output_shared",
        "output_accounts",
        "option_positions.sqlite3",
        "trade_events",
        "audit",
        "locks",
        "runtime config",
        "user overlay config",
        "active release",
        "rollback release",
    ]
    failures = [
        item
        for item in operations
        if item.get("ok") is False and str(item.get("reason") or "") != "missing"
    ]
    freed_bytes = sum(max(0, int(item.get("freed_bytes") or 0)) for item in operations if item.get("ok"))
    successful_journal = any(item.get("kind") == "journal" and item.get("ok") for item in operations)
    changed = bool(confirm and (deleted_paths or successful_journal))
    status = "dry_run"
    if confirm and failures:
        status = "partial_failure" if changed else "cleanup_failed"
    elif confirm:
        status = "cleaned"

    return {
        **base,
        "ok": not failures,
        "status": status,
        "changed": changed,
        "active_release": str(active_release),
        "kept_releases": [{"path": str(path), "version": path.name} for path in kept],
        "delete_releases": release_items,
        "cache_dirs": cache_items,
        "output_runs_cleanup": output_runs_cleanup,
        "output_runs_plan_sha256": _output_runs_plan_sha256(output_runs_cleanup),
        "expected_output_runs_plan_sha256": expected_plan_sha256 or None,
        "runtime_logs_cleanup": runtime_logs_cleanup,
        "journal_vacuum_size": journal_vacuum_size,
        "estimated_freed_bytes": estimated,
        "freed_bytes": freed_bytes if confirm else 0,
        "failure_count": len(failures),
        "failures": failures,
        "deleted_paths": deleted_paths,
        "operations": operations,
        "protected": unsafe_roots,
    }


__all__ = ["service_cleanup"]


class _OutputRunsPlanChanged(Exception):
    pass
