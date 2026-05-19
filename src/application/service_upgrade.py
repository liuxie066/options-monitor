from __future__ import annotations

import json
import os
import shutil
import shlex
import subprocess
from functools import cmp_to_key
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


class ServiceRestartError(RuntimeError):
    def __init__(self, message: str, *, remediation: list[str]) -> None:
        super().__init__(message)
        self.remediation = remediation


class RuntimeConfigPrepareError(RuntimeError):
    def __init__(self, message: str, *, remediation: list[str]) -> None:
        super().__init__(message)
        self.remediation = remediation


def _load_service_profile(runtime_root: Path) -> dict[str, Any]:
    profile_path = runtime_root / "service.profile.json"
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return profile if isinstance(profile, dict) else {}


def _restart_profile(profile: dict[str, Any]) -> dict[str, Any]:
    raw = profile.get("restart")
    return raw if isinstance(raw, dict) else {}


def _restart_command_prefix(profile: dict[str, Any]) -> list[str]:
    restart = _restart_profile(profile)
    raw_prefix = restart.get("command_prefix")
    if isinstance(raw_prefix, list) and raw_prefix:
        prefix = [str(item).strip() for item in raw_prefix if str(item).strip()]
        if prefix:
            return prefix
    if bool(restart.get("requires_sudo") or profile.get("restart_requires_sudo")):
        return ["sudo", "-n", "systemctl"]
    return ["systemctl"]


def _restart_remediation(*, profile: dict[str, Any], service_name: str, command: list[str]) -> list[str]:
    deploy_user = str(profile.get("deploy_user") or "").strip()
    sudoers = _restart_profile(profile).get("sudoers")
    suggestions = [str(item) for item in sudoers] if isinstance(sudoers, list) else []
    if not suggestions and deploy_user:
        suggestions = [
            f"{deploy_user} ALL=(root) NOPASSWD: /bin/systemctl restart {service_name}",
            f"{deploy_user} ALL=(root) NOPASSWD: /usr/bin/systemctl restart {service_name}",
        ]
    remediation = [
        f"manual_restart: sudo systemctl restart {service_name}",
        f"failed_command: {' '.join(shlex.quote(part) for part in command)}",
    ]
    if suggestions:
        remediation.append("sudoers_minimal:")
        remediation.extend(suggestions)
    return remediation


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
    profile = _load_service_profile(runtime_root)
    if not profile:
        return []
    provider = str(profile.get("service_provider") or "").strip().lower()
    raw_services = profile.get("services")
    services: list[Any] = raw_services if isinstance(raw_services, list) else []
    restarted: list[str] = []
    if provider != "systemd":
        return restarted
    command_prefix = _restart_command_prefix(profile)
    for item in services:
        name = str(item.get("name") if isinstance(item, dict) else item or "").strip()
        if not name.endswith(".service") or "trade-intake" not in name:
            continue
        command = [*command_prefix, "restart", name]
        result = _run_command(command, cwd=None, run_cmd=run_cmd, timeout=60)
        operations.append(result)
        if not result["ok"]:
            raise ServiceRestartError(
                f"failed to restart {name}: {' '.join(shlex.quote(part) for part in command)}",
                remediation=_restart_remediation(profile=profile, service_name=name, command=command),
            )
        restarted.append(name)
    return restarted


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

    rebuilt = _rebuild_and_validate_runtime_configs(
        targets=targets,
        cwd=target_dir,
        run_cmd=run_cmd,
        operations=operations,
        phase="pre_switch",
    )

    return {"status": "prepared", "targets": targets, "overlays": overlays, "rebuilt": rebuilt}


def _release_python(target_dir: Path) -> Path:
    return target_dir / ".venv" / "bin" / "python"


def _ensure_release_runtime(
    *,
    target_dir: Path,
    run_cmd: Callable[..., Any],
    operations: list[dict[str, Any]],
) -> Path:
    venv_python = _release_python(target_dir)
    if not venv_python.exists():
        _run_required(
            ["python3", "-m", "venv", ".venv"],
            cwd=target_dir,
            run_cmd=run_cmd,
            operations=operations,
            timeout=300,
        )
    if not venv_python.exists() or not os.access(venv_python, os.X_OK):
        raise RuntimeError(f"release virtualenv python is missing after setup: {venv_python}")

    _run_required(
        [str(venv_python), "-m", "pip", "install", "-U", "pip"],
        cwd=target_dir,
        run_cmd=run_cmd,
        operations=operations,
        timeout=600,
    )
    _run_required(
        [str(venv_python), "-m", "pip", "install", "-r", "requirements.txt", "-c", "constraints.txt"],
        cwd=target_dir,
        run_cmd=run_cmd,
        operations=operations,
        timeout=1200,
    )
    server_requirements = target_dir / "requirements" / "server.txt"
    server_constraints = target_dir / "constraints" / "server.txt"
    if server_requirements.exists() and server_constraints.exists():
        _run_required(
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
            cwd=target_dir,
            run_cmd=run_cmd,
            operations=operations,
            timeout=1200,
        )
    _run_required(
        [
            str(venv_python),
            "-c",
            "from pathlib import Path; import sys; assert Path(sys.executable).exists(); import src.application.multi_account_tick",
        ],
        cwd=target_dir,
        run_cmd=run_cmd,
        operations=operations,
        timeout=120,
    )
    return venv_python


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
        f"prepare release runtime at {target_dir / '.venv'}",
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
    symlink_switched = False
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
            _ensure_release_runtime(target_dir=target_dir, run_cmd=run_cmd, operations=operations)
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
            "previous_dir": str(previous_dir),
            "error": f"{type(exc).__name__}: {exc}",
            **({"remediation": exc.remediation} if isinstance(exc, (RuntimeConfigPrepareError, ServiceRestartError)) else {}),
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
        "runtime_config_prepare": runtime_config_prepare,
        "post_switch_runtime_config_validate": post_switch_runtime_config_validate,
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
    "load_upgrade_status",
    "service_rollback",
    "service_upgrade",
    "service_upgrade_check",
    "upgrade_status_path",
    "write_upgrade_status",
]
