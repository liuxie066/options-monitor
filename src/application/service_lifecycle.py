"""Explicit, preview-bound installation and activation of one OM service instance.

Rendering remains owned by service_deploy. This module never executes rendered
shell snippets, changes authoring configuration, or starts jobs during install.
"""
from __future__ import annotations

import difflib
import fcntl
import hashlib
import json
import os
import platform
import plistlib
import pwd
import re
import shlex
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_primitives import configured_markets
from src.application.config_yaml import load_yaml_config_file, resolve_yaml_bot_config
from src.application.runtime_config_freshness import check_runtime_config_freshness, check_runtime_config_identity
from src.application.secret_store import credential_spec
from src.application.service_deploy import load_service_profile, render_service_bundle
from src.application.service_drift import _expected_bundle_from_profile
from src.application.write_contract import attach_write_contract

_SERVICE_NAME = re.compile(r"options-monitor-[a-z0-9][a-z0-9.-]*\.(service|timer)\Z")
_LAUNCH_LABEL = re.compile(r"com\.options-monitor\.[a-z0-9][a-z0-9.-]*\Z")
_SIDE_EFFECTS = [
    "启动定时扫描、交易采集、到期维护与投影验证；交易采集和到期维护会按现有规则写入本地账本及状态。",
    "已启用的通知可能向所选收件人发送消息；安装文件本身不会启动任务。",
    "此操作不自动下单。停止会暂停本实例的定时任务和正在运行的任务，不删除数据。",
]


def _error(code: str, message: str, **details: Any) -> None:
    raise AgentToolError(code=code, message=message, details=details or None)


def _sha(value: bytes | dict[str, Any]) -> str:
    raw = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _permission_error(path: Path, exc: PermissionError) -> None:
    _error("SERVICE_PERMISSION_REQUIRED", "cannot inspect or manage this service path with the current identity; "
           "use a separately authorized privileged invocation and preserve the runtime and --deploy-user arguments",
           path=str(path), reason=str(exc))


def _no_links(path: Path) -> None:
    try:
        for candidate in (path, *path.parents):
            if candidate.is_symlink():
                _error("SERVICE_PATH_UNSAFE", "service managed paths cannot contain symbolic links", path=str(candidate))
    except PermissionError as exc:
        _permission_error(path, exc)


