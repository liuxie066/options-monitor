from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cmp_to_key
from pathlib import Path
import re
import subprocess
from typing import Any


VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
TAG_RE = re.compile(r"^v(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$")


@dataclass(frozen=True)
class _SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[tuple[int, Any], ...]


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


def _parse_prerelease(value: str) -> tuple[tuple[int, Any], ...]:
    if not value:
        return ()
    parts: list[tuple[int, Any]] = []
    for token in value.split("."):
        if token.isdigit():
            parts.append((0, int(token)))
        else:
            parts.append((1, token))
    return tuple(parts)


def parse_version(value: str) -> _SemVer:
    if not VERSION_RE.match(value):
        raise ValueError(f"invalid version: {value}")
    core, sep, prerelease = value.partition("-")
    major_s, minor_s, patch_s = core.split(".")
    return _SemVer(
        major=int(major_s),
        minor=int(minor_s),
        patch=int(patch_s),
        prerelease=_parse_prerelease(prerelease if sep else ""),
    )


def compare_versions(left: str, right: str) -> int:
    a = parse_version(left)
    b = parse_version(right)
    if (a.major, a.minor, a.patch) != (b.major, b.minor, b.patch):
        return -1 if (a.major, a.minor, a.patch) < (b.major, b.minor, b.patch) else 1
    if not a.prerelease and not b.prerelease:
        return 0
    if not a.prerelease:
        return 1
    if not b.prerelease:
        return -1
    for ai, bi in zip(a.prerelease, b.prerelease):
        if ai == bi:
            continue
        if ai[0] != bi[0]:
            return -1 if ai[0] < bi[0] else 1
        return -1 if ai[1] < bi[1] else 1
    if len(a.prerelease) == len(b.prerelease):
        return 0
    return -1 if len(a.prerelease) < len(b.prerelease) else 1


def _extract_release_tags(stdout: str) -> list[tuple[str, str]]:
    found: dict[str, str] = {}
    for line in stdout.splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        ref = parts[1].strip()
        prefix = "refs/tags/"
        if not ref.startswith(prefix):
            continue
        tag = ref[len(prefix):]
        match = TAG_RE.match(tag)
        if not match:
            continue
        version = match.group("version")
        found[version] = tag
    return sorted(found.items(), key=cmp_to_key(lambda left, right: compare_versions(left[0], right[0])))


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

    tags = _extract_release_tags(str(proc.stdout or ""))
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
        message = f"当前已是最新版本 {current_version}"
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
