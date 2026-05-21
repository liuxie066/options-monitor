from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cmp_to_key
from pathlib import Path
import re
import subprocess
from typing import Any, Callable


VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
TAG_RE = re.compile(r"^v(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$")


@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[tuple[int, Any], ...]


def checked_at(now_fn: Callable[[], datetime] | None = None) -> str:
    now = (now_fn or (lambda: datetime.now(timezone.utc)))()
    return now.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_version(value: str) -> SemVer:
    if not VERSION_RE.match(value):
        raise ValueError(f"invalid version: {value}")
    core, sep, prerelease = value.partition("-")
    major_s, minor_s, patch_s = core.split(".")
    return SemVer(
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


def parse_release_tags(stdout: str) -> list[tuple[str, str]]:
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


def select_latest_release(tags: list[tuple[str, str]]) -> tuple[str, str] | None:
    return tags[-1] if tags else None


def resolve_upgrade_target(
    *,
    current_version: str,
    repo_root: Path,
    cache_root: Path,
    remote_name: str = "origin",
    explicit_target: str | None = None,
    fetch: bool = True,
    run_cmd: Callable[..., Any] = subprocess.run,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    checked = checked_at(now_fn)
    cache_repo = cache_root / "git" / "options-monitor.git"
    try:
        parse_version(current_version)
    except Exception as exc:
        return _target_error(
            current_version=None,
            remote_name=remote_name,
            checked_at=checked,
            cache_repo=cache_repo,
            error=str(exc),
            message="版本检查失败：本地版本无效",
        )

    target = _version_text(explicit_target or "")
    if target:
        try:
            parse_version(target)
        except ValueError as exc:
            return _target_error(
                current_version=current_version,
                remote_name=remote_name,
                checked_at=checked,
                cache_repo=cache_repo,
                error=str(exc),
                message="版本检查失败：目标版本无效",
            )
        return _target_result(
            current_version=current_version,
            latest_version=target,
            release_tag=f"v{target}",
            remote_name=remote_name,
            checked_at=checked,
            cache_repo=cache_repo,
            target_source="explicit",
            cache_fetched=False,
            tag_count=0,
            highest_tag_seen=None,
            selected_tag=f"v{target}",
        )

    cache_fetched = False
    if fetch:
        ensure = _ensure_cache_fetched(
            repo_root=repo_root,
            cache_repo=cache_repo,
            remote_name=remote_name,
            run_cmd=run_cmd,
        )
        if not ensure["ok"]:
            return _target_error(
                current_version=current_version,
                remote_name=remote_name,
                checked_at=checked,
                cache_repo=cache_repo,
                error=str(ensure.get("error") or "failed to refresh upgrade git cache"),
                message="版本检查失败",
            )
        cache_fetched = bool(ensure.get("cache_fetched"))

    result = _run(
        ["git", f"--git-dir={cache_repo}", "for-each-ref", "--format=%(objectname) %(refname)", "refs/tags"],
        cwd=None,
        run_cmd=run_cmd,
        timeout=120,
    )
    if not result["ok"]:
        return _target_error(
            current_version=current_version,
            remote_name=remote_name,
            checked_at=checked,
            cache_repo=cache_repo,
            error=str(result.get("stderr") or result.get("stdout") or f"git tag listing failed for {cache_repo}").strip(),
            message="版本检查失败",
        )

    tags = parse_release_tags(str(result.get("stdout") or ""))
    latest = select_latest_release(tags)
    if latest is None:
        return _target_error(
            current_version=current_version,
            remote_name=remote_name,
            checked_at=checked,
            cache_repo=cache_repo,
            error="no valid release tags found in upgrade cache",
            message="未找到可用发布版本",
            cache_fetched=cache_fetched,
            tag_count=0,
        )

    latest_version, release_tag = latest
    return _target_result(
        current_version=current_version,
        latest_version=latest_version,
        release_tag=release_tag,
        remote_name=remote_name,
        checked_at=checked,
        cache_repo=cache_repo,
        target_source="latest_from_cache",
        cache_fetched=cache_fetched,
        tag_count=len(tags),
        highest_tag_seen=release_tag,
        selected_tag=release_tag,
    )


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


def _version_text(value: str) -> str:
    text = str(value or "").strip()
    return text[1:] if text.startswith("v") else text


def _target_result(
    *,
    current_version: str,
    latest_version: str,
    release_tag: str,
    remote_name: str,
    checked_at: str,
    cache_repo: Path,
    target_source: str,
    cache_fetched: bool,
    tag_count: int,
    highest_tag_seen: str | None,
    selected_tag: str | None,
) -> dict[str, Any]:
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
        "ok": True,
        "current_version": current_version,
        "latest_version": latest_version,
        "update_available": update_available,
        "remote_name": remote_name,
        "checked_at": checked_at,
        "release_tag": release_tag,
        "message": message,
        "error": None,
        "target_source": target_source,
        "cache_repo": str(cache_repo),
        "cache_fetched": bool(cache_fetched),
        "tag_count": int(tag_count),
        "highest_tag_seen": highest_tag_seen,
        "selected_tag": selected_tag,
    }


def _target_error(
    *,
    current_version: str | None,
    remote_name: str,
    checked_at: str,
    cache_repo: Path,
    error: str,
    message: str,
    cache_fetched: bool = False,
    tag_count: int = 0,
) -> dict[str, Any]:
    return {
        "ok": False,
        "current_version": current_version,
        "latest_version": None,
        "update_available": False,
        "remote_name": remote_name,
        "checked_at": checked_at,
        "release_tag": None,
        "message": message,
        "error": error,
        "target_source": None,
        "cache_repo": str(cache_repo),
        "cache_fetched": bool(cache_fetched),
        "tag_count": int(tag_count),
        "highest_tag_seen": None,
        "selected_tag": None,
    }


def _ensure_cache_fetched(
    *,
    repo_root: Path,
    cache_repo: Path,
    remote_name: str,
    run_cmd: Callable[..., Any],
) -> dict[str, Any]:
    cache_repo.parent.mkdir(parents=True, exist_ok=True)
    if cache_repo.exists():
        result = _run(
            ["git", f"--git-dir={cache_repo}", "fetch", "--tags", "--prune", remote_name],
            cwd=None,
            run_cmd=run_cmd,
            timeout=600,
        )
        if not result["ok"]:
            return {"ok": False, "cache_fetched": False, "error": result.get("stderr") or result.get("stdout")}
        return {"ok": True, "cache_fetched": True}

    remote_url = _repo_remote_url(repo_root=repo_root, remote_name=remote_name, run_cmd=run_cmd)
    if not remote_url:
        return {
            "ok": False,
            "cache_fetched": False,
            "error": f"upgrade git cache is missing and current release has no remote URL: {cache_repo}",
        }
    result = _run(
        ["git", "clone", "--mirror", remote_url, str(cache_repo)],
        cwd=None,
        run_cmd=run_cmd,
        timeout=600,
    )
    if not result["ok"]:
        return {"ok": False, "cache_fetched": False, "error": result.get("stderr") or result.get("stdout")}
    return {"ok": True, "cache_fetched": True}


def _repo_remote_url(*, repo_root: Path, remote_name: str, run_cmd: Callable[..., Any]) -> str | None:
    result = _run(
        ["git", "config", "--get", f"remote.{remote_name}.url"],
        cwd=repo_root,
        run_cmd=run_cmd,
        timeout=30,
    )
    if not result["ok"]:
        return None
    return str(result.get("stdout") or "").strip() or None


def _run(
    command: list[str],
    *,
    cwd: Path | None,
    run_cmd: Callable[..., Any],
    timeout: int,
) -> dict[str, Any]:
    try:
        proc = run_cmd(
            command,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        return {
            "ok": False,
            "returncode": int(exc.returncode),
            "stdout": str(exc.stdout or ""),
            "stderr": str(exc.stderr or ""),
        }
    except Exception as exc:
        return {"ok": False, "returncode": 1, "stdout": "", "stderr": str(exc)}
    rc = int(getattr(proc, "returncode", 1))
    return {
        "ok": rc == 0,
        "returncode": rc,
        "stdout": str(getattr(proc, "stdout", "") or ""),
        "stderr": str(getattr(proc, "stderr", "") or ""),
    }


__all__ = [
    "TAG_RE",
    "VERSION_RE",
    "SemVer",
    "checked_at",
    "compare_versions",
    "parse_release_tags",
    "parse_version",
    "resolve_upgrade_target",
    "select_latest_release",
]
