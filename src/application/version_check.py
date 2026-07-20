from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import subprocess
from typing import Any

from src.application.release_target import (
    BUMP_KINDS,
    VERSION_RE,
    bump_version as _bump_version,
    compare_versions,
    parse_release_tags,
)
from src.application.release_version_recommendation import (
    SCHEMA_VERSION as RECOMMENDATION_SCHEMA_VERSION,
    recommend_release_version,
)


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
    return _bump_version(current_version, bump)


def update_local_version(
    *,
    base_dir: Path | None = None,
    target_version: str | None = None,
    bump: str | None = None,
    apply: bool = False,
    allow_downgrade: bool = False,
    remote_name: str = "origin",
    recommendation_digest: str | None = None,
    expected_base_version: str | None = None,
    expected_target_version: str | None = None,
    recommendation_fn=None,
    now_fn=None,
) -> dict[str, Any]:
    base = (base_dir or repo_base()).resolve()
    explicit_target = str(target_version or "").strip()
    explicit_bump = str(bump or "").strip().lower()
    if explicit_target and explicit_bump:
        raise ValueError("provide either target_version or bump, not both")
    if explicit_bump == "auto":
        return _update_local_version_auto(
            base=base,
            apply=apply,
            remote_name=remote_name,
            recommendation_digest=recommendation_digest,
            expected_base_version=expected_base_version,
            expected_target_version=expected_target_version,
            recommendation_fn=recommendation_fn or recommend_release_version,
        )

    version_path = (base / "VERSION").resolve()
    current_version = _read_current_version(base)
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
        _write_version_atomic(version_path, next_version)

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


def _update_local_version_auto(
    *,
    base: Path,
    apply: bool,
    remote_name: str,
    recommendation_digest: str | None,
    expected_base_version: str | None,
    expected_target_version: str | None,
    recommendation_fn,
) -> dict[str, Any]:
    version_path = (base / "VERSION").resolve()
    current_version = _read_current_version(base)
    expected_base = str(expected_base_version or "").strip()
    expected_target = str(expected_target_version or "").strip()
    expected_digest = str(recommendation_digest or "").strip()

    if not apply:
        return recommendation_fn(base_dir=base, remote_name=remote_name)

    missing = [
        name
        for name, value in (
            ("recommendation_digest", expected_digest),
            ("expected_base_version", expected_base),
            ("expected_target_version", expected_target),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"auto apply requires: {', '.join(missing)}")
    if not VERSION_RE.match(expected_base) or not VERSION_RE.match(expected_target):
        raise ValueError("expected base and target versions must be valid semver values")

    if current_version == expected_target:
        return {
            "schema_version": RECOMMENDATION_SCHEMA_VERSION,
            "status": "already_at_target",
            "mode": "applied",
            "reason_code": None,
            "message": f"VERSION already at expected target {expected_target}; no write performed",
            "expected": {"base_version": expected_base, "target_version": expected_target},
            "current_version": current_version,
            "review_flags": [],
            "write": {
                "changed": False,
                "already_at_target": True,
                "version_path": str(version_path),
            },
        }
    if current_version != expected_base:
        return _stale_result(
            message=f"current VERSION {current_version} no longer matches expected base {expected_base}",
            expected_base=expected_base,
            expected_target=expected_target,
            current_version=current_version,
        )

    recomputed = recommendation_fn(base_dir=base, remote_name=remote_name)
    if recomputed.get("status") != "recommended":
        return recomputed
    recommendation = recomputed.get("recommendation") or {}
    actual_base = str((recomputed.get("base") or {}).get("version") or "")
    actual_target = str(recommendation.get("target_version") or "")
    actual_digest = str(recomputed.get("recommendation_digest") or "")
    if actual_base != expected_base or actual_target != expected_target or actual_digest != expected_digest:
        return _stale_result(
            message="release version recommendation changed after preview",
            expected_base=expected_base,
            expected_target=expected_target,
            current_version=current_version,
            recomputed={
                "base_version": actual_base or None,
                "target_version": actual_target or None,
                "recommendation_digest": actual_digest or None,
            },
        )

    _write_version_atomic(version_path, expected_target)
    applied = dict(recomputed)
    applied.update(
        {
            "status": "applied",
            "mode": "applied",
            "message": f"VERSION updated from {expected_base} to {expected_target}",
            "write": {
                "changed": True,
                "already_at_target": False,
                "version_path": str(version_path),
            },
        }
    )
    return applied


def _stale_result(
    *,
    message: str,
    expected_base: str,
    expected_target: str,
    current_version: str,
    recomputed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": RECOMMENDATION_SCHEMA_VERSION,
        "status": "stale",
        "mode": "dry_run",
        "reason_code": "RECOMMENDATION_STALE",
        "message": message,
        "expected": {
            "base_version": expected_base,
            "target_version": expected_target,
        },
        "recomputed": dict(recomputed or {}),
        "current_version": current_version,
        "review_flags": [],
        "write": {"changed": False, "already_at_target": False},
    }


def _write_version_atomic(version_path: Path, version: str) -> None:
    tmp_path = version_path.with_name(f"{version_path.name}.tmp")
    tmp_path.write_text(version + "\n", encoding="utf-8")
    tmp_path.replace(version_path)


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
