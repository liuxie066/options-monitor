from __future__ import annotations

import json
import os
import plistlib
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, cast


ServiceTarget = Literal["systemd", "launchd"]
ServiceProvider = Literal["systemd", "launchd", "manual", "openclaw"]

DEFAULT_MARKETS: tuple[str, ...] = ("us", "hk")
DEFAULT_ACCOUNTS: tuple[str, ...] = ("lx", "sy")
DEFAULT_TIMEOUT_SECONDS = 600


@dataclass(frozen=True)
class RenderedServiceFile:
    relative_path: str
    content: str
    install_path: str
    kind: str

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        out = {
            "relative_path": self.relative_path,
            "install_path": self.install_path,
            "kind": self.kind,
        }
        if include_content:
            out["content"] = self.content
        return out


def normalize_target(value: str) -> ServiceTarget:
    out = str(value or "").strip().lower()
    if out not in {"systemd", "launchd"}:
        raise ValueError(f"unsupported service target: {value}")
    return cast(ServiceTarget, out)


def normalize_markets(values: list[str] | tuple[str, ...] | None) -> list[str]:
    raw_values = values or DEFAULT_MARKETS
    out: list[str] = []
    for raw in raw_values:
        market = str(raw or "").strip().lower()
        if not market:
            continue
        if market not in {"us", "hk"}:
            raise ValueError(f"unsupported market: {raw}")
        if market not in out:
            out.append(market)
    return out or list(DEFAULT_MARKETS)


def normalize_accounts(values: list[str] | tuple[str, ...] | None) -> list[str]:
    raw_values = values or DEFAULT_ACCOUNTS
    out: list[str] = []
    for raw in raw_values:
        account = str(raw or "").strip()
        if account and account not in out:
            out.append(account)
    return out or list(DEFAULT_ACCOUNTS)


def default_runtime_root(target: ServiceTarget, *, home: Path | None = None) -> Path:
    if target == "systemd":
        return Path("/var/lib/options-monitor")
    home_dir = home or Path.home()
    return home_dir / "Library" / "Application Support" / "options-monitor"


def _resolve_path(value: str | Path | None, *, base: Path, default: Path) -> Path:
    raw = str(value or "").strip()
    if not raw:
        return default.resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def _config_path_for_market(market: str, *, repo_root: Path, config_paths: dict[str, str | Path] | None) -> Path:
    configured = (config_paths or {}).get(market)
    default = repo_root / f"config.{market}.json"
    return _resolve_path(configured, base=repo_root, default=default)


def _json_arg(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _systemd_quote_arg(value: str | Path) -> str:
    text = str(value)
    if not text:
        return '""'
    if any(ch.isspace() for ch in text) or any(ch in text for ch in ('"', "'", "\\", ";")):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _systemd_join_args(args: list[str]) -> str:
    return " ".join(_systemd_quote_arg(arg) for arg in args)


def _systemd_environment_assignment(name: str, value: str | Path) -> str:
    escaped = f"{name}={value}".replace("\\", "\\\\").replace('"', '\\"')
    return f'Environment="{escaped}"'


def _systemd_environment_file(path: Path) -> str:
    return f"EnvironmentFile={_systemd_quote_arg(path)}"


def _systemd_unit(
    *,
    description: str,
    repo_root: Path,
    runtime_root: Path,
    exec_args: list[str],
    env_file: Path | None = None,
    service_type: str = "oneshot",
    restart: str | None = None,
) -> str:
    lines = [
        "[Unit]",
        f"Description={description}",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        f"Type={service_type}",
        f"WorkingDirectory={_systemd_quote_arg(repo_root)}",
        _systemd_environment_assignment("PYTHONUNBUFFERED", "1"),
        _systemd_environment_assignment("OM_RUNTIME_ROOT", runtime_root),
    ]
    if env_file is not None:
        lines.append(_systemd_environment_file(env_file))
    lines.append("ExecStart=" + _systemd_join_args(exec_args))
    if restart:
        lines.append(f"Restart={restart}")
        lines.append("RestartSec=10")
    lines.extend(["StandardOutput=journal", "StandardError=journal", ""])
    return "\n".join(lines)


def _systemd_timer(*, description: str, unit_name: str, interval: str | None = None, calendar: str | None = None) -> str:
    timer_lines = [
        "[Unit]",
        f"Description={description}",
        "",
        "[Timer]",
    ]
    if calendar:
        timer_lines.append(f"OnCalendar={calendar}")
    else:
        timer_lines.extend(["OnBootSec=2min", f"OnUnitActiveSec={interval or '10min'}"])
    timer_lines.extend([
        "Persistent=true",
        f"Unit={unit_name}",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ])
    return "\n".join(timer_lines)


def _launchd_plist(
    *,
    label: str,
    repo_root: Path,
    runtime_root: Path,
    program_args: list[str],
    log_root: Path,
    start_interval: int | None = None,
    start_calendar_interval: dict[str, int] | None = None,
    keep_alive: bool = False,
) -> str:
    payload: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": program_args,
        "WorkingDirectory": str(repo_root),
        "EnvironmentVariables": {
            "OM_RUNTIME_ROOT": str(runtime_root),
            "PYTHONUNBUFFERED": "1",
        },
        "StandardOutPath": str(log_root / f"{label}.out.log"),
        "StandardErrorPath": str(log_root / f"{label}.err.log"),
        "RunAtLoad": bool(keep_alive),
    }
    if keep_alive:
        payload["KeepAlive"] = True
    if start_interval is not None:
        payload["StartInterval"] = int(start_interval)
    if start_calendar_interval is not None:
        payload["StartCalendarInterval"] = start_calendar_interval
    return plistlib.dumps(payload, sort_keys=True).decode("utf-8")