def _file_fact(path: Path) -> dict[str, Any]:
    _no_links(path)
    try:
        if not path.exists():
            return {"path": str(path), "exists": False}
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            _error("SERVICE_PATH_UNSAFE", "expected a regular managed file", path=str(path))
        return {"path": str(path), "exists": True, "sha256": _sha(path.read_bytes()),
                "uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode)}
    except PermissionError as exc:
        _permission_error(path, exc)


def _identity(target: str, deploy_user: str | None, euid: int) -> Any:
    if target == "launchd":
        if euid == 0:
            _error("SERVICE_IDENTITY_REQUIRED", "launchd installation must run as the logged-in user, not root")
        user = pwd.getpwuid(euid)
        if deploy_user and deploy_user != user.pw_name:
            _error("SERVICE_IDENTITY_MISMATCH", "LaunchAgents must belong to the current user")
        return user
    if euid == 0 and not deploy_user:
        _error("SERVICE_IDENTITY_REQUIRED", "root must supply --deploy-user; do not rerun the whole setup with sudo")
    try:
        user = pwd.getpwnam(deploy_user) if deploy_user else pwd.getpwuid(euid)
    except KeyError:
        _error("SERVICE_IDENTITY_REQUIRED", "deployment user does not exist", deploy_user=deploy_user)
    if user.pw_uid == 0:
        _error("SERVICE_IDENTITY_REQUIRED", "choose the non-root owner of the authoring configuration")
    if euid not in {0, user.pw_uid}:
        _error("SERVICE_IDENTITY_MISMATCH", "current user cannot manage another deployment identity")
    return user


def _owned_instance(runtime: Path, source: Path, user: Any, env_file: Path | None, *, source_required: bool = True) -> None:
    sources = (source,) if source_required or source.exists() else ()
    for path in (runtime, *sources, *(tuple([env_file]) if env_file and env_file.exists() else ())):
        _no_links(path)
        if not path.exists():
            _error("SERVICE_CONFIG_MISSING", "complete setup before installing services", path=str(path))
        if path.stat().st_uid != user.pw_uid:
            _error("SERVICE_IDENTITY_MISMATCH", "runtime and configuration must belong to the deployment user", path=str(path),
                   expected_uid=user.pw_uid, actual_uid=path.stat().st_uid)


def _same_scope(profile: dict[str, Any], *, runtime: Path, repo: Path, target: str) -> None:
    for key, expected in (("runtime_root", runtime), ("repo_root", repo)):
        if os.path.abspath(str(profile.get(key) or "")) != str(expected):
            _error("SERVICE_INSTANCE_CONFLICT", "installed profile belongs to another instance; takeover is not supported",
                   field=key, existing=profile.get(key), requested=str(expected))
    if profile.get("service_provider") != target:
        _error("SERVICE_INSTANCE_CONFLICT", "installed profile uses another service provider")


def _unit_scope(path: Path, content: str, *, target: str, runtime: Path, repo: Path) -> None:
    if target == "launchd":
        try:
            payload = plistlib.loads(content.encode())
            value = payload.get("EnvironmentVariables", {}).get("OM_RUNTIME_ROOT")
            working_directory = payload.get("WorkingDirectory")
        except Exception:
            value = None
            working_directory = None
    else:
        value = None
        working_directory = None
        for line in content.splitlines():
            if line.startswith("WorkingDirectory="):
                try:
                    working_directory = shlex.split(line.split("=", 1)[1])[0]
                except (ValueError, IndexError):
                    pass
            if line.startswith("Environment="):
                try:
                    for item in shlex.split(line.split("=", 1)[1]):
                        if item.startswith("OM_RUNTIME_ROOT="):
                            value = item.split("=", 1)[1]
                except ValueError:
                    pass
    if value != str(runtime):
        _error("SERVICE_INSTANCE_CONFLICT", "installed definition belongs to another or unknown runtime; takeover is not supported",
               path=str(path), existing_runtime=value, requested_runtime=str(runtime))

    if "opend" not in path.name and working_directory != str(repo):
        _error("SERVICE_INSTANCE_CONFLICT", "installed definition belongs to another repository; takeover is not supported",
               path=str(path), existing_repo=working_directory, requested_repo=str(repo))


def _definition_preview(content: str, kind: str) -> str:
    safe_env = {"OM_RUNTIME_ROOT", "OM_ENV_FILE", "HOME", "PYTHONUNBUFFERED"}
    if kind == "launchd_plist":
        payload = plistlib.loads(content.encode())
        env = payload.get("EnvironmentVariables") or {}
        for key in env:
            if key not in safe_env:
                env[key] = "<redacted>"
        return plistlib.dumps(payload, sort_keys=False).decode()
    lines = []
    for line in content.splitlines(keepends=True):
        if line.startswith("Environment="):
            try:
                assignments = shlex.split(line.split("=", 1)[1])
                safe = [item if item.split("=", 1)[0] in safe_env else item.split("=", 1)[0] + "=<redacted>" for item in assignments]
                line = "Environment=" + shlex.join(safe) + "\n"
            except ValueError:
                line = "Environment=<unparseable, redacted>\n"
        lines.append(line)
    return "".join(lines)


def _managed_files(bundle: dict[str, Any], *, target: str, unit_root: Path, runtime: Path, repo: Path) -> list[dict[str, Any]]:
    files = []
    for item in bundle["files"]:
        relative = str(item["relative_path"])
        kind = str(item["kind"])
        if kind == "service_profile":
            path = runtime / "service.profile.json"
        elif target == "launchd" and kind == "launchd_plist":
            name = Path(relative).name
            if not _LAUNCH_LABEL.fullmatch(name.removesuffix(".plist")):
                _error("SERVICE_PATH_UNSAFE", "invalid generated launchd label")
            path = unit_root / name
        elif target == "systemd" and kind in {"systemd_service", "systemd_timer", "systemd_secret_dropin"}:
            rel = Path(relative).relative_to("systemd")
            if kind == "systemd_secret_dropin":
                valid = len(rel.parts) == 2 and rel.parts[0].endswith(".service.d") and _SERVICE_NAME.fullmatch(rel.parts[0][:-2])
                valid = valid and rel.parts[1] == "zzzz-secret-credentials.conf"
            else:
                valid = len(rel.parts) == 1 and _SERVICE_NAME.fullmatch(rel.name)
            if not valid:
                _error("SERVICE_PATH_UNSAFE", "unsupported generated unit path", path=relative)
            path = unit_root / rel
        else:
            _error("SERVICE_PROFILE_UNSUPPORTED", "basic lifecycle cannot install this advanced service asset; use the advanced deployment workflow", kind=kind)
        fact = _file_fact(path)
        content = str(item["content"])
        if fact["exists"] and kind in {"systemd_service", "systemd_timer", "launchd_plist"}:
            # Timers contain no runtime; their matching service is checked separately.
            if kind != "systemd_timer":
                _unit_scope(path, path.read_text(), target=target, runtime=runtime, repo=repo)
        files.append({"path": str(path), "kind": kind, "content": content,
                      "sha256": _sha(content.encode()), "before": fact})
    return files


def _run(command: list[str], run_cmd: Callable[..., Any], *, mutation: bool = False) -> dict[str, Any]:
    try:
        result = run_cmd(command, capture_output=True, text=True, check=False, timeout=20 if mutation else 3)
        return {"command": command, "ok": result.returncode == 0, "returncode": result.returncode,
                "stdout": str(result.stdout or "")[:2000] if mutation else str(result.stdout or ""), "stderr": str(result.stderr or "")[:1000]}
    except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt) and not mutation:
            raise
        return {"command": command, "ok": False, "outcome": "unknown" if mutation else "unavailable",
                "error": type(exc).__name__}


