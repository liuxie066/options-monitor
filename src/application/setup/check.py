from __future__ import annotations

import importlib.util
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

from src.application.agent_tool_config import load_runtime_config
from src.application.agent_tool_contracts import AgentToolError
from src.application.config_yaml_init import DEFAULT_FUTU_ACCOUNT_ID
from src.application.config_yaml import load_yaml_config_file
from src.application.platform_profile import PlatformProfile, current_platform_profile
from src.application.bot.model_config import ModelSettings, load_bot_llm_config
from src.application.runtime_config_readiness import evaluate_runtime_config_readiness
from src.application.runtime_paths import resolve_runtime_root
from src.application.settings import build_effective_env, diagnose_effective_settings
from src.infrastructure.futu_gateway import inspect_futu_sdk_earnings_calendar_capability
from src.infrastructure.private_storage import private_path


def run_setup_check(
    *,
    repo_root: str | Path,
    markets: Iterable[str] | None = None,
    env_file: str | Path | None = None,
    include_local_env_file: bool = True,
) -> dict[str, Any]:
    root = Path(repo_root).expanduser().resolve()
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
        "repo-local virtualenv is present" if venv_python.exists() else "repo-local virtualenv is missing; om will fall back to system python",
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
        env_file=env_file,
        include_local_env_file=include_local_env_file,
    )
    runtime = resolve_runtime_root(repo_root=root, environ=effective_env.values, user_home=Path.home())
    configured_markets = _configured_markets(runtime.runtime_root)
    selected_markets = _normalize_markets(markets) if markets is not None else configured_markets or ["us", "hk"]

    repo_bot_config = root / "config.bot.json"
    resolved_bot_config = runtime.runtime_root / "resolved" / "config.bot.json"
    bot_config = (
        repo_bot_config
        if runtime.source == "repo_default" and repo_bot_config.exists()
        else resolved_bot_config
    )
    model_raw, model_error = load_bot_llm_config(
        config_path=bot_config,
        require_config=False,
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
        "ok" if model_context_ok else "warn",
        "active Bot model context is valid" if model_context_ok else "active Bot model context is missing or invalid",
        {
            "config_path": str(bot_config),
            "context_window_tokens": model_settings.context_window_tokens if model_settings else None,
            "max_output_tokens": model_settings.max_output_tokens if model_settings else None,
            "error": model_error or (None if model_settings else "model_context_missing"),
        },
        hint=None if model_context_ok else "Build or fix the resolved Bot config with a valid context_window_tokens value.",
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
        "ok" if session_parent_ok else "warn",
        "Bot Host parent exists and is writable" if session_parent_ok else "Bot Host parent is missing or not writable",
        {
            "host_audit_db": str(audit_db),
            "session_path": str(session_path),
            "parent": str(session_parent),
            "parent_exists": session_parent.is_dir(),
            "parent_is_symlink": session_parent_is_symlink,
            "session_exists": session_path.exists(),
        },
        hint=None if session_parent_ok else f"Create and grant write access to the Session parent: {session_parent}",
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
        env_file=env_file,
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
        hint="om settings doctor",
    )

    config_ok_markets: list[str] = []
    config_steps: list[str] = []
    repair_accounts: set[str] = set()
    source_path = runtime.runtime_root / "config.yaml"
    for market in selected_markets:
        config_path = runtime.runtime_root / f"config.{market}.json"
        if not config_path.exists():
            if source_path.is_file() and market in configured_markets:
                command = (f"om config build --source yaml --market {market}"
                           f" --config-yaml {shlex.quote(str(source_path))} --output {shlex.quote(str(config_path))}")
                hint = f"{command} --dry-run"
                config_steps.extend((hint, command))
            elif source_path.is_file():
                hint = f"config.yaml does not configure {market.upper()}; add the market before building its snapshot."
                config_steps.append(hint)
            else:
                hint = "om setup init (previews an external config directory before writing)"
                config_steps.append("om setup init")
            add(
                f"config.{market}",
                "error",
                f"{market.upper()} runtime config is missing",
                {"config_path": str(config_path)},
                hint=hint,
            )
            continue
        try:
            _path, cfg = load_runtime_config(config_key=market, config_path=config_path)
        except AgentToolError as exc:
            if source_path.is_file():
                repair = (f"om config build --source yaml --market {market}"
                          f" --config-yaml {shlex.quote(str(source_path))} --output {shlex.quote(str(config_path))}")
                config_steps.extend((f"{repair} --dry-run", repair))
            else:
                repair = "Restore config.yaml from a trusted backup before rebuilding the runtime snapshot."
                config_steps.append(repair)
            add(f"config.{market}", "error", exc.message, {"config_path": str(config_path)}, hint=repair)
            continue
        account_settings = cfg.get("account_settings") if isinstance(cfg, dict) else None
        placeholder_accounts = [
            account
            for account, settings in (account_settings or {}).items()
            if isinstance(settings, dict)
            and settings.get("type") == "futu"
            and isinstance(settings.get("futu"), dict)
            and settings["futu"].get("account_id") == DEFAULT_FUTU_ACCOUNT_ID
        ] if isinstance(account_settings, dict) else []
        if placeholder_accounts:
            if source_path.is_file():
                for account in placeholder_accounts:
                    if account in repair_accounts:
                        continue
                    repair_accounts.add(account)
                    command = (f"om accounts edit --market {market} --account-label {account}"
                               f" --futu-acc-id ACCOUNT_ID --config-yaml {shlex.quote(str(source_path))}")
                    config_steps.extend(("将 ACCOUNT_ID 换成真实数字，先预览再确认写入：",
                                         command, f"{command} --apply --confirm"))
                hint = "Use om accounts edit to preview the real Futu account ID, then publish with --apply --confirm."
            else:
                hint = "Restore config.yaml from a trusted backup before editing the Futu account ID."
                config_steps.append(hint)
            add(
                f"config.{market}",
                "error",
                f"{market.upper()} Futu account ID is still {DEFAULT_FUTU_ACCOUNT_ID}",
                {"config_path": str(config_path), "accounts": placeholder_accounts},
                hint=hint,
            )
            continue
        readiness = evaluate_runtime_config_readiness(
            dict(cfg),
            repo_root=root,
            runtime_config_path=config_path,
            explicit_market=market,
            config_key=market,
        )
        if not readiness["ok"]:
            config_steps.append(f"om config validate --config-path {config_path} --market {market}")
            add(
                f"config.{market}",
                "error",
                f"{market.upper()} runtime config is not ready",
                readiness,
                hint=f"om config validate --config-path {config_path} --market {market}",
            )
            continue
        config_ok_markets.append(market)
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

    next_steps = _next_steps(
        config_ok_markets=config_ok_markets,
        config_steps=config_steps,
        settings=settings,
        profile=profile,
    )
    error_count = sum(1 for item in checks if item.get("status") == "error")
    warning_count = sum(1 for item in checks if item.get("status") == "warn")
    return {
        "summary": {
            "ok": error_count == 0,
            "bot_ready": model_context_ok and session_parent_ok,
            "error_count": error_count,
            "warning_count": warning_count,
        },
        "repo_root": str(root),
        "markets": selected_markets,
        "platform_profile": profile.to_dict(),
        "checks": checks,
        "next_steps": next_steps,
    }


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


def _configured_markets(runtime_root: Path) -> list[str]:
    source = runtime_root / "config.yaml"
    if source.is_file():
        try:
            document = load_yaml_config_file(source)
            market_doc = document.get("markets")
            if isinstance(market_doc, dict):
                configured = [market for market in ("us", "hk") if market in market_doc]
                if configured:
                    return configured
        except (AgentToolError, OSError, UnicodeError):
            pass
    return [market for market in ("us", "hk") if (runtime_root / f"config.{market}.json").is_file()]


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
    config_steps: list[str],
    settings: dict[str, Any],
    profile: PlatformProfile,
) -> list[str]:
    steps: list[str] = list(dict.fromkeys(config_steps))
    settings_summary_raw = settings.get("summary")
    settings_summary: dict[str, Any] = settings_summary_raw if isinstance(settings_summary_raw, dict) else {}
    if int(settings_summary.get("warning_count") or 0) or int(settings_summary.get("error_count") or 0):
        steps.append("om settings doctor")
    for market in config_ok_markets:
        steps.append(f"om doctor --config-key {market}")
    if config_ok_markets and profile.service_target != "manual":
        steps.append("om service render --help")
    if not steps:
        steps.append("om doctor --config-key us")
    return steps
