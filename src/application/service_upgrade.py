from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import shlex
import subprocess
import sys
import sysconfig
import time
from functools import cmp_to_key
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.application.service_drift import service_drift
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


def default_upgrade_cache_root(repo_root: Path) -> Path:
    configured = str(os.environ.get("OM_UPGRADE_CACHE_ROOT") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    repo = Path(repo_root).expanduser()
    return (repo.parent / "_cache").resolve()


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
    env: dict[str, str] | None = None,
    timeout: int = 300,
) -> dict[str, Any]:
    started_at = utc_now_iso()
    started = time.monotonic()
    kwargs: dict[str, Any] = {
        "cwd": (str(cwd) if cwd is not None else None),
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "check": False,
    }
    if env is not None:
        kwargs["env"] = env
    proc = run_cmd(
        command,
        **kwargs,
    )
    rc = int(getattr(proc, "returncode", 1))
    stdout = str(getattr(proc, "stdout", "") or "")
    stderr = str(getattr(proc, "stderr", "") or "")
    return {
        "command": command,
        "cwd": str(cwd) if cwd is not None else None,
        "started_at": started_at,
        "ended_at": utc_now_iso(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "returncode": rc,
        "stdout": stdout[-4000:],
        "stderr": stderr[-4000:],
        "ok": rc == 0,
        **({"env_overrides": sorted(set(env) - set(os.environ))} if env is not None else {}),
    }


def _run_required(
    command: list[str],
    *,
    cwd: Path | None,
    run_cmd: Callable[..., Any],
    operations: list[dict[str, Any]],
    env: dict[str, str] | None = None,
    timeout: int = 300,
) -> dict[str, Any]:
    result = _run_command(command, cwd=cwd, run_cmd=run_cmd, env=env, timeout=timeout)
    operations.append(result)
    if not result["ok"]:
        raise RuntimeError(f"command failed: {' '.join(shlex.quote(part) for part in command)}")
    return result


class ServiceRestartError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        remediation: list[str],
        failed_services: list[str] | None = None,
        restarted_services: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.remediation = remediation
        self.failed_services = failed_services or []
        self.restarted_services = restarted_services or []


class RuntimeConfigPrepareError(RuntimeError):
    def __init__(self, message: str, *, remediation: list[str]) -> None:
        super().__init__(message)
        self.remediation = remediation


class RuntimePrepareError(RuntimeError):
    def __init__(self, message: str, *, runtime_prepare: dict[str, Any]) -> None:
        super().__init__(message)
        self.runtime_prepare = runtime_prepare


def _load_service_profile(runtime_root: Path) -> dict[str, Any]:
    profile_path = runtime_root / "service.profile.json"
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return profile if isinstance(profile, dict) else {}


def _repo_root_symlink_candidates(*, repo_root: Path, runtime_root: Path) -> list[Path]:
    candidates: list[Path] = []
    profile = _load_service_profile(runtime_root)
    raw_profile_repo = str(profile.get("repo_root") or "").strip()
    if raw_profile_repo:
        candidates.append(Path(raw_profile_repo).expanduser())

    resolved = repo_root.resolve()
    search_roots = [resolved.parent]
    if resolved.parent.name == "releases":
        search_roots.append(resolved.parent.parent)
    for root in search_roots:
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_symlink():
                candidates.append(child)

    out: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            if candidate.is_symlink() and candidate.resolve() == resolved:
                out.append(candidate)
        except OSError:
            continue
    return out


def _coerce_repo_root_to_current_symlink(*, repo_root: str | Path, runtime_root: Path) -> tuple[Path, Path, dict[str, Any]]:
    requested = Path(repo_root).expanduser()
    if requested.is_symlink():
        return requested, requested.resolve(), {"source": "argument", "requested_repo_root": str(requested), "coerced": False}

    candidates = _repo_root_symlink_candidates(repo_root=requested, runtime_root=runtime_root)
    if candidates:
        selected = candidates[0]
        return selected, selected.resolve(), {
            "source": "runtime_profile_or_sibling_symlink",
            "requested_repo_root": str(requested),
            "selected_repo_root": str(selected),
            "coerced": True,
        }
    return requested, requested.resolve(), {"source": "argument", "requested_repo_root": str(requested), "coerced": False}


def _restart_profile(profile: dict[str, Any]) -> dict[str, Any]:
    raw = profile.get("restart")
    return raw if isinstance(raw, dict) else {}


def _is_root_process() -> bool:
    try:
        return os.geteuid() == 0
    except (AttributeError, OSError):
        return False


def _restart_command_policy(profile: dict[str, Any]) -> tuple[list[str], str]:
    restart = _restart_profile(profile)
    raw_prefix = restart.get("command_prefix")
    if isinstance(raw_prefix, list) and raw_prefix:
        prefix = [str(item).strip() for item in raw_prefix if str(item).strip()]
        if prefix:
            return prefix, "profile.command_prefix"
    raw_command = restart.get("restart_command") or profile.get("restart_command")
    if isinstance(raw_command, str) and raw_command.strip():
        parts = shlex.split(raw_command)
        if parts[-1:] == ["restart"]:
            parts = parts[:-1]
        if parts:
            return parts, "profile.restart_command"
    if isinstance(raw_command, list) and raw_command:
        parts = [str(item).strip() for item in raw_command if str(item).strip()]
        if parts[-1:] == ["restart"]:
            parts = parts[:-1]
        if parts:
            return parts, "profile.restart_command"
    requires_sudo = restart.get("requires_sudo")
    if bool(requires_sudo or profile.get("restart_requires_sudo")):
        return ["sudo", "-n", "systemctl"], "profile.requires_sudo"
    if requires_sudo is False:
        return ["systemctl"], "profile.requires_sudo_false"
    provider = str(profile.get("service_provider") or "").strip().lower()
    deploy_user = str(profile.get("deploy_user") or "").strip()
    if provider == "systemd" and (deploy_user and deploy_user != "root"):
        return ["sudo", "-n", "systemctl"], "deploy_user_sudo_fallback"
    if provider == "systemd" and not _is_root_process():
        return ["sudo", "-n", "systemctl"], "non_root_sudo_fallback"
    return ["systemctl"], "root_systemctl_default"


def _restart_command_prefix(profile: dict[str, Any]) -> list[str]:
    return _restart_command_policy(profile)[0]


def _restart_remediation(*, profile: dict[str, Any], service_names: list[str], command_by_service: dict[str, list[str]]) -> list[str]:
    deploy_user = str(profile.get("deploy_user") or "").strip()
    sudoers = _restart_profile(profile).get("sudoers")
    suggestions = [str(item) for item in sudoers] if isinstance(sudoers, list) else []
    if not suggestions and deploy_user:
        suggestions = [
            item
            for service_name in service_names
            for item in (
                f"{deploy_user} ALL=(root) NOPASSWD: /bin/systemctl restart {service_name}",
                f"{deploy_user} ALL=(root) NOPASSWD: /usr/bin/systemctl restart {service_name}",
            )
        ]
    remediation = [
        *(f"manual_restart: sudo systemctl restart {service_name}" for service_name in service_names),
        *(
            f"failed_command: {' '.join(shlex.quote(part) for part in command_by_service.get(service_name, []))}"
            for service_name in service_names
            if command_by_service.get(service_name)
        ),
    ]
    if suggestions:
        remediation.append("sudoers_minimal:")
        remediation.extend(suggestions)
    return remediation


def _restart_service_names(profile: dict[str, Any]) -> list[str]:
    restart = _restart_profile(profile)
    raw_restart_services = restart.get("services") or restart.get("restart_services") or profile.get("restart_services")
    explicit = isinstance(raw_restart_services, list) and bool(raw_restart_services)
    if isinstance(raw_restart_services, list) and raw_restart_services:
        names = [str(item.get("name") if isinstance(item, dict) else item or "").strip() for item in raw_restart_services]
    else:
        raw_services = profile.get("services")
        services: list[Any] = raw_services if isinstance(raw_services, list) else []
        names = [str(item.get("name") if isinstance(item, dict) else item or "").strip() for item in services]
    out: list[str] = []
    for name in names:
        if not name.endswith(".service"):
            continue
        if not explicit and "trade-intake" not in name and "feishu-ws" not in name:
            continue
        if name not in out:
            out.append(name)
    return out


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


def _cache_repo_path(cache_root: Path) -> Path:
    return cache_root / "git" / "options-monitor.git"


def _cache_remote_url(*, cache_root: Path, remote_name: str, run_cmd: Callable[..., Any]) -> str:
    cache_repo = _cache_repo_path(cache_root)
    if not cache_repo.exists():
        raise RuntimeError(f"upgrade git cache is missing: {cache_repo}")
    result = _run_command(
        ["git", f"--git-dir={cache_repo}", "config", "--get", f"remote.{remote_name}.url"],
        cwd=None,
        run_cmd=run_cmd,
        timeout=30,
    )
    if not result["ok"]:
        raise RuntimeError(f"failed to resolve cached remote URL for {remote_name}")
    url = str(result.get("stdout") or "").strip()
    if not url:
        raise RuntimeError(f"cached remote URL is empty for {remote_name}")
    return url


def _resolve_upgrade_remote_url(
    *,
    repo_root: Path,
    cache_root: Path,
    remote_name: str,
    run_cmd: Callable[..., Any],
) -> str:
    errors: list[str] = []
    try:
        return _remote_url(repo_root=repo_root, remote_name=remote_name, run_cmd=run_cmd)
    except Exception as exc:
        errors.append(f"current_release: {exc}")
    try:
        return _cache_remote_url(cache_root=cache_root, remote_name=remote_name, run_cmd=run_cmd)
    except Exception as exc:
        errors.append(f"upgrade_cache: {exc}")
    raise RuntimeError(
        "failed to resolve upgrade remote URL from current release or upgrade cache; "
        + "; ".join(errors)
    )


def _release_tags_from_ls_remote(stdout: str) -> list[tuple[str, str]]:
    found: dict[str, str] = {}
    for line in stdout.splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        ref = parts[1].strip()
        prefix = "refs/tags/"
        if not ref.startswith(prefix):
            continue
        tag = ref[len(prefix) :]
        if not tag.startswith("v"):
            continue
        version = tag[1:]
        try:
            parse_version(version)
        except ValueError:
            continue
        found[version] = tag
    return sorted(found.items(), key=cmp_to_key(lambda left, right: compare_versions(left[0], right[0])))


def _version_check_from_cache(
    *,
    repo_root: Path,
    cache_root: Path,
    remote_name: str,
    run_cmd: Callable[..., Any],
    now_fn: Callable[[], datetime] | None,
) -> dict[str, Any]:
    checked_at = utc_now_iso(now_fn)
    try:
        current_version = _read_version(repo_root)
        parse_version(current_version)
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
            "source": "upgrade_cache",
            "cache_repo": str(_cache_repo_path(cache_root)),
        }

    cache_repo = _cache_repo_path(cache_root)
    if not cache_repo.exists():
        return {
            "current_version": current_version,
            "latest_version": None,
            "update_available": False,
            "remote_name": remote_name,
            "checked_at": checked_at,
            "release_tag": None,
            "message": "版本检查失败",
            "ok": False,
            "error": f"upgrade git cache is missing: {cache_repo}",
            "source": "upgrade_cache",
            "cache_repo": str(cache_repo),
        }

    result = _run_command(
        ["git", f"--git-dir={cache_repo}", "ls-remote", "--tags", "--refs", remote_name],
        cwd=None,
        run_cmd=run_cmd,
        timeout=120,
    )
    if not result["ok"]:
        error = str(
            result.get("stderr")
            or result.get("stdout")
            or f"git ls-remote failed for remote {remote_name}"
        ).strip()
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
            "source": "upgrade_cache",
            "cache_repo": str(cache_repo),
        }

    tags = _release_tags_from_ls_remote(str(result.get("stdout") or ""))
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
            "source": "upgrade_cache",
            "cache_repo": str(cache_repo),
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
        "source": "upgrade_cache",
        "cache_repo": str(cache_repo),
    }


