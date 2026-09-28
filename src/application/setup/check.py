from __future__ import annotations

import importlib.util
import json
import os
import platform
import re
import shutil
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Iterable

from domain.domain.multi_tick import resolve_notification_route_from_config
from src.application.account_config import (
    ACCOUNT_TYPE_EXTERNAL_HOLDINGS,
    accounts_from_config,
    resolve_account_type,
)
from src.application.agent_tool_config import load_runtime_config
from src.application.agent_tool_contracts import AgentToolError
from src.application.config_yaml import load_yaml_config_file
from src.application.assistant.operation_policy import load_operation_policy_from_env
from src.application.assistant.settings import AssistantSettings
from src.application.platform_profile import PlatformProfile, current_platform_profile
from src.application.bot.model_config import ModelSettings, load_assistant_llm_config
from src.application.runtime_config_readiness import evaluate_runtime_config_readiness
from src.application.runtime_paths import resolve_runtime_root
from src.application.config_validator import validate_assistant_config
from src.application.secret_store import SecretError
from src.application.secret_store.registry import (
    FEISHU_BOT_APP_SECRET,
    FEISHU_HOLDINGS_APP_SECRET,
    INBOUND_OPERATION_HMAC_KEY,
)
from src.application.settings import build_effective_env, diagnose_effective_settings
from src.infrastructure.futu_gateway import inspect_futu_sdk_earnings_calendar_capability
from src.infrastructure.private_storage import private_path
from src.infrastructure.secret_store.factory import build_secret_provisioner