def build_service_profile(
    *,
    target: ServiceTarget,
    repo_root: Path,
    runtime_root: Path,
    accounts: list[str],
    markets: list[str],
    service_names: list[str],
    config_paths: dict[str, Path],
    env_file: Path | None = None,
) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "schema_version": 1,
        "service_provider": target,
        "repo_root": str(repo_root),
        "runtime_root": str(runtime_root),
        "accounts": accounts,
        "markets": markets,
        "paths": {
            "report_dir": str(runtime_root / "output" / "reports"),
            "state_dir": str(runtime_root / "output" / "state"),
            "shared_state_dir": str(runtime_root / "output_shared" / "state"),
            "accounts_root": str(runtime_root / "output_accounts"),
            "runs_root": str(runtime_root / "output_runs"),
        },
        "config_paths": {key: str(value) for key, value in config_paths.items()},
        "services": [{"name": name} for name in service_names],
    }
    if env_file is not None:
        profile["env_file"] = str(env_file)
    return profile


def render_service_bundle(
    *,
    target: str,
    repo_root: str | Path | None = None,
    runtime_root: str | Path | None = None,
    accounts: list[str] | tuple[str, ...] | None = None,
    markets: list[str] | tuple[str, ...] | None = None,
    config_paths: dict[str, str | Path] | None = None,
    env_file: str | Path | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    include_content: bool = True,
) -> dict[str, Any]:
    target_key = normalize_target(target)
    repo = Path(repo_root or Path.cwd()).expanduser().resolve()
    runtime = _resolve_path(runtime_root, base=repo, default=default_runtime_root(target_key))
    env_file_path = _resolve_path(env_file, base=repo, default=Path()) if env_file else None
    if env_file_path is not None and target_key != "systemd":
        raise ValueError("--env-file is only supported for systemd service rendering")
    account_values = normalize_accounts(accounts)
    market_values = normalize_markets(markets)
    config_by_market = {
        market: _config_path_for_market(market, repo_root=repo, config_paths=config_paths)
        for market in market_values
    }
    om = str(repo / "om")
    om_agent = str(repo / "om-agent")
    lock_root = runtime / "locks"
    log_root = runtime / "logs"

    files: list[RenderedServiceFile] = []
    service_names: list[str] = []

    def add(relative_path: str, content: str, *, install_path: str, kind: str, service_name: str | None = None) -> None:
        files.append(RenderedServiceFile(relative_path=relative_path, content=content, install_path=install_path, kind=kind))
        if service_name:
            service_names.append(service_name)

    if target_key == "systemd":
        for market in market_values:
            service_name = f"options-monitor-tick-{market}.service"
            timer_name = f"options-monitor-tick-{market}.timer"
            tick_args = [
                om,
                "run",
                "tick-cron",
                "--market",
                market,
                "--config",
                str(config_by_market[market]),
                "--accounts",
                *account_values,
                "--timeout",
                str(int(timeout_seconds)),
                "--lock-path",
                str(lock_root / f"tick-{market}.lock"),
                "--trigger-job-id",
                service_name.removesuffix(".service"),
                "--trigger-job-name",
                f"options-monitor {market} tick",
                "--trigger-schedule",
                "systemd timer",
            ]
            add(
                f"systemd/{service_name}",
                _systemd_unit(
                    description=f"Options Monitor {market.upper()} tick",
                    repo_root=repo,
                    runtime_root=runtime,
                    env_file=env_file_path,
                    exec_args=tick_args,
                ),
                install_path=f"/etc/systemd/system/{service_name}",
                kind="systemd_service",
                service_name=service_name,
            )
            add(
                f"systemd/{timer_name}",
                _systemd_timer(description=f"Options Monitor {market.upper()} tick timer", unit_name=service_name),
                install_path=f"/etc/systemd/system/{timer_name}",
                kind="systemd_timer",
                service_name=timer_name,
            )

            auto_close_service = f"options-monitor-auto-close-{market}.service"
            auto_close_timer = f"options-monitor-auto-close-{market}.timer"
            auto_close_args = [
                om,
                "option-positions",
                "auto-close-expired",
                "--config",
                str(config_by_market[market]),
                "--accounts",
                *account_values,
                "--apply",
                "--quiet",
            ]
            add(
                f"systemd/{auto_close_service}",
                _systemd_unit(
                    description=f"Options Monitor {market.upper()} expired option maintenance",
                    repo_root=repo,
                    runtime_root=runtime,
                    env_file=env_file_path,
                    exec_args=auto_close_args,
                ),
                install_path=f"/etc/systemd/system/{auto_close_service}",
                kind="systemd_service",
                service_name=auto_close_service,
            )
            add(
                f"systemd/{auto_close_timer}",
                _systemd_timer(
                    description=f"Options Monitor {market.upper()} expired option maintenance timer",
                    unit_name=auto_close_service,
                    calendar="*-*-* 00:10:00",
                ),
                install_path=f"/etc/systemd/system/{auto_close_timer}",
                kind="systemd_timer",
                service_name=auto_close_timer,
            )

        trade_market = "us" if "us" in config_by_market else market_values[0]
        trade_service = "options-monitor-trade-intake.service"
        trade_args = [
            om,
            "run",
            "trade-intake",
            "--config",
            str(config_by_market[trade_market]),
            "--mode",
            "apply",
        ]
        add(
            f"systemd/{trade_service}",
            _systemd_unit(
                description="Options Monitor trade intake listener",
                repo_root=repo,
                runtime_root=runtime,
                env_file=env_file_path,
                exec_args=trade_args,
                service_type="simple",
                restart="always",
            ),
            install_path=f"/etc/systemd/system/{trade_service}",
            kind="systemd_service",
            service_name=trade_service,
        )

        status_service = "options-monitor-runtime-status.service"
        status_timer = "options-monitor-runtime-status.timer"
        status_args = [
            om_agent,
            "run",
            "--tool",
            "runtime_status",
            "--input-json",
            _json_arg({"profile_path": str(runtime / "service.profile.json")}),
        ]
        add(
            f"systemd/{status_service}",
            _systemd_unit(
                description="Options Monitor runtime status snapshot",
                repo_root=repo,
                runtime_root=runtime,
                env_file=env_file_path,
                exec_args=status_args,
            ),
            install_path=f"/etc/systemd/system/{status_service}",
            kind="systemd_service",
            service_name=status_service,
        )
        add(
            f"systemd/{status_timer}",
            _systemd_timer(description="Options Monitor runtime status timer", unit_name=status_service, interval="15min"),
            install_path=f"/etc/systemd/system/{status_timer}",
            kind="systemd_timer",
            service_name=status_timer,
        )
    else:
        for market in market_values:
            label = f"com.options-monitor.tick-{market}"
            tick_args = [
                om,
                "run",
                "tick-cron",
                "--market",
                market,
                "--config",
                str(config_by_market[market]),
                "--accounts",
                *account_values,
                "--timeout",
                str(int(timeout_seconds)),
                "--lock-path",
                str(lock_root / f"tick-{market}.lock"),
                "--trigger-job-id",
                label,
                "--trigger-job-name",
                f"options-monitor {market} tick",
                "--trigger-schedule",
                "launchd StartInterval",
            ]
            add(
                f"launchd/{label}.plist",
                _launchd_plist(
                    label=label,
                    repo_root=repo,
                    runtime_root=runtime,
                    program_args=tick_args,
                    log_root=log_root,
                    start_interval=600,
                ),
                install_path=f"~/Library/LaunchAgents/{label}.plist",
                kind="launchd_plist",
                service_name=label,
            )

            auto_label = f"com.options-monitor.auto-close-{market}"
            auto_close_args = [
                om,
                "option-positions",
                "auto-close-expired",
                "--config",
                str(config_by_market[market]),
                "--accounts",
                *account_values,
                "--apply",
                "--quiet",
            ]
            add(
                f"launchd/{auto_label}.plist",
                _launchd_plist(
                    label=auto_label,
                    repo_root=repo,
                    runtime_root=runtime,
                    program_args=auto_close_args,
                    log_root=log_root,
                    start_calendar_interval={"Hour": 0, "Minute": 10},
                ),
                install_path=f"~/Library/LaunchAgents/{auto_label}.plist",
                kind="launchd_plist",
                service_name=auto_label,
            )

        trade_market = "us" if "us" in config_by_market else market_values[0]
        trade_label = "com.options-monitor.trade-intake"
        trade_args = [
            om,
            "run",
            "trade-intake",
            "--config",
            str(config_by_market[trade_market]),
            "--mode",
            "apply",
        ]
        add(
            f"launchd/{trade_label}.plist",
            _launchd_plist(
                label=trade_label,
                repo_root=repo,
                runtime_root=runtime,
                program_args=trade_args,
                log_root=log_root,
                keep_alive=True,
            ),
            install_path=f"~/Library/LaunchAgents/{trade_label}.plist",
            kind="launchd_plist",
            service_name=trade_label,
        )

        status_label = "com.options-monitor.runtime-status"
        status_args = [
            om_agent,
            "run",
            "--tool",
            "runtime_status",
            "--input-json",
            _json_arg({"profile_path": str(runtime / "service.profile.json")}),
        ]
        add(
            f"launchd/{status_label}.plist",
            _launchd_plist(
                label=status_label,
                repo_root=repo,
                runtime_root=runtime,
                program_args=status_args,
                log_root=log_root,
                start_interval=900,
            ),
            install_path=f"~/Library/LaunchAgents/{status_label}.plist",
            kind="launchd_plist",
            service_name=status_label,
        )

    profile = build_service_profile(
        target=target_key,
        repo_root=repo,
        runtime_root=runtime,
        accounts=account_values,
        markets=market_values,
        service_names=service_names,
        config_paths=config_by_market,
        env_file=env_file_path,
    )
    profile_content = json.dumps(profile, ensure_ascii=False, indent=2) + "\n"
    add(
        "service.profile.json",
        profile_content,
        install_path=str(runtime / "service.profile.json"),
        kind="service_profile",
    )

    commands = _install_commands(target_key, files=files, runtime_root=runtime)
    return {
        "target": target_key,
        "repo_root": str(repo),
        "runtime_root": str(runtime),
        **({"env_file": str(env_file_path)} if env_file_path is not None else {}),
        "accounts": account_values,
        "markets": market_values,
        "files": [item.to_dict(include_content=include_content) for item in files],
        "commands": commands,
        "summary": {
            "file_count": len(files),
            "service_count": len(service_names),
            "service_provider": target_key,
        },
    }


