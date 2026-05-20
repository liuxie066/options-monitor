from __future__ import annotations

import importlib.util
import platform
import sys
from pathlib import Path
from typing import Any, Iterable

from src.application.agent_tool_config import load_runtime_config
from src.application.agent_tool_contracts import AgentToolError
from src.application.config_validator import validate_config
from src.application.runtime_paths import resolve_runtime_root
from src.application.settings import diagnose_effective_settings


def run_setup_check(
    *,
    repo_root: str | Path,
    markets: Iterable[str] | None = None,
    env_file: str | Path | None = None,
    include_local_env_file: bool = True,
) -> dict[str, Any]:
    root = Path(repo_root).expanduser().resolve()
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

    missing_deps = [name for name in ("pandas", "futu") if importlib.util.find_spec(name) is None]
    add(
        "install.dependencies",
        "ok" if not missing_deps else "error",
        "required Python imports are available" if not missing_deps else "required Python imports are missing",
        {"missing": missing_deps} if missing_deps else {"checked": ["pandas", "futu"]},
        hint="./.venv/bin/pip install -r requirements.txt -c constraints.txt" if missing_deps else None,
    )

    settings = diagnose_effective_settings(
        repo_root=root,
        env_file=env_file,
        include_local_env_file=include_local_env_file,
    )
    settings_summary = settings.get("summary") if isinstance(settings.get("summary"), dict) else {}
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
        hint="./om settings doctor",
    )

    config_ok_markets: list[str] = []
    for market in selected_markets:
        config_path = root / f"config.{market}.json"
        if not config_path.exists():
            add(
                f"config.{market}",
                "warn",
                f"{market.upper()} runtime config is missing",
                {"config_path": str(config_path)},
                hint=f"./om setup init --market {market} --account lx --futu-acc-id <futu-account-id>",
            )
            continue
        try:
            _path, cfg = load_runtime_config(config_key=market, config_path=config_path)
            validate_config(dict(cfg))
        except AgentToolError as exc:
            add(f"config.{market}", "error", exc.message, {"config_path": str(config_path)}, hint=exc.hint)
            continue
        except SystemExit as exc:
            add(f"config.{market}", "error", str(exc), {"config_path": str(config_path)}, hint=f"./om config validate --config-path {config_path}")
            continue
        config_ok_markets.append(market)
        add(
            f"config.{market}",
            "ok",
            f"{market.upper()} runtime config validates",
            {"config_path": str(config_path)},
        )

    runtime = resolve_runtime_root(repo_root=root)
    sqlite_path = runtime.runtime_root / "output_shared" / "state" / "option_positions.sqlite3"
    add(
        "runtime_root",
        "ok" if runtime.runtime_root.exists() else "info",
        "runtime root exists" if runtime.runtime_root.exists() else "runtime root does not exist yet; it will be created by runtime writes",
        {
            "runtime_root": str(runtime.runtime_root),
            "source": runtime.source,
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

    next_steps = _next_steps(config_ok_markets=config_ok_markets, selected_markets=selected_markets, settings=settings)
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


def _next_steps(*, config_ok_markets: list[str], selected_markets: list[str], settings: dict[str, Any]) -> list[str]:
    steps: list[str] = []
    missing_markets = [market for market in selected_markets if market not in config_ok_markets]
    if missing_markets:
        market = missing_markets[0]
        steps.append(f"./om setup init --market {market} --account lx --futu-acc-id <futu-account-id>")
    settings_summary = settings.get("summary") if isinstance(settings.get("summary"), dict) else {}
    if int(settings_summary.get("warning_count") or 0) or int(settings_summary.get("error_count") or 0):
        steps.append("./om settings doctor")
    for market in config_ok_markets:
        steps.append(f"./om doctor --config-key {market}")
    if not steps:
        steps.append("./om doctor --config-key us")
    return steps