def run_setup_check(
    *,
    repo_root: str | Path,
    markets: Iterable[str] | None = None,
    runtime_root: str | Path | None = None,
    env_file: str | Path | None = None,
    include_local_env_file: bool = True,
) -> dict[str, Any]:
    root = Path(repo_root).expanduser().resolve()
    inherited_root = str(os.environ.get("OM_RUNTIME_ROOT") or "").strip()
    if runtime_root and inherited_root and Path(runtime_root).expanduser().resolve() != Path(inherited_root).expanduser().resolve():
        raise AgentToolError(code="INPUT_ERROR", message="--runtime-root conflicts with OM_RUNTIME_ROOT")
    inherited_env = str(os.environ.get("OM_ENV_FILE") or "").strip()
    if env_file and inherited_env and Path(env_file).expanduser().resolve() != Path(inherited_env).expanduser().resolve():
        raise AgentToolError(code="INPUT_ERROR", message="--env-file conflicts with OM_ENV_FILE")
    selected_markets = _normalize_markets(markets)
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, message: str, value: Any | None = None, hint: str | None = None) -> None:
        item: dict[str, Any] = {"name": name, "status": status, "message": message}
        if value is not None:
            item["value"] = value
        if hint:
            item["hint"] = hint
        checks.append(item)

    version = _read_text(root / "VERSION")
    profile = current_platform_profile()
    selected_env_file = env_file or inherited_env or None
    selected_root = runtime_root or inherited_root or None
    if selected_env_file is None and selected_root:
        requested_root = Path(selected_root).expanduser().resolve()
        selected_env_file = (
            profile.default_env_file
            if requested_root == profile.default_runtime_root
            else requested_root / "options-monitor.env"
        )
    add(
        "platform",
        "ok" if profile.platform in {"linux", "macos"} else "warn",
        f"{profile.platform} platform profile selected" if profile.platform in {"linux", "macos"} else "unsupported platform; service setup is manual",
        profile.to_dict(),
        hint="Use Linux or macOS for managed service deployment." if profile.platform == "other" else None,
    )

    add(
        "install.repo",
        "ok" if (root / "om").exists() and (root / "src").is_dir() else "error",
        "options-monitor repository layout is present" if (root / "om").exists() and (root / "src").is_dir() else "options-monitor repository layout is incomplete",
        {"repo_root": str(root), "version": version or None},
    )

    venv_python = root / ".venv" / "bin" / "python"
    add(
        "install.venv",
        "ok" if venv_python.exists() else "warn",
        "repo-local virtualenv is present" if venv_python.exists() else "repo-local virtualenv is missing; ./om will fall back to system python",
        {"python": sys.executable, "repo_venv_python": str(venv_python)},
        hint="Run scripts/install.sh or create .venv and install requirements.txt with constraints.txt." if not venv_python.exists() else None,
    )

    runtime_imports = ["pandas", "futu"]
    missing_deps = [name for name in runtime_imports if importlib.util.find_spec(name) is None]
    add(
        "install.dependencies",
        "ok" if not missing_deps else "error",
        "required Python imports are available" if not missing_deps else "required Python imports are missing",
        {"missing": missing_deps, "checked": runtime_imports} if missing_deps else {"checked": runtime_imports},
        hint="./.venv/bin/pip install -r requirements.txt -c constraints.txt" if missing_deps else None,
    )

    earnings_calendar_capability = inspect_futu_sdk_earnings_calendar_capability()
    earnings_calendar_supported = bool(earnings_calendar_capability.get("supported"))
    minimum_futu_version = str(earnings_calendar_capability.get("minimum_version") or "10.9.6908")
    add(
        "install.futu_earnings_calendar",
        "ok" if earnings_calendar_supported else "error",
        (
            "Futu SDK earnings-calendar capability is available"
            if earnings_calendar_supported
            else "Futu SDK earnings-calendar capability is unavailable"
        ),
        earnings_calendar_capability,
        hint=(
            None
            if earnings_calendar_supported
            else f"Install futu-api>={minimum_futu_version} and use an OpenD build that supports get_earnings_calendar."
        ),
    )

    server_deps_available = importlib.util.find_spec("lark_oapi") is not None
    add(
        "install.server_dependencies",
        "ok" if server_deps_available else "info",
        "server dependency set is installed" if server_deps_available else "server dependency set is optional; install it before running Feishu long-connection inbound",
        {
            "lark_oapi": server_deps_available,
            "needed_for": ["inbound.feishu_ws", "service render --include-feishu-ws"],
        },
        hint="./.venv/bin/pip install -r requirements/server.txt -c constraints/server.txt" if not server_deps_available else None,
    )

    effective_env = build_effective_env(
        repo_root=root,
        env_file=selected_env_file,
        include_local_env_file=include_local_env_file,
    )
    configured_root = str(effective_env.values.get("OM_RUNTIME_ROOT") or "").strip()
    if runtime_root and configured_root and Path(runtime_root).expanduser().resolve() != Path(configured_root).expanduser().resolve():
        raise AgentToolError(code="INPUT_ERROR", message="--runtime-root conflicts with OM_RUNTIME_ROOT from effective settings")
    runtime = resolve_runtime_root(repo_root=root, runtime_root=runtime_root, environ=effective_env.values)
    advice_runtime_root = (
        profile.default_runtime_root
        if profile.platform == "macos" and runtime.source == "repo_default"
        else runtime.runtime_root
    )
    advice_env_file = effective_env.env_file or (
        advice_runtime_root / "options-monitor.env"
        if runtime.source != "repo_default" and advice_runtime_root != profile.default_runtime_root
        else profile.default_env_file
    )
    om_command = _quote(root / "om")
    config_init_command = (
        f"{om_command} config init --output {_quote(advice_runtime_root / 'config.yaml')} "
        f"--runtime-output-dir {_quote(advice_runtime_root)} "
        + " ".join(f"--market {market}" for market in selected_markets)
        + " --no-build"
    )

    resolved_assistant_config = runtime.runtime_root / "resolved" / "config.assistant.json"
    source_yaml = runtime.runtime_root / "config.yaml"
    assistant_config = (
        resolved_assistant_config
        if resolved_assistant_config.exists() or source_yaml.exists()
        else root / "config.assistant.json"
    )
    bot_enabled = _assistant_bot_enabled(assistant_config)
    if source_yaml.exists():
        try:
            authoring = load_yaml_config_file(source_yaml)
        except AgentToolError:
            bot_enabled = True
        else:
            authored_assistant = authoring.get("assistant")
            authored_bot = authored_assistant.get("bot") if isinstance(authored_assistant, dict) else None
            bot_enabled = bot_enabled or bool(
                isinstance(authored_assistant, dict) and authored_assistant.get("enabled")
                and isinstance(authored_bot, dict) and authored_bot.get("enabled")
            )
    model_raw, model_error = (
        load_assistant_llm_config(config_path=assistant_config, require_config=False)
        if bot_enabled
        else (None, None)
    )
    model_settings: ModelSettings | None = None
    if model_raw is not None and model_error is None:
        try:
            model_settings = ModelSettings.from_config(model_raw)
        except Exception:
            model_error = "invalid_model_config"
    model_context_ok = model_settings is not None
    add(
        "bot.model_context",
        "info" if not bot_enabled else ("ok" if model_context_ok else "error"),
        "Bot is disabled; model context is not required" if not bot_enabled else ("active Bot model context is valid" if model_context_ok else "active Bot model context is missing or invalid"),
        {
            "config_path": str(assistant_config),
            "bot_enabled": bot_enabled,
            "context_window_tokens": model_settings.context_window_tokens if model_settings else None,
            "max_output_tokens": model_settings.max_output_tokens if model_settings else None,
            "error": model_error or (None if model_settings or not bot_enabled else "model_context_missing"),
        },
        hint=None if not bot_enabled or model_context_ok else "Build or fix the resolved assistant config with a valid context_window_tokens value.",
    )

    audit_raw = str(effective_env.values.get("OM_INBOUND_AUDIT_DB") or "").strip()
    audit_db = Path(audit_raw).expanduser() if audit_raw else runtime.runtime_root / "output_shared" / "state" / "inbound_control.sqlite3"
    if not audit_db.is_absolute():
        audit_db = root / audit_db
    audit_db = private_path(audit_db)
    session_path = audit_db
    session_parent = session_path.parent
    session_parent_is_symlink = session_parent.is_symlink()
    session_parent_ok = (
        session_parent.is_dir()
        and not session_parent_is_symlink
        and os.access(session_parent, os.W_OK | os.X_OK)
    )
    add(
        "bot.session_path",
        "info" if not bot_enabled else ("ok" if session_parent_ok else "error"),
        "Bot is disabled; session path is not required" if not bot_enabled else ("Bot Host parent exists and is writable" if session_parent_ok else "Bot Host parent is missing or not writable"),
        {
            "host_audit_db": str(audit_db),
            "session_path": str(session_path),
            "parent": str(session_parent),
            "parent_exists": session_parent.is_dir(),
            "parent_is_symlink": session_parent_is_symlink,
            "session_exists": session_path.exists(),
        },
        hint=None if not bot_enabled or session_parent_ok else f"Create and grant write access to the Session parent: {session_parent}",
    )
    installer_mode = str(effective_env.values.get("OM_UPGRADE_INSTALLER") or "auto").strip().lower()
    if installer_mode not in {"auto", "uv", "pip"}:
        installer_mode = "auto"
    uv_path = shutil.which("uv")
    add(
        "upgrade.uv",
        "ok" if uv_path else ("warn" if installer_mode == "uv" else "info"),
        "uv is available for service upgrade dependency installation" if uv_path else "uv is not available; service upgrade will use pip fallback",
        {"installer_mode": installer_mode, "uv_path": uv_path, "cache_env": {"UV_CACHE_DIR": effective_env.values.get("UV_CACHE_DIR")}},
        hint=(
            "Install uv on the remote host or set OM_UPGRADE_INSTALLER=pip before running service upgrade."
            if not uv_path and installer_mode == "uv"
            else "Install uv on the remote host to speed up service upgrade dependency installation."
            if not uv_path
            else None
        ),
    )

    settings = diagnose_effective_settings(
        repo_root=root,
        env_file=selected_env_file,
        include_local_env_file=include_local_env_file,
    )
    settings_summary_raw = settings.get("summary")
    settings_summary: dict[str, Any] = settings_summary_raw if isinstance(settings_summary_raw, dict) else {}
    add(
        "settings",
        "error" if int(settings_summary.get("error_count") or 0) > 0 else ("warn" if int(settings_summary.get("warning_count") or 0) > 0 else "ok"),
        "settings diagnostics completed",
        {
            "env_file": settings.get("env_file"),
            "env_file_loaded": bool(settings.get("env_file_loaded")),
            "error_count": int(settings_summary.get("error_count") or 0),
            "warning_count": int(settings_summary.get("warning_count") or 0),
        },
        hint=f"{om_command} settings doctor",
    )

    config_ok_markets: list[str] = []
    market_configs: dict[str, dict[str, Any]] = {}
    for market in selected_markets:
        config_path = runtime.runtime_root / f"config.{market}.json"
        if not config_path.exists():
            recovery_command = (
                _mac_config_build_command(om_command, advice_runtime_root, market)
                if profile.platform == "macos" and (advice_runtime_root / "config.yaml").exists()
                else config_init_command
            )
            add(
                f"config.{market}",
                "warn",
                f"{market.upper()} runtime config is missing",
                {"config_path": str(config_path)},
                hint=(
                    f"export OM_RUNTIME_ROOT={_quote(advice_runtime_root)}; {recovery_command}"
                    if profile.platform == "macos" and runtime.source == "repo_default"
                    else recovery_command
                ),
            )
            continue
        try:
            _path, cfg = load_runtime_config(config_key=market, config_path=config_path)
        except AgentToolError as exc:
            add(f"config.{market}", "error", exc.message, {"config_path": str(config_path)}, hint=exc.hint)
            continue
        readiness = evaluate_runtime_config_readiness(
            dict(cfg),
            repo_root=root,
            runtime_config_path=config_path,
            explicit_market=market,
            config_key=market,
        )
        if not readiness["ok"]:
            add(
                f"config.{market}",
                "error",
                f"{market.upper()} runtime config is not ready",
                readiness,
                hint=f"{om_command} config validate --config-path {_quote(config_path)} --market {market}",
            )
            continue
        config_ok_markets.append(market)
        market_configs[market] = dict(cfg)
        add(
            f"config.{market}",
            "ok",
            f"{market.upper()} runtime config validates",
            readiness,
        )

    sqlite_path = runtime.runtime_root / "output_shared" / "state" / "option_positions.sqlite3"
    add(
        "runtime_root",
        "ok" if runtime.runtime_root.exists() else "info",
        "runtime root exists" if runtime.runtime_root.exists() else "runtime root does not exist yet; it will be created by runtime writes",
        {
            "runtime_root": str(runtime.runtime_root),
            "source": runtime.source,
            "recommended_runtime_root": str(profile.default_runtime_root),
            "recommended_env_file": str(profile.default_env_file),
            "option_positions_sqlite": str(sqlite_path),
            "option_positions_sqlite_exists": sqlite_path.exists(),
        },
    )

    add(
        "service",
        "info",
        "service/timer state is observed only; setup check does not install, enable, or start services",
        _service_probe(selected_markets),
    )

    credential_guidance, credential_steps = _credential_guidance(
        repo_root=root,
        market_configs=market_configs,
        unknown_markets=[market for market in selected_markets if market not in market_configs],
        assistant_config=assistant_config,
        effective_env=effective_env.values,
        platform_name=profile.platform,
    )
    guidance_incomplete = bool(
        credential_guidance["unknown_requirements"]
        or any(item["storage_status"] != "present" for item in credential_guidance["credentials"])
    )
    add(
        "credential_guidance",
        "warn" if guidance_incomplete else "ok",
        "credential storage guidance is incomplete" if guidance_incomplete else "credential storage observed; service consumption not verified",
        credential_guidance,
    )

    next_steps = credential_steps + _next_steps(
        config_ok_markets=config_ok_markets,
        selected_markets=selected_markets,
        settings=settings,
        profile=profile,
        repo_root=root,
        runtime_root=advice_runtime_root,
        om_command=om_command,
        config_init_command=config_init_command,
        env_file=advice_env_file,
        market_configs=market_configs,
        needs_runtime_export=profile.platform == "macos" and runtime.source == "repo_default",
    )
    error_count = sum(1 for item in checks if item.get("status") == "error")
    warning_count = sum(1 for item in checks if item.get("status") == "warn")
    return {
        "summary": {
            "ok": error_count == 0,
            "error_count": error_count,
            "warning_count": warning_count,
        },
        "repo_root": str(root),
        "markets": selected_markets,
        "platform_profile": profile.to_dict(),
        "checks": checks,
        "next_steps": next_steps,
    }