def _query(name: str, *, target: str, uid: int, content: str, run_cmd: Callable[..., Any]) -> dict[str, Any]:
    if target == "systemd":
        result = _run(["systemctl", "show", name, "--property=LoadState,ActiveState,SubState,UnitFileState,FragmentPath,Environment", "--no-pager"], run_cmd)
        values = dict(line.split("=", 1) for line in result.get("stdout", "").splitlines() if "=" in line)
        load, active, enabled = (values.get(k) for k in ("LoadState", "ActiveState", "UnitFileState"))
        state = "unknown"
        if result["ok"]:
            if load == "not-found":
                state = "absent"
            elif load == "loaded" and active in {"active", "activating", "inactive", "failed", "deactivating"}:
                state = "running" if active == "active" else ("loaded-idle" if active == "inactive" else active)
        runtime = None
        try:
            for assignment in shlex.split(values.get("Environment") or ""):
                if assignment.startswith("OM_RUNTIME_ROOT="):
                    runtime = assignment.split("=", 1)[1]
        except ValueError:
            pass
        return {"name": name, "state": state, "enabled": enabled, "definition_path": values.get("FragmentPath"),
                "runtime_root": runtime, "probe": {key: value for key, value in result.items() if key not in {"stdout", "stderr"}}}
    result = _run(["launchctl", "print", f"gui/{uid}/{name}"], run_cmd)
    keep_alive = bool(plistlib.loads(content.encode()).get("KeepAlive"))
    state = "unknown"
    if result["ok"]:
        running = re.search(r"\bpid\s*=\s*[1-9]\d*", result.get("stdout", ""))
        state = "running" if running else ("loaded-not-running" if keep_alive else "loaded-idle")
    elif result.get("returncode") in {3, 113} or re.search(r"could not find service|no such process|not found", result.get("stderr", ""), re.I):
        state = "absent"
    disabled = _run(["launchctl", "print-disabled", f"gui/{uid}"], run_cmd)
    if not disabled["ok"]:
        state = "unknown"
    is_disabled = bool(re.search(r'"' + re.escape(name) + r'"\s*=>\s*true', disabled.get("stdout", "")))
    path_match = re.search(r"(?m)^\s*path = (.+)$", result.get("stdout", ""))
    runtime_match = re.search(r"(?m)^\s*OM_RUNTIME_ROOT => (.+)$", result.get("stdout", ""))
    return {"name": name, "state": state, "enabled": not is_disabled if disabled["ok"] else None,
            "definition_path": path_match.group(1).strip() if path_match else None,
            "runtime_root": runtime_match.group(1).strip() if runtime_match else None,
            "probe": {key: value for key, value in result.items() if key not in {"stdout", "stderr"}}}


def _units(files: list[dict[str, Any]], target: str) -> list[dict[str, Any]]:
    units = []
    for item in files:
        if item["kind"] not in {"systemd_service", "systemd_timer", "launchd_plist"}:
            continue
        name = Path(item["path"]).name
        content = item["content"]
        if target == "launchd":
            name = name.removesuffix(".plist")
            startup = True
        else:
            startup = name.endswith(".timer") or ("Type=oneshot" not in content and not name.endswith("-alert.service"))
        units.append({"name": name, "path": item["path"], "content": content, "startup": startup})
    return sorted(units, key=lambda item: (not item["name"].endswith(".timer"), item["name"]))


def _activation_commands(action: str, units: list[dict[str, Any]], states: dict[str, dict[str, Any]], *, target: str, uid: int) -> list[list[str]]:
    commands = []
    for unit in units:
        name, state = unit["name"], states[unit["name"]]
        if state["state"] == "unknown":
            _error("SERVICE_STATE_UNKNOWN", "cannot plan activation from an unknown service state", service=name)
        if target == "systemd":
            if action == "start" and unit["startup"]:
                if state["enabled"] in {"masked", "masked-runtime"}:
                    _error("SERVICE_MASKED", "service is explicitly masked; review it in advanced service management", service=name)
                if state["state"] != "running" or state["enabled"] not in {"enabled", "enabled-runtime"}:
                    commands.append(["systemctl", "enable", "--now", name])
            elif action == "stop":
                if unit["startup"] and state["enabled"] not in {"disabled", "masked", "masked-runtime"}:
                    commands.append(["systemctl", "disable", "--now", name])
                elif state["state"] not in {"absent", "loaded-idle"}:
                    commands.append(["systemctl", "stop", name])
        else:
            service = f"gui/{uid}/{name}"
            if action == "start":
                if state["enabled"] is False:
                    commands.append(["launchctl", "enable", service])
                if state["state"] == "absent":
                    commands.append(["launchctl", "bootstrap", f"gui/{uid}", unit["path"]])
                elif state["state"] == "loaded-not-running":
                    commands.append(["launchctl", "kickstart", service])
            else:
                if state["enabled"] is not False:
                    commands.append(["launchctl", "disable", service])
                if state["state"] != "absent":
                    commands.append(["launchctl", "bootout", service])
    return commands


