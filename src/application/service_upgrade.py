from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import platform
import re
import shutil
import shlex
import subprocess
import sys
import sysconfig
import tempfile
import time
import tomllib
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.application import plugin_distribution, runtime_bundle
from src.application.agent_tool_contracts import AgentToolError
from src.application.file_locks import single_instance_lock
from src.application.plugin_runtime import PLUGINS, read_managed_plugin, read_source_plugin, source_plugin_store

from src.application.release_target import compare_versions, parse_version, resolve_upgrade_target
from src.application.runtime_config_freshness import (
    GENERATED_KEY,
    check_runtime_config_freshness,
    check_runtime_config_identity,
)
from src.application.service_drift import (
    SERVICE_ACTIVATION_POLICY_ENSURE_ACTIVE,
    SERVICE_ACTIVATION_POLICY_PRESERVE_EXISTING,
    normalize_service_activation_policy,
    service_drift,
)
from src.infrastructure.io_utils import parse_last_json_obj

PROVIDER_PLUGINS = ("market-opend", "broker-futu", "broker-longbridge")

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


def _read_upgrade_preview_json(path: Path) -> tuple[dict[str, Any] | None, tuple | None]:
    """Read a preview input with evidence that the named file stayed unchanged."""
    try:
        source = path.open("rb")
    except FileNotFoundError:
        try:
            path.lstat()
        except FileNotFoundError:
            return None, ("absent",)
        except OSError:
            pass
        return None, None
    except OSError:
        return None, None
    payload = None
    try:
        with source:
            before = os.fstat(source.fileno())
            raw = source.read()
            after = os.fstat(source.fileno())
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            return None, None
        payload = parsed
        named = path.stat()
        identities = [
            (item.st_dev, item.st_ino, item.st_mode, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
            for item in (before, after, named)
        ]
        if identities[0] == identities[1] == identities[2]:
            return payload, ("present", identities[0], raw)
    except (OSError, UnicodeError, ValueError):
        pass
    return payload, None


def _publish_early_upgrade_preview_status(
    *,
    runtime_root: Path,
    repo_root: str | Path,
    baseline: tuple,
    status_witness: tuple | None,
    profile_witness: tuple | None,
    payload: dict[str, Any],
    missing_target_dir: Path | None = None,
) -> None:
    """Preserve an early preview receipt only while its original inputs hold."""
    if status_witness is None or profile_witness is None:
        return
    lock_path = runtime_root / "locks" / "upgrade.lock"
    with ExitStack() as publication_stack:
        try:
            publication_stack.enter_context(_UpgradeLock(lock_path))
        except RuntimeError as exc:
            if str(exc) == f"upgrade lock already exists: {lock_path}":
                return
            raise
        if _deployment_user_error(runtime_root):
            return
        status_path = upgrade_status_path(runtime_root)
        profile_path = runtime_root / "service.profile.json"
        if (
            _read_upgrade_preview_json(status_path)[1] != status_witness
            or _read_upgrade_preview_json(profile_path)[1] != profile_witness
        ):
            return
        try:
            repo_link, repo, resolution = _coerce_repo_root_to_current_symlink(
                repo_root=repo_root, runtime_root=runtime_root,
            )
            observed = (repo_link, repo, resolution, _read_version(repo), repo_link.is_symlink())
            if observed != baseline:
                return
            if missing_target_dir is not None and missing_target_dir.exists():
                return
        except (OSError, UnicodeError, RuntimeError):
            return
        if (
            _read_upgrade_preview_json(status_path)[1] != status_witness
            or _read_upgrade_preview_json(profile_path)[1] != profile_witness
        ):
            return
        # Keep writer errors outside the read-evidence catches above.
        write_upgrade_status(runtime_root=runtime_root, payload=payload)


class _UpgradeLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None
        self._guard: ExitStack | None = None

    def __enter__(self) -> "_UpgradeLock":
        # Keep the advisory inode stable while preserving the transient PID file.
        with ExitStack() as guard:
            try:
                guard.enter_context(single_instance_lock(
                    self.path.with_name(self.path.name + ".guard"),
                    busy_message=f"upgrade lock already exists: {self.path}",
                ))
            except AgentToolError as exc:
                if exc.code == "RESOURCE_BUSY":
                    raise RuntimeError(f"upgrade lock already exists: {self.path}") from exc
                raise
            try:
                self.fd = self._open_lock()
                remaining = str(os.getpid()).encode("utf-8")
                while remaining:
                    written = os.write(self.fd, remaining)
                    if written <= 0:
                        raise OSError(f"failed to write upgrade lock PID: {self.path}")
                    remaining = remaining[written:]
                self._guard = guard.pop_all()
            except BaseException:
                self._release_pid()
                raise
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
        guard, self._guard = self._guard, None
        try:
            self._release_pid()
        finally:
            if guard is not None:
                guard.close()

    def _release_pid(self) -> None:
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            owned = os.fstat(fd)
            try:
                current = self.path.lstat()
                if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                    self.path.unlink()
            except FileNotFoundError:
                pass
        finally:
            os.close(fd)


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


def _clip_command_output(text: str, limit: int | None) -> str:
    """Trim captured output to its tail; failures and summaries print last.

    Callers that parse a child's stdout as JSON pass `limit=None`: a truncated
    document is not parseable, and the drift response lists every unit.
    """
    return text if limit is None else text[-limit:]


def _run_command(
    command: list[str],
    *,
    cwd: Path | None,
    run_cmd: Callable[..., Any],
    env: dict[str, str] | None = None,
    timeout: int = 300,
    stdout_limit: int | None = 4000,
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
    try:
        proc = run_cmd(
            command,
            **kwargs,
        )
    except subprocess.SubprocessError as exc:
        stdout = str(getattr(exc, "stdout", None) or getattr(exc, "output", None) or "")
        stderr = str(getattr(exc, "stderr", None) or f"{type(exc).__name__}: {exc}")
        return {
            "command": command,
            "cwd": str(cwd) if cwd is not None else None,
            "started_at": started_at,
            "ended_at": utc_now_iso(),
            "duration_seconds": round(time.monotonic() - started, 3),
            "returncode": None,
            "stdout": _clip_command_output(stdout, stdout_limit),
            "stderr": stderr[-4000:],
            "ok": False,
            "subprocess_error": type(exc).__name__,
            **(
                {"timed_out": True, "timeout_seconds": timeout}
                if isinstance(exc, subprocess.TimeoutExpired)
                else {}
            ),
            **({"env_overrides": sorted(set(env) - set(os.environ))} if env is not None else {}),
        }
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
        "stdout": _clip_command_output(stdout, stdout_limit),
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


def _deployment_user_error(runtime_root: Path) -> dict[str, Any] | None:
    deploy_user = str(_load_service_profile(runtime_root).get("deploy_user") or "").strip()
    if not deploy_user:
        return None
    try:
        import pwd

        expected_uid = pwd.getpwnam(deploy_user).pw_uid
        actual_uid = os.geteuid()
    except (ImportError, AttributeError, KeyError, OSError):
        message = f"无法核实部署用户 {deploy_user}，请检查 service.profile.json 和系统用户。"
    else:
        if actual_uid == expected_uid:
            return None
        message = (
            f"当前执行用户 UID={actual_uid} 与部署用户 {deploy_user} (UID={expected_uid}) 不一致；"
            "请以部署用户执行升级或回滚，不要用 sudo 包裹整个命令。"
        )
    # Reject before even writing status: atomic replacement would change file ownership.
    return {
        "ok": False,
        "status": "deployment_user_check_failed",
        "changed": False,
        "message": message,
        "remediation": [message],
        "operations": [],
    }


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
        return _cache_remote_url(cache_root=cache_root, remote_name="origin", run_cmd=run_cmd)
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
    if _is_runtime_bundle(repo_root):
        try:
            manifest = runtime_bundle.load_manifest(repo_root, expected_platform=runtime_bundle.current_platform())
            if target_version:
                release = runtime_bundle.resolve_release(_tag_text(target_version), repository=manifest["repository"])
                tag = release["tag_name"]
                latest = _version_text(tag)
                return {"ok": True, "current_version": current_version, "latest_version": latest,
                        "release_tag": tag, "selected_tag": tag, "update_available": compare_versions(current_version, latest) < 0,
                        "source": "runtime_bundle", "target_source": "runtime_bundle", "checked_at": utc_now_iso(now_fn)}
            from src.application.version_check import check_version_update
            return {**check_version_update(base_dir=repo_root, remote_name=remote_name, run_cmd=run_cmd, now_fn=now_fn),
                    "source": "runtime_bundle", "target_source": "runtime_bundle"}
        except (runtime_bundle.RuntimeBundleError, OSError, ValueError) as exc:
            return {"ok": False, "current_version": current_version, "latest_version": None,
                    "update_available": False, "error": str(exc), "source": "runtime_bundle",
                    "message": "版本检查失败：运行包发布信息不可用"}
    try:
        remote_url = (
            _cache_remote_url(cache_root=cache_root, remote_name="origin", run_cmd=run_cmd)
            if _cache_repo_path(cache_root).exists()
            else _resolve_upgrade_remote_url(
                repo_root=repo_root, cache_root=cache_root, remote_name=remote_name, run_cmd=run_cmd,
            )
        )
    except Exception:
        return {
            "ok": False, "current_version": current_version, "latest_version": None,
            "update_available": False, "remote_name": remote_name, "checked_at": utc_now_iso(now_fn),
            "release_tag": None, "selected_tag": None, "target_source": None,
            "cache_repo": str(_cache_repo_path(cache_root)), "cache_fetched": False,
            "tag_count": 0, "highest_tag_seen": None, "source": "release_target",
            "message": ("版本检查失败：无法确定升级来源（cache origin unavailable）"
                        if _cache_repo_path(cache_root).exists() else "版本检查失败：无法确定升级来源"),
            "error": "upgrade_source_unavailable",
        }
    resolved = resolve_upgrade_target(
        remote_url=remote_url,
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
        _cache_remote_url(cache_root=cache_root, remote_name="origin", run_cmd=run_cmd)
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
            ["git", "clone", "--mirror", "--origin", "origin", remote_url, str(cache_repo)],
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


def _materialize_runtime_bundle(*, current_dir: Path, tag: str, target_dir: Path) -> dict[str, Any]:
    manifest = runtime_bundle.load_manifest(current_dir, expected_platform=runtime_bundle.current_platform())
    if target_dir.exists():
        runtime_bundle.load_manifest(target_dir, expected_tag=tag, expected_platform=manifest["platform"], verify_files=True)
        return {"method": "reuse_existing_runtime_bundle", "target_dir": str(target_dir), "tag": tag}
    release = runtime_bundle.resolve_release(tag, repository=manifest["repository"])
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".om-upgrade-bundle-", dir=target_dir.parent) as temporary:
        stage = Path(temporary)
        archive = runtime_bundle.download_bundle(release, stage / "bundle.tar.gz", platform_name=manifest["platform"])
        unpacked = stage / "release"
        runtime_bundle.materialize_bundle(archive, unpacked, expected_tag=tag, expected_platform=manifest["platform"])
        result = runtime_bundle.validate_runtime(unpacked)
        if result.get("ok") is not True:
            raise RuntimePrepareError("bundled runtime validation failed", runtime_prepare=result)
        os.rename(unpacked, target_dir)
    return {"method": "runtime_bundle", "target_dir": str(target_dir), "tag": tag, "platform": manifest["platform"]}


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
    if provider == "launchd":
        # The target drift owner has already booted out and reloaded every loaded
        # job. A second kickstart would rerun scheduled financial jobs.
        return restarted
    if provider != "systemd":
        raise ServiceTransitionError("unsupported service provider", status="unsupported_service_provider", remediation=[])
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
    monotonic_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if not profile:
        return {"ok": True, "status": "skipped", "reason": "service_profile_missing", "checks": [], "failed_checks": []}
    provider = str(profile.get("service_provider") or "").strip().lower()
    if provider not in {"systemd", "launchd"}:
        return {"ok": False, "status": "unsupported", "reason": f"unsupported_provider:{provider or 'missing'}", "checks": [], "failed_checks": []}

    services = _restart_service_names(profile)
    checks: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    if provider == "launchd":
        from src.application.service_lifecycle import launchd_upgrade_state
        try:
            observed = launchd_upgrade_state(profile=profile, repo_root=repo_root,
                runtime_root=Path(profile["runtime_root"]), run_cmd=run_cmd)
            expected = profile.get("_launchd_expected_states") or {}
            for unit in observed["units"]:
                name = unit["name"]
                row = observed["states"][name]
                wanted = expected.get(name)
                valid = (row["state"] != "loaded-not-running"
                         and Path(unit["path"]).read_bytes() == unit["content"].encode())
                if wanted:
                    valid = valid and ((row["state"] == "absent") == (wanted["state"] == "absent")) and row["enabled"] == wanted["enabled"]
                checks.append({"service": name, "check": "launchd-definition-activation", "ok": valid})
            failed.extend(row for row in checks if not row["ok"])
            services = [unit["name"] for unit in observed["units"] if observed["states"][unit["name"]]["state"] == "running"]
        except (AgentToolError, OSError, ValueError) as exc:
            return {"ok": False, "status": "error", "provider": provider, "checks": [],
                    "failed_checks": [{"check": "launchd-readback", "ok": False, "error": str(exc)}]}

    for service_name in (services if provider == "systemd" else []):
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

    opend_profile = profile.get("opend")
    opend_entries = opend_profile.get("services") if isinstance(opend_profile, dict) else None
    opend_entries = opend_entries if isinstance(opend_entries, list) else []
    # Upgrade gate D1: 5 s polls, 300 s floor, 600 s cap, 35 s child grace.
    # The shared watchdog config defaults to 25 s, which is too short after a restart.
    budget = 300.0
    requested_budget = budget
    config_paths = profile.get("config_paths")
    if isinstance(config_paths, dict):
        for raw_path in config_paths.values():
            try:
                config = json.loads(Path(raw_path).expanduser().read_text(encoding="utf-8"))
                candidate = config.get("watchdog", {}).get("retry_timeout_sec")
            except (OSError, TypeError, ValueError, AttributeError):
                continue
            if (isinstance(candidate, (int, float)) and not isinstance(candidate, bool)
                    and candidate > 0 and (isinstance(candidate, int) or math.isfinite(candidate))):
                requested_budget = max(requested_budget, candidate)
    budget = min(requested_budget, 600.0)
    for service_name in (name for name in services if "opend" in name):
        endpoint = next((item for item in opend_entries
                         if isinstance(item, dict) and item.get("service_name") == service_name), {})
        host, port = str(endpoint.get("host") or "").strip(), endpoint.get("port")
        if not host or not isinstance(port, int) or not 1 <= port <= 65535:
            public = {"service": service_name, "check": "opend-login-check", "ok": False,
                      "reason_code": "OPEND_ENDPOINT_MISSING"}
        else:
            started = monotonic_fn()
            deadline = started + budget
            attempts = 0
            max_attempts = math.ceil(budget / 5) + 1
            for attempt in range(max_attempts):
                remaining = max(0.0, deadline - monotonic_fn())
                # Flag reference: external_services.run_opend_watchdog; omit --ensure for this read-only gate.
                command = [str(_release_python(repo_root)), "-m", "src.infrastructure.opend_watchdog",
                           "--json", "--host", host, "--port", str(port),
                           "--retry-interval-sec", "5", "--retry-timeout-sec", str(remaining),
                           "--success-threshold", "2", "--required-capability", "both", "--retry-enabled"]
                result = _run_command(command, cwd=repo_root, run_cmd=run_cmd,
                                      timeout=math.ceil(remaining) + 35, stdout_limit=None)
                attempts += 1
                payload = parse_last_json_obj(
                    (result.get("stdout") or "") + "\n" + (result.get("stderr") or "")
                )
                if not isinstance(payload, dict):
                    payload = {}
                public = {"service": service_name, "check": "opend-login-check",
                          "ok": bool(result.get("ok")) and payload.get("ok") is True,
                          "reason_code": payload.get("error_code")}
                result["ok"] = public["ok"]
                remaining = deadline - monotonic_fn()
                if public["ok"] or remaining <= 0 or attempt == max_attempts - 1:
                    break
                sleep_fn(min(5.0, remaining))
            result["stdout"] = _clip_command_output(str(result.get("stdout") or ""), 4000)
            result.update(operation="post_upgrade_service_health", check="opend-login-check", service=service_name,
                          attempts=attempts, waited_seconds=round(monotonic_fn() - started, 3),
                          retry_budget_seconds=budget)
            if requested_budget > budget:
                result["retry_budget_requested_seconds"] = requested_budget
            operations.append(result)
        checks.append(public)
        if not public["ok"]:
            failed.append(public)

    feishu_service = "com.options-monitor.feishu-ws" if provider == "launchd" else "options-monitor-feishu-ws.service"
    if feishu_service in services:
        command = _feishu_ws_check_command(profile=profile, repo_root=repo_root)
        env = _child_env_from_profile(profile)
        result = _run_command(command, cwd=repo_root, run_cmd=run_cmd, env=env, timeout=60)
        result["operation"] = "post_upgrade_service_health"
        result["check"] = "feishu-ws-check"
        result["service"] = feishu_service
        operations.append(result)
        public = {
            "service": feishu_service,
            "check": "feishu-ws-check",
            "ok": bool(result.get("ok")),
            "stdout": str(result.get("stdout") or "").strip(),
            "stderr": str(result.get("stderr") or "").strip(),
        }
        checks.append(public)
        if not result.get("ok"):
            failed.append(public)

    wechat_service = "com.options-monitor.wechat-clawbot" if provider == "launchd" else "options-monitor-wechat-clawbot.service"
    if wechat_service in services:
        command = _wechat_clawbot_check_command(profile=profile, repo_root=repo_root)
        env = _child_env_from_profile(profile)
        result = _run_command(command, cwd=repo_root, run_cmd=run_cmd, env=env, timeout=60)
        result["operation"] = "post_upgrade_service_health"
        result["check"] = "wechat-clawbot-check"
        result["service"] = wechat_service
        operations.append(result)
        public = {
            "service": wechat_service,
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
    bot_config_path = _bot_config_path_from_profile(profile=profile)
    if bot_config_path:
        command.extend(["--bot-config", bot_config_path])
    env_file = str(profile.get("env_file") or "").strip()
    if env_file:
        command.extend(["--env-file", str(Path(env_file).expanduser())])
    credential_env_file = _feishu_agent_credential_env_file(profile)
    if credential_env_file:
        command.extend(["--credential-env-file", credential_env_file])
    if _feishu_ws_check_needs_sudo_for_env_file(profile):
        return ["sudo", "-n", *command]
    return command


def _feishu_agent_credential_env_file(profile: dict[str, Any]) -> str:
    raw = profile.get("feishu_agent_credential")
    credential = raw if isinstance(raw, dict) else {}
    if not bool(credential.get("enabled")):
        return ""
    value = str(credential.get("runtime_env_file") or "").strip()
    return str(Path(value).expanduser()) if value else ""


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
    bot_config_path = str(payload.get("bot_config_path") or "").strip()
    if bot_config_path:
        command.extend(["--bot-config", bot_config_path])
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
    env_files = [str(profile.get("env_file") or "").strip()]
    credential_env_file = _feishu_agent_credential_env_file(profile)
    if credential_env_file:
        env_files.append(credential_env_file)
    paths = [Path(value).expanduser() for value in env_files if value]
    if not paths:
        return False
    try:
        return any(not os.access(str(path), os.R_OK) for path in paths)
    except OSError:
        return True


def _bot_config_path_from_profile(*, profile: dict[str, Any], runtime_root: Path | None = None) -> str:
    raw = str(profile.get("bot_config_path") or "").strip()
    if raw:
        return raw
    feishu_ws = profile.get("feishu_ws")
    if isinstance(feishu_ws, dict):
        raw = str(feishu_ws.get("bot_config_path") or "").strip()
        if raw:
            return raw
    authoring = profile.get("config_authoring")
    if isinstance(authoring, dict) and str(authoring.get("source") or "").strip().lower() == "yaml":
        root = runtime_root
        if root is None and str(profile.get("runtime_root") or "").strip():
            root = Path(str(profile["runtime_root"])).expanduser()
        if root is not None:
            return str(root / "resolved" / "config.bot.json")
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
    if any(item.get("check") == "feishu-ws-check" for item in failed_checks) and (profile or {}).get("service_provider") == "launchd":
        command = _feishu_ws_check_command(profile=profile or {}, repo_root=repo_root or Path((profile or {}).get("repo_root") or "."))
        remediation.append("manual_check: " + shlex.join(command))
    elif any(item.get("check") == "feishu-ws-check" for item in failed_checks):
        env_file = str((profile or {}).get("env_file") or "").strip()
        if env_file:
            command = f"manual_check: sudo -n ./om inbound feishu-ws --check --env-file {shlex.quote(env_file)}"
            credential_env_file = _feishu_agent_credential_env_file(profile or {})
            if credential_env_file:
                command += f" --credential-env-file {shlex.quote(credential_env_file)}"
            remediation.append(command)
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
    if any(item.get("check") == "opend-login-check" for item in failed_checks):
        remediation.append("manual_check: verify OpenD login and the endpoint in service.profile.json")
    return remediation


def _service_reconcile_failed(service_reconcile: dict[str, Any]) -> bool:
    if not service_reconcile:
        return False
    if service_reconcile.get("apply_errors"):
        return True
    summary_raw = service_reconcile.get("summary")
    summary = summary_raw if isinstance(summary_raw, dict) else {}
    return summary.get("ok") is not True


def _service_reconcile_remediation(service_reconcile: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for item in service_reconcile.get("apply_errors") or []:
        out.append(f"service_reconcile_error: {item}")
    for item in service_reconcile.get("manual_actions") or []:
        out.append(str(item))
    return out


def _parse_drift_response(stdout: str) -> dict[str, Any] | None:
    """Unwrap only a successful drift response from the child CLI."""
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if (
        payload.get("tool_name") == "service.drift"
        and payload.get("ok") is True
        and isinstance(payload.get("data"), dict)
    ):
        return payload["data"]
    return None


def _reconcile_services_from_current_release(
    *,
    repo_link: Path,
    target_dir: Path,
    runtime: Path,
    activation_policy: str,
    run_cmd: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Reconcile the service bundle using the code of the release now current.

    The upgrade runs as the *previous* release's `om`, so reconciling in process
    renders the previous release's bundle: units the new release declares read as
    "not expected" and are never installed. On the 3.7.0 -> 3.7.1 upgrade
    (2026-09-24) the in-process reconcile reported 26 expected units and wrote
    nothing, while 3.7.1's renderer expects 29 — the trade-intake heartbeat timer
    and its failure alert stayed missing until `om service drift --confirm` was
    run from the new release by hand. Handing this step to `<target_dir>/om` keeps
    the desired state in step with whatever `current` now points at.

    Forward upgrade, explicit rollback and failure compensation must use the
    release now current. A standalone controller need not be that release.
    """
    command = [
        str(target_dir / "om"),
        "service",
        "drift",
        "--repo-root",
        str(repo_link),
        "--runtime-root",
        str(runtime),
        "--profile-path",
        str(runtime / "service.profile.json"),
        "--confirm",
    ]
    # Normalise before comparing: any unnormalised spelling would fall through to
    # ensure-active in the child and re-enable a timer an operator paused on
    # purpose, which is the state the preserve policy exists to carry across.
    if normalize_service_activation_policy(activation_policy) == SERVICE_ACTIVATION_POLICY_PRESERVE_EXISTING:
        command.append("--preserve-activation-state")
    # `om` prints the whole response, and the drift details list every expected and
    # installed unit, so read stdout whole rather than through the usual tail.
    result = _run_command(command, cwd=repo_link, run_cmd=run_cmd, timeout=300, stdout_limit=None)
    stdout = str(result.get("stdout") or "")
    if result.get("ok") is True and result.get("returncode") == 0:
        reconcile = _parse_drift_response(stdout)
        if reconcile is not None:
            return reconcile
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    error = error if isinstance(error, dict) else {}
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    raw_remediation = details.get("remediation")
    child_remediation = [str(item) for item in raw_remediation] if isinstance(raw_remediation, list) else []
    reason = str(error.get("message") or result.get("stderr") or "unreadable child response").strip()[-400:]
    if child_remediation:
        reason += f"; {child_remediation[0][:400]}"
    raise ServiceTransitionError(
        f"service drift reconciliation produced no successful result: {reason}",
        status="upgraded_service_reconcile_failed",
        remediation=[
            f"command failed: {' '.join(shlex.quote(part) for part in command)}",
            f"returncode: {result.get('returncode')}",
            f"stderr: {str(result.get('stderr') or '').strip()[-400:]}",
            f"stdout: {stdout.strip()[-400:]}",
            *child_remediation,
        ],
    )


def capture_preserved_timer_activation_states(
    *,
    repo_root: Path,
    runtime_root: Path,
    profile: dict[str, Any],
    run_cmd: Callable[..., Any] = subprocess.run,
) -> dict[str, dict[str, str]]:
    if profile.get("service_provider") == "launchd":
        from src.application.service_lifecycle import launchd_upgrade_state
        try:
            observed = launchd_upgrade_state(profile=profile, repo_root=repo_root,
                                            runtime_root=runtime_root, run_cmd=run_cmd)
        except (AgentToolError, OSError, ValueError) as exc:
            raise ServiceTransitionError(str(exc), status="service_activation_snapshot_failed", remediation=["inspect launchd identity, definitions and manager state before retrying"]) from exc
        return {name: {"state": row["state"], "enabled": row["enabled"]} for name, row in observed["states"].items()}
    observed = service_drift(
        repo_root=repo_root,
        runtime_root=runtime_root,
        profile_path=runtime_root / "service.profile.json",
        profile=profile,
        confirm=False,
        run_cmd=run_cmd,
    )
    if observed.get("checked") is False or observed.get("supported") is False:
        reason = str(observed.get("reason") or "service drift status unavailable")
        raise ServiceTransitionError(
            f"could not capture timer activation state: {reason}",
            status="service_activation_snapshot_failed",
            remediation=[
                "manual_check_timer_state: verify systemctl is-enabled and "
                "systemctl is-active for the managed timers"
            ],
        )
    activation_states_raw = observed.get("activation_states")
    activation_states = (
        activation_states_raw if isinstance(activation_states_raw, dict) else {}
    )
    active_states_raw = observed.get("active_states")
    active_states = active_states_raw if isinstance(active_states_raw, dict) else {}
    expected_services_raw = observed.get("expected_services")
    expected_services = (
        {str(item) for item in expected_services_raw}
        if isinstance(expected_services_raw, list)
        else set()
    )
    installed_units_raw = observed.get("installed_units")
    installed_units = (
        {str(item) for item in installed_units_raw}
        if isinstance(installed_units_raw, list)
        else set()
    )
    timer_names = {
        str(name)
        for name in set(activation_states) | set(active_states)
        if str(name).endswith(".timer")
    }
    timer_names.update(
        name
        for name in expected_services & installed_units
        if name.endswith(".timer")
    )
    snapshot: dict[str, dict[str, str]] = {}
    unknown_timer_states: list[str] = []
    for name in sorted(timer_names):
        activation_state = str(activation_states.get(name) or "").strip().lower()
        active_state = str(active_states.get(name) or "").strip().lower()
        if (
            activation_state in {"", "unknown"}
            or active_state in {"", "unknown"}
        ):
            unknown_timer_states.append(name)
            continue
        paused_by_activation = activation_state in {"disabled", "masked"}
        paused_by_activity = active_state == "inactive"
        if not paused_by_activation and not paused_by_activity:
            continue
        snapshot[str(name)] = {
            key: value
            for key, value in (
                ("activation_state", activation_state),
                ("active_state", active_state),
            )
            if value not in {"", "unknown"}
        }
    if unknown_timer_states:
        raise ServiceTransitionError(
            "could not determine whether managed timers were active before the "
            "release switch: " + ", ".join(unknown_timer_states),
            status="service_activation_snapshot_failed",
            remediation=[
                "manual_check_timer_state: "
                f"systemctl is-enabled {name}; systemctl is-active {name}"
                for name in unknown_timer_states
            ],
        )
    return snapshot


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


def _profile_bot_config_target(profile: dict[str, Any], *, runtime_root: Path) -> dict[str, str] | None:
    raw_authoring = profile.get("config_authoring")
    authoring = raw_authoring if isinstance(raw_authoring, dict) else {}
    if str(authoring.get("source") or "").strip().lower() != "yaml":
        return None
    config_yaml = str(authoring.get("config_yaml") or "").strip()
    if not config_yaml:
        return None
    bot_config_path = _bot_config_path_from_profile(profile=profile, runtime_root=runtime_root)
    if not bot_config_path:
        bot_config_path = str(runtime_root / "resolved" / "config.bot.json")
    return {
        "source": "yaml",
        "config_yaml": config_yaml,
        "config_path": bot_config_path,
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


def _rebuild_bot_config(
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
            "missing YAML authoring source for Bot config target",
            remediation=["rerender_service_profile: ./om service render ... --config-yaml <path>"],
        )
    try:
        _run_required(
            [
                "./om",
                "config",
                "build-bot",
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
            f"failed to {phase} rebuild Bot config: {config_path}",
            remediation=[
                f"manual_rebuild: cd {cwd} && ./om config build-bot --source yaml --config-yaml {config_yaml} --output {config_path}",
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
    bot_target = _profile_bot_config_target(profile, runtime_root=runtime_root)
    if not targets and bot_target is None:
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
    staged_bot_target: dict[str, str] | None = None
    if bot_target is not None:
        bot_live_path = Path(bot_target["config_path"]).expanduser()
        bot_staged_path = staged_root / f"assistant-{bot_live_path.name}"
        staged_bot_target = {**bot_target, "config_path": str(bot_staged_path)}
        artifacts.append(
            {
                "kind": "bot_config",
                "live_path": str(bot_live_path),
                "staged_path": str(bot_staged_path),
            }
        )

    rebuilt = _rebuild_and_validate_runtime_configs(
        targets=staged_targets,
        cwd=target_dir,
        run_cmd=run_cmd,
        operations=operations,
        phase="pre_switch",
    )
    bot_rebuilt = _rebuild_bot_config(
        target=staged_bot_target,
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
        "bot_target": bot_target,
        "staging_root": str(staging_root),
        "manifest_path": str(manifest_path),
        "artifacts": artifacts,
        "overlays": [],
        "preserved_hotfixes": [],
        "rebuilt": rebuilt,
        "bot_rebuilt": bot_rebuilt,
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
    bot_config = prepared.get("bot_target")
    if isinstance(bot_config, dict) and bot_config.get("config_path"):
        path = Path(str(bot_config["config_path"]))
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeConfigPrepareError(
                f"committed Bot config is invalid: {path}",
                remediation=[f"manual_rebuild: cd {cwd} && ./om config build-bot --source yaml --output {path}"],
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeConfigPrepareError(
                f"committed Bot config must be an object: {path}",
                remediation=[f"inspect_bot_config: {path}"],
            )
        operations.append({"operation": "validate_bot_config", "path": str(path), "ok": True})
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


def _selected_source_plugins(
    *, include_pm: bool = False, include_position_advice: bool = False,
    include_feishu: bool = False, include_wechat: bool = False,
    provider_plugins: tuple[str, ...] = (),
) -> tuple[str, ...]:
    return tuple(key for key, selected in (
        ("pm", include_pm), ("position-advice", include_position_advice),
        ("feishu", include_feishu), ("wechat", include_wechat),
        *((key, True) for key in provider_plugins),
    ) if selected)


def _plugin_dependency_commands(
    venv_python: Path, *, target_dir: Path, installer: str = "pip", **selection: Any,
) -> list[tuple[list[str], int]]:
    dependencies = []
    for plugin_id in _selected_source_plugins(**selection):
        project = tomllib.loads((target_dir / "plugins" / PLUGINS[plugin_id]["source"] / "pyproject.toml").read_text())
        metadata = project.get("project", {})
        if "dependencies" in metadata.get("dynamic", []):
            raise RuntimeError(f"{plugin_id}: source-dependent dynamic dependencies cannot use a specification-only venv cache")
        dependencies.extend(metadata.get("dependencies", []))
    if not dependencies:
        return []
    command = (["uv", "pip", "install", "-p", str(venv_python)] if installer == "uv"
               else [str(venv_python), "-m", "pip", "install"])
    return [(command + ["-c", "constraints.txt", *sorted(set(dependencies))], 1200)]


def _source_plugin_fingerprint(package: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*")):
        relative = path.relative_to(package)
        if (not path.is_file() or any(part in {"build", "dist", "__pycache__", ".git"}
                or part.endswith(".egg-info") for part in relative.parts) or path.suffix == ".pyc"):
            continue
        digest.update(relative.as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _prepare_source_plugins(
    *, target_dir: Path, previous_dir: Path | None, selected: tuple[str, ...],
    venv_python: Path, installer: str, env: dict[str, str],
    run_cmd: Callable[..., Any], operations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    results = []
    store = source_plugin_store(target_dir)
    for plugin_id in PLUGINS:
        obsolete = store / plugin_id
        if plugin_id not in selected and obsolete.exists():
            # Preserve previous staged files while removing this release's selection.
            obsolete.rename(store / f".disabled-{plugin_id}-{time.time_ns()}")
    if not selected:
        return results
    store.mkdir(parents=True, exist_ok=True)
    for plugin_id in selected:
        item = PLUGINS[plugin_id]
        package = target_dir / "plugins" / item["source"]
        fingerprint = _source_plugin_fingerprint(package)
        version = tomllib.loads((package / "pyproject.toml").read_text())["project"]["version"]
        destination = store / plugin_id
        reusable = None
        for release in (target_dir, previous_dir):
            if release is None:
                continue
            try:
                receipt = read_source_plugin(release, plugin_id)
            except (OSError, ValueError, KeyError):
                continue
            if receipt and receipt["source_sha256"] == fingerprint and receipt["version"] == version:
                reusable = source_plugin_store(release) / plugin_id
                break
        result = {"operation": "prepare_source_plugin", "plugin": plugin_id,
                  "version": version, "source_sha256": fingerprint, "directory": str(destination)}
        if reusable == destination:
            result.update(status="unchanged", installed=False, ok=True)
        else:
            with tempfile.TemporaryDirectory(prefix=f".{plugin_id}-", dir=store) as temporary:
                staged = Path(temporary) / "package"
                if reusable is not None:
                    shutil.copytree(reusable, staged)
                    result.update(status="reused", installed=False, reused_from=str(reusable))
                else:
                    staged.mkdir()
                    command = (["uv", "pip", "install", "-p", str(venv_python)] if installer == "uv"
                               else [str(venv_python), "-m", "pip", "install"])
                    command += ["--no-deps", *(["--no-compile"] if installer == "pip" else []),
                                "--target", str(staged / "site"), "./plugins/" + item["source"]]
                    try:
                        _run_required(command, cwd=target_dir, run_cmd=run_cmd, operations=operations,
                                      timeout=600, env=env)
                    except RuntimeError:
                        if installer != "uv" or _upgrade_installer_mode() != "auto":
                            raise
                        # uv-created environments need not contain pip. Bootstrap
                        # it without rebuilding dependencies, then retry this plugin.
                        _run_required([str(venv_python), "-m", "ensurepip", "--upgrade"],
                                      cwd=target_dir, run_cmd=run_cmd, operations=operations, timeout=300, env=env)
                        command = [str(venv_python), "-m", "pip", "install", "--no-deps", "--no-compile",
                                   "--upgrade", "--target", str(staged / "site"), "./plugins/" + item["source"]]
                        _run_required(command, cwd=target_dir, run_cmd=run_cmd, operations=operations,
                                      timeout=600, env=env)
                        result["fallback"] = "pip"
                    files = {p.relative_to(staged / "site").as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in (staged / "site").rglob("*") if p.is_file()}
                    _write_json_atomic_file(staged / "receipt.json", {
                        "schema_version": 1, "plugin_id": plugin_id, "distribution": item["distribution"],
                        "version": version, "source_sha256": fingerprint, "files": files,
                    })
                    result.update(status="installed", installed=True)
                if destination.exists():
                    shutil.rmtree(destination)
                staged.replace(destination)
            try:
                read_source_plugin(target_dir, plugin_id)
            except (OSError, ValueError, KeyError) as exc:
                raise RuntimeError(f"{plugin_id}: installed source plugin validation failed: {exc}") from exc
            result["ok"] = True
        operations.append(result)
        results.append(result)
    return results


def _pip_install_commands(
    venv_python: Path, *, target_dir: Path, include_pm: bool = False, include_longbridge: bool = False,
    include_position_advice: bool = False,
    include_feishu: bool = False,
    include_wechat: bool = False,
    provider_plugins: tuple[str, ...] = (),
) -> list[tuple[list[str], int]]:
    commands = [
        ([str(venv_python), "-m", "pip", "install", "-U", "pip"], 600),
        ([str(venv_python), "-m", "pip", "install", "-r", "requirements.txt", "-c", "constraints.txt"], 1200),
    ]
    commands.extend(_plugin_dependency_commands(
        venv_python, target_dir=target_dir, include_pm=include_pm, include_position_advice=include_position_advice,
        include_feishu=include_feishu, include_wechat=include_wechat, provider_plugins=provider_plugins,
    ))
    if include_longbridge:
        commands.append(([str(venv_python), "-m", "pip", "install", "-r", "requirements/longbridge.txt"], 600))
    return commands


def _uv_install_commands(
    venv_python: Path,
    *,
    venv_dir: Path,
    target_dir: Path,
    include_server: bool,
    bootstrap_python: str,
    include_pm: bool = False,
    include_longbridge: bool = False,
    include_position_advice: bool = False,
    include_feishu: bool = False,
    include_wechat: bool = False,
    provider_plugins: tuple[str, ...] = (),
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
    commands.extend(_plugin_dependency_commands(
        venv_python, target_dir=target_dir, include_pm=include_pm, include_position_advice=include_position_advice,
        include_feishu=include_feishu, include_wechat=include_wechat, provider_plugins=provider_plugins,
        installer="uv",
    ))
    if include_longbridge:
        commands.append(([
            "uv", "pip", "install", "-p", str(venv_python), "-r", "requirements/longbridge.txt",
        ], 600))
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
    include_pm: bool = False,
    include_longbridge: bool = False,
    include_position_advice: bool = False,
    include_feishu: bool = False,
    include_wechat: bool = False,
    provider_plugins: tuple[str, ...] = (),
) -> None:
    if not venv_python.exists():
        command = [bootstrap_python, "-m", "venv", str(venv_dir)]
        _run_required(command, cwd=target_dir, run_cmd=run_cmd, operations=operations, env=env, timeout=300)
        operations[-1]["runtime_prepare_installer"] = "pip"
        commands.append(command)
    pip_commands = _pip_install_commands(
        venv_python, target_dir=target_dir, include_pm=include_pm, include_longbridge=include_longbridge,
        include_position_advice=include_position_advice,
        include_feishu=include_feishu, include_wechat=include_wechat, provider_plugins=provider_plugins,
    )
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


def _is_runtime_bundle(root: Path) -> bool:
    return (root / "om.runtime.json").exists() or (root / "runtime").exists()


def _release_python(target_dir: Path) -> Path:
    if _is_runtime_bundle(target_dir):
        manifest = runtime_bundle.load_manifest(target_dir)
        return target_dir / manifest["python"]["executable"]
    return target_dir / ".venv" / "bin" / "python"


def _venv_python(venv_dir: Path) -> Path:
    return venv_dir / "bin" / "python"


def _ensure_bot_runtime(
    target_dir: Path,
    run_cmd: Callable[..., Any],
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    readiness = _runtime_readiness(release_dir=target_dir)
    _run_required([str(_release_python(target_dir)), "-c",
                   "import src.application.bot.host; import src.application.bot.runtime"],
                  cwd=target_dir, run_cmd=run_cmd, operations=operations, timeout=30)
    return readiness


def _dependency_context(
    *, include_server: bool, python_spec: str, installer_mode: str,
    include_pm: bool = False, include_longbridge: bool = False,
    include_position_advice: bool = False,
    include_feishu: bool = False,
    include_wechat: bool = False,
    provider_plugins: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "include_server": bool(include_server),
        **({"include_pm": True} if include_pm else {}),
        **({"include_position_advice": True} if include_position_advice else {}),
        **({"include_longbridge": True} if include_longbridge else {}),
        **({"include_feishu": True} if include_feishu else {}),
        **({"include_wechat": True} if include_wechat else {}),
        **({"provider_plugins": sorted(provider_plugins)} if provider_plugins else {}),
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
    include_pm: bool = False,
    include_longbridge: bool = False,
    include_position_advice: bool = False,
    include_feishu: bool = False,
    include_wechat: bool = False,
    provider_plugins: tuple[str, ...] = (),
) -> str:
    # v2 caches contain dependencies only; never reuse a v1 cache with plugin code.
    digest = hashlib.sha256(b"om-dependency-venv-v2\0")
    selected_python_spec = python_spec or f"{sys.version_info.major}.{sys.version_info.minor}"
    context = _dependency_context(
        include_server=include_server,
        python_spec=selected_python_spec,
        installer_mode=installer_mode,
        include_pm=include_pm,
        include_longbridge=include_longbridge,
        include_position_advice=include_position_advice,
        include_feishu=include_feishu, include_wechat=include_wechat, provider_plugins=provider_plugins,
    )
    digest.update(json.dumps(context, sort_keys=True).encode("utf-8"))
    digest.update(b"\n")
    for path in _dependency_files(
        target_dir, include_server=include_server, include_pm=include_pm, include_longbridge=include_longbridge,
        include_position_advice=include_position_advice,
        include_feishu=include_feishu, include_wechat=include_wechat, provider_plugins=provider_plugins,
    ):
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


def _dependency_files(
    target_dir: Path, *, include_server: bool, include_pm: bool = False, include_longbridge: bool = False,
    include_position_advice: bool = False,
    include_feishu: bool = False,
    include_wechat: bool = False,
    provider_plugins: tuple[str, ...] = (),
) -> list[Path]:
    roots = [target_dir / "requirements.txt", target_dir / "constraints.txt"]
    if include_server:
        roots.extend(
            [
                target_dir / "requirements" / "server.txt",
                target_dir / "constraints" / "server.txt",
            ]
        )
    if include_longbridge:
        roots.append(target_dir / "requirements/longbridge.txt")
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
    for selected, name in (
        (include_pm, "portfolio-management"), (include_position_advice, "position-advice"),
        (include_feishu, "feishu-channel"), (include_wechat, "wechat-channel"),
        *((True, PLUGINS[key]["source"]) for key in provider_plugins),
    ):
        if selected:
            package = target_dir / "plugins" / name
            ordered.append(package / "pyproject.toml")
            ordered.extend(package / name for name in ("setup.py", "setup.cfg") if (package / name).is_file())
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
    if not (_shared_venv_marker(venv_dir).exists() and python.exists() and os.access(python, os.X_OK)):
        return False
    for script in (venv_dir / "bin").glob("pip*"):
        if not script.is_file():
            continue
        first_line = script.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
        if not first_line or not first_line[0].startswith("#!"):
            continue
        interpreter = Path(first_line[0][2:])
        if interpreter.parent != venv_dir / "bin" or not interpreter.exists():
            return False
    return True


def _relocate_venv_scripts(*, venv_dir: Path, built_at: Path) -> None:
    old = f"#!{built_at}/bin/".encode()
    new = f"#!{venv_dir}/bin/".encode()
    for script in (venv_dir / "bin").iterdir():
        if script.is_symlink() or not script.is_file():
            continue
        payload = script.read_bytes()
        if payload.startswith(old):
            script.write_bytes(new + payload[len(old):])


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


def _installed_optional_packages(
    *, previous_dir: Path, run_cmd: Callable[..., Any], operations: list[dict[str, Any]],
) -> tuple[bool, bool, bool]:
    python = _release_python(previous_dir)
    if not python.exists():
        return False, False, False
    # Query this instance, without importing the plugin or inheriting PYTHONPATH.
    code = (
        "import importlib.metadata as m, json\n"
        "installed = []\n"
        "for name in ('om-pm-plugin', 'longbridge', 'om-position-advice-plugin'):\n"
        "    try: m.distribution(name)\n"
        "    except m.PackageNotFoundError: installed.append(False)\n"
        "    else: installed.append(True)\n"
        "print(json.dumps(installed))\n"
    )
    result = _run_required(
        [str(python), "-I", "-c", code], cwd=previous_dir,
        run_cmd=run_cmd, operations=operations, timeout=30,
    )
    try:
        installed = json.loads(result["stdout"])
    except (ValueError, TypeError) as exc:
        raise RuntimeError("could not determine installed optional packages in current runtime") from exc
    if not isinstance(installed, list) or len(installed) != 3 or any(type(item) is not bool for item in installed):
        raise RuntimeError("current runtime optional package probe did not return three booleans")
    return (installed[0] or read_source_plugin(previous_dir, "pm") is not None,
            installed[1],
            installed[2] or read_source_plugin(previous_dir, "position-advice") is not None)


def _message_package_selection(
    *, previous_dir: Path, runtime_root: Path, run_cmd: Callable[..., Any], operations: list[dict[str, Any]],
) -> tuple[bool, bool]:
    plugin_aware = (previous_dir / "src/application/channels/plugins.py").is_file()
    legacy = [
        not plugin_aware and not (previous_dir / f"plugins/{package}/pyproject.toml").is_file()
        and (previous_dir / implementation).is_file()
        for package, implementation in (
            ("feishu-channel", "src/infrastructure/feishu_bot.py"),
            ("wechat-channel", "src/application/channels/wechat_clawbot/ilink_client.py"),
        )
    ]
    # Releases predating channel implementations carry no channel selection to migrate.
    if not plugin_aware and not any(legacy) and not any(
        (previous_dir / f"plugins/{package}/pyproject.toml").is_file()
        for package in ("feishu-channel", "wechat-channel")
    ):
        return False, False
    python = _release_python(previous_dir)
    if not python.exists():
        raise RuntimeError("could not determine installed message packages: current Python is missing")
    code = (
        "import importlib.metadata as m, json\n"
        "installed = []\n"
        "for name in ('om-feishu-channel-plugin', 'om-wechat-channel-plugin'):\n"
        "    try: m.distribution(name)\n"
        "    except m.PackageNotFoundError: installed.append(False)\n"
        "    else: installed.append(True)\n"
        "print(json.dumps(installed))\n"
    )
    result = _run_required(
        [str(python), "-I", "-c", code], cwd=previous_dir,
        run_cmd=run_cmd, operations=operations, timeout=30,
    )
    try:
        installed = json.loads(result["stdout"])
    except (ValueError, TypeError) as exc:
        raise RuntimeError("could not determine installed message packages in current runtime") from exc
    if not isinstance(installed, list) or len(installed) != 2 or any(type(item) is not bool for item in installed):
        raise RuntimeError("current runtime message package probe did not return two booleans")
    if any(legacy):
        enabled = _legacy_message_channels_enabled(runtime_root=runtime_root)
        installed = [present or (builtin and active) for present, builtin, active in zip(installed, legacy, enabled)]
    return (installed[0] or read_source_plugin(previous_dir, "feishu") is not None,
            installed[1] or read_source_plugin(previous_dir, "wechat") is not None)


def _legacy_message_channels_enabled(*, runtime_root: Path) -> tuple[bool, bool]:
    from src.application.channels.plugins import channel_activity_enabled
    from domain.domain.multi_tick import resolve_notification_route_from_config

    profile_path = runtime_root / "service.profile.json"
    try:
        profile = json.loads(profile_path.read_text()) if profile_path.exists() else {}
        if not isinstance(profile, dict):
            raise ValueError("service profile must be an object")
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"could not determine legacy message channels from {profile_path}") from exc
    paths = {
        _runtime_config_path_for_market(market=market, runtime_root=runtime_root, profile=profile)
        for market in _verify_markets(runtime_root=runtime_root, profile=profile)
    }
    bot = _bot_config_path_from_profile(profile=profile, runtime_root=runtime_root)
    if bot:
        paths.add(Path(bot).expanduser().resolve())
    configs = []
    for path in sorted(paths):
        try:
            config = json.loads(path.read_text())
            if not isinstance(config, dict):
                raise ValueError("runtime config must be an object")
            configs.append(config)
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"could not determine legacy message channels from {path}") from exc
    enabled = []
    service_names = {
        str(item.get("name", "") if isinstance(item, dict) else item)
        for item in profile.get("services", [])
    }
    for channel, profile_key, unit in (
        ("feishu", "feishu_ws", "feishu-ws"), ("wechat_clawbot", "wechat_clawbot", "wechat-clawbot"),
    ):
        section = profile.get(profile_key, {})
        receive = isinstance(section, dict) and section.get("enabled") is True or bool(
            service_names & {f"options-monitor-{unit}.service", f"com.options-monitor.{unit}"}
        )
        selected_configs = configs
        if isinstance(section, dict) and section.get("config_key"):
            selected_path = _runtime_config_path_for_market(
                market=section["config_key"], runtime_root=runtime_root, profile=profile,
            )
            selected_configs = [json.loads(selected_path.read_text())]
        active = receive and (not selected_configs or any(
            channel_activity_enabled(config, channel, activity="receive") for config in selected_configs
        ))
        active = active or any(
            "channels" in config and channel_activity_enabled(config, channel, activity="receive")
            for config in configs
        )
        for config in configs:
            notifications = config.get("notifications", {})
            if not isinstance(notifications, dict):
                raise RuntimeError("could not determine legacy message channels: notifications must be an object")
            if "enabled" in notifications and type(notifications["enabled"]) is not bool:
                raise RuntimeError("could not determine legacy message channels: notifications.enabled must be a boolean")
            if not notifications.get("enabled", True):
                continue
            # Only a declared notification section carries legacy outbound intent.
            if not notifications:
                continue
            provider = resolve_notification_route_from_config(config=config)["provider"]
            if provider == ("feishu_app" if channel == "feishu" else "wechat_clawbot"):
                legacy_config = {**config, "notifications": {**notifications,
                    "enabled": notifications.get("enabled", True)}}
                active = active or channel_activity_enabled(legacy_config, channel, activity="notify")
        enabled.append(bool(active))
    return enabled[0], enabled[1]


def _validate_message_runtime(
    *, target_dir: Path, include_feishu: bool, include_wechat: bool,
    run_cmd: Callable[..., Any], operations: list[dict[str, Any]],
) -> None:
    if not any((include_feishu, include_wechat)) and not (
        target_dir / "src/application/channels/plugins.py"
    ).is_file():
        return
    code = (
        "import importlib.metadata as m, json, sys, tomllib\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from src.application.channels.plugins import load_channel_plugin\n"
        "for channel, package, name, selected in (\n"
        "    ('feishu', 'feishu-channel', 'om-feishu-channel-plugin', json.loads(sys.argv[2])),\n"
        "    ('wechat_clawbot', 'wechat-channel', 'om-wechat-channel-plugin', json.loads(sys.argv[3]))):\n"
        "    try: dist = m.distribution(name)\n"
        "    except m.PackageNotFoundError: dist = None\n"
        "    if selected:\n"
        "        assert dist is not None, name + ' lost during upgrade'\n"
        "        spec = tomllib.loads((Path(sys.argv[1]) / 'plugins' / package / 'pyproject.toml').read_text())\n"
        "        assert dist.version == spec['project']['version'], name + ' version differs from target release'\n"
        "        load_channel_plugin(channel)\n"
        "        if channel == 'feishu':\n"
        "            load_channel_plugin(channel, module='bot')\n"
        "            load_channel_plugin(channel, module='ws_client')\n"
        "            import lark_oapi\n"
        "        else: load_channel_plugin(channel, module='ilink_client')\n"
        "    else:\n"
        "        assert dist is None, name + ' unexpectedly installed in cached runtime'\n"
    )
    _run_required(
        [str(_release_python(target_dir)), "-I", "-c", code, str(target_dir),
         json.dumps(include_feishu), json.dumps(include_wechat)],
        cwd=target_dir, run_cmd=run_cmd, operations=operations, timeout=30,
    )


def _provider_catalog_supported(release_dir: Path) -> bool:
    path = release_dir / "src/application/plugin_runtime.py"
    if not path.is_file():
        return False
    try:
        tree = ast.parse(path.read_text())
        value = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == "PLUGINS" for target in node.targets))
        catalog = ast.literal_eval(value)
        present = set(PROVIDER_PLUGINS) & set(catalog)
        if not present:
            legacy = set(PLUGINS) - set(PROVIDER_PLUGINS)
            if set(catalog) != legacy or any(
                any(catalog[key].get(field) != PLUGINS[key][field] for field in ("distribution", "module"))
                for key in legacy
            ):
                raise ValueError("unrecognized legacy plugin catalog")
            return False
        if present != set(PROVIDER_PLUGINS) or any(
            any(catalog[key].get(field) != PLUGINS[key][field] for field in ("distribution", "module"))
            for key in PROVIDER_PLUGINS
        ):
            raise ValueError("incomplete provider catalog")
        return True
    except (OSError, ValueError, SyntaxError, StopIteration, TypeError, AttributeError) as exc:
        raise RuntimeError(f"could not determine provider package support from {path}") from exc


def _provider_upgrade_selection(
    *, previous_dir: Path, target_dir: Path, runtime_root: Path, managed: list[str],
    run_cmd: Callable[..., Any], operations: list[dict[str, Any]],
) -> tuple[list[str], tuple[str, ...]]:
    if not _provider_catalog_supported(target_dir):
        return managed, ()
    if _provider_catalog_supported(previous_dir):
        code = (
            "import importlib.metadata as m,json\n"
            "selected=[]\n"
            f"for key,name in {[(key, PLUGINS[key]['distribution']) for key in PROVIDER_PLUGINS]!r}:\n"
            "    try: m.distribution(name)\n"
            "    except m.PackageNotFoundError: pass\n"
            "    else: selected.append(key)\n"
            "print(json.dumps(selected))\n"
        )
        result = _run_required([str(_release_python(previous_dir)), "-I", "-c", code],
                               cwd=previous_dir, run_cmd=run_cmd, operations=operations, timeout=30)
        try:
            selected = json.loads(result["stdout"])
            if (not isinstance(selected, list) or any(key not in PROVIDER_PLUGINS for key in selected)
                    or len(set(selected)) != len(selected)):
                raise ValueError("invalid provider selection")
        except (ValueError, TypeError) as exc:
            raise RuntimeError("could not determine installed provider packages in current runtime") from exc
        selected.extend(key for key in PROVIDER_PLUGINS
                        if key not in selected and read_source_plugin(previous_dir, key) is not None)
        pip_selected = tuple(key for key in selected if key not in managed)
        if "market-opend" not in {*managed, *pip_selected}:
            raise RuntimeError("current runtime has no market package; use om plugins install market-opend --confirm")
        return managed, pip_selected

    from src.application.account_config import accounts_from_config, resolve_account_type

    selected = {"market-opend"}
    profile = _load_service_profile(runtime_root)
    markets = _verify_markets(runtime_root=runtime_root, profile=profile)
    if not markets:
        raise RuntimeError("could not determine legacy broker selection: no effective runtime configs")
    for market in markets:
        path = _runtime_config_path_for_market(market=market, runtime_root=runtime_root, profile=profile)
        try:
            config = json.loads(path.read_text())
            if not isinstance(config, dict):
                raise ValueError("runtime config must be an object")
            for account in accounts_from_config(config):
                selected.add("broker-" + resolve_account_type(config, account=account))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"could not determine legacy broker selection from {path}") from exc
    return list(dict.fromkeys([*managed, *(key for key in PROVIDER_PLUGINS if key in selected)])), ()


def _validate_provider_runtime(
    *, target_dir: Path, selected: tuple[str, ...], run_cmd: Callable[..., Any], operations: list[dict[str, Any]],
) -> None:
    if not _provider_catalog_supported(target_dir):
        return
    code = (
        "import importlib.metadata as m,json,sys,tomllib\n"
        "from pathlib import Path\n"
        "sys.dont_write_bytecode=True\n"
        "sys.path.insert(0,sys.argv[1])\n"
        "from src.application.plugin_runtime import PLUGINS,validate_plugin_import\n"
        f"for key in {PROVIDER_PLUGINS!r}:\n"
        "    item=PLUGINS[key]\n"
        "    try: dist=m.distribution(item['distribution'])\n"
        "    except m.PackageNotFoundError: dist=None\n"
        "    if key in json.loads(sys.argv[2]):\n"
        "        assert dist is not None, key+' lost during upgrade'\n"
        "        project=Path(sys.argv[1])/'plugins'/item['source']/'pyproject.toml'\n"
        "        assert dist.version == tomllib.loads(project.read_text())['project']['version'], key+' version mismatch'\n"
        "        validate_plugin_import(key)\n"
        "    else: assert dist is None, key+' unexpectedly installed in cached runtime'\n"
    )
    _run_required([str(_release_python(target_dir)), "-I", "-c", code, str(target_dir), json.dumps(selected)],
                  cwd=target_dir, run_cmd=run_cmd, operations=operations, timeout=30)


def _validate_rollback_pip_providers(
    *, target_dir: Path, managed: list[str], run_cmd: Callable[..., Any], operations: list[dict[str, Any]],
) -> None:
    if not _provider_catalog_supported(target_dir):
        return
    # Inspect the target environment itself; rollback never reconstructs or installs a selection.
    code = (
        "import importlib.metadata as m,json,sys\n"
        "sys.dont_write_bytecode=True\n"
        "sys.path.insert(0,sys.argv[1])\n"
        "from src.application.plugin_runtime import PLUGINS,plugin_installed,validate_plugin_import\n"
        "managed=json.loads(sys.argv[2])\n"
        "market=any(PLUGINS[key].get('kind')=='market' for key in managed)\n"
        f"for key in {PROVIDER_PLUGINS!r}:\n"
        "    if key in managed: continue\n"
        "    try: m.distribution(PLUGINS[key]['distribution'])\n"
        "    except m.PackageNotFoundError: continue\n"
        "    assert plugin_installed(key), key+' unavailable or incompatible in rollback target'\n"
        "    validate_plugin_import(key)\n"
        "    market=market or PLUGINS[key].get('kind')=='market'\n"
        "assert market, 'rollback target has no valid market package; use om plugins install market-opend --confirm'\n"
    )
    _run_required([str(_release_python(target_dir)), "-I", "-c", code, str(target_dir), json.dumps(managed)],
                  cwd=target_dir, run_cmd=run_cmd, operations=operations, timeout=30)


def _managed_plugin_selection(release_dir: Path) -> list[str]:
    selected = []
    for plugin_id in PLUGINS:
        try:
            if read_managed_plugin(release_dir, plugin_id) is not None:
                selected.append(plugin_id)
        except ValueError as exc:
            raise ServiceTransitionError(
                f"invalid managed plugin {plugin_id} in {release_dir}: {exc}",
                status="managed_plugin_invalid",
            ) from exc
    return selected


def _preserve_managed_plugins(
    *, target_dir: Path, selected: list[str], run_cmd: Callable[..., Any], operations: list[dict[str, Any]],
) -> None:
    # A failed earlier attempt must not resurrect a subsequently removed plugin.
    unexpected = set(_managed_plugin_selection(target_dir)) - set(selected)
    if unexpected:
        raise ServiceTransitionError(
            "target release has unselected managed plugins: " + ", ".join(sorted(unexpected))
            + "; remove them explicitly from the target release before retrying",
            status="managed_plugin_selection_mismatch",
        )
    for plugin_id in selected:
        result = plugin_distribution.install_plugin(
            plugin_id, repo_root=target_dir, confirm=True, python_executable=_release_python(target_dir),
        )
        operations.append({"operation": "preserve_managed_plugin", **result})
    _validate_target_managed_plugins(
        target_dir=target_dir, selected=selected, run_cmd=run_cmd, operations=operations,
    )


def _validate_target_managed_plugins(
    *, target_dir: Path, selected: list[str], run_cmd: Callable[..., Any], operations: list[dict[str, Any]],
) -> None:
    if not selected:
        return
    # Exercise the target's ordinary finder/loaders, not the previous store or staged import path.
    code = (
        "import importlib, json, sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import src\n"
        "from src.application import plugin_runtime\n"
        "from src.application.plugin_runtime import PLUGINS, plugin_store, read_managed_plugin\n"
        "root = Path(sys.argv[1]).resolve()\n"
        "version = (root / 'VERSION').read_text().strip()\n"
        "for plugin_id in json.loads(sys.argv[2]):\n"
        "    receipt = read_managed_plugin(root, plugin_id)\n"
        "    assert receipt is not None, 'managed plugin missing from target release'\n"
        "    assert receipt['version'] == version, 'managed wheel version differs from target OM release'\n"
        "    module = PLUGINS[plugin_id]['module']\n"
        "    plugin = importlib.import_module(module)\n"
        "    expected = plugin_store(root) / plugin_id / 'site' / module / '__init__.py'\n"
        "    assert Path(plugin.__file__).resolve() == expected.resolve(), 'plugin did not load from target store'\n"
        "    if hasattr(plugin_runtime, 'validate_plugin_import'):\n"
        "        plugin_runtime.validate_plugin_import(plugin_id)\n"
        # Historical rollback targets predate the shared four-plugin validator.
        "    elif plugin_id == 'pm':\n"
        "        from src.application.portfolio_management import load_portfolio_management_client_class\n"
        "        assert callable(load_portfolio_management_client_class())\n"
        "    elif plugin_id == 'position-advice':\n"
        "        from domain.domain.close_advice import require_position_advice_plugin\n"
        "        require_position_advice_plugin()\n"
        "    else:\n"
        "        raise ImportError('target release lacks channel plugin validation')\n"
    )
    _run_required(
        [str(_release_python(target_dir)), "-I", "-c", code, str(target_dir), json.dumps(selected)],
        cwd=target_dir, run_cmd=run_cmd, operations=operations, timeout=30,
    )


def _legacy_pm_enabled(*, previous_dir: Path, runtime_root: Path) -> bool:
    if ((previous_dir / "src/application/plugin_runtime.py").is_file()
            or (previous_dir / "plugins/portfolio-management/pyproject.toml").is_file()):
        return False
    if not (previous_dir / "src/infrastructure/portfolio_management_client.py").is_file():
        return False
    from src.application.portfolio_management import portfolio_management_enabled

    profile = _load_service_profile(runtime_root)
    for market in _verify_markets(runtime_root=runtime_root, profile=profile):
        path = _runtime_config_path_for_market(market=market, runtime_root=runtime_root, profile=profile)
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                raise ValueError("runtime config must be an object")
            if portfolio_management_enabled(config):
                return True
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"could not determine legacy PM integration from {path}") from exc
    return False


def _legacy_position_advice_enabled(*, previous_dir: Path, runtime_root: Path) -> bool:
    # A plugin-aware release with no installed distribution represents removal.
    if ((previous_dir / "src/application/plugin_runtime.py").is_file()
            or (previous_dir / "plugins/position-advice/pyproject.toml").is_file()):
        return False
    if not (previous_dir / "domain/domain/close_advice.py").is_file():
        return False
    profile = _load_service_profile(runtime_root)
    markets = _verify_markets(runtime_root=runtime_root, profile=profile)
    if not markets:
        raise RuntimeError(
            f"could not determine legacy Position Advice integration: no effective runtime configs in {runtime_root}"
        )
    for market in markets:
        path = _runtime_config_path_for_market(market=market, runtime_root=runtime_root, profile=profile)
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                raise ValueError("runtime config must be an object")
            advice = config.get("close_advice", {})
            if not isinstance(advice, dict) or type(advice.get("enabled", False)) is not bool:
                raise ValueError("close_advice.enabled must be a boolean")
            if advice.get("enabled", False):
                return True
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"could not determine legacy Position Advice integration from {path}") from exc
    return False


def _validate_optional_runtime(
    *, target_dir: Path, include_pm: bool, include_longbridge: bool, include_position_advice: bool = False,
    run_cmd: Callable[..., Any], operations: list[dict[str, Any]],
) -> None:
    code = (
        "import importlib.metadata as m, sys, tomllib\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import src\n"
        "try: dist = m.distribution('om-pm-plugin')\n"
        "except m.PackageNotFoundError: dist = None\n"
        "if sys.argv[2] == 'true':\n"
        "    assert dist is not None, 'installed PM package lost during upgrade'\n"
        "    spec = tomllib.loads((Path(sys.argv[1]) / 'plugins/portfolio-management/pyproject.toml').read_text())\n"
        "    assert dist.version == spec['project']['version'], 'PM package version differs from target release'\n"
        "    from src.application.portfolio_management import load_portfolio_management_client_class\n"
        "    load_portfolio_management_client_class()\n"
        "else:\n"
        "    assert dist is None, 'PM package unexpectedly installed in cached runtime'\n"
        "try: lb = m.distribution('longbridge')\n"
        "except m.PackageNotFoundError: lb = None\n"
        "if sys.argv[3] == 'true':\n"
        "    assert lb is not None, 'installed Longbridge SDK lost during upgrade'\n"
        "    from longbridge.openapi import Config, TradeContext\n"
        "else:\n"
        "    assert lb is None, 'Longbridge SDK unexpectedly installed in cached runtime'\n"
        "try: advice = m.distribution('om-position-advice-plugin')\n"
        "except m.PackageNotFoundError: advice = None\n"
        "if sys.argv[4] == 'true':\n"
        "    assert advice is not None, 'installed Position Advice package lost during upgrade'\n"
        "    spec = tomllib.loads((Path(sys.argv[1]) / 'plugins/position-advice/pyproject.toml').read_text())\n"
        "    assert advice.version == spec['project']['version'], 'Position Advice package version differs from target release'\n"
        "    from src.application.position_advice_plugin import require_position_advice_plugin\n"
        "    require_position_advice_plugin()\n"
        "else:\n"
        "    assert advice is None, 'Position Advice package unexpectedly installed in cached runtime'\n"
    )
    _run_required(
        [str(_release_python(target_dir)), "-I", "-c", code, str(target_dir),
         json.dumps(include_pm), json.dumps(include_longbridge), json.dumps(include_position_advice)],
        cwd=target_dir, run_cmd=run_cmd, operations=operations, timeout=30,
    )


def _ensure_release_runtime(
    *,
    target_dir: Path,
    cache_root: Path | None = None,
    previous_dir: Path | None = None,
    run_cmd: Callable[..., Any],
    operations: list[dict[str, Any]],
    include_pm: bool = False,
    include_longbridge: bool = False,
    include_position_advice: bool = False,
    include_feishu: bool = False,
    include_wechat: bool = False,
    provider_plugins: tuple[str, ...] = (),
) -> dict[str, Any]:
    _runtime_readiness(release_dir=target_dir)
    if _is_runtime_bundle(target_dir):
        runtime_bundle.load_manifest(target_dir, verify_files=True)
        selected = [name for name, enabled in (
            ("om-pm-plugin", include_pm), ("longbridge", include_longbridge),
            ("om-position-advice-plugin", include_position_advice),
            ("om-feishu-channel-plugin", include_feishu), ("om-wechat-channel-plugin", include_wechat),
        ) if enabled]
        selected.extend(PLUGINS[key]["distribution"] for key in provider_plugins)
        if selected:
            # Installing source extras would invalidate the bundle's pinned inventory.
            raise RuntimePrepareError(
                "runtime bundle cannot preserve optional packages: " + ", ".join(selected)
                + "; optional package installation is supported only by source releases; current remains unchanged",
                runtime_prepare={
                    "installer": "runtime_bundle", "unsupported_optional_packages": selected,
                },
            )
        result = runtime_bundle.validate_runtime(target_dir)
        if result.get("ok") is not True:
            raise RuntimePrepareError("bundled runtime validation failed", runtime_prepare=result)
        return {**result, "installer": "runtime_bundle", "python": str(_release_python(target_dir))}
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
        include_pm=include_pm,
        include_longbridge=include_longbridge,
        include_position_advice=include_position_advice,
        include_feishu=include_feishu, include_wechat=include_wechat, provider_plugins=provider_plugins,
    )
    dependency_context = _dependency_context(
        include_server=include_server, python_spec=python_spec, installer_mode=mode,
        include_pm=include_pm, include_longbridge=include_longbridge,
        include_position_advice=include_position_advice,
        include_feishu=include_feishu, include_wechat=include_wechat, provider_plugins=provider_plugins,
    )
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
        "dependency_hash_schema": 2,
        "legacy_cache_policy": "rebuild_once_then_normal_retention",
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
                target_dir=target_dir,
                include_server=include_server,
                bootstrap_python=bootstrap_python,
                include_pm=include_pm,
                include_longbridge=include_longbridge,
                include_position_advice=include_position_advice,
                include_feishu=include_feishu, include_wechat=include_wechat, provider_plugins=provider_plugins,
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
                    include_pm=include_pm,
                    include_longbridge=include_longbridge,
                    include_position_advice=include_position_advice,
                    include_feishu=include_feishu, include_wechat=include_wechat, provider_plugins=provider_plugins,
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
                include_pm=include_pm,
                include_longbridge=include_longbridge,
                include_position_advice=include_position_advice,
                include_feishu=include_feishu, include_wechat=include_wechat, provider_plugins=provider_plugins,
            )

        if not runtime_prepare["venv_reused"]:
            _shared_venv_marker(build_venv).write_text(utc_now_iso() + "\n", encoding="utf-8")
            # Virtualenv entry-point shebangs are absolute, so the atomic cache
            # rename must relocate them before the cache is declared reusable.
            build_venv.rename(shared_venv)
            _relocate_venv_scripts(venv_dir=shared_venv, built_at=build_venv)
            _link_release_venv(target_dir=target_dir, shared_venv=shared_venv)

        plugin_installer = runtime_prepare["installer"]
        if plugin_installer == "cache":
            plugin_installer = "uv" if mode in {"auto", "uv"} and _check_uv_available(
                target_dir=target_dir, run_cmd=run_cmd, operations=operations,
            ) else "pip"
        runtime_prepare["source_plugins"] = _prepare_source_plugins(
            target_dir=target_dir, previous_dir=previous_dir,
            selected=_selected_source_plugins(
                include_pm=include_pm, include_position_advice=include_position_advice,
                include_feishu=include_feishu, include_wechat=include_wechat, provider_plugins=provider_plugins,
            ),
            venv_python=_venv_python(shared_venv), installer=plugin_installer, env=install_env,
            run_cmd=run_cmd, operations=operations,
        )
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
        runtime_prepare["bot_runtime"] = _ensure_bot_runtime(target_dir, run_cmd, operations)
        _validate_optional_runtime(
            target_dir=target_dir, include_pm=include_pm, include_longbridge=include_longbridge,
            include_position_advice=include_position_advice,
            run_cmd=run_cmd, operations=operations,
        )
        _validate_message_runtime(
            target_dir=target_dir, include_feishu=include_feishu, include_wechat=include_wechat,
            run_cmd=run_cmd, operations=operations,
        )
        _validate_provider_runtime(target_dir=target_dir, selected=provider_plugins, run_cmd=run_cmd, operations=operations)
        shared_venv.touch()
        runtime_prepare["ended_at"] = utc_now_iso()
        runtime_prepare["duration_seconds"] = round(time.monotonic() - started, 3)
        return runtime_prepare
    except (RuntimeError, OSError, ValueError) as exc:
        if not runtime_prepare["venv_reused"]:
            shutil.rmtree(build_venv, ignore_errors=True)
            shutil.rmtree(shared_venv, ignore_errors=True)
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
    try:
        runtime_readiness = _runtime_readiness(release_dir=repo)
    except ServiceTransitionError as exc:
        runtime_readiness = {
            "ok": False,
            "status": exc.status,
            "error": str(exc),
            "remediation": exc.remediation,
        }
    update_ok = True
    if update_check is not None:
        update_ok = bool(update_check.get("ok")) and not bool(update_check.get("upgrade_available"))
    config_ok = all(bool(item.get("ok")) for item in configs.values()) if configs else False
    service_ok = bool(services.get("ok", True))
    upgrade_ok = not _upgrade_status_failed(upgrade_status)
    runtime_ok = runtime_readiness.get("ok") is True
    ok = bool(current_version) and update_ok and config_ok and service_ok and upgrade_ok and runtime_ok
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
        "runtime_readiness": runtime_readiness,
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


def _runtime_readiness(*, release_dir: Path) -> dict[str, Any]:
    if not (release_dir / "src/application/bot/runtime.py").is_file():
        raise ServiceTransitionError(
            f"Unsupported Bot runtime in release: {release_dir}; a Python Bot release is required",
            status="unsupported_runtime",
            remediation=["select a release containing the Python Bot runtime; legacy runtime execution is retired"],
        )
    return {"ok": True, "runtime": "python"}


# The public drift CLI recaptures timer state; compensation needs the original
# snapshot. Execute only this bounded sequence with the restored release's owners.
_RESTORED_SERVICE_COMPENSATION = """
import json
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from src.application import service_upgrade as upgrade
request = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
profile = request["profile"]
root = Path(request["repo_root"])
result = {"ok": False, "config_validate": [], "service_reconcile": {},
          "restarted_services": [], "service_health": {}, "operations": [],
          "errors": [], "remediation": []}
phase = "validate restored config"
try:
    targets = upgrade._profile_runtime_config_targets(profile)
    for target in targets:
        if target.get("source") == "yaml" and target.get("config_yaml"):
            upgrade._run_required(
                [str(root / "om"), "config", "validate", "--source", "yaml",
                 "--config-yaml", target["config_yaml"], "--market", target["market"]],
                cwd=root, run_cmd=subprocess.run, operations=result["operations"], timeout=120)
    result["config_validate"] = upgrade._validate_committed_runtime_configs(
        prepared={"targets": targets}, cwd=root, run_cmd=subprocess.run,
        operations=result["operations"])
    phase = "restore services"
    result["service_reconcile"] = upgrade.service_drift(
        repo_root=root, runtime_root=Path(request["runtime_root"]),
        profile_path=Path(request["runtime_root"]) / "service.profile.json",
        profile=profile, confirm=True, activation_policy=request["activation_policy"],
        preserved_activation_states=request["preserved_activation_states"],
        run_cmd=subprocess.run)
    reconcile = result["service_reconcile"]
    if reconcile.get("summary", {}).get("ok") is not True or reconcile.get("apply_errors"):
        result["remediation"].extend(upgrade._service_reconcile_remediation(reconcile))
        raise RuntimeError("restored service reconciliation did not succeed")
    if profile.get("service_provider") == "launchd":
        profile = {**profile, "_launchd_expected_states": request["preserved_activation_states"]}
    if request["restart_services"]:
        phase = "restore restart"
        result["restarted_services"] = upgrade._restart_services_from_loaded_profile(
            profile=profile, run_cmd=subprocess.run, operations=result["operations"])
        if profile.get("service_provider") == "launchd":
            result["restarted_services"] = list((reconcile.get("applied") or {}).get("restarted_services") or [])
        phase = "restore health"
        result["service_health"] = upgrade._post_upgrade_service_health(
            profile=profile, repo_root=root, run_cmd=subprocess.run,
            operations=result["operations"])
        if result["service_health"].get("ok") is not True:
            result["remediation"].extend(result["service_health"].get("remediation") or [])
            raise RuntimeError("restored service health did not succeed")
    result["ok"] = True
except Exception as exc:
    result["errors"].append(f"{phase}: {type(exc).__name__}: {exc}")
    result["remediation"].extend(getattr(exc, "remediation", []) or [])
    if phase == "restore restart":
        result["restarted_services"] = getattr(exc, "restarted_services", [])
print(json.dumps(result))
sys.exit(0 if result["ok"] else 1)
"""


def _restore_services_from_release(
    *, previous_dir: Path, repo_link: Path, runtime_root: Path,
    previous_profile: dict[str, Any], restart_services: bool,
    activation_policy: str, preserved_activation_states: dict[str, dict[str, str]],
    run_cmd: Callable[..., Any], operations: list[dict[str, Any]],
) -> dict[str, Any]:
    request = {
        "profile": previous_profile, "repo_root": str(repo_link),
        "runtime_root": str(runtime_root), "restart_services": restart_services,
        "activation_policy": activation_policy,
        "preserved_activation_states": preserved_activation_states,
    }
    # Private input is never printed or embedded in the logged command. -I stops
    # inherited PYTHONPATH/site settings from importing the newer controller.
    with tempfile.TemporaryDirectory(prefix="om-compensation-") as directory:
        path = Path(directory) / "request.json"
        with path.open("x", encoding="utf-8") as handle:
            os.chmod(path, 0o600)
            json.dump(request, handle)
        services = _restart_service_names(previous_profile) if restart_services else []
        timeout = 300 + 240 * len(_profile_runtime_config_targets(previous_profile))
        timeout += 120 * len(services) + 700 * sum("opend" in name for name in services)
        command = [str(_release_python(previous_dir)), "-I", "-c",
                   _RESTORED_SERVICE_COMPENSATION, str(previous_dir), str(path)]
        process = _run_command(command, cwd=previous_dir, run_cmd=run_cmd,
                               timeout=timeout, stdout_limit=None)
    try:
        result = json.loads(process.get("stdout") or "")
    except (TypeError, ValueError):
        result = None
    valid = (isinstance(result, dict) and isinstance(result.get("ok"), bool)
             and isinstance(result.get("config_validate"), list)
             and isinstance(result.get("service_reconcile"), dict)
             and isinstance(result.get("restarted_services"), list)
             and isinstance(result.get("service_health"), dict)
             and isinstance(result.get("operations"), list)
             and isinstance(result.get("errors"), list)
             and isinstance(result.get("remediation"), list))
    process["operation"] = "restore_services_from_release"
    process["stdout"] = _clip_command_output(str(process.get("stdout") or ""), 4000)
    operations.append(process)
    if not valid:
        return {"ok": False, "errors": ["restored release returned no valid compensation result"],
                "remediation": ["inspect restored release compensation stderr/exit status"]}
    operations.extend(result.pop("operations"))
    if process.get("ok") is not True or process.get("returncode") != 0:
        result["ok"] = False
        result["errors"].append("restored release compensation command failed")
    if result["ok"] and (result["errors"] or
                         result["service_reconcile"].get("summary", {}).get("ok") is not True or
                         result["service_reconcile"].get("apply_errors") or
                         (restart_services and result["service_health"].get("ok") is not True)):
        result["ok"] = False
        result["errors"].append("restored release compensation result is inconsistent")
    return result


def _compensate_service_transition(
    *,
    repo_link: Path,
    previous_dir: Path,
    runtime_root: Path,
    previous_profile: dict[str, Any],
    config_commit: dict[str, Any],
    restart_services: bool,
    activation_policy: str,
    preserved_activation_states: dict[str, dict[str, str]],
    run_cmd: Callable[..., Any],
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    errors: list[str] = []
    symlink_restored = False
    runtime_readiness: dict[str, Any] = {}
    config_restore: dict[str, Any] = {"ok": True, "status": "skipped", "restored": [], "errors": []}
    try:
        runtime_readiness = _runtime_readiness(release_dir=previous_dir)
    except ServiceTransitionError as exc:
        return {
            "ok": False,
            "status": exc.status,
            "symlink_restored": False,
            "config_restore": config_restore,
            "service_reconcile": {},
            "restarted_services": [],
            "service_health": {},
            "runtime_readiness": {},
            "remediation": exc.remediation,
            "errors": [str(exc)],
        }
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
        errors.extend(f"restore config: {item}" for item in config_restore.get("errors") or ["restoration failed without details"])

    restored: dict[str, Any] = {}
    if symlink_restored and not errors and previous_profile:
        try:
            restored = _restore_services_from_release(
                previous_dir=previous_dir, repo_link=repo_link, runtime_root=runtime_root,
                previous_profile=previous_profile, restart_services=restart_services,
                activation_policy=activation_policy,
                preserved_activation_states=preserved_activation_states,
                run_cmd=run_cmd, operations=operations,
            )
        except Exception as exc:
            errors.append(f"restore services: {type(exc).__name__}: {exc}")
        else:
            if restored.get("ok") is not True:
                errors.extend(str(item) for item in restored.get("errors") or ["restored services failed"])
    remediation = list(restored.get("remediation") or [])
    if errors:
        remediation.extend([
            "compensation incomplete: do not restart until the restored release validates its configuration",
            "restore compatible authoring YAML and generated configs from an operator backup; independent migrations were not reverted",
            f"validate and reconcile with restored release: {previous_dir / 'om'} service drift --repo-root {repo_link} --runtime-root {runtime_root}",
        ])
    return {
        "ok": not errors,
        "symlink_restored": symlink_restored,
        "config_restore": config_restore,
        "config_validate": restored.get("config_validate", []),
        "service_reconcile": restored.get("service_reconcile", {}),
        "restarted_services": restored.get("restarted_services", []),
        "service_health": restored.get("service_health", {}),
        "runtime_readiness": runtime_readiness,
        "remediation": remediation,
        "errors": errors,
    }


def _preflight_service_transition(
    *, profile: dict[str, Any], target_dir: Path, restart_services: bool,
    snapshot: dict[str, Any], run_cmd: Callable[..., Any], operations: list[dict[str, Any]],
) -> None:
    if not profile:
        return
    provider = profile.get("service_provider")
    if provider not in {"systemd", "launchd"}:
        raise ServiceTransitionError("unsupported service upgrade provider", status="unsupported_service_provider", remediation=[])
    if provider == "launchd":
        if not restart_services and any(row["state"] != "absent" for row in snapshot.values()):
            raise ServiceTransitionError("loaded launchd workers require controlled reconciliation", status="service_restart_required", remediation=["retry with service restart enabled"])
        probe = ("from pathlib import Path; import sys; "
                 "from src.application.service_lifecycle import launchd_upgrade_state, reconcile_launchd_services; "
                 "from src.application.service_deploy import load_service_profile; import subprocess; "
                 "launchd_upgrade_state(profile=load_service_profile(Path(sys.argv[1])/'service.profile.json'), "
                 "repo_root=Path(sys.argv[2]), runtime_root=Path(sys.argv[1]), run_cmd=subprocess.run)")
        _run_required([str(_release_python(target_dir)), "-c", probe,
                       str(profile["runtime_root"]), str(profile["repo_root"])],
                      cwd=target_dir, run_cmd=run_cmd, operations=operations, timeout=60)


def _cleanup_upgrade_venvs(
    *, cache_root: Path, releases_root: Path, target_dir: Path, runtime_root: Path,
) -> dict[str, Any]:
    from src.application.config_defaults import upgrade_keep_venvs
    from src.application.service_cleanup import cleanup_shared_venvs

    try:
        if _is_runtime_bundle(target_dir):
            return {"ok": True, "status": "skipped", "reason": "runtime bundle has no shared venv"}
        profile = _load_service_profile(runtime_root)
        paths = {_runtime_config_path_for_market(market=market, runtime_root=runtime_root, profile=profile)
                 for market in _verify_markets(runtime_root=runtime_root, profile=profile)}
        policies = set()
        for path in paths:
            if path.is_file():
                policies.add(upgrade_keep_venvs(json.loads(path.read_text())))
        if len(policies) > 1:
            raise ValueError("runtime.upgrade_keep_venvs differs between effective market configs")
        keep = policies.pop() if policies else upgrade_keep_venvs({})
        # A retained release must remain usable for rollback, even with N=0.
        protected = tuple((release / ".venv").resolve() for release in releases_root.iterdir()
                          if release.is_dir() and not release.is_symlink()
                          and re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", release.name)
                          and (release / ".venv").is_symlink())
        return cleanup_shared_venvs(cache_root=cache_root, current_venv=(target_dir / ".venv").resolve(),
                                    keep_other_venvs=keep, protected_venvs=protected)
    except Exception as exc:
        return {"ok": False, "status": "cleanup_failed", "error": f"{type(exc).__name__}: {exc}",
                "deleted_paths": [], "freed_bytes": 0, "operations": []}


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
    preserve_activation_state: bool = False,
    cleanup_after_upgrade: bool = False,
    cleanup_keep_releases: int = 2,
    run_cmd: Callable[..., Any] = subprocess.run,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    runtime = Path(runtime_root).expanduser().resolve()
    if error := _deployment_user_error(runtime):
        return {**error, "operation": "upgrade", "confirmed": bool(confirm)}
    with ExitStack() as transition_stack:
        lock_error: Exception | None = None
        if confirm:
            try:
                transition_stack.enter_context(_UpgradeLock(runtime / "locks" / "upgrade.lock"))
            except Exception as exc:
                lock_error = exc
            else:
                if error := _deployment_user_error(runtime):
                    return {**error, "operation": "upgrade", "confirmed": True}
        _, status_witness = (
            _read_upgrade_preview_json(upgrade_status_path(runtime)) if not confirm else (None, None)
        )
        _, profile_witness = (
            _read_upgrade_preview_json(runtime / "service.profile.json") if not confirm else (None, None)
        )
        repo_link, repo, repo_root_resolution = _coerce_repo_root_to_current_symlink(repo_root=repo_root, runtime_root=runtime)
        releases = Path(releases_root).expanduser().resolve() if releases_root else default_releases_root(repo_link)
        cache = Path(cache_root).expanduser().resolve() if cache_root else default_upgrade_cache_root(repo_link)
        current_version = _read_version(repo)
        repo_root_is_symlink = repo_link.is_symlink()
        preview_baseline = (repo_link, repo, repo_root_resolution, current_version, repo_root_is_symlink)
        activation_policy = (
            SERVICE_ACTIVATION_POLICY_PRESERVE_EXISTING
            if preserve_activation_state
            else SERVICE_ACTIVATION_POLICY_ENSURE_ACTIVE
        )
        check = {} if lock_error is not None else service_upgrade_check(
            repo_root=repo,
            runtime_root=runtime,
            cache_root=cache,
            target_version=target_version,
            remote_name=remote_name,
            run_cmd=run_cmd,
            now_fn=now_fn,
        )
        target = (
            _version_text(str(target_version or ""))
            if lock_error is not None
            else _version_text(str(check.get("latest_version") or "")) if check.get("ok") is True else ""
        )
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
            "activation_policy": activation_policy,
            "preserve_activation_state": bool(preserve_activation_state),
            "cleanup_after_upgrade": bool(cleanup_after_upgrade),
            "cleanup_keep_releases": max(2, int(cleanup_keep_releases or 2)),
            "updated_at": utc_now_iso(now_fn),
        }
        if lock_error is None and not target:
            out = {
                **status_base,
                "ok": False,
                "status": "no_target_version",
                "check": check,
                "changed": False,
                "message": check.get("message") or "没有可升级版本。",
                "operations": operations,
            }
            if confirm:
                write_upgrade_status(runtime_root=runtime, payload=out)
            else:
                _publish_early_upgrade_preview_status(
                    runtime_root=runtime, repo_root=repo_root, baseline=preview_baseline,
                    status_witness=status_witness, profile_witness=profile_witness, payload=out,
                )
            return out
        cmp = compare_versions(current_version, target) if lock_error is None and target else 0
        if lock_error is None and cmp == 0:
            out = {
                **status_base,
                "ok": True,
                "status": "already_current",
                "changed": False,
                "message": check.get("message") or f"没有可升级版本。当前已是最新版本 {current_version}",
                "operations": operations,
            }
            if confirm:
                write_upgrade_status(runtime_root=runtime, payload=out)
            else:
                _publish_early_upgrade_preview_status(
                    runtime_root=runtime, repo_root=repo_root, baseline=preview_baseline,
                    status_witness=status_witness, profile_witness=profile_witness, payload=out,
                )
            return out
        if lock_error is None and cmp > 0:
            out = {
                **status_base,
                "ok": False,
                "status": "target_older_than_current",
                "changed": False,
                "message": check.get("message") or f"当前版本 {current_version} 高于目标版本 {target}",
                "operations": operations,
            }
            if confirm:
                write_upgrade_status(runtime_root=runtime, payload=out)
            else:
                _publish_early_upgrade_preview_status(
                    runtime_root=runtime, repo_root=repo_root, baseline=preview_baseline,
                    status_witness=status_witness, profile_witness=profile_witness, payload=out,
                )
            return out
        if lock_error is None and _major_upgrade_blocked(current_version=current_version, target_version=target, allow_major=allow_major):
            out = {**status_base, "ok": False, "status": "blocked_major_upgrade", "changed": False, "operations": operations}
            if confirm:
                write_upgrade_status(runtime_root=runtime, payload=out)
            else:
                _publish_early_upgrade_preview_status(
                    runtime_root=runtime, repo_root=repo_root, baseline=preview_baseline,
                    status_witness=status_witness, profile_witness=profile_witness, payload=out,
                )
            return out

        target_dir = releases / target
        previous_dir = repo
        pre_upgrade_profile = _load_service_profile(runtime)
        preserved_activation_states: dict[str, dict[str, str]] = {}
        warnings = [] if repo_root_is_symlink else ["confirmed upgrade requires repo_root to be a current symlink"]
        bundled = _is_runtime_bundle(repo)
        planned = [
            f"download and validate runtime bundle {tag} into {target_dir}" if bundled else
            f"materialize {tag} into {target_dir} from git cache {cache / 'git' / 'options-monitor.git'}"
            if not target_dir.exists()
            else f"reuse existing release dir {target_dir}",
            f"verify bundled interpreter and dependencies at {target_dir}" if bundled else f"prepare release runtime at {target_dir / '.venv'}",
            "preserve validated managed plugin selection using exact target release wheels and verify target loading",
            f"validate {target_dir}",
            "verify the target contains the supported Python Bot runtime",
            f"switch {repo_link} -> {target_dir}",
            "reconcile service drift from current release",
            (
                "preserve pre-existing paused timer activation state"
                if preserve_activation_state
                else "repair expected timer activation drift"
            ),
            "restart long-running services" if restart_services else "skip service restart",
            "after success, retain current shared venv plus runtime.upgrade_keep_venvs recent others (default 2), protecting retained release references",
        ]
        if cleanup_after_upgrade:
            planned.append(f"cleanup old releases after successful upgrade, keep {status_base['cleanup_keep_releases']} releases")
        if not confirm:
            try:
                _managed_plugin_selection(previous_dir)
                if (preserve_activation_state or pre_upgrade_profile.get("service_provider") == "launchd") and pre_upgrade_profile:
                    preserved_activation_states = (
                        capture_preserved_timer_activation_states(
                            repo_root=repo_link,
                            runtime_root=runtime,
                            profile=pre_upgrade_profile,
                            run_cmd=run_cmd,
                        )
                    )
                    status_base["preserved_activation_units"] = sorted(
                        preserved_activation_states
                    )
                runtime_readiness = (
                    _runtime_readiness(release_dir=target_dir)
                    if target_dir.exists()
                    else {"ok": True, "status": "pending_materialization"}
                )
            except ServiceTransitionError as exc:
                return {
                    **status_base,
                    "ok": False,
                    "status": exc.status,
                    "changed": False,
                    "target_dir": str(target_dir),
                    "previous_dir": str(previous_dir),
                    "warnings": warnings,
                    "planned_operations": planned,
                    "version_check": check,
                    "remediation": exc.remediation,
                    "error": str(exc),
                    "operations": operations,
                }
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
                "runtime_readiness": runtime_readiness,
                "operations": operations,
            }
        if lock_error is None and not repo_root_is_symlink:
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

        symlink_switched = False
        release_materialize = (
            _release_materialize_summary(tag=str(tag), target_dir=target_dir, cache_root=cache)
            if target else {}
        )
        runtime_prepare: dict[str, Any] = {}
        runtime_config_prepare: dict[str, Any] = {}
        runtime_config_commit: dict[str, Any] = {}
        post_switch_runtime_config_validate: list[dict[str, Any]] = []
        service_reconcile: dict[str, Any] = {}
        service_health: dict[str, Any] = {}
        runtime_readiness: dict[str, Any] = {}
        compensation: dict[str, Any] = {}
        restarted: list[str] = []
        try:
            if lock_error is not None:
                raise lock_error
            managed_plugins = _managed_plugin_selection(previous_dir)
            if (preserve_activation_state or pre_upgrade_profile.get("service_provider") == "launchd") and pre_upgrade_profile:
                preserved_activation_states = (
                    capture_preserved_timer_activation_states(
                        repo_root=repo_link,
                        runtime_root=runtime,
                        profile=pre_upgrade_profile,
                        run_cmd=run_cmd,
                    )
                )
                status_base["preserved_activation_units"] = sorted(
                    preserved_activation_states
                )
                operations.append(
                    {
                        "operation": "capture_service_activation_state",
                        "activation_policy": activation_policy,
                        "preserved_activation_units": sorted(
                            preserved_activation_states
                        ),
                        "ok": True,
                    }
                )
            releases.mkdir(parents=True, exist_ok=True)
            if target_dir.exists() and bundled != _is_runtime_bundle(target_dir):
                raise ServiceTransitionError("target installation kind differs from current", status="installation_kind_mismatch", remediation=[])
            if bundled:
                release_materialize = _materialize_runtime_bundle(current_dir=repo, tag=str(tag), target_dir=target_dir)
            else:
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
            include_pm, include_longbridge, include_position_advice = _installed_optional_packages(
                previous_dir=previous_dir, run_cmd=run_cmd, operations=operations,
            )
            include_pm = "pm" not in managed_plugins and (
                include_pm or _legacy_pm_enabled(previous_dir=previous_dir, runtime_root=runtime)
            )
            include_position_advice = "position-advice" not in managed_plugins and (
                include_position_advice or _legacy_position_advice_enabled(previous_dir=previous_dir, runtime_root=runtime)
            )
            include_feishu, include_wechat = _message_package_selection(
                previous_dir=previous_dir, runtime_root=runtime, run_cmd=run_cmd, operations=operations,
            )
            include_feishu = include_feishu and "feishu" not in managed_plugins
            include_wechat = include_wechat and "wechat" not in managed_plugins
            managed_plugins, provider_plugins = _provider_upgrade_selection(
                previous_dir=previous_dir, target_dir=target_dir, runtime_root=runtime, managed=managed_plugins,
                run_cmd=run_cmd, operations=operations,
            )
            runtime_prepare = _ensure_release_runtime(
                target_dir=target_dir,
                previous_dir=previous_dir,
                cache_root=cache,
                run_cmd=run_cmd,
                operations=operations,
                include_pm=include_pm,
                include_longbridge=include_longbridge,
                include_position_advice=include_position_advice,
                include_feishu=include_feishu, include_wechat=include_wechat,
                provider_plugins=provider_plugins,
            )
            _preserve_managed_plugins(
                target_dir=target_dir, selected=managed_plugins, run_cmd=run_cmd, operations=operations,
            )
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
            runtime_readiness = _runtime_readiness(release_dir=target_dir)
            runtime_config_prepare = _prepare_runtime_configs_for_release(
                previous_dir=previous_dir,
                target_dir=target_dir,
                runtime_root=runtime,
                releases_root=releases,
                run_cmd=run_cmd,
                operations=operations,
            )
            runtime_readiness = _runtime_readiness(release_dir=target_dir)
            _preflight_service_transition(
                profile=pre_upgrade_profile, target_dir=target_dir, restart_services=restart_services,
                snapshot=preserved_activation_states, run_cmd=run_cmd, operations=operations,
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
                service_reconcile = _reconcile_services_from_current_release(
                    repo_link=repo_link,
                    target_dir=target_dir,
                    runtime=runtime,
                    activation_policy=activation_policy,
                    run_cmd=run_cmd,
                )
                if _service_reconcile_failed(service_reconcile):
                    raise ServiceTransitionError(
                        "service drift reconciliation failed after upgrade",
                        status="upgraded_service_reconcile_failed",
                        remediation=_service_reconcile_remediation(service_reconcile),
                    )
            restart_profile = _load_service_profile(runtime) or pre_upgrade_profile
            if restart_profile.get("service_provider") == "launchd":
                restart_profile = {**restart_profile, "_launchd_expected_states": preserved_activation_states}
            restarted = (
                _restart_services_from_loaded_profile(profile=restart_profile, run_cmd=run_cmd, operations=operations)
                if restart_services
                else []
            )
            if restart_profile.get("service_provider") == "launchd":
                restarted = list((service_reconcile.get("applied") or {}).get("restarted_services") or [])
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
                    activation_policy=activation_policy,
                    preserved_activation_states=preserved_activation_states,
                    run_cmd=run_cmd,
                    operations=operations,
                )
            compensated = bool(compensation.get("ok")) if symlink_switched else False
            remediation = [*exc.remediation, *compensation.get("remediation", [])]
            out = {
                **status_base,
                "ok": False,
                "status": "upgrade_failed_rolled_back" if compensated else "upgraded_restart_failed",
                "failure_status": "upgraded_restart_failed",
                "changed": bool(symlink_switched and not compensated),
                "symlink_switched": bool(symlink_switched and not compensation.get("symlink_restored")),
                "rolled_back": compensated,
                "config_rebuilt": bool(runtime_config_prepare.get("status") == "prepared"),
                "target_dir": str(target_dir) if target else None,
                "previous_dir": str(previous_dir),
                "release_materialize": release_materialize,
                "runtime_prepare": runtime_prepare,
                "runtime_config_prepare": runtime_config_prepare,
                "runtime_config_commit": runtime_config_commit,
                "post_switch_runtime_config_validate": post_switch_runtime_config_validate,
                "service_reconcile": service_reconcile,
                "service_health": service_health,
                "runtime_readiness": runtime_readiness,
                "restarted_services": exc.restarted_services,
                "restart_failed_services": exc.failed_services,
                "manual_remediation": remediation,
                "remediation": remediation,
                "compensation": compensation,
                "error": f"{type(exc).__name__}: {exc}",
                "operations": operations,
            }
            if lock_error is None:
                write_upgrade_status(runtime_root=runtime, payload=out)
            return out
        except Exception as exc:
            compensation_needed = symlink_switched
            if compensation_needed:
                compensation = _compensate_service_transition(
                    repo_link=repo_link,
                    previous_dir=previous_dir,
                    runtime_root=runtime,
                    previous_profile=pre_upgrade_profile,
                    config_commit=runtime_config_commit,
                    restart_services=restart_services,
                    activation_policy=activation_policy,
                    preserved_activation_states=preserved_activation_states,
                    run_cmd=run_cmd,
                    operations=operations,
                )
            compensated = bool(compensation.get("ok")) if compensation_needed else False
            failure_status = exc.status if isinstance(exc, ServiceTransitionError) else "failed"
            remediation = list(
                exc.remediation
                if isinstance(exc, (RuntimeConfigPrepareError, ServiceTransitionError))
                else []
            )
            remediation.extend(str(item) for item in compensation.get("remediation") or [])
            out = {
                **status_base,
                "ok": False,
                "status": "upgrade_failed_rolled_back" if compensated else failure_status,
                "failure_status": failure_status,
                "changed": bool(symlink_switched and not compensated),
                "symlink_switched": bool(symlink_switched and not compensation.get("symlink_restored")),
                "rolled_back": compensated,
                "config_rebuilt": bool(runtime_config_prepare.get("status") == "prepared"),
                "target_dir": str(target_dir) if target else None,
                "previous_dir": str(previous_dir),
                "release_materialize": release_materialize,
                "runtime_prepare": exc.runtime_prepare if isinstance(exc, RuntimePrepareError) else runtime_prepare,
                "runtime_config_prepare": runtime_config_prepare,
                "runtime_config_commit": runtime_config_commit,
                "post_switch_runtime_config_validate": post_switch_runtime_config_validate,
                "service_reconcile": service_reconcile,
                "service_health": service_health,
                "runtime_readiness": runtime_readiness,
                "compensation": compensation,
                "error": f"{type(exc).__name__}: {exc}",
                **({"remediation": remediation} if remediation else {}),
                "operations": operations,
            }
            if lock_error is None:
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
                # The outer transition guard covers both cleanup calls and terminal publication.
                from src.application.service_cleanup import _service_cleanup_under_transition_lock

                cleanup_plan = _service_cleanup_under_transition_lock(
                    repo_root=repo_link,
                    releases_root=releases,
                    runtime_root=runtime,
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
                    cleanup_result = _service_cleanup_under_transition_lock(
                        repo_root=repo_link,
                        releases_root=releases,
                        runtime_root=runtime,
                        keep_releases=max(2, int(cleanup_keep_releases or 2)),
                        cleanup_downloads=True,
                        cleanup_pip_cache=False,
                        include_apt_cache=False,
                        journal_vacuum_size=None,
                        confirm=True,
                        run_cmd=run_cmd,
                    )

        venv_cleanup_result = _cleanup_upgrade_venvs(
            cache_root=cache, releases_root=releases, target_dir=target_dir, runtime_root=runtime,
        )
        operations.append({"operation": "cleanup_shared_venvs", **venv_cleanup_result})
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
            "runtime_readiness": runtime_readiness,
            "restarted_services": restarted,
            "post_upgrade_venv_cleanup": venv_cleanup_result,
            **({"post_upgrade_cleanup": cleanup_result} if cleanup_result is not None else {}),
            "operations": operations,
        }
        write_upgrade_status(runtime_root=runtime, payload=out)
        return out


def _validate_rollback_target(*, target: str, releases_root: Path) -> Path:
    match = re.fullmatch(
        r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
        r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?",
        target,
    )
    if match is None or any(
        token.isdecimal() and len(token) > 1 and token.startswith("0")
        for token in (match.group(1) or "").split(".")
    ):
        raise ServiceTransitionError(
            f"rollback target is not a canonical supported version: {target!r}",
            status="rollback_target_invalid",
            remediation=["Select MAJOR.MINOR.PATCH with an optional canonical prerelease; do not supply a path."],
        )
    try:
        target_dir = (releases_root / target).resolve()
        if target_dir == releases_root or not target_dir.is_relative_to(releases_root):
            raise ValueError("resolved rollback target must remain strictly inside releases_root")
        if not target_dir.exists():
            raise ServiceTransitionError(
                f"rollback release directory is missing: {target_dir}",
                status="rollback_target_missing",
                remediation=["Select an existing released version inside releases_root."],
            )
        if not target_dir.is_dir():
            raise ValueError("rollback target is not a directory")
        if _read_version(target_dir) != target:
            raise ValueError("rollback target VERSION does not exactly match the selected version")
    except ServiceTransitionError:
        raise
    except (OSError, UnicodeError, RuntimeError, ValueError) as exc:
        raise ServiceTransitionError(
            f"invalid rollback release {target!r}: {exc}",
            status="rollback_target_invalid",
            remediation=["Select an in-root release directory with a readable VERSION matching the selected version."],
        ) from exc
    return target_dir


def service_rollback(
    *,
    repo_root: str | Path,
    runtime_root: str | Path,
    releases_root: str | Path | None = None,
    to_version: str | None = None,
    confirm: bool = False,
    restart_services: bool = True,
    preserve_activation_state: bool = False,
    run_cmd: Callable[..., Any] = subprocess.run,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    runtime = Path(runtime_root).expanduser().resolve()
    if error := _deployment_user_error(runtime):
        return {**error, "operation": "rollback", "confirmed": bool(confirm)}
    with ExitStack() as transition_stack:
        lock_error: Exception | None = None
        if confirm:
            try:
                transition_stack.enter_context(_UpgradeLock(runtime / "locks" / "upgrade.lock"))
            except Exception as exc:
                lock_error = exc
            else:
                if error := _deployment_user_error(runtime):
                    return {**error, "operation": "rollback", "confirmed": True}
        preview_status, status_witness = (
            _read_upgrade_preview_json(upgrade_status_path(runtime)) if not confirm else (None, None)
        )
        _, profile_witness = (
            _read_upgrade_preview_json(runtime / "service.profile.json") if not confirm else (None, None)
        )
        repo_link, repo, repo_root_resolution = _coerce_repo_root_to_current_symlink(repo_root=repo_root, runtime_root=runtime)
        releases = Path(releases_root).expanduser().resolve() if releases_root else default_releases_root(repo_link)
        status = (load_upgrade_status(runtime_root=runtime) if confirm else preview_status) or {}
        current_version = _read_version(repo)
        repo_root_is_symlink = repo_link.is_symlink()
        preview_baseline = (repo_link, repo, repo_root_resolution, current_version, repo_root_is_symlink)
        activation_policy = (
            SERVICE_ACTIVATION_POLICY_PRESERVE_EXISTING
            if preserve_activation_state
            else SERVICE_ACTIVATION_POLICY_ENSURE_ACTIVE
        )
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
            "activation_policy": activation_policy,
            "preserve_activation_state": bool(preserve_activation_state),
            "updated_at": utc_now_iso(now_fn),
        }
        if lock_error is None and not target:
            out = {**status_base, "ok": False, "status": "no_rollback_target", "changed": False, "operations": operations}
            if confirm:
                write_upgrade_status(runtime_root=runtime, payload=out)
            else:
                _publish_early_upgrade_preview_status(
                    runtime_root=runtime, repo_root=repo_root, baseline=preview_baseline,
                    status_witness=status_witness, profile_witness=profile_witness, payload=out,
                )
            return out
        if lock_error is None:
            try:
                target_dir = _validate_rollback_target(target=target, releases_root=releases)
            except ServiceTransitionError as exc:
                out = {
                    **status_base,
                    "ok": False,
                    "status": exc.status,
                    "changed": False,
                    "error": str(exc),
                    "remediation": exc.remediation,
                    "operations": operations,
                }
                if exc.status == "rollback_target_missing":
                    out["target_dir"] = str(releases / target)
                if confirm:
                    write_upgrade_status(runtime_root=runtime, payload=out)
                elif exc.status == "rollback_target_missing":
                    _publish_early_upgrade_preview_status(
                        runtime_root=runtime, repo_root=repo_root, baseline=preview_baseline,
                        status_witness=status_witness, profile_witness=profile_witness, payload=out,
                        missing_target_dir=releases / target,
                    )
                return out
        else:
            target_dir = releases / target
        if _is_runtime_bundle(repo) != _is_runtime_bundle(target_dir):
            return {**status_base, "ok": False, "status": "installation_kind_mismatch", "changed": False, "operations": operations}
        if _is_runtime_bundle(repo):
            try:
                runtime_bundle.load_manifest(target_dir, expected_tag=_tag_text(target),
                    expected_platform=runtime_bundle.current_platform(), verify_files=True)
            except runtime_bundle.RuntimeBundleError as exc:
                return {**status_base, "ok": False, "status": "invalid_runtime_bundle", "error": str(exc), "changed": False, "operations": operations}
        previous_profile = _load_service_profile(runtime)
        preserved_activation_states: dict[str, dict[str, str]] = {}
        if not confirm:
            try:
                if (preserve_activation_state or previous_profile.get("service_provider") == "launchd") and previous_profile:
                    preserved_activation_states = (
                        capture_preserved_timer_activation_states(
                            repo_root=repo_link,
                            runtime_root=runtime,
                            profile=previous_profile,
                            run_cmd=run_cmd,
                        )
                    )
                    status_base["preserved_activation_units"] = sorted(
                        preserved_activation_states
                    )
                _managed_plugin_selection(target_dir)
                runtime_readiness = _runtime_readiness(release_dir=target_dir)
            except ServiceTransitionError as exc:
                return {
                    **status_base,
                    "ok": False,
                    "status": exc.status,
                    "changed": False,
                    "target_dir": str(target_dir),
                    "warnings": [] if repo_root_is_symlink else ["confirmed rollback requires repo_root to be a current symlink"],
                    "remediation": exc.remediation,
                    "error": str(exc),
                    "operations": operations,
                }
            return {
                **status_base,
                "ok": True,
                "status": "dry_run",
                "changed": False,
                "target_dir": str(target_dir),
                "warnings": [] if repo_root_is_symlink else ["confirmed rollback requires repo_root to be a current symlink"],
                "planned_operations": [
                    "validate the rollback target's managed plugins and pip-owned providers using its interpreter",
                    f"stage and validate runtime configs with {target_dir}",
                    "verify the rollback target contains the supported Python Bot runtime",
                    f"switch {repo_link} -> {target_dir}",
                    "commit the staged runtime config bundle",
                    "reconcile service drift for the rollback release",
                    (
                        "preserve pre-existing paused timer activation state"
                        if preserve_activation_state
                        else "repair expected timer activation drift"
                    ),
                    "restart and health-check long-running services" if restart_services else "skip service restart",
                ],
                "runtime_readiness": runtime_readiness,
                "operations": operations,
            }

        symlink_switched = False
        runtime_config_prepare: dict[str, Any] = {}
        runtime_config_commit: dict[str, Any] = {}
        post_switch_runtime_config_validate: list[dict[str, str]] = []
        service_reconcile: dict[str, Any] = {}
        service_health: dict[str, Any] = {}
        runtime_readiness: dict[str, Any] = {}
        compensation: dict[str, Any] = {}
        restarted: list[str] = []
        try:
            if lock_error is not None:
                raise lock_error
            if (preserve_activation_state or previous_profile.get("service_provider") == "launchd") and previous_profile:
                preserved_activation_states = (
                    capture_preserved_timer_activation_states(
                        repo_root=repo_link,
                        runtime_root=runtime,
                        profile=previous_profile,
                        run_cmd=run_cmd,
                    )
                )
                status_base["preserved_activation_units"] = sorted(
                    preserved_activation_states
                )
                operations.append(
                    {
                        "operation": "capture_service_activation_state",
                        "activation_policy": activation_policy,
                        "preserved_activation_units": sorted(
                            preserved_activation_states
                        ),
                        "ok": True,
                    }
                )
            runtime_readiness = _runtime_readiness(release_dir=target_dir)
            managed_plugins = _managed_plugin_selection(target_dir)
            _validate_target_managed_plugins(
                target_dir=target_dir, selected=managed_plugins,
                run_cmd=run_cmd, operations=operations,
            )
            _validate_rollback_pip_providers(
                target_dir=target_dir, managed=managed_plugins,
                run_cmd=run_cmd, operations=operations,
            )
            runtime_config_prepare = _prepare_runtime_configs_for_release(
                previous_dir=repo,
                target_dir=target_dir,
                runtime_root=runtime,
                releases_root=releases,
                run_cmd=run_cmd,
                operations=operations,
            )
            runtime_readiness = _runtime_readiness(release_dir=target_dir)
            _preflight_service_transition(
                profile=previous_profile, target_dir=target_dir, restart_services=restart_services,
                snapshot=preserved_activation_states, run_cmd=run_cmd, operations=operations,
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
                try:
                    service_reconcile = _reconcile_services_from_current_release(
                        repo_link=repo_link,
                        target_dir=target_dir,
                        runtime=runtime,
                        activation_policy=activation_policy,
                        run_cmd=run_cmd,
                    )
                except ServiceTransitionError as exc:
                    raise ServiceTransitionError(
                        f"service drift reconciliation failed during rollback: {exc}",
                        status="rollback_service_reconcile_failed",
                        remediation=exc.remediation,
                    ) from exc
                if _service_reconcile_failed(service_reconcile):
                    raise ServiceTransitionError(
                        "service drift reconciliation failed during rollback",
                        status="rollback_service_reconcile_failed",
                        remediation=_service_reconcile_remediation(service_reconcile),
                    )
                if preserve_activation_state:
                    child_snapshot = service_reconcile.get("preserved_activation_states")
                    if not isinstance(child_snapshot, dict):
                        raise ServiceTransitionError(
                            "rollback service reconcile did not report its activation snapshot",
                            status="rollback_service_reconcile_failed",
                        )
                    if child_snapshot != preserved_activation_states:
                        status_base["preserved_activation_state_difference"] = {
                            "before_switch": preserved_activation_states,
                            "reconcile": child_snapshot,
                        }
            rollback_profile = _load_service_profile(runtime) or previous_profile
            if rollback_profile.get("service_provider") == "launchd":
                rollback_profile = {**rollback_profile, "_launchd_expected_states": preserved_activation_states}
            restarted = (
                _restart_services_from_loaded_profile(
                    profile=rollback_profile,
                    run_cmd=run_cmd,
                    operations=operations,
                )
                if restart_services
                else []
            )
            if rollback_profile.get("service_provider") == "launchd":
                restarted = list((service_reconcile.get("applied") or {}).get("restarted_services") or [])
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
            compensation_needed = symlink_switched
            if compensation_needed:
                compensation = _compensate_service_transition(
                    repo_link=repo_link,
                    previous_dir=repo,
                    runtime_root=runtime,
                    previous_profile=previous_profile,
                    config_commit=runtime_config_commit,
                    restart_services=restart_services,
                    activation_policy=activation_policy,
                    preserved_activation_states=preserved_activation_states,
                    run_cmd=run_cmd,
                    operations=operations,
                )
            compensated = bool(compensation.get("ok")) if compensation_needed else False
            failure_status = (
                exc.status
                if isinstance(exc, ServiceTransitionError)
                else "rollback_restart_failed"
                if isinstance(exc, ServiceRestartError)
                else "rollback_failed"
            )
            remediation = list(
                exc.remediation
                if isinstance(exc, (RuntimeConfigPrepareError, ServiceTransitionError, ServiceRestartError))
                else []
            )
            remediation.extend(str(item) for item in compensation.get("remediation") or [])
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
                "runtime_readiness": runtime_readiness,
                "compensation": compensation,
                "error": f"{type(exc).__name__}: {exc}",
                **({"remediation": remediation} if remediation else {}),
                "operations": operations,
            }
            if lock_error is None:
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
            "runtime_readiness": runtime_readiness,
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
