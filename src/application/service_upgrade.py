from __future__ import annotations

import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.application.version_check import check_version_update, compare_versions, parse_version


def utc_now_iso(now_fn: Callable[[], datetime] | None = None) -> str:
    now = (now_fn or (lambda: datetime.now(timezone.utc)))()
    return now.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_version(repo_root: Path) -> str:
    return (repo_root / "VERSION").read_text(encoding="utf-8").strip()


def _version_text(value: str) -> str:
    text = str(value or "").strip()
    return text[1:] if text.startswith("v") else text


def _tag_text(value: str) -> str:
    version = _version_text(value)
    return f"v{version}"


def default_releases_root(repo_root: Path) -> Path:
    repo = Path(repo_root).expanduser()
    return (repo.parent / "releases").resolve()


def upgrade_status_path(runtime_root: str | Path) -> Path:
    return Path(runtime_root).expanduser().resolve() / "upgrade_status.json"


def load_upgrade_status(*, runtime_root: str | Path) -> dict[str, Any] | None:
    path = upgrade_status_path(runtime_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def write_upgrade_status(*, runtime_root: str | Path, payload: dict[str, Any]) -> None:
    path = upgrade_status_path(runtime_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


class _UpgradeLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "_UpgradeLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise RuntimeError(f"upgrade lock already exists: {self.path}") from exc
        os.write(self.fd, str(os.getpid()).encode("utf-8"))
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _run_command(
    command: list[str],
    *,
    cwd: Path | None,
    run_cmd: Callable[..., Any],
    timeout: int = 300,
) -> dict[str, Any]:
    proc = run_cmd(
        command,
        cwd=(str(cwd) if cwd is not None else None),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    rc = int(getattr(proc, "returncode", 1))
    stdout = str(getattr(proc, "stdout", "") or "")
    stderr = str(getattr(proc, "stderr", "") or "")
    return {
        "command": command,
        "cwd": str(cwd) if cwd is not None else None,
        "returncode": rc,
        "stdout": stdout[-4000:],
        "stderr": stderr[-4000:],
        "ok": rc == 0,
    }


def _run_required(
    command: list[str],
    *,
    cwd: Path | None,
    run_cmd: Callable[..., Any],
    operations: list[dict[str, Any]],
    timeout: int = 300,
) -> dict[str, Any]:
    result = _run_command(command, cwd=cwd, run_cmd=run_cmd, timeout=timeout)
    operations.append(result)
    if not result["ok"]:
        raise RuntimeError(f"command failed: {' '.join(shlex.quote(part) for part in command)}")
    return result


def _remote_url(*, repo_root: Path, remote_name: str, run_cmd: Callable[..., Any]) -> str:
    result = _run_command(
        ["git", "config", "--get", f"remote.{remote_name}.url"],
        cwd=repo_root,
        run_cmd=run_cmd,
        timeout=30,
    )
    if not result["ok"]:
        raise RuntimeError(f"failed to resolve remote URL for {remote_name}")
    url = str(result.get("stdout") or "").strip()
    if not url:
        raise RuntimeError(f"remote URL is empty for {remote_name}")
    return url


def _major_upgrade_blocked(*, current_version: str, target_version: str, allow_major: bool) -> bool:
    if allow_major:
        return False
    current = parse_version(current_version)
    target = parse_version(target_version)
    return target.major != current.major


def _restart_services_from_profile(
    *,
    runtime_root: Path,
    run_cmd: Callable[..., Any],
    operations: list[dict[str, Any]],
) -> list[str]:
    profile_path = runtime_root / "service.profile.json"
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(profile, dict):
        return []
    provider = str(profile.get("service_provider") or "").strip().lower()
    services = profile.get("services") if isinstance(profile.get("services"), list) else []
    restarted: list[str] = []
    if provider != "systemd":
        return restarted
    for item in services:
        name = str(item.get("name") if isinstance(item, dict) else item or "").strip()
        if not name.endswith(".service") or "trade-intake" not in name:
            continue
        _run_required(["systemctl", "restart", name], cwd=None, run_cmd=run_cmd, operations=operations, timeout=60)
        restarted.append(name)
    return restarted


def service_upgrade_check(
    *,
    repo_root: str | Path,
    runtime_root: str | Path,
    remote_name: str = "origin",
    run_cmd: Callable[..., Any] = subprocess.run,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    repo = Path(repo_root).expanduser().resolve()
    runtime = Path(runtime_root).expanduser().resolve()
    version = check_version_update(base_dir=repo, remote_name=remote_name, run_cmd=run_cmd, now_fn=now_fn)
    status = load_upgrade_status(runtime_root=runtime)
    return {
        "ok": bool(version.get("ok")),
        "repo_root": str(repo),
        "runtime_root": str(runtime),
        "remote_name": remote_name,
        "checked_at": utc_now_iso(now_fn),
        "current_version": version.get("current_version"),
        "latest_version": version.get("latest_version"),
        "release_tag": version.get("release_tag"),
        "upgrade_available": bool(version.get("update_available")),
        "version_check": version,
        "last_upgrade": status,
    }


def _switch_current_symlink(*, current_link: Path, target_dir: Path) -> None:
    if not current_link.is_symlink():
        raise RuntimeError(f"repo_root must be a current symlink for confirmed upgrade: {current_link}")
    tmp_link = current_link.with_name(f".{current_link.name}.upgrade-tmp")
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    tmp_link.symlink_to(target_dir, target_is_directory=True)
    os.replace(tmp_link, current_link)


def service_upgrade(
    *,
    repo_root: str | Path,
    runtime_root: str | Path,
    releases_root: str | Path | None = None,
    target_version: str | None = None,
    remote_name: str = "origin",
    confirm: bool = False,
    auto: bool = False,
    allow_major: bool = False,
    restart_services: bool = True,
    run_cmd: Callable[..., Any] = subprocess.run,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    repo_link = Path(repo_root).expanduser()
    repo = repo_link.resolve()
    runtime = Path(runtime_root).expanduser().resolve()
    releases = Path(releases_root).expanduser().resolve() if releases_root else default_releases_root(repo_link)
    current_version = _read_version(repo)
    repo_root_is_symlink = repo_link.is_symlink()
    check = service_upgrade_check(
        repo_root=repo,
        runtime_root=runtime,
        remote_name=remote_name,
        run_cmd=run_cmd,
        now_fn=now_fn,
    )
    target = _version_text(target_version or str(check.get("latest_version") or ""))
    tag = _tag_text(target) if target else None
    operations: list[dict[str, Any]] = []
    status_base = {
        "schema_version": 1,
        "operation": "upgrade",
        "repo_root": str(repo_link),
        "runtime_root": str(runtime),
        "releases_root": str(releases),
        "current_version": current_version,
        "target_version": target or None,
        "release_tag": tag,
        "repo_root_is_symlink": repo_root_is_symlink,
        "auto": bool(auto),
        "confirmed": bool(confirm),
        "allow_major": bool(allow_major),
        "updated_at": utc_now_iso(now_fn),
    }
    if not target:
        out = {**status_base, "ok": False, "status": "no_target_version", "changed": False, "operations": operations}
        write_upgrade_status(runtime_root=runtime, payload=out)
        return out
    cmp = compare_versions(current_version, target)
    if cmp == 0:
        out = {**status_base, "ok": True, "status": "already_current", "changed": False, "operations": operations}
        write_upgrade_status(runtime_root=runtime, payload=out)
        return out
    if cmp > 0:
        out = {**status_base, "ok": False, "status": "target_older_than_current", "changed": False, "operations": operations}
        write_upgrade_status(runtime_root=runtime, payload=out)
        return out
    if _major_upgrade_blocked(current_version=current_version, target_version=target, allow_major=allow_major):
        out = {**status_base, "ok": False, "status": "blocked_major_upgrade", "changed": False, "operations": operations}
        write_upgrade_status(runtime_root=runtime, payload=out)
        return out

    target_dir = releases / target
    previous_dir = repo
    warnings = [] if repo_root_is_symlink else ["confirmed upgrade requires repo_root to be a current symlink"]
    planned = [
        f"clone {tag} into {target_dir}" if not target_dir.exists() else f"reuse existing release dir {target_dir}",
        f"validate {target_dir}",
        f"switch {repo_link} -> {target_dir}",
        "restart long-running services" if restart_services else "skip service restart",
    ]
    if not confirm:
        return {
            **status_base,
            "ok": True,
            "status": "dry_run",
            "changed": False,
            "target_dir": str(target_dir),
            "previous_dir": str(previous_dir),
            "warnings": warnings,
            "planned_operations": planned,
            "version_check": check,
            "operations": operations,
        }

    lock_path = runtime / "locks" / "upgrade.lock"
    try:
        with _UpgradeLock(lock_path):
            releases.mkdir(parents=True, exist_ok=True)
            remote_url = _remote_url(repo_root=repo, remote_name=remote_name, run_cmd=run_cmd)
            if not target_dir.exists():
                _run_required(
                    ["git", "clone", "--depth", "1", "--branch", str(tag), remote_url, str(target_dir)],
                    cwd=None,
                    run_cmd=run_cmd,
                    operations=operations,
                    timeout=600,
                )
            _run_required(
                ["python3", "scripts/release_check.py", "--tag", str(tag)],
                cwd=target_dir,
                run_cmd=run_cmd,
                operations=operations,
                timeout=120,
            )
            _run_required(
                ["./om-agent", "spec"],
                cwd=target_dir,
                run_cmd=run_cmd,
                operations=operations,
                timeout=120,
            )
            _switch_current_symlink(current_link=repo_link, target_dir=target_dir)
            restarted = (
                _restart_services_from_profile(runtime_root=runtime, run_cmd=run_cmd, operations=operations)
                if restart_services
                else []
            )
    except Exception as exc:
        out = {
            **status_base,
            "ok": False,
            "status": "failed",
            "changed": False,
            "target_dir": str(target_dir),
            "previous_dir": str(previous_dir),
            "error": f"{type(exc).__name__}: {exc}",
            "operations": operations,
        }
        write_upgrade_status(runtime_root=runtime, payload=out)
        return out

    out = {
        **status_base,
        "ok": True,
        "status": "upgraded",
        "changed": True,
        "target_dir": str(target_dir),
        "previous_dir": str(previous_dir),
        "restarted_services": restarted,
        "operations": operations,
    }
    write_upgrade_status(runtime_root=runtime, payload=out)
    return out


def service_rollback(
    *,
    repo_root: str | Path,
    runtime_root: str | Path,
    releases_root: str | Path | None = None,
    to_version: str | None = None,
    confirm: bool = False,
    restart_services: bool = True,
    run_cmd: Callable[..., Any] = subprocess.run,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    repo_link = Path(repo_root).expanduser()
    repo = repo_link.resolve()
    runtime = Path(runtime_root).expanduser().resolve()
    releases = Path(releases_root).expanduser().resolve() if releases_root else default_releases_root(repo_link)
    status = load_upgrade_status(runtime_root=runtime) or {}
    current_version = _read_version(repo)
    repo_root_is_symlink = repo_link.is_symlink()
    target = _version_text(to_version or str(status.get("current_version") or ""))
    operations: list[dict[str, Any]] = []
    status_base = {
        "schema_version": 1,
        "operation": "rollback",
        "repo_root": str(repo_link),
        "runtime_root": str(runtime),
        "releases_root": str(releases),
        "current_version": current_version,
        "target_version": target or None,
        "repo_root_is_symlink": repo_root_is_symlink,
        "confirmed": bool(confirm),
        "updated_at": utc_now_iso(now_fn),
    }
    if not target:
        out = {**status_base, "ok": False, "status": "no_rollback_target", "changed": False, "operations": operations}
        write_upgrade_status(runtime_root=runtime, payload=out)
        return out
    target_dir = releases / target
    if not target_dir.exists():
        out = {
            **status_base,
            "ok": False,
            "status": "rollback_target_missing",
            "changed": False,
            "target_dir": str(target_dir),
            "operations": operations,
        }
        write_upgrade_status(runtime_root=runtime, payload=out)
        return out
    if not confirm:
        return {
            **status_base,
            "ok": True,
            "status": "dry_run",
            "changed": False,
            "target_dir": str(target_dir),
            "warnings": [] if repo_root_is_symlink else ["confirmed rollback requires repo_root to be a current symlink"],
            "planned_operations": [f"switch {repo_link} -> {target_dir}"],
            "operations": operations,
        }

    try:
        with _UpgradeLock(runtime / "locks" / "upgrade.lock"):
            _switch_current_symlink(current_link=repo_link, target_dir=target_dir)
            restarted = (
                _restart_services_from_profile(runtime_root=runtime, run_cmd=run_cmd, operations=operations)
                if restart_services
                else []
            )
    except Exception as exc:
        out = {
            **status_base,
            "ok": False,
            "status": "failed",
            "changed": False,
            "target_dir": str(target_dir),
            "error": f"{type(exc).__name__}: {exc}",
            "operations": operations,
        }
        write_upgrade_status(runtime_root=runtime, payload=out)
        return out
    out = {
        **status_base,
        "ok": True,
        "status": "rolled_back",
        "changed": True,
        "target_dir": str(target_dir),
        "restarted_services": restarted,
        "operations": operations,
    }
    write_upgrade_status(runtime_root=runtime, payload=out)
    return out


__all__ = [
    "default_releases_root",
    "load_upgrade_status",
    "service_rollback",
    "service_upgrade",
    "service_upgrade_check",
    "upgrade_status_path",
    "write_upgrade_status",
]