def _atomic_file(path: Path, data: bytes, *, mode: int, uid: int, gid: int) -> None:
    _no_links(path)
    fd, raw = tempfile.mkstemp(prefix=".om-service-", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), mode)
            if os.geteuid() == 0:
                os.fchown(output.fileno(), uid, gid)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        if path.read_bytes() != data:
            raise OSError("managed file readback mismatch")
    finally:
        temporary.unlink(missing_ok=True)


def _mkdir(path: Path, *, uid: int, gid: int, mode: int = 0o700) -> None:
    _no_links(path)
    if path.exists():
        if not path.is_dir():
            _error("SERVICE_PATH_UNSAFE", "runtime directory path is not a directory", path=str(path))
        return
    _mkdir(path.parent, uid=uid, gid=gid, mode=mode)
    try:
        path.mkdir(mode=mode)
        # sudo can retain a private umask; public systemd definitions still
        # need traversable directories, while new runtime directories stay 0700.
        path.chmod(mode)
        if os.geteuid() == 0:
            os.chown(path, uid, gid)
    except PermissionError as exc:
        _permission_error(path, exc)


def service_lifecycle(
    action: str,
    *,
    repo_root: str | Path,
    runtime_root: str | Path,
    config_yaml: str | Path | None = None,
    env_file: str | Path | None = None,
    profile_path: str | Path | None = None,
    target: str | None = None,
    deploy_user: str | None = None,
    accounts: list[str] | None = None,
    markets: list[str] | None = None,
    include_opend: bool = False,
    include_feishu_ws: bool = False,
    include_wechat_clawbot: bool = False,
    channel_market: str | None = None,
    confirm: bool = False,
    expected_preview_sha256: str | None = None,
    run_cmd: Callable[..., Any] = subprocess.run,
    cancelled: Callable[[], bool] | None = None,
    system: str | None = None,
    unit_root: str | Path | None = None,
    credential_store_root: str | Path = "/etc/credstore.encrypted",
    euid: int | None = None,
) -> dict[str, Any]:
    """Preview, then apply using the returned preview_sha256 and identical scope.

    system/unit_root/euid/run_cmd are dependency-injection points for fixtures.
    Nonstandard roots require an injected runner and cannot call the host manager.
    """
    if action not in {"install", "start", "stop"}:
        _error("INPUT_ERROR", "service action must be install, start or stop")
    detected = {"Darwin": "launchd", "Linux": "systemd"}.get(system or platform.system())
    provider = target or detected
    if not provider or provider != detected:
        _error("SERVICE_PLATFORM_UNSUPPORTED", "service target must match this macOS/Linux host")
    if (system is not None or euid is not None or unit_root is not None) and run_cmd is subprocess.run:
        _error("SERVICE_TEST_CONTEXT_REQUIRED", "platform/path overrides require an isolated command runner")
    if (system is not None or euid is not None) and unit_root is None:
        _error("SERVICE_TEST_CONTEXT_REQUIRED", "platform/identity overrides also require an isolated unit root")
    effective_uid = os.geteuid() if euid is None else euid
    repo = Path(os.path.abspath(Path(repo_root).expanduser()))
    runtime = Path(runtime_root).expanduser().absolute()
    _no_links(runtime)
    profile_file = Path(profile_path).expanduser().absolute() if profile_path else runtime / "service.profile.json"
    if profile_file != runtime / "service.profile.json":
        _error("SERVICE_PATH_UNSAFE", "profile must belong to the selected runtime root")
    existing = load_service_profile(profile_file) if _file_fact(profile_file)["exists"] else None
    if existing:
        _same_scope(existing, runtime=runtime, repo=repo, target=provider)
        if action != "install":
            deploy_user = deploy_user or existing.get("deploy_user")
    user = _identity(provider, deploy_user, effective_uid)
    source = Path(config_yaml or ((existing or {}).get("config_authoring") or {}).get("config_yaml") or runtime / "config.yaml").expanduser().resolve()
    selected_env = Path(env_file or (existing or {}).get("env_file") or runtime / "options-monitor.env").expanduser().resolve()
    if action != "install" and existing:
        recorded_source = ((existing.get("config_authoring") or {}).get("config_yaml"))
        recorded_env = existing.get("env_file")
        if recorded_source and source != Path(recorded_source).expanduser().resolve():
            _error("SERVICE_INSTANCE_CONFLICT", "activation cannot override the installed authoring source")
        if recorded_env and selected_env != Path(recorded_env).expanduser().resolve():
            _error("SERVICE_INSTANCE_CONFLICT", "activation cannot override the installed ordinary env file")
        if provider == "systemd" and existing.get("deploy_user") and user.pw_name != existing["deploy_user"]:
            _error("SERVICE_INSTANCE_CONFLICT", "activation cannot override the installed deployment user")
    _owned_instance(runtime, source, user, selected_env, source_required=action != "stop")
    root = Path(unit_root).expanduser().absolute() if unit_root else (
        Path("/etc/systemd/system") if provider == "systemd" else Path(user.pw_dir) / "Library" / "LaunchAgents"
    )
    _no_links(root)
    expected_unit_uid = 0 if provider == "systemd" and unit_root is None else user.pw_uid
    if root.exists() and (root.stat().st_uid != expected_unit_uid or root.stat().st_mode & 0o022):
        _error("SERVICE_IDENTITY_MISMATCH", "service directory must be controlled by its deployment owner", path=str(root))
    if action == "install":
        doc = load_yaml_config_file(source)
        selected_markets = markets or configured_markets(doc)
        bundle = render_service_bundle(
            target=provider, repo_root=repo, runtime_root=runtime, config_yaml=source,
            config_paths={market: runtime / f"config.{market}.json" for market in selected_markets},
            accounts=accounts, markets=selected_markets,
            env_file=selected_env,
            deploy_user=user.pw_name if provider == "systemd" else None, deploy_home=user.pw_dir,
            use_default_deploy_user=False, include_opend=include_opend,
            include_feishu_ws=include_feishu_ws, feishu_ws_config_key=channel_market,
            include_wechat_clawbot=include_wechat_clawbot, wechat_clawbot_config_key=channel_market,
            include_secret_credentials=provider == "systemd", feature_aware_credentials=True,
            secret_credential_store_root=credential_store_root,
        )
        profile = json.loads(next(item["content"] for item in bundle["files"] if item["kind"] == "service_profile"))
        if existing and {item["name"] for item in existing.get("services", [])} - {item["name"] for item in profile["services"]}:
            _error("SERVICE_REMOVAL_REQUIRES_REVIEW", "install would remove previously managed services; stop and review the advanced service drift first")
    else:
        if existing is None:
            _error("SERVICE_NOT_INSTALLED", "install the selected runtime's service definitions first")
        profile = existing
        if action == "stop":
            installed = []
            for service in profile.get("services", []):
                name = str(service.get("name") or "")
                pattern = _SERVICE_NAME if provider == "systemd" else _LAUNCH_LABEL
                if not pattern.fullmatch(name):
                    _error("SERVICE_PATH_UNSAFE", "invalid service name in profile", name=name)
                filename = name if provider == "systemd" else name + ".plist"
                path = root / filename
                _file_fact(path)
                if not path.exists():
                    _error("SERVICE_DEFINITION_MISSING", "cannot verify the scope of a missing definition", path=str(path))
                kind = "launchd_plist" if provider == "launchd" else ("systemd_timer" if name.endswith(".timer") else "systemd_service")
                installed.append({"relative_path": provider + "/" + filename, "kind": kind, "content": path.read_text()})
            bundle = {"files": installed}
        else:
            bundle = _expected_bundle_from_profile(profile, provider=provider, repo_root=repo, runtime_root=runtime)
    files = _managed_files(bundle, target=provider, unit_root=root, runtime=runtime, repo=repo)
    if action == "install" and not selected_env.exists():
        if selected_env.parent != runtime:
            _error("SERVICE_PATH_UNSAFE", "new ordinary env file must be inside the selected runtime")
        files.insert(0, {"path": str(selected_env), "kind": "service_env", "content": "", "sha256": _sha(b""),
                         "before": _file_fact(selected_env), "purpose": "普通配置环境文件；空文件不包含任何密钥"})
    if action == "start" and profile.get("env_file") and not selected_env.is_file():
        _error("SERVICE_CONFIG_MISSING", "service ordinary env file is missing; preview install first", path=str(selected_env))
    # A feature turned off must not leave its former mandatory LoadCredential
    # drop-in behind. Only retire this owner's exact recorded credential file.
    if action == "install" and existing and provider == "systemd":
        desired_paths = {item["path"] for item in files}
        old_credentials = existing.get("secret_credentials") or {}
        for name in old_credentials.get("service_credentials", {}):
            if not _SERVICE_NAME.fullmatch(name) or not name.endswith(".service"):
                _error("SERVICE_PATH_UNSAFE", "invalid credential consumer in profile")
            path = root / (name + ".d") / "zzzz-secret-credentials.conf"
            fact = _file_fact(path)
            if str(path) not in desired_paths and fact["exists"]:
                files.insert(0, {"path": str(path), "kind": "systemd_secret_dropin", "content": None,
                                 "sha256": None, "before": fact, "remove": True})
    for item in files:
        expected_uid = user.pw_uid if item["kind"] in {"service_profile", "service_env"} else expected_unit_uid
        if item["before"]["exists"] and (item["before"]["uid"] != expected_uid or item["before"]["mode"] & 0o022):
            _error("SERVICE_IDENTITY_MISMATCH", "managed file must be controlled by its deployment owner", path=item["path"])
    if action != "install":
        # Stop uses actual installed definitions even when authoring changed.
        for item in files:
            if item["kind"] == "service_profile":
                continue
            if not item["before"]["exists"]:
                _error("SERVICE_DEFINITION_MISSING", "repair missing service definitions with install before activation", path=item["path"])
            if action == "start" and item["before"]["sha256"] != item["sha256"]:
                _error("SERVICE_DEFINITION_STALE", "service definitions changed; preview service install first", path=item["path"])
            if action == "stop":
                item["content"] = Path(item["path"]).read_text()
    inputs = [_file_fact(source), _file_fact(selected_env), _file_fact(profile_file)]
    for market, raw in profile.get("config_paths", {}).items():
        path = Path(raw)
        fact = _file_fact(path)
        inputs.append(fact)
        if action != "stop":
            if not fact["exists"]:
                _error("SERVICE_CONFIG_MISSING", "build runtime snapshots before installing or starting services", path=str(path))
            config = json.loads(path.read_text())
            identity = check_runtime_config_identity(config, explicit_market=market, runtime_config_path=path)
            freshness = check_runtime_config_freshness(config, repo_root=repo, market=market, runtime_config_path=path)
            if not identity["ok"] or not freshness["ok"]:
                _error("SERVICE_CONFIG_STALE", "rebuild runtime configuration before installing or starting services", path=str(path), identity=identity, freshness=freshness)
    bot_path = profile.get("bot_config_path")
    if bot_path and any((profile.get(channel) or {}).get("enabled") for channel in ("feishu_ws", "wechat_clawbot")):
        path = Path(bot_path)
        fact = _file_fact(path)
        inputs.append(fact)
        if action != "stop":
            if not fact["exists"]:
                _error("SERVICE_CONFIG_MISSING", "build the Bot runtime snapshot before installing its inbound service", path=str(path))
            actual = json.loads(path.read_text())
            expected, _ = resolve_yaml_bot_config(repo_root=repo, config_path=source)
            if any(actual.get(key) != expected.get(key) for key in ("bot", "inbound", "_resolved")):
                _error("SERVICE_CONFIG_STALE", "rebuild the Bot runtime snapshot before installing or starting its inbound service", path=str(path))
    units = _units(files, provider)
    before = {unit["name"]: _query(unit["name"], target=provider, uid=user.pw_uid, content=unit["content"], run_cmd=run_cmd)
              for unit in units}
    for unit in units:
        state = before[unit["name"]]
        if state["state"] == "unknown":
            _error("SERVICE_STATE_UNKNOWN", "cannot establish service ownership while the manager is unavailable", service=unit["name"])
        if state["state"] == "absent":
            continue
        if state["definition_path"] and state["definition_path"] != unit["path"]:
            _error("SERVICE_INSTANCE_CONFLICT", "service manager loaded another instance's definition", service=unit["name"],
                   loaded_path=state["definition_path"], requested_path=unit["path"])
        if state["runtime_root"] and state["runtime_root"] != str(runtime):
            _error("SERVICE_INSTANCE_CONFLICT", "service manager still holds another runtime's environment", service=unit["name"])
        if not Path(unit["path"]).exists() or not state["definition_path"] or (not unit["name"].endswith(".timer") and not state["runtime_root"]):
            _error("SERVICE_INSTANCE_UNVERIFIED", "cannot establish the loaded service's ownership", service=unit["name"])
    commands = _activation_commands(action, units, before, target=provider, uid=user.pw_uid) if action != "install" else []
    changed_files = [item for item in files if item["before"].get("sha256") != item["sha256"]]
    public_files = []
    for item in files:
        public = {key: value for key, value in item.items() if key != "content"}
        if item in changed_files:
            if item["before"]["exists"]:
                public["backup_path"] = item["path"] + ".om-backup-" + item["before"]["sha256"][:16]
            # Unit definitions contain credential names/paths, never values.
            # Include the actual definition difference for informed confirmation.
            public["diff"] = "".join(difflib.unified_diff(
                _definition_preview(Path(item["path"]).read_text(), item["kind"]).splitlines(keepends=True) if item["before"]["exists"] else [],
                _definition_preview(item["content"] or "", item["kind"]).splitlines(keepends=True), fromfile="installed", tofile="requested"))
        public_files.append(public)
    scope = {"provider": provider, "repo_root": str(repo), "runtime_root": str(runtime), "config_yaml": str(source),
             "env_file": str(selected_env), "profile_path": str(profile_file), "unit_root": str(root),
             "deploy_user": user.pw_name, "deploy_uid": user.pw_uid,
             "runtime_owner": {"uid": runtime.stat().st_uid, "gid": runtime.stat().st_gid, "mode": stat.S_IMODE(runtime.stat().st_mode)}, "accounts": profile.get("accounts"), "markets": profile.get("markets")}
    required = sorted({name for values in (profile.get("secret_credentials") or {}).get("service_credentials", {}).values() for name in values})
    missing = []
    inaccessible = []
    for name in required:
        spec = credential_spec(name)
        store = Path((profile.get("secret_credentials") or {}).get("store_root") or credential_store_root)
        candidate = store / spec.systemd_credential_id
        try:
            good = stat.S_ISREG(candidate.lstat().st_mode) and candidate.stat().st_size > 0
        except FileNotFoundError:
            good = False
        except OSError:
            inaccessible.append(name)
            continue
        if not good:
            missing.append(name)
    state_fields = ("state", "enabled", "definition_path", "runtime_root")
    state_facts = {name: {key: row[key] for key in state_fields} for name, row in before.items()}
    token = _sha({"action": action, "scope": scope, "inputs": inputs, "files": public_files,
                  "states": state_facts,
                  "commands": commands, "required_credentials": required})
    out = {"ok": True, "action": action, "status": "preview", "scope": scope, "preview_sha256": token,
           "files": public_files, "input_files": inputs, "tasks": [unit["name"] for unit in units], "side_effects": _SIDE_EFFECTS,
           "required_credentials": required, "missing_credentials": missing, "unreadable_credential_metadata": inaccessible, "credential_readiness": "encrypted_store_metadata_only" if provider == "systemd" else "keychain_runtime_not_checked",
           "planned_operations": ([{"remove" if item.get("remove") else "write": item["path"]} for item in changed_files] + ([["systemctl", "daemon-reload"]] if provider == "systemd" else []) if action == "install" else commands),
           "before": before, "operations": [], "changed": False, "files_changed": False, "confirmed": bool(confirm)}
    if not confirm:
        return attach_write_contract(out, dry_run=True, write_applied=False, generate_audit_id=False)
    if not expected_preview_sha256 or expected_preview_sha256 != token:
        _error("STALE_PREVIEW", "apply requires the current service preview_sha256 and the same scope", actual_preview_sha256=token)
    if cancelled and cancelled():
        return attach_write_contract({**out, "status": "cancelled", "ok": False}, dry_run=False, write_applied=False)
    if action == "start" and (missing or inaccessible):
        _error("SERVICE_CREDENTIALS_MISSING", "provision required encrypted credentials before starting services", logical_names=missing, inaccessible=inaccessible)
    if provider == "systemd" and effective_uid != 0 and unit_root is None:
        _error("SERVICE_PERMISSION_REQUIRED", "system services require a separately authorized privileged invocation; preserve runtime and deploy-user arguments", scope=scope)
    # Unit names are shared by every runtime of this deployment identity.
    # Serialize on the actual manager directory, then recheck the preview's
    # input/definition facts before any instance can claim those names.
    unit_gid = 0 if expected_unit_uid == 0 else user.pw_gid
    _mkdir(root, uid=expected_unit_uid, gid=unit_gid, mode=0o755 if provider == "systemd" else 0o700)
    lock_path = root / f".options-monitor-{provider}-lifecycle.lock"
    _no_links(lock_path)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except PermissionError as exc:
        _permission_error(lock_path, exc)
    try:
        lock_stat = os.fstat(descriptor)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != expected_unit_uid or lock_stat.st_mode & 0o022:
            _error("SERVICE_IDENTITY_MISMATCH", "service operation lock must be controlled by its deployment owner", path=str(lock_path))
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _error("SERVICE_BUSY", "another service operation is using this manager directory", unit_root=str(root))
        for fact in inputs + [item["before"] for item in files]:
            if _file_fact(Path(fact["path"])) != fact:
                _error("STALE_PREVIEW", "service inputs changed while awaiting the operation lock")
        locked_states = {unit["name"]: _query(unit["name"], target=provider, uid=user.pw_uid,
                                             content=unit["content"], run_cmd=run_cmd) for unit in units}
        if {name: {key: row[key] for key in state_fields} for name, row in locked_states.items()} != state_facts:
            _error("STALE_PREVIEW", "service manager state changed while awaiting the operation lock")
        if action == "install":
            for name in ("logs", "locks", "output_accounts", "output_shared", "output_runs"):
                _mkdir(runtime / name, uid=user.pw_uid, gid=user.pw_gid)
            for item in changed_files:
                if cancelled and cancelled():
                    out.update(ok=False, status="cancelled")
                    break
                path = Path(item["path"])
                owner_uid = 0 if provider == "systemd" and item["kind"] not in {"service_profile", "service_env"} and unit_root is None else user.pw_uid
                owner_gid = 0 if owner_uid == 0 else user.pw_gid
                public_systemd_path = provider == "systemd" and item["kind"] not in {"service_profile", "service_env"}
                _mkdir(path.parent, uid=owner_uid, gid=owner_gid, mode=0o755 if public_systemd_path else 0o700)
                backup = None
                if item["before"]["exists"]:
                    backup = path.with_name(path.name + ".om-backup-" + item["before"]["sha256"][:16])
                    _no_links(backup)
                    if backup.exists() and backup.read_bytes() != path.read_bytes():
                        raise OSError("existing service backup content does not match")
                    if not backup.exists():
                        _atomic_file(backup, path.read_bytes(), mode=0o600, uid=owner_uid, gid=owner_gid)
                operation = {"operation": "remove" if item.get("remove") else "write", "path": str(path), "backup_path": str(backup) if backup else None, "readback": "unverified"}
                out["operations"].append(operation)
                out["changed"] = True
                out["files_changed"] = True
                if item.get("remove"):
                    path.unlink()
                    if path.exists():
                        raise OSError("credential drop-in removal readback mismatch")
                else:
                    _atomic_file(path, item["content"].encode(), mode=0o600 if item["kind"] in {"service_profile", "service_env"} else 0o644, uid=owner_uid, gid=owner_gid)
                operation["readback"] = "matched"
            else:
                out["status"] = "installed"
                if provider == "systemd":
                    result = _run(["systemctl", "daemon-reload"], run_cmd, mutation=True)
                    out["operations"].append(result)
                    out["changed"] = True
                    if not result["ok"]:
                        out.update(ok=False, status="unknown" if result.get("outcome") == "unknown" else "failed")
            out["after"] = {unit["name"]: _query(unit["name"], target=provider, uid=user.pw_uid, content=unit["content"], run_cmd=run_cmd) for unit in units}
        else:
            for command in commands:
                if cancelled and cancelled():
                    out.update(ok=False, status="cancelled")
                    break
                result = _run(command, run_cmd, mutation=True)
                out["operations"].append(result)
                out["changed"] = True
                if not result["ok"]:
                    out.update(ok=False, status="unknown" if result.get("outcome") == "unknown" else "failed")
                    break
            else:
                out["status"] = "started" if action == "start" else "stopped"
            after = {unit["name"]: _query(unit["name"], target=provider, uid=user.pw_uid, content=unit["content"], run_cmd=run_cmd) for unit in units}
            out["after"] = after
            desired = [unit for unit in units if unit["startup"] or action == "stop"]
            scope_readback = all(
                row["state"] == "absent" or (
                    row["definition_path"] == unit["path"] and
                    (unit["name"].endswith(".timer") or row["runtime_root"] == str(runtime))
                )
                for unit in units for row in (after[unit["name"]],)
            )
            ready = scope_readback and all(
                (after[unit["name"]]["state"] in ({"running", "loaded-idle"} if provider == "launchd" else {"running"})
                 and after[unit["name"]]["enabled"] in ({True} if provider == "launchd" else {"enabled", "enabled-runtime"}))
                if action == "start" else
                (after[unit["name"]]["state"] in {"absent", "loaded-idle"}
                 and (not unit["startup"] or after[unit["name"]]["enabled"] in {False, "disabled", "masked", "masked-runtime"}))
                for unit in desired
            )
            if out["ok"] and not ready:
                out.update(ok=False, status="unverified")
    except (OSError, KeyboardInterrupt, AgentToolError) as exc:
        if isinstance(exc, AgentToolError) and not out["operations"]:
            raise
        out.update(ok=False, status="cancelled" if isinstance(exc, KeyboardInterrupt) else "failed", error=str(exc))
    finally:
        os.close(descriptor)
    return attach_write_contract(out, dry_run=False, write_applied=out["changed"], rollback_hint="Use the reported backups only after a fresh preview; re-read service state before retrying.")
