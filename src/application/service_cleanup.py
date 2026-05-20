from __future__ import annotations

import os
from functools import cmp_to_key
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from src.application.version_check import compare_versions


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
    dirs = [path for path in releases_root.iterdir() if path.is_dir() and not path.is_symlink()]
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
    keep_releases: int = 2,
    include_apt_cache: bool = False,
    journal_vacuum_size: str | None = None,
    cleanup_downloads: bool = False,
    cleanup_pip_cache: bool = False,
    confirm: bool = False,
    run_cmd: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    repo_link = Path(repo_root).expanduser()
    releases = Path(releases_root).expanduser().resolve() if releases_root else _default_releases_root(repo_link)
    keep_count = max(2, int(keep_releases or 2))
    base = {
        "schema_version": 1,
        "confirmed": bool(confirm),
        "repo_root": str(repo_link),
        "releases_root": str(releases),
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
    estimated = sum(int(item["estimated_bytes"]) for item in release_items) + sum(int(item["estimated_bytes"]) for item in cache_items)
    deleted_paths: list[str] = []
    operations: list[dict[str, Any]] = []

    if confirm:
        for path in delete_releases:
            if path.resolve() == active_release or path.resolve() in kept_set or not _safe_child(path, parent=releases):
                operations.append({"path": str(path), "ok": False, "skipped": True, "reason": "unsafe_release_path"})
                continue
            _delete_path(path)
            deleted_paths.append(str(path))
            operations.append({"path": str(path), "ok": True, "kind": "release"})
        for item in cache_items:
            path = Path(str(item["path"]))
            if not item.get("exists"):
                continue
            if item.get("kind") == "apt_cache":
                result = run_cmd(["apt-get", "clean"], capture_output=True, text=True, timeout=120, check=False)
                operations.append(
                    {
                        "kind": "apt_cache",
                        "command": ["apt-get", "clean"],
                        "ok": int(getattr(result, "returncode", 1)) == 0,
                        "returncode": int(getattr(result, "returncode", 1)),
                        "stdout": str(getattr(result, "stdout", "") or "")[-2000:],
                        "stderr": str(getattr(result, "stderr", "") or "")[-2000:],
                    }
                )
                if int(getattr(result, "returncode", 1)) == 0:
                    deleted_paths.append(str(path))
                continue
            if item.get("kind") == "downloads" and not _safe_child(path, parent=apps_root):
                operations.append({"path": str(path), "ok": False, "skipped": True, "reason": "unsafe_cache_path"})
                continue
            _delete_path(path)
            deleted_paths.append(str(path))
            operations.append({"path": str(path), "ok": True, "kind": item.get("kind")})
        if journal_vacuum_size:
            operations.append({"kind": "journal", **_run_journal_vacuum(size=journal_vacuum_size, run_cmd=run_cmd)})

    unsafe_roots = [
        "/var/lib/options-monitor",
        "output",
        "output_shared",
        "output_runs",
        "locks",
        "runtime config",
        "user overlay config",
        "active release",
        "rollback release",
    ]

    return {
        **base,
        "ok": True,
        "status": "cleaned" if confirm else "dry_run",
        "changed": bool(confirm and (deleted_paths or journal_vacuum_size)),
        "active_release": str(active_release),
        "kept_releases": [{"path": str(path), "version": path.name} for path in kept],
        "delete_releases": release_items,
        "cache_dirs": cache_items,
        "journal_vacuum_size": journal_vacuum_size,
        "estimated_freed_bytes": estimated,
        "freed_bytes": estimated if confirm else 0,
        "deleted_paths": deleted_paths,
        "operations": operations,
        "protected": unsafe_roots,
    }


__all__ = ["service_cleanup"]
