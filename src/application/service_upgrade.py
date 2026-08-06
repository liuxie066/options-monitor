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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.application.release_target import compare_versions, parse_version, resolve_upgrade_target
from src.application.runtime_config_freshness import (
    GENERATED_KEY,
    check_runtime_config_freshness,
    check_runtime_config_identity,
)
from src.application.service_drift import service_drift

_CHILD_ENV_PASSTHROUGH_NAMES = {
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "MOONSHOT_API_KEY",
    "KIMI_API_KEY",
    "OPENAI_API_KEY",
}


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
        self.fd = self._open_lock()
        os.write(self.fd, str(os.getpid()).encode("utf-8"))
        return self

    def _open_lock(self) -> int:
        try:
            return os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            if not _upgrade_lock_is_stale(self.path):
                raise RuntimeError(f"upgrade lock already exists: {self.path}") from exc
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except OSError as unlink_exc:
                raise RuntimeError(f"failed to remove stale upgrade lock: {self.path}") from unlink_exc
            try:
                return os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError as retry_exc:
                raise RuntimeError(f"upgrade lock already exists: {self.path}") from retry_exc

    def __exit__(self, *_exc: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _upgrade_lock_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _upgrade_lock_is_stale(path: Path) -> bool:
    pid = _upgrade_lock_pid(path)
    return pid is None or not _pid_is_running(pid)


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


class ServiceTransitionError(RuntimeError):
    def __init__(self, message: str, *, status: str, remediation: list[str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.remediation = remediation or []


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
        if (
            not explicit
            and "opend" not in name
            and "trade-intake" not in name
            and "feishu-ws" not in name
            and "wechat-clawbot" not in name
        ):
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


def _version_check_for_upgrade(
    *,
    repo_root: Path,
    cache_root: Path,
    target_version: str | None = None,
    remote_name: str,
    run_cmd: Callable[..., Any],
    now_fn: Callable[[], datetime] | None,
) -> dict[str, Any]:
    try:
        current_version = _read_version(repo_root)
    except Exception as exc:
        return {
            "current_version": None,
            "latest_version": None,
            "update_available": False,
            "remote_name": remote_name,
            "checked_at": utc_now_iso(now_fn),
            "release_tag": None,
            "message": "版本检查失败：本地版本无效",
            "ok": False,
            "error": str(exc),
            "source": "release_target",
        }
    resolved = resolve_upgrade_target(
        current_version=current_version,
        repo_root=repo_root,
        cache_root=cache_root,
        explicit_target=target_version,
        remote_name=remote_name,
        run_cmd=run_cmd,
        now_fn=now_fn,
    )
    return {**resolved, "source": resolved.get("target_source") or "release_target"}


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

    if "options-monitor-wechat-clawbot.service" in services:
        command = _wechat_clawbot_check_command(profile=profile, repo_root=repo_root)
        env = _child_env_from_profile(profile)
        result = _run_command(command, cwd=repo_root, run_cmd=run_cmd, env=env, timeout=60)
        result["operation"] = "post_upgrade_service_health"
        result["check"] = "wechat-clawbot-check"
        result["service"] = "options-monitor-wechat-clawbot.service"
        operations.append(result)
        public = {
            "service": "options-monitor-wechat-clawbot.service",
            "check": "wechat-clawbot-check",
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
        "remediation": _service_health_remediation(failed, profile=profile, repo_root=repo_root),
    }


def _feishu_ws_config_key(profile: dict[str, Any]) -> str:
    feishu_ws = profile.get("feishu_ws")
    raw = feishu_ws.get("config_key") if isinstance(feishu_ws, dict) else None
    key = str(raw or "").strip().lower()
    if key in {"us", "hk"}:
        return key
    config_paths = profile.get("config_paths")
    if isinstance(config_paths, dict):
        markets = [str(item).strip().lower() for item in config_paths if str(item).strip().lower() in {"us", "hk"}]
        if len(markets) == 1:
            return markets[0]
    return ""


def _feishu_ws_check_command(*, profile: dict[str, Any], repo_root: Path) -> list[str]:
    key = _feishu_ws_config_key(profile)
    command = [str(repo_root / "om"), "inbound", "feishu-ws", "--check"]
    if key:
        command.extend(["--config-key", key])
    config_paths = profile.get("config_paths")
    if isinstance(config_paths, dict):
        config_path = str(config_paths.get(key) or "").strip()
        if config_path:
            command.extend(["--config-path", config_path])
    assistant_config_path = _assistant_config_path_from_profile(profile=profile)
    if assistant_config_path:
        command.extend(["--assistant-config", assistant_config_path])
    env_file = str(profile.get("env_file") or "").strip()
    if env_file:
        command.extend(["--env-file", str(Path(env_file).expanduser())])
    if _feishu_ws_check_needs_sudo_for_env_file(profile):
        return ["sudo", "-n", *command]
    return command


def _wechat_clawbot_check_command(*, profile: dict[str, Any], repo_root: Path) -> list[str]:
    wechat = profile.get("wechat_clawbot")
    payload = wechat if isinstance(wechat, dict) else {}
    command = [str(repo_root / "om"), "channel", "wechat-clawbot", "serve", "--check"]
    label = str(payload.get("label") or "").strip()
    if label:
        command.extend(["--label", label])
    state_dir = str(payload.get("state_dir") or "").strip()
    if state_dir:
        command.extend(["--state-dir", state_dir])
    config_key = str(payload.get("config_key") or "").strip()
    if config_key:
        command.extend(["--config-key", config_key])
    config_paths = profile.get("config_paths")
    if isinstance(config_paths, dict):
        config_path = str(config_paths.get(config_key) or "").strip()
        if config_path:
            command.extend(["--config-path", config_path])
    assistant_config_path = str(payload.get("assistant_config_path") or "").strip()
    if assistant_config_path:
        command.extend(["--assistant-config", assistant_config_path])
    audit_db = str(payload.get("audit_db") or "").strip()
    if audit_db:
        command.extend(["--audit-db", audit_db])
    allowed_senders = str(payload.get("allowed_senders") or "").strip()
    if allowed_senders:
        command.extend(["--allowed-senders", allowed_senders])
    return command


def _feishu_ws_check_needs_sudo_for_env_file(profile: dict[str, Any]) -> bool:
    if _is_root_process():
        return False
    env_file = str(profile.get("env_file") or "").strip()
    if not env_file:
        return False
    try:
        return not os.access(str(Path(env_file).expanduser()), os.R_OK)
    except OSError:
        return True


def _assistant_config_path_from_profile(*, profile: dict[str, Any], runtime_root: Path | None = None) -> str:
    raw = str(profile.get("assistant_config_path") or "").strip()
    if raw:
        return raw
    feishu_ws = profile.get("feishu_ws")
    if isinstance(feishu_ws, dict):
        raw = str(feishu_ws.get("assistant_config_path") or "").strip()
        if raw:
            return raw
    authoring = profile.get("config_authoring")
    if isinstance(authoring, dict) and str(authoring.get("source") or "").strip().lower() == "yaml":
        root = runtime_root
        if root is None and str(profile.get("runtime_root") or "").strip():
            root = Path(str(profile["runtime_root"])).expanduser()
        if root is not None:
            return str(root / "resolved" / "config.assistant.json")
    return ""


def _child_env_from_profile(profile: dict[str, Any]) -> dict[str, str] | None:
    env: dict[str, str] = {}
    for key in ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            env[key] = value
    for key, value in os.environ.items():
        if not (key.startswith("OM_") or key in _CHILD_ENV_PASSTHROUGH_NAMES):
            continue
        text = str(value or "").strip()
        if text:
            env[key] = text
    runtime_root = str(profile.get("runtime_root") or "").strip()
    if runtime_root:
        env["OM_RUNTIME_ROOT"] = runtime_root
    env["PYTHONUNBUFFERED"] = "1"
    env_file = str(profile.get("env_file") or "").strip()
    if env_file:
        env["OM_ENV_FILE"] = str(Path(env_file).expanduser())
    return env


def _service_health_remediation(
    failed_checks: list[dict[str, Any]],
    *,
    profile: dict[str, Any] | None = None,
    repo_root: Path | None = None,
) -> list[str]:
    services = sorted({str(item.get("service") or "") for item in failed_checks if str(item.get("service") or "").strip()})
    remediation: list[str] = []
    for service_name in services:
        if service_name.endswith(".service"):
            remediation.append(f"manual_enable: sudo systemctl enable --now {service_name}")
            remediation.append(f"manual_restart: sudo systemctl restart {service_name}")
    if any(item.get("check") == "feishu-ws-check" for item in failed_checks):
        env_file = str((profile or {}).get("env_file") or "").strip()
        if env_file:
            remediation.append(f"manual_check: sudo -n ./om inbound feishu-ws --check --env-file {shlex.quote(env_file)}")
        else:
            remediation.append("manual_check: source the env file, then run ./om inbound feishu-ws --check")
    if any(item.get("check") == "wechat-clawbot-check" for item in failed_checks):
        profile_payload = profile or {}
        root = repo_root
        if root is None:
            raw_root = str(profile_payload.get("repo_root") or "").strip()
            root = Path(raw_root).expanduser() if raw_root else Path(".")
        command = _wechat_clawbot_check_command(profile=profile_payload, repo_root=root)
        remediation.append("manual_check: " + " ".join(shlex.quote(str(part)) for part in command))
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
    raw_authoring = profile.get("config_authoring")
    authoring = raw_authoring if isinstance(raw_authoring, dict) else {}
    authoring_source = str(authoring.get("source") or "").strip().lower()
    authoring_yaml = str(authoring.get("config_yaml") or "").strip()
    raw_authoring_markets = authoring.get("markets")
    authoring_markets = {
        str(item).strip().lower()
        for item in raw_authoring_markets
    } if isinstance(raw_authoring_markets, list) else set(markets)
    raw_config_sources = profile.get("config_sources")
    config_sources: dict[Any, Any] = raw_config_sources if isinstance(raw_config_sources, dict) else {}
    targets: list[dict[str, str]] = []
    for market in markets:
        if market not in {"us", "hk"}:
            continue
        path = str(config_paths.get(market) or "").strip()
        if path:
            target = {"market": market, "config_path": path, "source": "missing_yaml"}
            raw_market_source = config_sources.get(market)
            market_source = raw_market_source if isinstance(raw_market_source, dict) else {}
            source = str(market_source.get("source") or "").strip().lower()
            config_yaml = str(market_source.get("config_yaml") or "").strip()
            if source == "yaml" and config_yaml:
                target["source"] = "yaml"
                target["config_yaml"] = config_yaml
            elif authoring_source == "yaml" and authoring_yaml and market in authoring_markets:
                target["source"] = "yaml"
                target["config_yaml"] = authoring_yaml
            targets.append(target)
    return targets


def _profile_assistant_config_target(profile: dict[str, Any], *, runtime_root: Path) -> dict[str, str] | None:
    raw_authoring = profile.get("config_authoring")
    authoring = raw_authoring if isinstance(raw_authoring, dict) else {}
    if str(authoring.get("source") or "").strip().lower() != "yaml":
        return None
    config_yaml = str(authoring.get("config_yaml") or "").strip()
    if not config_yaml:
        return None
    assistant_config_path = _assistant_config_path_from_profile(profile=profile, runtime_root=runtime_root)
    if not assistant_config_path:
        assistant_config_path = str(runtime_root / "resolved" / "config.assistant.json")
    return {
        "source": "yaml",
        "config_yaml": config_yaml,
        "config_path": assistant_config_path,
    }


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
        source = str(item.get("source") or "").strip().lower()
        config_yaml = str(item.get("config_yaml") or "").strip()
        if source != "yaml" or not config_yaml:
            raise RuntimeConfigPrepareError(
                f"missing YAML authoring source for runtime config target {market}: {config_path}",
                remediation=[
                    "rerender_service_profile: ./om service render ... --config-yaml <path>",
                    "migrate_legacy_json_once: ./om config migrate-yaml --apply --output config.yaml",
                ],
            )
        build_command = [
            "./om",
            "config",
            "build",
            "--source",
            "yaml",
            "--market",
            market,
            "--config-yaml",
            config_yaml,
            "--output",
            config_path,
        ]
        validate_command = ["./om", "config", "validate", "--config-path", config_path, "--market", market]
        manual_rebuild = (
            f"manual_rebuild: cd {cwd} && ./om config build --source yaml "
            f"--market {market} --config-yaml {config_yaml} --output {config_path}"
        )
        manual_validate = f"manual_validate: cd {cwd} && ./om config validate --config-path {config_path} --market {market}"
        try:
            _run_required(
                build_command,
                cwd=cwd,
                run_cmd=run_cmd,
                operations=operations,
                timeout=120,
            )
            _run_required(
                validate_command,
                cwd=cwd,
                run_cmd=run_cmd,
                operations=operations,
                timeout=120,
            )
        except RuntimeError as exc:
            raise RuntimeConfigPrepareError(
                f"failed to {phase} rebuild/validate runtime config for {market}: {config_path}",
                remediation=[
                    manual_rebuild,
                    manual_validate,
                    f"inspect_last_operation: {exc}",
                ],
            ) from exc
        rebuilt.append({"market": market, "config_path": config_path, "source": source, "phase": phase})
    return rebuilt


def _rebuild_assistant_config(
    *,
    target: dict[str, str] | None,
    cwd: Path,
    run_cmd: Callable[..., Any],
    operations: list[dict[str, Any]],
    phase: str,
) -> dict[str, str] | None:
    if target is None:
        return None
    config_yaml = str(target.get("config_yaml") or "").strip()
    config_path = str(target.get("config_path") or "").strip()
    if not config_yaml or not config_path:
        raise RuntimeConfigPrepareError(
            "missing YAML authoring source for assistant config target",
            remediation=["rerender_service_profile: ./om service render ... --config-yaml <path>"],
        )
    try:
        _run_required(
            [
                "./om",
                "config",
                "build-assistant",
                "--source",
                "yaml",
                "--config-yaml",
                config_yaml,
                "--output",
                config_path,
            ],
            cwd=cwd,
            run_cmd=run_cmd,
            operations=operations,
            timeout=120,
        )
    except RuntimeError as exc:
        raise RuntimeConfigPrepareError(
            f"failed to {phase} rebuild assistant config: {config_path}",
            remediation=[
                f"manual_rebuild: cd {cwd} && ./om config build-assistant --source yaml --config-yaml {config_yaml} --output {config_path}",
                f"inspect_last_operation: {exc}",
            ],
        ) from exc
    return {"config_path": config_path, "source": "yaml", "phase": phase}


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
    assistant_target = _profile_assistant_config_target(profile, runtime_root=runtime_root)
    if not targets and assistant_target is None:
        return {"status": "skipped", "reason": "service profile has no runtime config targets"}

    staging_root = runtime_root / "upgrade_staging" / f"{previous_dir.name}-to-{target_dir.name}"
    if staging_root.exists():
        shutil.rmtree(staging_root)
    staged_root = staging_root / "desired"
    staged_root.mkdir(parents=True, exist_ok=True)
    staged_targets: list[dict[str, str]] = []
    artifacts: list[dict[str, str]] = []
    for item in targets:
        live_path = Path(item["config_path"]).expanduser()
        staged_path = staged_root / f"{item['market']}-{live_path.name}"
        staged_targets.append({**item, "config_path": str(staged_path)})
        artifacts.append(
            {
                "kind": "runtime_config",
                "market": item["market"],
                "live_path": str(live_path),
                "staged_path": str(staged_path),
            }
        )
    staged_assistant_target: dict[str, str] | None = None
    if assistant_target is not None:
        assistant_live_path = Path(assistant_target["config_path"]).expanduser()
        assistant_staged_path = staged_root / f"assistant-{assistant_live_path.name}"
        staged_assistant_target = {**assistant_target, "config_path": str(assistant_staged_path)}
        artifacts.append(
            {
                "kind": "assistant_config",
                "live_path": str(assistant_live_path),
                "staged_path": str(assistant_staged_path),
            }
        )

    rebuilt = _rebuild_and_validate_runtime_configs(
        targets=staged_targets,
        cwd=target_dir,
        run_cmd=run_cmd,
        operations=operations,
        phase="pre_switch",
    )
    assistant_rebuilt = _rebuild_assistant_config(
        target=staged_assistant_target,
        cwd=target_dir,
        run_cmd=run_cmd,
        operations=operations,
        phase="pre_switch",
    )
    for artifact in artifacts:
        _retarget_staged_rebuild_command(
            staged_path=Path(artifact["staged_path"]),
            live_path=Path(artifact["live_path"]),
        )
    manifest_path = staging_root / "manifest.json"
    _write_json_atomic_file(
        manifest_path,
        {
            "schema_version": 1,
            "state": "prepared",
            "previous_dir": str(previous_dir),
            "target_dir": str(target_dir),
            "releases_root": str(releases_root),
            "artifacts": artifacts,
        },
    )

    return {
        "status": "prepared",
        "targets": targets,
        "assistant_target": assistant_target,
        "staging_root": str(staging_root),
        "manifest_path": str(manifest_path),
        "artifacts": artifacts,
        "overlays": [],
        "preserved_hotfixes": [],
        "rebuilt": rebuilt,
        "assistant_rebuilt": assistant_rebuilt,
    }


def _write_json_atomic_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _retarget_staged_rebuild_command(*, staged_path: Path, live_path: Path) -> None:
    try:
        payload = json.loads(staged_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeConfigPrepareError(
            f"staged runtime config is not valid JSON: {staged_path}",
            remediation=[f"inspect_staged_config: {staged_path}"],
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeConfigPrepareError(
            f"staged runtime config must be a JSON object: {staged_path}",
            remediation=[f"inspect_staged_config: {staged_path}"],
        )
    generated = payload.get(GENERATED_KEY)
    if isinstance(generated, dict):
        command = str(generated.get("rebuild_command") or "")
        if command:
            generated["rebuild_command"] = command.replace(str(staged_path), str(live_path))
    _write_json_atomic_file(staged_path, payload)


def _commit_prepared_runtime_configs(
    *,
    prepared: dict[str, Any],
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    if prepared.get("status") != "prepared":
        return {"status": "skipped", "artifacts": []}
    staging_root = Path(str(prepared["staging_root"]))
    manifest_path = Path(str(prepared["manifest_path"]))
    backup_root = staging_root / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    committed: list[dict[str, Any]] = []
    manifest = {
        "schema_version": 1,
        "state": "committing",
        "artifacts": prepared.get("artifacts") or [],
        "committed": committed,
    }
    _write_json_atomic_file(manifest_path, manifest)
    try:
        for index, raw in enumerate(prepared.get("artifacts") or []):
            artifact = dict(raw)
            staged_path = Path(str(artifact["staged_path"]))
            live_path = Path(str(artifact["live_path"]))
            if not staged_path.is_file():
                raise RuntimeError(f"staged config missing: {staged_path}")
            live_path.parent.mkdir(parents=True, exist_ok=True)
            existed = live_path.exists()
            backup_path = backup_root / f"{index}-{live_path.name}"
            if existed:
                shutil.copy2(live_path, backup_path)
            temp_path = live_path.with_name(f".{live_path.name}.upgrade-tmp")
            shutil.copy2(staged_path, temp_path)
            os.replace(temp_path, live_path)
            item = {
                **artifact,
                "existed_before": existed,
                "backup_path": str(backup_path) if existed else None,
            }
            committed.append(item)
            operations.append(
                {
                    "operation": "commit_runtime_config",
                    "path": str(live_path),
                    "staged_path": str(staged_path),
                    "ok": True,
                }
            )
            manifest["committed"] = committed
            _write_json_atomic_file(manifest_path, manifest)
    except Exception as exc:
        restore = _restore_committed_runtime_configs(
            commit={"status": "committing", "manifest_path": str(manifest_path), "artifacts": committed},
            operations=operations,
        )
        raise RuntimeConfigPrepareError(
            f"failed to commit runtime config bundle: {type(exc).__name__}: {exc}",
            remediation=[
                f"inspect_upgrade_manifest: {manifest_path}",
                *([f"manual_restore_required: {item}" for item in restore.get("errors") or []]),
            ],
        ) from exc
    manifest["state"] = "committed"
    _write_json_atomic_file(manifest_path, manifest)
    return {
        "status": "committed",
        "manifest_path": str(manifest_path),
        "artifacts": committed,
    }


def _restore_committed_runtime_configs(
    *,
    commit: dict[str, Any],
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    restored: list[str] = []
    errors: list[str] = []
    for artifact in reversed(list(commit.get("artifacts") or [])):
        live_path = Path(str(artifact.get("live_path") or ""))
        backup_raw = str(artifact.get("backup_path") or "")
        try:
            if bool(artifact.get("existed_before")):
                backup_path = Path(backup_raw)
                if not backup_path.is_file():
                    raise RuntimeError(f"backup missing: {backup_path}")
                temp_path = live_path.with_name(f".{live_path.name}.restore-tmp")
                shutil.copy2(backup_path, temp_path)
                os.replace(temp_path, live_path)
            else:
                live_path.unlink(missing_ok=True)
        except Exception as exc:
            errors.append(f"{live_path}: {type(exc).__name__}: {exc}")
            operations.append(
                {
                    "operation": "restore_runtime_config",
                    "path": str(live_path),
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        else:
            restored.append(str(live_path))
            operations.append({"operation": "restore_runtime_config", "path": str(live_path), "ok": True})
    manifest_raw = str(commit.get("manifest_path") or "")
    if manifest_raw:
        manifest_path = Path(manifest_raw)
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                payload["state"] = "restored" if not errors else "restore_failed"
                payload["restore_errors"] = errors
                _write_json_atomic_file(manifest_path, payload)
        except Exception:
            pass
    return {"ok": not errors, "restored": restored, "errors": errors}


def _validate_committed_runtime_configs(
    *,
    prepared: dict[str, Any],
    cwd: Path,
    run_cmd: Callable[..., Any],
    operations: list[dict[str, Any]],
) -> list[dict[str, str]]:
    validated: list[dict[str, str]] = []
    for item in prepared.get("targets") or []:
        market = str(item["market"])
        config_path = str(item["config_path"])
        try:
            _run_required(
                ["./om", "config", "validate", "--config-path", config_path, "--market", market],
                cwd=cwd,
                run_cmd=run_cmd,
                operations=operations,
                timeout=120,
            )
        except RuntimeError as exc:
            raise RuntimeConfigPrepareError(
                f"failed to validate committed runtime config for {market}: {config_path}",
                remediation=[f"manual_validate: cd {cwd} && ./om config validate --config-path {config_path} --market {market}"],
            ) from exc
        validated.append({"market": market, "config_path": config_path, "phase": "post_switch"})
    assistant = prepared.get("assistant_target")
    if isinstance(assistant, dict) and assistant.get("config_path"):
        path = Path(str(assistant["config_path"]))
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeConfigPrepareError(
                f"committed assistant config is invalid: {path}",
                remediation=[f"manual_rebuild: cd {cwd} && ./om config build-assistant --source yaml --output {path}"],
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeConfigPrepareError(
                f"committed assistant config must be an object: {path}",
                remediation=[f"inspect_assistant_config: {path}"],
            )
        operations.append({"operation": "validate_assistant_config", "path": str(path), "ok": True})
    return validated


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


def _uv_install_commands(
    venv_python: Path,
    *,
    venv_dir: Path,
    include_server: bool,
    bootstrap_python: str,
) -> list[tuple[list[str], int]]:
    commands: list[tuple[list[str], int]] = [
        (["uv", "venv", "--python", bootstrap_python, str(venv_dir)], 300),
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
    bootstrap_python: str,
) -> None:
    if not venv_python.exists():
        command = [bootstrap_python, "-m", "venv", str(venv_dir)]
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
    python_spec: str | None = None,
    installer_mode: str = "auto",
) -> str:
    digest = hashlib.sha256()
    selected_python_spec = python_spec or f"{sys.version_info.major}.{sys.version_info.minor}"
    context = _dependency_context(
        include_server=include_server,
        python_spec=selected_python_spec,
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
    bootstrap_python = sys.executable
    python_spec = f"{sys.version_info.major}.{sys.version_info.minor}"
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
                bootstrap_python=bootstrap_python,
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
                    bootstrap_python=bootstrap_python,
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
                bootstrap_python=bootstrap_python,
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
    target_version: str | None = None,
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
        target_version=target_version,
        remote_name=remote_name,
        run_cmd=run_cmd,
        now_fn=now_fn,
    )
    status = load_upgrade_status(runtime_root=runtime)
    check_status = _service_upgrade_check_status(version)
    return {
        "ok": bool(version.get("ok")),
        "status": check_status,
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
        "target_source": version.get("target_source"),
        "cache_repo": version.get("cache_repo"),
        "cache_fetched": bool(version.get("cache_fetched")),
        "tag_count": version.get("tag_count"),
        "highest_tag_seen": version.get("highest_tag_seen"),
        "selected_tag": version.get("selected_tag"),
        "message": version.get("message"),
        "version_check": version,
        "last_upgrade": status,
    }


def service_upgrade_verify(
    *,
    repo_root: str | Path,
    runtime_root: str | Path,
    cache_root: str | Path | None = None,
    remote_name: str = "origin",
    check_latest: bool = True,
    run_cmd: Callable[..., Any] = subprocess.run,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    runtime = Path(runtime_root).expanduser().resolve()
    repo_link, repo, repo_root_resolution = _coerce_repo_root_to_current_symlink(repo_root=repo_root, runtime_root=runtime)
    cache = Path(cache_root).expanduser().resolve() if cache_root else default_upgrade_cache_root(repo_link)
    current_version = _read_version(repo)
    update_check = (
        service_upgrade_check(
            repo_root=repo_link,
            runtime_root=runtime,
            cache_root=cache,
            remote_name=remote_name,
            run_cmd=run_cmd,
            now_fn=now_fn,
        )
        if check_latest
        else None
    )
    profile = _load_service_profile(runtime)
    configs = {
        market: _runtime_config_verify_summary(
            market=market,
            repo_root=repo,
            runtime_root=runtime,
            profile=profile,
            current_version=current_version,
        )
        for market in _verify_markets(runtime_root=runtime, profile=profile)
    }
    upgrade_status = load_upgrade_status(runtime_root=runtime) or {}
    services = _compact_service_health(upgrade_status.get("service_health"), profile=profile)
    update_ok = True
    if update_check is not None:
        update_ok = bool(update_check.get("ok")) and not bool(update_check.get("upgrade_available"))
    config_ok = all(bool(item.get("ok")) for item in configs.values()) if configs else False
    service_ok = bool(services.get("ok", True))
    upgrade_ok = not _upgrade_status_failed(upgrade_status)
    ok = bool(current_version) and update_ok and config_ok and service_ok and upgrade_ok
    return {
        "ok": ok,
        "status": "ok" if ok else "attention_required",
        "checked_at": utc_now_iso(now_fn),
        "repo_root": str(repo_link),
        "repo_root_resolved": str(repo),
        "repo_root_is_symlink": repo_link.is_symlink(),
        "repo_root_resolution": repo_root_resolution,
        "runtime_root": str(runtime),
        "upgrade_cache_root": str(cache),
        "version": {
            "current": current_version,
            "latest": update_check.get("latest_version") if update_check else None,
            "release_tag": update_check.get("release_tag") if update_check else f"v{current_version}",
            "update_status": update_check.get("status") if update_check else "not_checked",
            "upgrade_available": bool(update_check.get("upgrade_available")) if update_check else None,
        },
        "config": configs,
        "services": services,
        "upgrade": _compact_upgrade_status(upgrade_status),
    }


def _verify_markets(*, runtime_root: Path, profile: dict[str, Any]) -> list[str]:
    config_paths = profile.get("config_paths") if isinstance(profile.get("config_paths"), dict) else {}
    markets = [str(market).strip().lower() for market in config_paths if str(market).strip().lower() in {"us", "hk"}]
    if markets:
        return sorted(set(markets))
    discovered = []
    for market in ("us", "hk"):
        if (runtime_root / f"config.{market}.json").exists():
            discovered.append(market)
    return discovered


def _runtime_config_path_for_market(*, market: str, runtime_root: Path, profile: dict[str, Any]) -> Path:
    config_paths = profile.get("config_paths") if isinstance(profile.get("config_paths"), dict) else {}
    raw = config_paths.get(market) if isinstance(config_paths, dict) else None
    return Path(raw).expanduser().resolve() if raw else runtime_root / f"config.{market}.json"


def _runtime_config_verify_summary(
    *,
    market: str,
    repo_root: Path,
    runtime_root: Path,
    profile: dict[str, Any],
    current_version: str,
) -> dict[str, Any]:
    path = _runtime_config_path_for_market(market=market, runtime_root=runtime_root, profile=profile)
    if not path.exists():
        return {"ok": False, "path": str(path), "exists": False, "reason": "runtime_config_missing"}
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "path": str(path), "exists": True, "reason": f"runtime_config_invalid:{type(exc).__name__}"}
    if not isinstance(cfg, dict):
        return {"ok": False, "path": str(path), "exists": True, "reason": "runtime_config_not_object"}
    identity = check_runtime_config_identity(
        cfg,
        explicit_market=market,
        runtime_config_path=path,
        required_source_format="yaml",
    )
    freshness = check_runtime_config_freshness(
        cfg,
        repo_root=repo_root,
        market=market,
        runtime_config_path=path,
    )
    generated = cfg.get(GENERATED_KEY) if isinstance(cfg.get(GENERATED_KEY), dict) else {}
    generated_version = str(generated.get("version") or "").strip()
    version_ok = (not generated_version) or generated_version == current_version
    return {
        "ok": bool(identity.get("ok")) and bool(freshness.get("ok")) and version_ok,
        "path": str(path),
        "exists": True,
        "market": market,
        "source_format": freshness.get("source_format"),
        "generated_version": generated_version or None,
        "version_ok": version_ok,
        "identity_ok": bool(identity.get("ok")),
        "freshness_ok": bool(freshness.get("ok")),
        "error_codes": _config_error_codes(identity, freshness, version_ok=version_ok),
    }


def _config_error_codes(*results: dict[str, Any], version_ok: bool) -> list[str]:
    codes: list[str] = []
    for result in results:
        errors = result.get("errors") if isinstance(result.get("errors"), list) else []
        for item in errors:
            if isinstance(item, dict) and item.get("code"):
                codes.append(str(item["code"]))
    if not version_ok:
        codes.append("generated_version_mismatch")
    return codes


def _compact_service_health(raw: Any, *, profile: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        provider = str(profile.get("service_provider") or "").strip().lower() if profile else ""
        return {"ok": True, "status": "unknown", "source": "upgrade_status", "provider": provider, "services": {}}
    checks = raw.get("checks") if isinstance(raw.get("checks"), list) else []
    services: dict[str, dict[str, Any]] = {}
    for check in checks:
        if not isinstance(check, dict):
            continue
        service = str(check.get("service") or "").strip()
        action = str(check.get("check") or "").strip()
        if not service or not action:
            continue
        services.setdefault(service, {})[action] = "ok" if bool(check.get("ok")) else "error"
    return {
        "ok": bool(raw.get("ok", True)),
        "status": raw.get("status") or ("ok" if bool(raw.get("ok", True)) else "error"),
        "source": "upgrade_status",
        "provider": raw.get("provider"),
        "services": services,
        "failed_checks": [
            {
                "service": item.get("service"),
                "check": item.get("check"),
            }
            for item in (raw.get("failed_checks") if isinstance(raw.get("failed_checks"), list) else [])
            if isinstance(item, dict)
        ],
    }


def _compact_upgrade_status(status: dict[str, Any]) -> dict[str, Any]:
    if not status:
        return {"available": False, "has_status_record": False, "last_status": None}
    return {
        # Legacy field: this means upgrade_status.json exists, not that a newer release is available.
        "available": True,
        "has_status_record": True,
        "last_status": status.get("status"),
        "ok": bool(status.get("ok")),
        "status": status.get("status"),
        "current_version": status.get("current_version"),
        "target_version": status.get("target_version"),
        "release_tag": status.get("release_tag"),
        "updated_at": status.get("updated_at"),
        "symlink_switched": bool(status.get("symlink_switched")),
        "config_rebuilt": bool(status.get("config_rebuilt")),
        "runtime_failed": bool(status.get("runtime_failed")),
        "restart_failed_services": status.get("restart_failed_services") or [],
        "remediation": status.get("manual_remediation") or status.get("remediation") or [],
    }


def _upgrade_status_failed(status: dict[str, Any]) -> bool:
    if not status:
        return False
    if bool(status.get("ok", True)):
        return False
    return str(status.get("status") or "").strip() not in {"already_current", "dry_run"}


def _service_upgrade_check_status(version: dict[str, Any]) -> str:
    if not bool(version.get("ok")):
        return "upgrade_check_failed"
    if bool(version.get("update_available")):
        return "upgrade_available"
    current = _version_text(str(version.get("current_version") or ""))
    latest = _version_text(str(version.get("latest_version") or ""))
    if current and latest:
        try:
            cmp = compare_versions(current, latest)
        except Exception:
            return "upgrade_check_failed"
        if cmp == 0:
            return "no_upgrade_available"
        if cmp > 0:
            return "current_ahead"
    return "no_upgrade_available"


def _switch_current_symlink(*, current_link: Path, target_dir: Path) -> None:
    if not current_link.is_symlink():
        raise RuntimeError(f"repo_root must be a current symlink for confirmed upgrade: {current_link}")
    tmp_link = current_link.with_name(f".{current_link.name}.upgrade-tmp")
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    tmp_link.symlink_to(target_dir, target_is_directory=True)
    os.replace(tmp_link, current_link)


def _compensate_service_transition(
    *,
    repo_link: Path,
    previous_dir: Path,
    runtime_root: Path,
    previous_profile: dict[str, Any],
    config_commit: dict[str, Any],
    restart_services: bool,
    run_cmd: Callable[..., Any],
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    errors: list[str] = []
    symlink_restored = False
    config_restore: dict[str, Any] = {"ok": True, "status": "skipped", "restored": [], "errors": []}
    try:
        _switch_current_symlink(current_link=repo_link, target_dir=previous_dir)
    except Exception as exc:
        errors.append(f"restore symlink: {type(exc).__name__}: {exc}")
    else:
        symlink_restored = True
        operations.append(
            {
                "operation": "restore_current_symlink",
                "path": str(repo_link),
                "target": str(previous_dir),
                "ok": True,
            }
        )
        config_restore = _restore_committed_runtime_configs(commit=config_commit, operations=operations)
    if not config_restore.get("ok", True):
        errors.extend(f"restore config: {item}" for item in config_restore.get("errors") or [])

    service_reconcile: dict[str, Any] = {}
    if symlink_restored and previous_profile:
        try:
            service_reconcile = service_drift(
                repo_root=repo_link,
                runtime_root=runtime_root,
                profile_path=runtime_root / "service.profile.json",
                profile=previous_profile,
                confirm=True,
                run_cmd=run_cmd,
            )
        except Exception as exc:
            errors.append(f"restore services: {type(exc).__name__}: {exc}")
        else:
            if _service_reconcile_failed(service_reconcile):
                errors.extend(f"restore services: {item}" for item in _service_reconcile_remediation(service_reconcile))

    restarted: list[str] = []
    service_health: dict[str, Any] = {}
    if symlink_restored and restart_services:
        try:
            restarted = _restart_services_from_loaded_profile(
                profile=previous_profile,
                run_cmd=run_cmd,
                operations=operations,
            )
        except ServiceRestartError as exc:
            restarted = exc.restarted_services
            errors.append(f"restore restart: {exc}")
            errors.extend(f"restore restart: {item}" for item in exc.remediation)
        try:
            service_health = _post_upgrade_service_health(
                profile=previous_profile,
                repo_root=repo_link,
                run_cmd=run_cmd,
                operations=operations,
            )
        except Exception as exc:
            errors.append(f"restore health: {type(exc).__name__}: {exc}")
        else:
            if not bool(service_health.get("ok", True)):
                errors.extend(f"restore health: {item}" for item in service_health.get("remediation") or [])

    return {
        "ok": not errors,
        "symlink_restored": symlink_restored,
        "config_restore": config_restore,
        "service_reconcile": service_reconcile,
        "restarted_services": restarted,
        "service_health": service_health,
        "errors": errors,
    }


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
        target_version=target_version,
        remote_name=remote_name,
        run_cmd=run_cmd,
        now_fn=now_fn,
    )
    target = _version_text(str(check.get("latest_version") or ""))
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
        out = {
            **status_base,
            "ok": False,
            "status": "no_target_version",
            "changed": False,
            "message": check.get("message") or "没有可升级版本。",
            "operations": operations,
        }
        write_upgrade_status(runtime_root=runtime, payload=out)
        return out
    cmp = compare_versions(current_version, target)
    if cmp == 0:
        out = {
            **status_base,
            "ok": True,
            "status": "already_current",
            "changed": False,
            "message": check.get("message") or f"没有可升级版本。当前已是最新版本 {current_version}",
            "operations": operations,
        }
        write_upgrade_status(runtime_root=runtime, payload=out)
        return out
    if cmp > 0:
        out = {
            **status_base,
            "ok": False,
            "status": "target_older_than_current",
            "changed": False,
            "message": check.get("message") or f"当前版本 {current_version} 高于目标版本 {target}",
            "operations": operations,
        }
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
    runtime_config_commit: dict[str, Any] = {}
    post_switch_runtime_config_validate: list[dict[str, Any]] = []
    service_reconcile: dict[str, Any] = {}
    service_health: dict[str, Any] = {}
    compensation: dict[str, Any] = {}
    restarted: list[str] = []
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
                [str(_release_python(target_dir)), "scripts/release_check.py", "--tag", str(tag)],
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
            runtime_config_commit = _commit_prepared_runtime_configs(
                prepared=runtime_config_prepare,
                operations=operations,
            )
            post_switch_runtime_config_validate = _validate_committed_runtime_configs(
                prepared=runtime_config_prepare,
                cwd=repo_link,
                run_cmd=run_cmd,
                operations=operations,
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
                if _service_reconcile_failed(service_reconcile):
                    raise ServiceTransitionError(
                        "service drift reconciliation failed after upgrade",
                        status="upgraded_service_reconcile_failed",
                        remediation=_service_reconcile_remediation(service_reconcile),
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
            if service_health and not bool(service_health.get("ok", True)):
                raise ServiceTransitionError(
                    "service health checks failed after upgrade",
                    status="upgraded_service_health_failed",
                    remediation=[str(item) for item in service_health.get("remediation") or []],
                )
    except ServiceRestartError as exc:
        if symlink_switched:
            compensation = _compensate_service_transition(
                repo_link=repo_link,
                previous_dir=previous_dir,
                runtime_root=runtime,
                previous_profile=pre_upgrade_profile,
                config_commit=runtime_config_commit,
                restart_services=restart_services,
                run_cmd=run_cmd,
                operations=operations,
            )
        compensated = bool(compensation.get("ok")) if symlink_switched else False
        out = {
            **status_base,
            "ok": False,
            "status": "upgrade_failed_rolled_back" if compensated else "upgraded_restart_failed",
            "failure_status": "upgraded_restart_failed",
            "changed": bool(symlink_switched and not compensated),
            "symlink_switched": bool(symlink_switched and not compensation.get("symlink_restored")),
            "rolled_back": compensated,
            "config_rebuilt": bool(runtime_config_prepare.get("status") == "prepared"),
            "target_dir": str(target_dir),
            "previous_dir": str(previous_dir),
            "release_materialize": release_materialize,
            "runtime_prepare": runtime_prepare,
            "runtime_config_prepare": runtime_config_prepare,
            "runtime_config_commit": runtime_config_commit,
            "post_switch_runtime_config_validate": post_switch_runtime_config_validate,
            "service_reconcile": service_reconcile,
            "service_health": service_health,
            "restarted_services": exc.restarted_services,
            "restart_failed_services": exc.failed_services,
            "manual_remediation": exc.remediation,
            "remediation": exc.remediation,
            "compensation": compensation,
            "error": f"{type(exc).__name__}: {exc}",
            "operations": operations,
        }
        write_upgrade_status(runtime_root=runtime, payload=out)
        return out
    except Exception as exc:
        if symlink_switched:
            compensation = _compensate_service_transition(
                repo_link=repo_link,
                previous_dir=previous_dir,
                runtime_root=runtime,
                previous_profile=pre_upgrade_profile,
                config_commit=runtime_config_commit,
                restart_services=restart_services,
                run_cmd=run_cmd,
                operations=operations,
            )
        compensated = bool(compensation.get("ok")) if symlink_switched else False
        failure_status = exc.status if isinstance(exc, ServiceTransitionError) else "failed"
        remediation = (
            exc.remediation
            if isinstance(exc, (RuntimeConfigPrepareError, ServiceTransitionError))
            else []
        )
        out = {
            **status_base,
            "ok": False,
            "status": "upgrade_failed_rolled_back" if compensated else failure_status,
            "failure_status": failure_status,
            "changed": bool(symlink_switched and not compensated),
            "symlink_switched": bool(symlink_switched and not compensation.get("symlink_restored")),
            "rolled_back": compensated,
            "config_rebuilt": bool(runtime_config_prepare.get("status") == "prepared"),
            "target_dir": str(target_dir),
            "previous_dir": str(previous_dir),
            "release_materialize": release_materialize,
            "runtime_prepare": exc.runtime_prepare if isinstance(exc, RuntimePrepareError) else runtime_prepare,
            "runtime_config_prepare": runtime_config_prepare,
            "runtime_config_commit": runtime_config_commit,
            "post_switch_runtime_config_validate": post_switch_runtime_config_validate,
            "service_reconcile": service_reconcile,
            "service_health": service_health,
            "compensation": compensation,
            "error": f"{type(exc).__name__}: {exc}",
            **({"remediation": remediation} if remediation else {}),
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
        "runtime_config_commit": runtime_config_commit,
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
            "planned_operations": [
                f"stage and validate runtime configs with {target_dir}",
                f"switch {repo_link} -> {target_dir}",
                "commit the staged runtime config bundle",
                "reconcile service drift for the rollback release",
                "restart and health-check long-running services" if restart_services else "skip service restart",
            ],
            "operations": operations,
        }

    symlink_switched = False
    runtime_config_prepare: dict[str, Any] = {}
    runtime_config_commit: dict[str, Any] = {}
    post_switch_runtime_config_validate: list[dict[str, str]] = []
    service_reconcile: dict[str, Any] = {}
    service_health: dict[str, Any] = {}
    compensation: dict[str, Any] = {}
    restarted: list[str] = []
    previous_profile = _load_service_profile(runtime)
    try:
        with _UpgradeLock(runtime / "locks" / "upgrade.lock"):
            runtime_config_prepare = _prepare_runtime_configs_for_release(
                previous_dir=repo,
                target_dir=target_dir,
                runtime_root=runtime,
                releases_root=releases,
                run_cmd=run_cmd,
                operations=operations,
            )
            _switch_current_symlink(current_link=repo_link, target_dir=target_dir)
            symlink_switched = True
            runtime_config_commit = _commit_prepared_runtime_configs(
                prepared=runtime_config_prepare,
                operations=operations,
            )
            post_switch_runtime_config_validate = _validate_committed_runtime_configs(
                prepared=runtime_config_prepare,
                cwd=repo_link,
                run_cmd=run_cmd,
                operations=operations,
            )
            if previous_profile:
                service_reconcile = service_drift(
                    repo_root=repo_link,
                    runtime_root=runtime,
                    profile_path=runtime / "service.profile.json",
                    profile=previous_profile,
                    confirm=True,
                    run_cmd=run_cmd,
                )
                if _service_reconcile_failed(service_reconcile):
                    raise ServiceTransitionError(
                        "service drift reconciliation failed during rollback",
                        status="rollback_service_reconcile_failed",
                        remediation=_service_reconcile_remediation(service_reconcile),
                    )
            rollback_profile = _load_service_profile(runtime) or previous_profile
            restarted = (
                _restart_services_from_loaded_profile(
                    profile=rollback_profile,
                    run_cmd=run_cmd,
                    operations=operations,
                )
                if restart_services
                else []
            )
            service_health = (
                _post_upgrade_service_health(
                    profile=rollback_profile,
                    repo_root=repo_link,
                    run_cmd=run_cmd,
                    operations=operations,
                )
                if restart_services
                else {"ok": True, "status": "skipped", "reason": "service_restart_disabled", "checks": [], "failed_checks": []}
            )
            if service_health and not bool(service_health.get("ok", True)):
                raise ServiceTransitionError(
                    "service health checks failed during rollback",
                    status="rollback_service_health_failed",
                    remediation=[str(item) for item in service_health.get("remediation") or []],
                )
    except Exception as exc:
        if symlink_switched:
            compensation = _compensate_service_transition(
                repo_link=repo_link,
                previous_dir=repo,
                runtime_root=runtime,
                previous_profile=previous_profile,
                config_commit=runtime_config_commit,
                restart_services=restart_services,
                run_cmd=run_cmd,
                operations=operations,
            )
        compensated = bool(compensation.get("ok")) if symlink_switched else False
        failure_status = (
            exc.status
            if isinstance(exc, ServiceTransitionError)
            else "rollback_restart_failed"
            if isinstance(exc, ServiceRestartError)
            else "rollback_failed"
        )
        remediation = (
            exc.remediation
            if isinstance(exc, (RuntimeConfigPrepareError, ServiceTransitionError, ServiceRestartError))
            else []
        )
        out = {
            **status_base,
            "ok": False,
            "status": "rollback_failed_restored" if compensated else failure_status,
            "failure_status": failure_status,
            "changed": bool(symlink_switched and not compensated),
            "symlink_switched": bool(symlink_switched and not compensation.get("symlink_restored")),
            "restored_original": compensated,
            "target_dir": str(target_dir),
            "runtime_config_prepare": runtime_config_prepare,
            "runtime_config_commit": runtime_config_commit,
            "post_switch_runtime_config_validate": post_switch_runtime_config_validate,
            "service_reconcile": service_reconcile,
            "service_health": service_health,
            "compensation": compensation,
            "error": f"{type(exc).__name__}: {exc}",
            **({"remediation": remediation} if remediation else {}),
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
        "runtime_config_prepare": runtime_config_prepare,
        "runtime_config_commit": runtime_config_commit,
        "post_switch_runtime_config_validate": post_switch_runtime_config_validate,
        "service_reconcile": service_reconcile,
        "service_health": service_health,
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