def _version_check_for_upgrade(
    *,
    repo_root: Path,
    cache_root: Path,
    remote_name: str,
    run_cmd: Callable[..., Any],
    now_fn: Callable[[], datetime] | None,
) -> dict[str, Any]:
    repo_check = check_version_update(base_dir=repo_root, remote_name=remote_name, run_cmd=run_cmd, now_fn=now_fn)
    if repo_check.get("ok"):
        return {**repo_check, "source": "current_release"}
    cache_check = _version_check_from_cache(
        repo_root=repo_root,
        cache_root=cache_root,
        remote_name=remote_name,
        run_cmd=run_cmd,
        now_fn=now_fn,
    )
    if cache_check.get("ok"):
        return {**cache_check, "fallback_from": "current_release", "current_release_error": repo_check.get("error")}
    return {**repo_check, "source": "current_release", "cache_version_check": cache_check}


def _release_materialize_summary(*, tag: str, target_dir: Path, cache_root: Path) -> dict[str, Any]:
    cache_repo = _cache_repo_path(cache_root)
    return {
        "method": "reuse_existing_release" if target_dir.exists() else "git_cache_archive",
        "cache_root": str(cache_root),
        "cache_repo": str(cache_repo),
        "target_dir": str(target_dir),
        "tag": tag,
        "cache_initialized": False,
        "fetched": False,
    }