def _install_commands(target: ServiceTarget, *, files: list[RenderedServiceFile], runtime_root: Path) -> dict[str, list[str]]:
    mkdirs = [
        "mkdir -p "
        + " ".join(shlex.quote(str(path)) for path in (runtime_root, runtime_root / "logs", runtime_root / "locks"))
    ]
    if target == "systemd":
        timer_names = [
            Path(item.install_path).name
            for item in files
            if item.kind == "systemd_timer"
        ]
        service_names = [
            Path(item.install_path).name
            for item in files
            if item.kind == "systemd_service" and "trade-intake" in item.install_path
        ]
        return {
            "prepare": mkdirs,
            "reload": ["systemctl daemon-reload"],
            "enable": [*(f"systemctl enable --now {name}" for name in timer_names), *(f"systemctl enable --now {name}" for name in service_names)],
            "status": [*(f"systemctl status {name}" for name in service_names), "systemctl list-timers 'options-monitor*'"],
        }
    labels = [
        Path(item.install_path).name.removesuffix(".plist")
        for item in files
        if item.kind == "launchd_plist"
    ]
    return {
        "prepare": mkdirs,
        "enable": [*(f"launchctl bootstrap gui/$UID ~/Library/LaunchAgents/{label}.plist" for label in labels)],
        "status": [*(f"launchctl print gui/$UID/{label}" for label in labels)],
    }