def _assistant_bot_enabled(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True  # An unreadable existing config needs the Bot validation error.
    assistant = raw.get("assistant") if isinstance(raw, dict) else None
    bot = assistant.get("bot") if isinstance(assistant, dict) else None
    return bool(isinstance(assistant, dict) and assistant.get("enabled") and isinstance(bot, dict) and bot.get("enabled"))


def _credential_guidance(
    *,
    repo_root: Path,
    market_configs: Mapping[str, dict[str, Any]],
    unknown_markets: list[str],
    assistant_config: Path,
    effective_env: Mapping[str, str],
    platform_name: str,
) -> tuple[dict[str, Any], list[str]]:
    requirements: dict[str, set[str]] = {}
    unknown: list[str] = [f"market:{market}" for market in unknown_markets]

    def require(name: str, feature: str) -> None:
        requirements.setdefault(name, set()).add(feature)

    for market, cfg in market_configs.items():
        try:
            if any(
                resolve_account_type(cfg, account=account) == ACCOUNT_TYPE_EXTERNAL_HOLDINGS
                for account in accounts_from_config(cfg)
            ):
                require(FEISHU_HOLDINGS_APP_SECRET, f"holdings:{market}")
            if resolve_notification_route_from_config(config=cfg)["provider"] == "feishu_app":
                require(FEISHU_BOT_APP_SECRET, f"notifications:{market}")
        except (ValueError, TypeError):
            unknown.append(f"market:{market}")

    try:
        assistant_raw = json.loads(assistant_config.read_text(encoding="utf-8"))
        if not isinstance(assistant_raw, dict):
            raise ValueError("assistant config must be an object")
        validate_assistant_config(assistant_raw)
        assistant = AssistantSettings.from_runtime_config(assistant_raw)
        if assistant.enabled and assistant.bot.enabled:
            if not assistant.llm.enabled:
                unknown.append("assistant_model")
            elif assistant.llm.credential_name:
                require(assistant.llm.credential_name, "assistant_bot")
    except (OSError, ValueError, SystemExit):
        unknown.append("assistant_config")

    policy = load_operation_policy_from_env(environ=effective_env)
    if policy.operations_enabled and any((
        policy.trade_write_enabled,
        policy.symbol_write_enabled,
        policy.upgrade_write_enabled,
        policy.model_write_enabled,
        policy.monitor_run_enabled,
    )):
        require(INBOUND_OPERATION_HMAC_KEY, "inbound_operations")

    credentials: list[dict[str, Any]] = []
    steps: list[str] = []
    backend = str(effective_env.get("OM_SECRET_BACKEND") or os.environ.get("OM_SECRET_BACKEND") or "auto").strip().lower()
    store = None
    if requirements and backend != "env":
        try:
            store = build_secret_provisioner(backend=backend)
        except (SecretError, ValueError):
            store = None
    for name, features in requirements.items():
        storage_status = "unknown"
        if store is not None:
            try:
                storage_status = "present" if store.status(name).configured else "missing"
            except (SecretError, ValueError):
                pass
        credentials.append({
            "logical_name": name,
            "features": sorted(features),
            "storage_status": storage_status,
            "consumer_verified": False,
            "runtime_consumer_status": (
                "pending"
                if platform_name == "linux" and not str(effective_env.get("CREDENTIALS_DIRECTORY") or os.environ.get("CREDENTIALS_DIRECTORY") or "").strip()
                else "unknown"
            ),
        })
        if storage_status == "missing":
            prefix = f"sudo {_quote(repo_root / 'om')}" if platform_name == "linux" else "om"
            steps.append(f"{prefix} secrets set {name}")
    return {
        "credentials": credentials,
        "unknown_requirements": list(dict.fromkeys(unknown)),
        "conditional_requirements": [
            {"feature": "feishu_ws_service", "logical_name": FEISHU_BOT_APP_SECRET},
            {"feature": "trade_event_pagination", "logical_name": INBOUND_OPERATION_HMAC_KEY},
        ],
        "consumer_verified": False,
    }, steps


def _normalize_markets(markets: Iterable[str] | None) -> list[str]:
    raw = [str(item or "").strip().lower() for item in (markets or ["us", "hk"])]
    out: list[str] = []
    for item in raw:
        if item == "all":
            for market in ("us", "hk"):
                if market not in out:
                    out.append(market)
            continue
        if item in {"us", "hk"} and item not in out:
            out.append(item)
    return out or ["us", "hk"]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _service_probe(markets: list[str]) -> dict[str, Any]:
    system = platform.system().lower()
    if system == "darwin":
        launch_agents = Path.home() / "Library" / "LaunchAgents"
        files = [
            launch_agents / f"com.options-monitor.tick-{market}.plist"
            for market in markets
        ]
        files.extend([
            launch_agents / "com.options-monitor.trade-intake.plist",
            launch_agents / "com.options-monitor.feishu-ws.plist",
        ])
        return {
            "target": "launchd",
            "configured_files": [str(path) for path in files if path.exists()],
            "checked_files": [str(path) for path in files],
        }
    files = [
        Path("/etc/systemd/system") / f"options-monitor-tick-{market}.timer"
        for market in markets
    ]
    files.extend([
        Path("/etc/systemd/system/options-monitor-trade-intake.service"),
        Path("/etc/systemd/system/options-monitor-feishu-ws.service"),
    ])
    return {
        "target": "systemd" if system == "linux" else "manual",
        "configured_files": [str(path) for path in files if path.exists()],
        "checked_files": [str(path) for path in files],
    }


def _next_steps(
    *,
    config_ok_markets: list[str],
    selected_markets: list[str],
    settings: dict[str, Any],
    profile: PlatformProfile,
    repo_root: Path,
    runtime_root: Path,
    om_command: str,
    config_init_command: str,
    env_file: Path,
    market_configs: Mapping[str, dict[str, Any]],
    needs_runtime_export: bool,
) -> list[str]:
    steps: list[str] = []
    if needs_runtime_export:
        steps.append(f"export OM_RUNTIME_ROOT={_quote(runtime_root)}")
    missing_markets = [market for market in selected_markets if market not in config_ok_markets]
    if missing_markets:
        if not (runtime_root / "config.yaml").exists():
            steps.append(config_init_command)
        for market in missing_markets:
            steps.append(_mac_config_build_command(om_command, runtime_root, market))
        steps.append(
            f"{om_command} config build-assistant --source yaml "
            f"--config-yaml {_quote(runtime_root / 'config.yaml')} "
            f"--output {_quote(runtime_root / 'resolved' / 'config.assistant.json')}"
        )
    settings_summary_raw = settings.get("summary")
    settings_summary: dict[str, Any] = settings_summary_raw if isinstance(settings_summary_raw, dict) else {}
    if int(settings_summary.get("warning_count") or 0) or int(settings_summary.get("error_count") or 0):
        steps.append(f"{om_command} settings doctor --env-file {_quote(env_file)}")
    for market in config_ok_markets:
        steps.append(f"{om_command} doctor --config-key {market} --config-path {_quote(runtime_root / f'config.{market}.json')} --env-file {_quote(env_file)}")
    if config_ok_markets and profile.service_target != "manual":
        accounts = sorted({account for cfg in market_configs.values() for account in accounts_from_config(cfg)})
        command = (
            f"{om_command} service render --target {profile.service_target} "
            f"--repo-root {_quote(repo_root)} --runtime-root {_quote(runtime_root)} "
            f"--env-file {_quote(env_file)} --config-yaml {_quote(runtime_root / 'config.yaml')} "
            f"--markets {' '.join(config_ok_markets)}"
        )
        if accounts:
            command += f" --accounts {' '.join(accounts)}"
        for market in config_ok_markets:
            command += f" --config-{market} {_quote(runtime_root / f'config.{market}.json')}"
        steps.append(command + " --output-dir /tmp/options-monitor-service")
    if not steps:
        steps.append(f"{om_command} doctor --config-key us")
    return steps


def _quote(value: str | Path) -> str:
    return shlex.quote(str(value))


def _mac_config_build_command(om_command: str, runtime_root: Path, market: str) -> str:
    return (
        f"{om_command} config build --source yaml --market {market} "
        f"--config-yaml {_quote(runtime_root / 'config.yaml')} "
        f"--output {_quote(runtime_root / f'config.{market}.json')}"
    )
