from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import subprocess
from typing import Any

from src.application.release_target import VERSION_RE, compare_versions, parse_release_tags, parse_version

BUMP_KINDS = {"major", "minor", "patch"}


def repo_base() -> Path:
    return Path(__file__).resolve().parents[2]


def _checked_at(now_fn=None) -> str:
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    return now_fn().astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_current_version(base_dir: Path) -> str:
    value = (base_dir / "VERSION").read_text(encoding="utf-8").strip()
    if not VERSION_RE.match(value):
        raise ValueError(f"invalid VERSION format: {value}")
    return value


def bump_version(current_version: str, bump: str = "patch") -> str:
    parsed = parse_version(current_version)
    kind = str(bump or "patch").strip().lower()
    if kind not in BUMP_KINDS:
        raise ValueError(f"bump must be one of: {', '.join(sorted(BUMP_KINDS))}")
    if kind == "major":
        return f"{parsed.major + 1}.0.0"
    if kind == "minor":
        return f"{parsed.major}.{parsed.minor + 1}.0"
    return f"{parsed.major}.{parsed.minor}.{parsed.patch + 1}"


def update_local_version(
    *,
    base_dir: Path | None = None,
    target_version: str | None = None,
    bump: str | None = None,
    apply: bool = False,
    allow_downgrade: bool = False,
    now_fn=None,
) -> dict[str, Any]:
    base = (base_dir or repo_base()).resolve()
    version_path = (base / "VERSION").resolve()
    current_version = _read_current_version(base)
    explicit_target = str(target_version or "").strip()
    explicit_bump = str(bump or "").strip().lower()
    if explicit_target and explicit_bump:
        raise ValueError("provide either target_version or bump, not both")
    if explicit_target:
        if not VERSION_RE.match(explicit_target):
            raise ValueError(f"invalid target version: {explicit_target}")
        next_version = explicit_target
    else:
        next_version = bump_version(current_version, explicit_bump or "patch")

    cmp = compare_versions(current_version, next_version)
    if cmp > 0 and not allow_downgrade:
        raise ValueError(f"target version {next_version} is lower than current VERSION {current_version}")

    changed = current_version != next_version
    if apply and changed:
        tmp_path = version_path.with_name(f"{version_path.name}.tmp")
        tmp_path.write_text(next_version + "\n", encoding="utf-8")
        tmp_path.replace(version_path)

    mode = "applied" if apply else "dry_run"
    if not changed:
        message = f"VERSION already at {current_version}"
    elif apply:
        message = f"VERSION updated from {current_version} to {next_version}"
    else:
        message = f"VERSION would update from {current_version} to {next_version}"

    return {
        "ok": True,
        "mode": mode,
        "current_version": current_version,
        "target_version": next_version,
        "changed": bool(changed and apply),
        "would_change": bool(changed),
        "allow_downgrade": bool(allow_downgrade),
        "version_path": str(version_path),
        "updated_at": _checked_at(now_fn),
        "message": message,
    }


def check_version_update(
    *,
    base_dir: Path | None = None,
    remote_name: str = "origin",
    run_cmd=None,
    now_fn=None,
) -> dict[str, Any]:
    base = (base_dir or repo_base()).resolve()
    checked_at = _checked_at(now_fn)
    try:
        current_version = _read_current_version(base)
    except Exception as exc:
        return {
            "current_version": None,
            "latest_version": None,
            "update_available": False,
            "remote_name": remote_name,
            "checked_at": checked_at,
            "release_tag": None,
            "message": "版本检查失败：本地版本无效",
            "ok": False,
            "error": str(exc),
        }

    run_cmd = run_cmd or subprocess.run
    try:
        proc = run_cmd(
            ["git", "ls-remote", "--tags", "--refs", remote_name],
            cwd=str(base),
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = str(exc.stderr or exc.stdout or exc).strip()
        error = stderr or f"git ls-remote failed for remote {remote_name}"
        return {
            "current_version": current_version,
            "latest_version": None,
            "update_available": False,
            "remote_name": remote_name,
            "checked_at": checked_at,
            "release_tag": None,
            "message": "版本检查失败",
            "ok": False,
            "error": error,
        }
    except Exception as exc:
        return {
            "current_version": current_version,
            "latest_version": None,
            "update_available": False,
            "remote_name": remote_name,
            "checked_at": checked_at,
            "release_tag": None,
            "message": "版本检查失败",
            "ok": False,
            "error": str(exc),
        }

    tags = parse_release_tags(str(proc.stdout or ""))
    if not tags:
        return {
            "current_version": current_version,
            "latest_version": None,
            "update_available": False,
            "remote_name": remote_name,
            "checked_at": checked_at,
            "release_tag": None,
            "message": "未找到可用发布版本",
            "ok": False,
            "error": "no valid release tags found on remote",
        }

    latest_version, release_tag = tags[-1]
    cmp = compare_versions(current_version, latest_version)
    if cmp < 0:
        message = f"发现新版本 {latest_version}，当前 {current_version}"
        update_available = True
    elif cmp == 0:
        message = f"没有可升级版本。当前已是最新版本 {current_version}"
        update_available = False
    else:
        message = f"当前版本 {current_version} 高于远端最新版本 {latest_version}"
        update_available = False

    return {
        "current_version": current_version,
        "latest_version": latest_version,
        "update_available": update_available,
        "remote_name": remote_name,
        "checked_at": checked_at,
        "release_tag": release_tag,
        "message": message,
        "ok": True,
        "error": None,
    }