def write_service_bundle(bundle: dict[str, Any], output_dir: str | Path) -> list[str]:
    root = Path(output_dir).expanduser().resolve()
    written: list[str] = []
    for item in bundle.get("files", []):
        if not isinstance(item, dict):
            continue
        rel = str(item.get("relative_path") or "").strip()
        content = str(item.get("content") or "")
        if not rel:
            continue
        path = (root / rel).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(str(path))
    return written


def load_service_profile(path: str | Path) -> dict[str, Any]:
    profile_path = Path(path).expanduser()
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("service profile must be a JSON object")
    return payload


def service_status_from_profile(
    profile: dict[str, Any],
    *,
    include_status: bool = False,
    run_cmd: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    provider = str(profile.get("service_provider") or profile.get("provider") or "manual").strip().lower()
    services_raw = profile.get("services")
    services = services_raw if isinstance(services_raw, list) else []
    normalized_services: list[dict[str, Any]] = []
    for item in services:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("label") or "").strip()
        else:
            name = str(item or "").strip()
        if name:
            normalized_services.append({"name": name})
    out = {
        "provider": provider,
        "runtime_root": profile.get("runtime_root"),
        "repo_root": profile.get("repo_root"),
        "service_count": len(normalized_services),
        "services": normalized_services,
        "status_checked": bool(include_status),
    }
    if not include_status:
        return out
    checked: list[dict[str, Any]] = []
    for service in normalized_services:
        name = str(service.get("name") or "")
        checked.append({**service, **_check_one_service(provider=provider, name=name, run_cmd=run_cmd)})
    out["services"] = checked
    return out