def _materialize_release_from_git_cache(
    *,
    remote_url: str,
    tag: str,
    target_dir: Path,
    cache_root: Path,
    run_cmd: Callable[..., Any],
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    cache_repo = _cache_repo_path(cache_root)
    out = _release_materialize_summary(tag=tag, target_dir=target_dir, cache_root=cache_root)
    if target_dir.exists():
        return out

    cache_repo.parent.mkdir(parents=True, exist_ok=True)
    if cache_repo.exists():
        _run_required(
            ["git", f"--git-dir={cache_repo}", "fetch", "--tags", "--prune", "origin"],
            cwd=None,
            run_cmd=run_cmd,
            operations=operations,
            timeout=600,
        )
        out["fetched"] = True
    else:
        _run_required(
            ["git", "clone", "--mirror", remote_url, str(cache_repo)],
            cwd=None,
            run_cmd=run_cmd,
            operations=operations,
            timeout=600,
        )
        out["cache_initialized"] = True

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = target_dir.with_name(f".{target_dir.name}.archive-tmp")
    tar_path = target_dir.with_name(f".{target_dir.name}.tar")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    try:
        if tar_path.exists():
            tar_path.unlink()
        tmp_dir.mkdir(parents=True, exist_ok=False)
        _run_required(
            ["git", f"--git-dir={cache_repo}", "archive", "--format=tar", "-o", str(tar_path), tag],
            cwd=None,
            run_cmd=run_cmd,
            operations=operations,
            timeout=300,
        )
        _run_required(
            ["tar", "-xf", str(tar_path), "-C", str(tmp_dir)],
            cwd=None,
            run_cmd=run_cmd,
            operations=operations,
            timeout=300,
        )
        if not (tmp_dir / "VERSION").exists():
            raise RuntimeError(f"archived release is missing VERSION: {tag}")
        os.replace(tmp_dir, target_dir)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    finally:
        try:
            tar_path.unlink()
        except FileNotFoundError:
            pass
    return out


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
    profile = _load_service_profile(runtime_root)
    return _restart_services_from_loaded_profile(profile=profile, run_cmd=run_cmd, operations=operations)


def _restart_services_from_loaded_profile(
    *,
    profile: dict[str, Any],
    run_cmd: Callable[..., Any],
    operations: list[dict[str, Any]],
) -> list[str]:
    if not profile:
        return []
    provider = str(profile.get("service_provider") or "").strip().lower()
    restarted: list[str] = []
    if provider != "systemd":
        return restarted
    command_prefix, command_source = _restart_command_policy(profile)
    failed: list[str] = []
    command_by_service: dict[str, list[str]] = {}
    for name in _restart_service_names(profile):
        command = [*command_prefix, "restart", name]
        command_by_service[name] = command
        result = _run_command(command, cwd=None, run_cmd=run_cmd, timeout=60)
        result["command_source"] = command_source
        operations.append(result)
        if not result["ok"]:
            failed.append(name)
            continue
        restarted.append(name)
    if failed:
        raise ServiceRestartError(
            f"failed to restart services: {', '.join(failed)}",
            failed_services=failed,
            restarted_services=restarted,
            remediation=_restart_remediation(profile=profile, service_names=failed, command_by_service=command_by_service),
        )
    return restarted


def _post_upgrade_service_health(
    *,
    profile: dict[str, Any],
    repo_root: Path,
    run_cmd: Callable[..., Any],
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    if not profile:
        return {"ok": True, "status": "skipped", "reason": "service_profile_missing", "checks": [], "failed_checks": []}
    provider = str(profile.get("service_provider") or "").strip().lower()
    if provider != "systemd":
        return {"ok": True, "status": "skipped", "reason": f"unsupported_provider:{provider or 'missing'}", "checks": [], "failed_checks": []}

    services = _restart_service_names(profile)
    checks: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for service_name in services:
        for action in ("is-active", "is-enabled"):
            command = ["systemctl", action, service_name]
            result = _run_command(command, cwd=None, run_cmd=run_cmd, timeout=30)
            result["operation"] = "post_upgrade_service_health"
            result["check"] = action
            result["service"] = service_name
            operations.append(result)
            public = {
                "service": service_name,
                "check": action,
                "ok": bool(result.get("ok")),
                "stdout": str(result.get("stdout") or "").strip(),
                "stderr": str(result.get("stderr") or "").strip(),
            }
            checks.append(public)
            if not result.get("ok"):
                failed.append(public)

    if "options-monitor-feishu-ws.service" in services:
        command = _feishu_ws_check_command(profile=profile, repo_root=repo_root)
        env = _child_env_from_profile(profile)
        result = _run_command(command, cwd=repo_root, run_cmd=run_cmd, env=env, timeout=60)
        result["operation"] = "post_upgrade_service_health"
        result["check"] = "feishu-ws-check"
        result["service"] = "options-monitor-feishu-ws.service"
        operations.append(result)
        public = {
            "service": "options-monitor-feishu-ws.service",
            "check": "feishu-ws-check",
            "ok": bool(result.get("ok")),
            "stdout": str(result.get("stdout") or "").strip(),
            "stderr": str(result.get("stderr") or "").strip(),
        }
        checks.append(public)
        if not result.get("ok"):
            failed.append(public)

    return {
        "ok": not failed,
        "status": "ok" if not failed else "error",
        "provider": provider,
        "services": services,
        "checks": checks,
        "failed_checks": failed,
        "remediation": _service_health_remediation(failed),
    }


def _feishu_ws_config_key(profile: dict[str, Any]) -> str:
    feishu_ws = profile.get("feishu_ws")
    raw = feishu_ws.get("config_key") if isinstance(feishu_ws, dict) else None
    key = str(raw or "us").strip().lower()
    return key if key in {"us", "hk"} else "us"


def _feishu_ws_check_command(*, profile: dict[str, Any], repo_root: Path) -> list[str]:
    key = _feishu_ws_config_key(profile)
    command = [str(repo_root / "om"), "inbound", "feishu-ws", "--check", "--config-key", key]
    config_paths = profile.get("config_paths")
    if isinstance(config_paths, dict):
        config_path = str(config_paths.get(key) or "").strip()
        if config_path:
            command.extend(["--config-path", config_path])
    return command


def _child_env_from_profile(profile: dict[str, Any]) -> dict[str, str] | None:
    env: dict[str, str] = {}
    for key in ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            env[key] = value
    runtime_root = str(profile.get("runtime_root") or "").strip()
    if runtime_root:
        env["OM_RUNTIME_ROOT"] = runtime_root
    env["PYTHONUNBUFFERED"] = "1"
    env_file = str(profile.get("env_file") or "").strip()
    if env_file:
        env["OM_ENV_FILE"] = str(Path(env_file).expanduser())
    return env


def _service_health_remediation(failed_checks: list[dict[str, Any]]) -> list[str]:
    services = sorted({str(item.get("service") or "") for item in failed_checks if str(item.get("service") or "").strip()})
    remediation: list[str] = []
    for service_name in services:
        if service_name.endswith(".service"):
            remediation.append(f"manual_enable: sudo systemctl enable --now {service_name}")
            remediation.append(f"manual_restart: sudo systemctl restart {service_name}")
    if any(item.get("check") == "feishu-ws-check" for item in failed_checks):
        remediation.append("manual_check: source the env file, then run ./om inbound feishu-ws --check")
    return remediation


def _service_reconcile_failed(service_reconcile: dict[str, Any]) -> bool:
    if not service_reconcile:
        return False
    if service_reconcile.get("apply_errors"):
        return True
    summary_raw = service_reconcile.get("summary")
    summary = summary_raw if isinstance(summary_raw, dict) else {}
    return str(summary.get("status") or "").strip().lower() == "error"


def _service_reconcile_remediation(service_reconcile: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for item in service_reconcile.get("apply_errors") or []:
        out.append(f"service_reconcile_error: {item}")
    for item in service_reconcile.get("manual_actions") or []:
        out.append(str(item))
    return out


def _profile_runtime_config_targets(profile: dict[str, Any]) -> list[dict[str, str]]:
    raw_markets = profile.get("markets")
    markets = [str(item).strip().lower() for item in raw_markets] if isinstance(raw_markets, list) else []
    raw_config_paths = profile.get("config_paths")
    config_paths: dict[Any, Any] = raw_config_paths if isinstance(raw_config_paths, dict) else {}
    targets: list[dict[str, str]] = []
    for market in markets:
        if market not in {"us", "hk"}:
            continue
        path = str(config_paths.get(market) or "").strip()
        if path:
            targets.append({"market": market, "config_path": path})
    return targets


def _user_overlay_names(markets: list[str]) -> list[str]:
    return ["user.common.json", *(f"user.{market}.json" for market in markets if market in {"us", "hk"})]


def _safe_read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _resolve_runtime_metadata_path(raw: Any, *, repo_root: Path) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


def _metadata_overlay_paths(*, runtime_config_path: Path, repo_root: Path, market: str) -> dict[str, Path]:
    cfg = _safe_read_json_object(runtime_config_path)
    generated = cfg.get("_generated") if isinstance(cfg, dict) else None
    if not isinstance(generated, dict):
        return {}
    sources = generated.get("sources")
    if not isinstance(sources, list):
        return {}
    out: dict[str, Path] = {}
    for item in sources:
        if not isinstance(item, dict) or not bool(item.get("loaded")):
            continue
        role = str(item.get("role") or "").strip()
        if role == "common_user":
            name = "user.common.json"
        elif role == "market_user":
            name = f"user.{market}.json"
        else:
            continue
        path = _resolve_runtime_metadata_path(item.get("path"), repo_root=repo_root)
        if path is not None and path.exists():
            out[name] = path
    return out


def _release_version_for_sort(path: Path) -> str:
    version_path = path / "VERSION"
    if version_path.exists():
        try:
            return version_path.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return path.name


def _compare_release_dirs_desc(left: Path, right: Path) -> int:
    left_version = _release_version_for_sort(left)
    right_version = _release_version_for_sort(right)
    try:
        return -compare_versions(left_version, right_version)
    except Exception:
        if left.name == right.name:
            return 0
        return -1 if left.name > right.name else 1


def _complete_release_overlay_dirs(*, releases_root: Path, names: list[str], exclude_dirs: set[Path]) -> list[Path]:
    if not releases_root.exists():
        return []
    candidates = [
        path
        for path in releases_root.iterdir()
        if path.is_dir() and path.resolve() not in exclude_dirs
    ]
    out: list[Path] = []
    for release_dir in sorted(candidates, key=cmp_to_key(_compare_release_dirs_desc)):
        configs = release_dir / "configs"
        if all((configs / name).exists() for name in names):
            out.append(configs)
    return out


def _overlay_source_candidates(
    *,
    previous_dir: Path,
    target_dir: Path,
    runtime_root: Path,
    releases_root: Path,
    targets: list[dict[str, str]],
    markets: list[str],
    names: list[str],
) -> dict[str, list[Path]]:
    candidates: dict[str, list[Path]] = {name: [] for name in names}

    def add(name: str, path: Path) -> None:
        if name not in candidates or not path.exists() or path.resolve() == (target_dir / "configs" / name).resolve():
            return
        resolved = path.resolve()
        if resolved not in {item.resolve() for item in candidates[name]}:
            candidates[name].append(path)

    runtime_configs_by_market = {item["market"]: Path(item["config_path"]).expanduser() for item in targets}
    for market in markets:
        config_path = runtime_configs_by_market.get(market)
        if config_path is None:
            continue
        for name, source in _metadata_overlay_paths(
            runtime_config_path=config_path,
            repo_root=previous_dir,
            market=market,
        ).items():
            add(name, source)

    runtime_configs = runtime_root / "configs"
    for name in names:
        add(name, runtime_configs / name)
        add(name, previous_dir / "configs" / name)

    exclude_dirs = {target_dir.resolve()}
    for configs in _complete_release_overlay_dirs(releases_root=releases_root, names=names, exclude_dirs=exclude_dirs):
        for name in names:
            add(name, configs / name)

    return candidates


def _migrate_user_overlay_configs(
    *,
    previous_dir: Path,
    target_dir: Path,
    runtime_root: Path,
    releases_root: Path,
    targets: list[dict[str, str]],
    markets: list[str],
) -> list[dict[str, str]]:
    target_configs = target_dir / "configs"
    target_configs.mkdir(parents=True, exist_ok=True)
    names = _user_overlay_names(markets)
    candidates = _overlay_source_candidates(
        previous_dir=previous_dir,
        target_dir=target_dir,
        runtime_root=runtime_root,
        releases_root=releases_root,
        targets=targets,
        markets=markets,
        names=names,
    )
    out: list[dict[str, str]] = []
    for name in names:
        target = target_configs / name
        if target.exists():
            out.append({"name": name, "status": "exists", "source": str(target), "target": str(target)})
            continue
        source = candidates[name][0] if candidates[name] else None
        if source is not None:
            shutil.copy2(source, target)
            out.append({"name": name, "status": "copied", "source": str(source), "target": str(target)})
            continue
        out.append({"name": name, "status": "missing_source", "source": "", "target": str(target)})
    return out


def _missing_user_overlay_configs(*, target_dir: Path, markets: list[str]) -> list[Path]:
    return [
        target_dir / "configs" / name
        for name in _user_overlay_names(markets)
        if not (target_dir / "configs" / name).exists()
    ]


PRESERVED_RUNTIME_CONFIG_PATHS = (
    ("inbound", "feishu_ws", "ack_reaction"),
)


def _nested_get(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = payload
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _nested_set(payload: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = payload
    for key in path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[path[-1]] = value


def _preserve_runtime_config_hotfixes(*, target_dir: Path, targets: list[dict[str, str]]) -> list[dict[str, str]]:
    overlay_path = target_dir / "configs" / "user.common.json"
    overlay = _safe_read_json_object(overlay_path) or {}
    preserved: list[dict[str, str]] = []
    changed = False

    for target in targets:
        config_path = Path(target["config_path"]).expanduser()
        runtime_cfg = _safe_read_json_object(config_path)
        if not runtime_cfg:
            continue
        for path in PRESERVED_RUNTIME_CONFIG_PATHS:
            current_value = _nested_get(runtime_cfg, path)
            overlay_value = _nested_get(overlay, path)
            if current_value in (None, "") or overlay_value not in (None, ""):
                continue
            _nested_set(overlay, path, current_value)
            changed = True
            preserved.append(
                {
                    "path": ".".join(path),
                    "value": str(current_value),
                    "source": str(config_path),
                    "target": str(overlay_path),
                    "reason": "runtime_config_hotfix_preserved",
                }
            )

    if changed:
        overlay_path.write_text(json.dumps(overlay, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return preserved


def _runtime_config_remediation(*, runtime_root: Path, target_dir: Path, missing: list[Path]) -> list[str]:
    names = " ".join(path.name for path in missing)
    return [
        f"restore_user_overlays: copy {names} into {target_dir / 'configs'}",
        f"preferred_runtime_overlays: mkdir -p {runtime_root / 'configs'} && copy user.common.json/user.hk.json/user.us.json there before upgrade",
        "fallback_release_search: restore the missing files from the newest known-good release under the releases directory",
    ]


def _rebuild_and_validate_runtime_configs(
    *,
    targets: list[dict[str, str]],
    cwd: Path,
    run_cmd: Callable[..., Any],
    operations: list[dict[str, Any]],
    phase: str,
) -> list[dict[str, str]]:
    rebuilt: list[dict[str, str]] = []
    for item in targets:
        market = item["market"]
        config_path = item["config_path"]
        try:
            _run_required(
                ["./om", "config", "build", "--market", market, "--output", config_path],
                cwd=cwd,
                run_cmd=run_cmd,
                operations=operations,
                timeout=120,
            )
            _run_required(
                ["./om", "config", "validate", "--config-path", config_path, "--market", market],
                cwd=cwd,
                run_cmd=run_cmd,
                operations=operations,
                timeout=120,
            )
        except RuntimeError as exc:
            raise RuntimeConfigPrepareError(
                f"failed to {phase} rebuild/validate runtime config for {market}: {config_path}",
                remediation=[
                    f"manual_rebuild: cd {cwd} && ./om config build --market {market} --output {config_path}",
                    f"manual_validate: cd {cwd} && ./om config validate --config-path {config_path} --market {market}",
                    f"inspect_last_operation: {exc}",
                ],
            ) from exc
        rebuilt.append({"market": market, "config_path": config_path, "phase": phase})
    return rebuilt


def _prepare_runtime_configs_for_release(
    *,
    previous_dir: Path,
    target_dir: Path,
    runtime_root: Path,
    releases_root: Path,
    run_cmd: Callable[..., Any],
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    profile = _load_service_profile(runtime_root)
    targets = _profile_runtime_config_targets(profile)
    if not targets:
        return {"status": "skipped", "reason": "service profile has no runtime config targets"}

    markets = [item["market"] for item in targets]
    overlays = _migrate_user_overlay_configs(
        previous_dir=previous_dir,
        target_dir=target_dir,
        runtime_root=runtime_root,
        releases_root=releases_root,
        targets=targets,
        markets=markets,
    )
    missing = _missing_user_overlay_configs(target_dir=target_dir, markets=markets)
    if missing:
        raise RuntimeConfigPrepareError(
            "release is missing required market user config overlays",
            remediation=_runtime_config_remediation(runtime_root=runtime_root, target_dir=target_dir, missing=missing),
        )
    preserved_hotfixes = _preserve_runtime_config_hotfixes(target_dir=target_dir, targets=targets)

    rebuilt = _rebuild_and_validate_runtime_configs(
        targets=targets,
        cwd=target_dir,
        run_cmd=run_cmd,
        operations=operations,
        phase="pre_switch",
    )

    return {"status": "prepared", "targets": targets, "overlays": overlays, "preserved_hotfixes": preserved_hotfixes, "rebuilt": rebuilt}


def _upgrade_installer_mode() -> str:
    mode = str(os.environ.get("OM_UPGRADE_INSTALLER") or "auto").strip().lower()
    return mode if mode in {"auto", "uv", "pip"} else "auto"


def _runtime_install_env(*, cache_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    uv_cache = cache_root / "uv"
    pip_cache = cache_root / "pip"
    env.setdefault("UV_CACHE_DIR", str(uv_cache))
    env.setdefault("PIP_CACHE_DIR", str(pip_cache))
    pip_index = str(env.get("PIP_INDEX_URL") or "").strip()
    if pip_index and not str(env.get("UV_INDEX_URL") or "").strip():
        env["UV_INDEX_URL"] = pip_index
    return env


def _command_error(result: dict[str, Any]) -> str:
    stderr = str(result.get("stderr") or "").strip()
    stdout = str(result.get("stdout") or "").strip()
    command = " ".join(str(part) for part in result.get("command") or [])
    detail = stderr or stdout or f"returncode={result.get('returncode')}"
    return f"{command}: {detail}" if command else detail


def _pip_install_commands(venv_python: Path) -> list[tuple[list[str], int]]:
    return [
        ([str(venv_python), "-m", "pip", "install", "-U", "pip"], 600),
        ([str(venv_python), "-m", "pip", "install", "-r", "requirements.txt", "-c", "constraints.txt"], 1200),
    ]


def _uv_install_commands(venv_python: Path, *, venv_dir: Path, include_server: bool) -> list[tuple[list[str], int]]:
    commands: list[tuple[list[str], int]] = [
        (["uv", "venv", "--python", "python3", str(venv_dir)], 300),
        (["uv", "pip", "install", "-p", str(venv_python), "-r", "requirements.txt", "-c", "constraints.txt"], 1200),
    ]
    if include_server:
        commands.append(
            (
                [
                    "uv",
                    "pip",
                    "install",
                    "-p",
                    str(venv_python),
                    "-r",
                    "requirements/server.txt",
                    "-c",
                    "constraints/server.txt",
                ],
                1200,
            )
        )
    return commands


def _run_runtime_install_commands(
    *,
    commands: list[tuple[list[str], int]],
    cwd: Path,
    run_cmd: Callable[..., Any],
    operations: list[dict[str, Any]],
    installer: str,
    env: dict[str, str] | None = None,
) -> None:
    for command, timeout in commands:
        result = _run_command(command, cwd=cwd, run_cmd=run_cmd, env=env, timeout=timeout)
        result["runtime_prepare_installer"] = installer
        operations.append(result)
        if not result["ok"]:
            raise RuntimeError(_command_error(result))


def _run_pip_runtime_prepare(
    *,
    target_dir: Path,
    venv_dir: Path,
    venv_python: Path,
    include_server: bool,
    run_cmd: Callable[..., Any],
    operations: list[dict[str, Any]],
    commands: list[list[str]],
    env: dict[str, str],
) -> None:
    if not venv_python.exists():
        command = ["python3", "-m", "venv", str(venv_dir)]
        _run_required(command, cwd=target_dir, run_cmd=run_cmd, operations=operations, env=env, timeout=300)
        operations[-1]["runtime_prepare_installer"] = "pip"
        commands.append(command)
    pip_commands = _pip_install_commands(venv_python)
    if include_server:
        pip_commands.append(
            (
                [
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    "requirements/server.txt",
                    "-c",
                    "constraints/server.txt",
                ],
                1200,
            )
        )
    _run_runtime_install_commands(
        commands=pip_commands,
        cwd=target_dir,
        run_cmd=run_cmd,
        operations=operations,
        installer="pip",
        env=env,
    )
    commands.extend(command for command, _timeout in pip_commands)


def _check_uv_available(*, target_dir: Path, run_cmd: Callable[..., Any], operations: list[dict[str, Any]]) -> bool:
    result = _run_command(["sh", "-lc", "command -v uv"], cwd=target_dir, run_cmd=run_cmd, timeout=30)
    result["runtime_prepare_installer_check"] = "uv"
    operations.append(result)
    return bool(result.get("ok"))


def _release_python(target_dir: Path) -> Path:
    return target_dir / ".venv" / "bin" / "python"


def _venv_python(venv_dir: Path) -> Path:
    return venv_dir / "bin" / "python"


def _dependency_context(*, include_server: bool, python_spec: str, installer_mode: str) -> dict[str, Any]:
    return {
        "include_server": bool(include_server),
        "installer_mode": installer_mode,
        "python_implementation": sys.implementation.name,
        "python_spec": python_spec,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "platform": sysconfig.get_platform(),
        "platform_machine": platform.machine(),
        "platform_system": platform.system(),
    }


def _dependency_hash(
    target_dir: Path,
    *,
    include_server: bool,
    python_spec: str = "python3",
    installer_mode: str = "auto",
) -> str:
    digest = hashlib.sha256()
    context = _dependency_context(
        include_server=include_server,
        python_spec=python_spec,
        installer_mode=installer_mode,
    )
    digest.update(json.dumps(context, sort_keys=True).encode("utf-8"))
    digest.update(b"\n")
    for path in _dependency_files(target_dir, include_server=include_server):
        try:
            rel = path.relative_to(target_dir)
            label = rel.as_posix()
        except ValueError:
            label = str(path)
        digest.update(f"path:{label}\n".encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except FileNotFoundError:
            digest.update(b"__missing__")
        digest.update(b"\n")
    return digest.hexdigest()[:16]


def _dependency_files(target_dir: Path, *, include_server: bool) -> list[Path]:
    roots = [target_dir / "requirements.txt", target_dir / "constraints.txt"]
    if include_server:
        roots.extend(
            [
                target_dir / "requirements" / "server.txt",
                target_dir / "constraints" / "server.txt",
            ]
        )
    seen: set[Path] = set()
    ordered: list[Path] = []

    def visit(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        ordered.append(path)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        for ref in _requirement_refs(path, text):
            visit(ref)

    for root in roots:
        visit(root)
    return ordered


def _requirement_refs(path: Path, text: str) -> list[Path]:
    refs: list[Path] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token in {"-r", "--requirement", "-c", "--constraint"} and i + 1 < len(tokens):
                refs.append((path.parent / tokens[i + 1]).resolve())
                i += 2
                continue
            if token.startswith("-r") and len(token) > 2:
                refs.append((path.parent / token[2:]).resolve())
            elif token.startswith("-c") and len(token) > 2:
                refs.append((path.parent / token[2:]).resolve())
            elif token.startswith("--requirement="):
                refs.append((path.parent / token.split("=", 1)[1]).resolve())
            elif token.startswith("--constraint="):
                refs.append((path.parent / token.split("=", 1)[1]).resolve())
            i += 1
    return refs


def _shared_venv_path(cache_root: Path, dependency_hash: str) -> Path:
    return cache_root / "venvs" / dependency_hash


def _shared_venv_build_path(shared_venv: Path) -> Path:
    return shared_venv.with_name(f".{shared_venv.name}.tmp.{os.getpid()}")


def _shared_venv_marker(venv_dir: Path) -> Path:
    return venv_dir / ".options-monitor-deps-complete"


def _shared_venv_valid(venv_dir: Path) -> bool:
    python = _venv_python(venv_dir)
    return _shared_venv_marker(venv_dir).exists() and python.exists() and os.access(python, os.X_OK)


def _link_release_venv(*, target_dir: Path, shared_venv: Path) -> None:
    release_venv = target_dir / ".venv"
    if release_venv.is_symlink() and release_venv.resolve() == shared_venv.resolve():
        return
    if release_venv.is_symlink() or release_venv.exists():
        if release_venv.is_dir() and not release_venv.is_symlink():
            shutil.rmtree(release_venv)
        else:
            release_venv.unlink()
    release_venv.symlink_to(shared_venv, target_is_directory=True)


def _ensure_release_runtime(
    *,
    target_dir: Path,
    cache_root: Path | None = None,
    run_cmd: Callable[..., Any],
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    cache = cache_root or default_upgrade_cache_root(target_dir)
    release_venv = target_dir / ".venv"
    release_python = _release_python(target_dir)
    server_requirements = target_dir / "requirements" / "server.txt"
    server_constraints = target_dir / "constraints" / "server.txt"
    include_server = server_requirements.exists() and server_constraints.exists()
    mode = _upgrade_installer_mode()
    python_spec = "python3"
    dependency_hash = _dependency_hash(
        target_dir,
        include_server=include_server,
        python_spec=python_spec,
        installer_mode=mode,
    )
    dependency_context = _dependency_context(include_server=include_server, python_spec=python_spec, installer_mode=mode)
    shared_venv = _shared_venv_path(cache, dependency_hash)
    build_venv = _shared_venv_build_path(shared_venv)
    build_python = _venv_python(build_venv)
    install_env = _runtime_install_env(cache_root=cache)
    commands: list[list[str]] = []
    started_at = utc_now_iso()
    started = time.monotonic()
    runtime_prepare: dict[str, Any] = {
        "installer": "pip",
        "mode": mode,
        "fallback": False,
        "venv_strategy": "dependency_hash_cache",
        "venv_reused": False,
        "venv_path": str(release_venv),
        "python": str(release_python),
        "dependency_hash": dependency_hash,
        "dependency_context": dependency_context,
        "shared_venv_path": str(shared_venv),
        "shared_venv_build_path": str(build_venv),
        "python_spec": python_spec,
        "cache_root": str(cache),
        "uv_cache_dir": install_env.get("UV_CACHE_DIR"),
        "pip_cache_dir": install_env.get("PIP_CACHE_DIR"),
        "commands": commands,
        "started_at": started_at,
    }

    if _shared_venv_valid(shared_venv):
        _link_release_venv(target_dir=target_dir, shared_venv=shared_venv)
        runtime_prepare["installer"] = "cache"
        runtime_prepare["venv_reused"] = True
    else:
        if shared_venv.exists():
            shutil.rmtree(shared_venv)
        if build_venv.exists():
            shutil.rmtree(build_venv)
        shared_venv.parent.mkdir(parents=True, exist_ok=True)

    try:
        uv_available = (
            _check_uv_available(target_dir=target_dir, run_cmd=run_cmd, operations=operations)
            if not runtime_prepare["venv_reused"] and mode in {"auto", "uv"}
            else False
        )
        use_uv = mode == "uv" and uv_available or mode == "auto" and uv_available
        if runtime_prepare["venv_reused"]:
            pass
        elif use_uv:
            runtime_prepare["installer"] = "uv"
            uv_commands = _uv_install_commands(
                build_python,
                venv_dir=build_venv,
                include_server=include_server,
            )
            commands.extend(command for command, _timeout in uv_commands)
            try:
                _run_runtime_install_commands(
                    commands=uv_commands,
                    cwd=target_dir,
                    run_cmd=run_cmd,
                    operations=operations,
                    installer="uv",
                    env=install_env,
                )
                if not build_python.exists():
                    raise RuntimeError(f"uv did not create shared virtualenv python: {build_python}")
            except RuntimeError as exc:
                runtime_prepare["uv_error"] = str(exc)
                if mode == "uv":
                    raise
                shutil.rmtree(build_venv, ignore_errors=True)
                runtime_prepare["installer"] = "pip"
                runtime_prepare["fallback"] = True
                runtime_prepare["fallback_from"] = "uv"
                _run_pip_runtime_prepare(
                    target_dir=target_dir,
                    venv_dir=build_venv,
                    venv_python=build_python,
                    include_server=include_server,
                    run_cmd=run_cmd,
                    operations=operations,
                    commands=commands,
                    env=install_env,
                )
        else:
            if mode == "uv":
                runtime_prepare["installer"] = "uv"
                runtime_prepare["uv_error"] = "uv is not available on PATH"
                raise RuntimeError("uv is not available on PATH")
            _run_pip_runtime_prepare(
                target_dir=target_dir,
                venv_dir=build_venv,
                venv_python=build_python,
                include_server=include_server,
                run_cmd=run_cmd,
                operations=operations,
                commands=commands,
                env=install_env,
            )

        if not runtime_prepare["venv_reused"]:
            _shared_venv_marker(build_venv).write_text(utc_now_iso() + "\n", encoding="utf-8")
            build_venv.rename(shared_venv)
            _link_release_venv(target_dir=target_dir, shared_venv=shared_venv)

        if not release_python.exists() or not os.access(release_python, os.X_OK):
            raise RuntimeError(f"release virtualenv python is missing after setup: {release_python}")
        _run_required(
            [
                str(release_python),
                "-c",
                "from pathlib import Path; import sys; assert Path(sys.executable).exists(); import src.application.multi_account_tick",
            ],
            cwd=target_dir,
            run_cmd=run_cmd,
            operations=operations,
            timeout=120,
        )
        commands.append(
            [
                str(release_python),
                "-c",
                "from pathlib import Path; import sys; assert Path(sys.executable).exists(); import src.application.multi_account_tick",
            ]
        )
        runtime_prepare["ended_at"] = utc_now_iso()
        runtime_prepare["duration_seconds"] = round(time.monotonic() - started, 3)
        return runtime_prepare
    except RuntimeError as exc:
        if not runtime_prepare["venv_reused"]:
            shutil.rmtree(build_venv, ignore_errors=True)
        runtime_prepare["ended_at"] = utc_now_iso()
        runtime_prepare["duration_seconds"] = round(time.monotonic() - started, 3)
        raise RuntimePrepareError(str(exc), runtime_prepare=runtime_prepare) from exc


def service_upgrade_check(
    *,
    repo_root: str | Path,
    runtime_root: str | Path,
    cache_root: str | Path | None = None,
    remote_name: str = "origin",
    run_cmd: Callable[..., Any] = subprocess.run,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    runtime = Path(runtime_root).expanduser().resolve()
    repo_link, repo, repo_root_resolution = _coerce_repo_root_to_current_symlink(repo_root=repo_root, runtime_root=runtime)
    cache = Path(cache_root).expanduser().resolve() if cache_root else default_upgrade_cache_root(repo_link)
    version = _version_check_for_upgrade(
        repo_root=repo,
        cache_root=cache,
        remote_name=remote_name,
        run_cmd=run_cmd,
        now_fn=now_fn,
    )
    status = load_upgrade_status(runtime_root=runtime)
    return {
        "ok": bool(version.get("ok")),
        "repo_root": str(repo_link),
        "repo_root_resolved": str(repo),
        "repo_root_resolution": repo_root_resolution,
        "runtime_root": str(runtime),
        "upgrade_cache_root": str(cache),
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
    cache_root: str | Path | None = None,
    target_version: str | None = None,
    remote_name: str = "origin",
    confirm: bool = False,
    auto: bool = False,
    allow_major: bool = False,
    restart_services: bool = True,
    cleanup_after_upgrade: bool = False,
    cleanup_keep_releases: int = 2,
    run_cmd: Callable[..., Any] = subprocess.run,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    runtime = Path(runtime_root).expanduser().resolve()
    repo_link, repo, repo_root_resolution = _coerce_repo_root_to_current_symlink(repo_root=repo_root, runtime_root=runtime)
    releases = Path(releases_root).expanduser().resolve() if releases_root else default_releases_root(repo_link)
    cache = Path(cache_root).expanduser().resolve() if cache_root else default_upgrade_cache_root(repo_link)
    current_version = _read_version(repo)
    repo_root_is_symlink = repo_link.is_symlink()
    check = service_upgrade_check(
        repo_root=repo,
        runtime_root=runtime,
        cache_root=cache,
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
        "upgrade_cache_root": str(cache),
        "current_version": current_version,
        "target_version": target or None,
        "release_tag": tag,
        "repo_root_is_symlink": repo_root_is_symlink,
        "repo_root_resolution": repo_root_resolution,
        "auto": bool(auto),
        "confirmed": bool(confirm),
        "allow_major": bool(allow_major),
        "cleanup_after_upgrade": bool(cleanup_after_upgrade),
        "cleanup_keep_releases": max(2, int(cleanup_keep_releases or 2)),
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
        f"materialize {tag} into {target_dir} from git cache {cache / 'git' / 'options-monitor.git'}"
        if not target_dir.exists()
        else f"reuse existing release dir {target_dir}",
        f"prepare release runtime at {target_dir / '.venv'}",
        f"validate {target_dir}",
        f"switch {repo_link} -> {target_dir}",
        "reconcile service drift from current release",
        "restart long-running services" if restart_services else "skip service restart",
    ]
    if cleanup_after_upgrade:
        planned.append(f"cleanup old releases after successful upgrade, keep {status_base['cleanup_keep_releases']} releases")
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
    if not repo_root_is_symlink:
        out = {
            **status_base,
            "ok": False,
            "status": "repo_root_not_symlink",
            "changed": False,
            "target_dir": str(target_dir),
            "previous_dir": str(previous_dir),
            "warnings": warnings,
            "reason": "repo_root must be the current symlink path for confirmed upgrade",
            "operations": operations,
        }
        write_upgrade_status(runtime_root=runtime, payload=out)
        return out

    lock_path = runtime / "locks" / "upgrade.lock"
    symlink_switched = False
    release_materialize = _release_materialize_summary(tag=str(tag), target_dir=target_dir, cache_root=cache)
    runtime_prepare: dict[str, Any] = {}
    runtime_config_prepare: dict[str, Any] = {}
    post_switch_runtime_config_validate: list[dict[str, Any]] = []
    service_reconcile: dict[str, Any] = {}
    service_health: dict[str, Any] = {}
    pre_upgrade_profile = _load_service_profile(runtime)
    try:
        with _UpgradeLock(lock_path):
            releases.mkdir(parents=True, exist_ok=True)
            remote_url = (
                ""
                if target_dir.exists()
                else _resolve_upgrade_remote_url(
                    repo_root=repo,
                    cache_root=cache,
                    remote_name=remote_name,
                    run_cmd=run_cmd,
                )
            )
            release_materialize = _materialize_release_from_git_cache(
                remote_url=remote_url,
                tag=str(tag),
                target_dir=target_dir,
                cache_root=cache,
                run_cmd=run_cmd,
                operations=operations,
            )
            write_upgrade_status(
                runtime_root=runtime,
                payload={
                    **status_base,
                    "ok": True,
                    "status": "runtime_preparing",
                    "changed": False,
                    "symlink_switched": False,
                    "target_dir": str(target_dir),
                    "previous_dir": str(previous_dir),
                    "release_materialize": release_materialize,
                    "operations": operations,
                },
            )
            runtime_prepare = _ensure_release_runtime(target_dir=target_dir, cache_root=cache, run_cmd=run_cmd, operations=operations)
            write_upgrade_status(
                runtime_root=runtime,
                payload={
                    **status_base,
                    "ok": True,
                    "status": "runtime_prepared",
                    "changed": False,
                    "symlink_switched": False,
                    "target_dir": str(target_dir),
                    "previous_dir": str(previous_dir),
                    "release_materialize": release_materialize,
                    "runtime_prepare": runtime_prepare,
                    "operations": operations,
                },
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
            runtime_config_prepare = _prepare_runtime_configs_for_release(
                previous_dir=previous_dir,
                target_dir=target_dir,
                runtime_root=runtime,
                releases_root=releases,
                run_cmd=run_cmd,
                operations=operations,
            )
            _switch_current_symlink(current_link=repo_link, target_dir=target_dir)
            symlink_switched = True
            post_switch_runtime_config_validate = (
                _rebuild_and_validate_runtime_configs(
                    targets=runtime_config_prepare.get("targets", []),
                    cwd=repo_link,
                    run_cmd=run_cmd,
                    operations=operations,
                    phase="post_switch",
                )
                if runtime_config_prepare.get("status") == "prepared"
                else []
            )
            if pre_upgrade_profile:
                service_reconcile = service_drift(
                    repo_root=repo_link,
                    runtime_root=runtime,
                    profile_path=runtime / "service.profile.json",
                    profile=pre_upgrade_profile,
                    confirm=True,
                    run_cmd=run_cmd,
                )
            restart_profile = _load_service_profile(runtime) or pre_upgrade_profile
            restarted = (
                _restart_services_from_loaded_profile(profile=restart_profile, run_cmd=run_cmd, operations=operations)
                if restart_services
                else []
            )
            service_health = (
                _post_upgrade_service_health(
                    profile=restart_profile,
                    repo_root=repo_link,
                    run_cmd=run_cmd,
                    operations=operations,
                )
                if restart_services
                else {"ok": True, "status": "skipped", "reason": "service_restart_disabled", "checks": [], "failed_checks": []}
            )
    except ServiceRestartError as exc:
        out = {
            **status_base,
            "ok": True,
            "status": "upgraded_restart_failed",
            "changed": bool(symlink_switched),
            "symlink_switched": bool(symlink_switched),
            "config_rebuilt": bool(runtime_config_prepare.get("status") == "prepared"),
            "target_dir": str(target_dir),
            "previous_dir": str(previous_dir),
            "release_materialize": release_materialize,
            "runtime_prepare": runtime_prepare,
            "runtime_config_prepare": runtime_config_prepare,
            "post_switch_runtime_config_validate": post_switch_runtime_config_validate,
            "service_reconcile": service_reconcile,
            "service_health": service_health,
            "restarted_services": exc.restarted_services,
            "restart_failed_services": exc.failed_services,
            "manual_remediation": exc.remediation,
            "remediation": exc.remediation,
            "error": f"{type(exc).__name__}: {exc}",
            "operations": operations,
        }
        write_upgrade_status(runtime_root=runtime, payload=out)
        return out
    except Exception as exc:
        out = {
            **status_base,
            "ok": False,
            "status": "failed",
            "changed": bool(symlink_switched),
            "symlink_switched": bool(symlink_switched),
            "config_rebuilt": bool(runtime_config_prepare.get("status") == "prepared"),
            "target_dir": str(target_dir),
            "previous_dir": str(previous_dir),
            "release_materialize": release_materialize,
            "runtime_prepare": exc.runtime_prepare if isinstance(exc, RuntimePrepareError) else runtime_prepare,
            "error": f"{type(exc).__name__}: {exc}",
            **({"remediation": exc.remediation} if isinstance(exc, RuntimeConfigPrepareError) else {}),
            "operations": operations,
        }
        write_upgrade_status(runtime_root=runtime, payload=out)
        return out

    if _service_reconcile_failed(service_reconcile):
        out = {
            **status_base,
            "ok": True,
            "status": "upgraded_service_reconcile_failed",
            "changed": bool(symlink_switched),
            "symlink_switched": bool(symlink_switched),
            "config_rebuilt": bool(runtime_config_prepare.get("status") == "prepared"),
            "target_dir": str(target_dir),
            "previous_dir": str(previous_dir),
            "release_materialize": release_materialize,
            "runtime_prepare": runtime_prepare,
            "runtime_config_prepare": runtime_config_prepare,
            "post_switch_runtime_config_validate": post_switch_runtime_config_validate,
            "service_reconcile": service_reconcile,
            "service_health": service_health,
            "restarted_services": restarted,
            "manual_remediation": _service_reconcile_remediation(service_reconcile),
            "remediation": _service_reconcile_remediation(service_reconcile),
            "operations": operations,
        }
        write_upgrade_status(runtime_root=runtime, payload=out)
        return out

    if service_health and not bool(service_health.get("ok", True)):
        out = {
            **status_base,
            "ok": True,
            "status": "upgraded_service_health_failed",
            "changed": bool(symlink_switched),
            "symlink_switched": bool(symlink_switched),
            "config_rebuilt": bool(runtime_config_prepare.get("status") == "prepared"),
            "target_dir": str(target_dir),
            "previous_dir": str(previous_dir),
            "release_materialize": release_materialize,
            "runtime_prepare": runtime_prepare,
            "runtime_config_prepare": runtime_config_prepare,
            "post_switch_runtime_config_validate": post_switch_runtime_config_validate,
            "service_reconcile": service_reconcile,
            "service_health": service_health,
            "restarted_services": restarted,
            "manual_remediation": service_health.get("remediation") or [],
            "remediation": service_health.get("remediation") or [],
            "operations": operations,
        }
        write_upgrade_status(runtime_root=runtime, payload=out)
        return out

    cleanup_result: dict[str, Any] | None = None
    if cleanup_after_upgrade:
        if not symlink_switched or runtime_config_prepare.get("status") != "prepared":
            cleanup_result = {
                "ok": True,
                "status": "skipped",
                "changed": False,
                "reason": "cleanup-after-upgrade requires symlink switch and prepared runtime configs",
                "symlink_switched": bool(symlink_switched),
                "runtime_config_status": runtime_config_prepare.get("status"),
            }
        else:
            from src.application.service_cleanup import service_cleanup

            cleanup_plan = service_cleanup(
                repo_root=repo_link,
                releases_root=releases,
                keep_releases=max(2, int(cleanup_keep_releases or 2)),
                cleanup_downloads=True,
                cleanup_pip_cache=False,
                include_apt_cache=False,
                journal_vacuum_size=None,
                confirm=False,
                run_cmd=run_cmd,
            )
            if not cleanup_plan.get("ok"):
                cleanup_result = {
                    **cleanup_plan,
                    "status": "skipped",
                    "changed": False,
                    "reason": "cleanup-after-upgrade could not confirm active release",
                }
            elif len(cleanup_plan.get("kept_releases", [])) < max(2, int(cleanup_keep_releases or 2)):
                cleanup_result = {
                    **cleanup_plan,
                    "status": "skipped",
                    "changed": False,
                    "reason": "cleanup-after-upgrade requires at least keep_releases retained releases",
                }
            else:
                cleanup_result = service_cleanup(
                    repo_root=repo_link,
                    releases_root=releases,
                    keep_releases=max(2, int(cleanup_keep_releases or 2)),
                    cleanup_downloads=True,
                    cleanup_pip_cache=False,
                    include_apt_cache=False,
                    journal_vacuum_size=None,
                    confirm=True,
                    run_cmd=run_cmd,
                )

    out = {
        **status_base,
        "ok": True,
        "status": "upgraded",
        "changed": True,
        "target_dir": str(target_dir),
        "previous_dir": str(previous_dir),
        "symlink_switched": True,
        "config_rebuilt": bool(runtime_config_prepare.get("status") == "prepared"),
        "release_materialize": release_materialize,
        "runtime_prepare": runtime_prepare,
        "runtime_config_prepare": runtime_config_prepare,
        "post_switch_runtime_config_validate": post_switch_runtime_config_validate,
        "service_reconcile": service_reconcile,
        "service_health": service_health,
        "restarted_services": restarted,
        **({"post_upgrade_cleanup": cleanup_result} if cleanup_result is not None else {}),
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
    runtime = Path(runtime_root).expanduser().resolve()
    repo_link, repo, repo_root_resolution = _coerce_repo_root_to_current_symlink(repo_root=repo_root, runtime_root=runtime)
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
        "repo_root_resolution": repo_root_resolution,
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

    symlink_switched = False
    try:
        with _UpgradeLock(runtime / "locks" / "upgrade.lock"):
            _switch_current_symlink(current_link=repo_link, target_dir=target_dir)
            symlink_switched = True
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
            "changed": bool(symlink_switched),
            "symlink_switched": bool(symlink_switched),
            "target_dir": str(target_dir),
            "error": f"{type(exc).__name__}: {exc}",
            **({"remediation": exc.remediation} if isinstance(exc, ServiceRestartError) else {}),
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
    "default_upgrade_cache_root",
    "load_upgrade_status",
    "service_rollback",
    "service_upgrade",
    "service_upgrade_check",
    "upgrade_status_path",
    "write_upgrade_status",
]