def _check_one_service(*, provider: str, name: str, run_cmd: Callable[..., Any]) -> dict[str, Any]:
    if provider == "systemd":
        return _run_status_command(["systemctl", "is-active", name], run_cmd=run_cmd)
    if provider == "launchd":
        return _run_status_command(["launchctl", "print", f"gui/{os.getuid()}/{name}"], run_cmd=run_cmd)
    return {"status": "skipped", "message": f"service provider does not support command checks: {provider}"}


def _run_status_command(command: list[str], *, run_cmd: Callable[..., Any]) -> dict[str, Any]:
    try:
        proc = run_cmd(command, capture_output=True, text=True, timeout=10, check=False)
    except Exception as exc:
        return {
            "status": "unknown",
            "command": command,
            "error": f"{type(exc).__name__}: {exc}",
        }
    stdout = str(getattr(proc, "stdout", "") or "").strip()
    stderr = str(getattr(proc, "stderr", "") or "").strip()
    rc = int(getattr(proc, "returncode", 1))
    return {
        "status": "ok" if rc == 0 else "warn",
        "command": command,
        "returncode": rc,
        "stdout": stdout[:1000],
        "stderr": stderr[:1000],
    }


__all__ = [
    "build_service_profile",
    "default_runtime_root",
    "load_service_profile",
    "normalize_accounts",
    "normalize_markets",
    "normalize_target",
    "render_service_bundle",
    "service_status_from_profile",
    "write_service_bundle",
]
