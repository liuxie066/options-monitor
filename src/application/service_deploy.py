from __future__ import annotations

import json
import os
import plistlib
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Callable, Literal, cast

from src.application.platform_profile import default_runtime_root_for_service_target
from src.application.settings import build_effective_env


ServiceTarget = Literal["systemd", "launchd"]
ServiceProvider = Literal["systemd", "launchd", "manual", "openclaw"]

DEFAULT_MARKETS: tuple[str, ...] = ("us", "hk")
DEFAULT_ACCOUNTS: tuple[str, ...] = ("lx", "sy")
DEFAULT_TIMEOUT_SECONDS = 600
US_TICK_SYSTEMD_CALENDAR = "Mon..Fri *-*-* 09..16:00/10:00 America/New_York"
HK_TICK_SYSTEMD_CALENDAR = "Mon..Fri *-*-* 09..16:00/10:00 Asia/Hong_Kong"
AUTO_CLOSE_SYSTEMD_CALENDAR = "*-*-* 05:30:00 Asia/Shanghai"
AUTO_CLOSE_LAUNCHD_CALENDAR = {"Hour": 5, "Minute": 30}
PROJECTION_VERIFY_SYSTEMD_CALENDAR = "*-*-* 06:00:00 Asia/Shanghai"
PROJECTION_VERIFY_LAUNCHD_CALENDAR = {"Hour": 6, "Minute": 0}
AUTO_UPGRADE_SYSTEMD_CALENDAR = "*-*-* 06:10:00 Asia/Shanghai"
AUTO_UPGRADE_LAUNCHD_CALENDAR = {"Hour": 6, "Minute": 10}


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
    return default_runtime_root_for_service_target(target, home=home)


def default_systemd_deploy_user() -> str:
    env = build_effective_env().values
    return str(env.get("OM_DEPLOY_USER") or env.get("DEPLOY_USER") or "").strip()


def default_systemd_deploy_home(deploy_user: str) -> Path:
    user = str(deploy_user or "").strip()
    if user == "root":
        return Path("/root")
    return Path("/home") / user


def _resolve_path(value: str | Path | None, *, base: Path, default: Path) -> Path:
    raw = str(value or "").strip()
    if not raw:
        return default.resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def _absolute_path_preserve_symlink(value: str | Path, *, base: Path | None = None) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (base or Path.cwd()) / path


def _config_path_for_market(
    market: str,
    *,
    repo_root: Path,
    runtime_root: Path | None = None,
    config_paths: dict[str, str | Path] | None,
) -> Path:
    configured = (config_paths or {}).get(market)
    default_root = runtime_root or repo_root
    default = default_root / f"config.{market}.json"
    if configured is None or str(configured).strip() == "":
        return default
    return _absolute_path_preserve_symlink(configured, base=repo_root)


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
    deploy_user: str | None = None,
    deploy_home: Path | None = None,
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
    ]
    if deploy_user:
        lines.append(f"User={deploy_user}")
    if deploy_home is not None:
        lines.append(_systemd_environment_assignment("HOME", deploy_home))
    lines.extend(
        [
            _systemd_environment_assignment("PYTHONUNBUFFERED", "1"),
            _systemd_environment_assignment("OM_RUNTIME_ROOT", runtime_root),
        ]
    )
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


def _systemd_tick_calendar(market: str) -> str | None:
    if market == "us":
        return US_TICK_SYSTEMD_CALENDAR
    if market == "hk":
        return HK_TICK_SYSTEMD_CALENDAR
    return None


def _launchd_plist(
    *,
    label: str,
    repo_root: Path,
    runtime_root: Path,
    program_args: list[str],
    log_root: Path,
    env_file: Path | None = None,
    start_interval: int | None = None,
    start_calendar_interval: dict[str, int] | None = None,
    keep_alive: bool = False,
) -> str:
    environment = {
        "OM_RUNTIME_ROOT": str(runtime_root),
        "PYTHONUNBUFFERED": "1",
    }
    if env_file is not None:
        environment["OM_ENV_FILE"] = str(env_file)
    payload: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": program_args,
        "WorkingDirectory": str(repo_root),
        "EnvironmentVariables": environment,
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
    deploy_user: str | None = None,
    deploy_home: Path | None = None,
    auto_upgrade_enabled: bool = False,
    feishu_ws: dict[str, Any] | None = None,
) -> dict[str, Any]:
    restartable_services = [
        name
        for name in service_names
        if (str(name).endswith(".service") and ("trade-intake" in str(name) or "feishu-ws" in str(name)))
    ]
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
    if deploy_user:
        profile["deploy_user"] = str(deploy_user)
    if deploy_home is not None:
        profile["deploy_home"] = str(deploy_home)
    if target == "systemd" and deploy_user and str(deploy_user).strip() != "root":
        profile["restart"] = {
            "requires_sudo": True,
            "command_prefix": ["sudo", "-n", "systemctl"],
            "services": restartable_services,
            "sudoers": [
                item
                for service_name in restartable_services
                for item in (
                    f"{deploy_user} ALL=(root) NOPASSWD: /bin/systemctl restart {service_name}",
                    f"{deploy_user} ALL=(root) NOPASSWD: /usr/bin/systemctl restart {service_name}",
                )
            ],
        }
    elif target == "systemd" and restartable_services:
        profile["restart"] = {
            "requires_sudo": False,
            "command_prefix": ["systemctl"],
            "services": restartable_services,
        }
    if auto_upgrade_enabled:
        profile["auto_upgrade"] = {
            "enabled": True,
            "schedule_beijing": "06:10",
        }
    if feishu_ws is not None:
        profile["feishu_ws"] = dict(feishu_ws)
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
    deploy_user: str | None = None,
    deploy_home: str | Path | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    include_auto_upgrade: bool = False,
    include_feishu_ws: bool = False,
    feishu_ws_config_key: str = "us",
    include_content: bool = True,
) -> dict[str, Any]:
    target_key = normalize_target(target)
    repo = _absolute_path_preserve_symlink(repo_root or Path.cwd())
    runtime = _resolve_path(runtime_root, base=repo, default=default_runtime_root(target_key))
    env_file_path = _resolve_path(env_file, base=repo, default=Path()) if env_file else None
    systemd_user = default_systemd_deploy_user() if target_key == "systemd" else None
    if deploy_user is not None and str(deploy_user).strip():
        systemd_user = str(deploy_user).strip()
    systemd_home = default_systemd_deploy_home(systemd_user) if target_key == "systemd" and systemd_user else None
    if deploy_home is not None and str(deploy_home).strip():
        systemd_home = Path(deploy_home).expanduser()
    account_values = normalize_accounts(accounts)
    market_values = normalize_markets(markets)
    config_default_root = runtime if include_auto_upgrade else None
    config_by_market = {
        market: _config_path_for_market(
            market,
            repo_root=repo,
            runtime_root=config_default_root,
            config_paths=config_paths,
        )
        for market in market_values
    }
    om = str(repo / "om")
    om_agent = str(repo / "om-agent")
    lock_root = runtime / "locks"
    log_root = runtime / "logs"
    runtime_data_config = runtime / "portfolio.runtime.json"
    inbound_audit_db = runtime / "output_shared" / "state" / "inbound_control.sqlite3"
    feishu_ws_config_key_value = str(feishu_ws_config_key or "us").strip().lower() or "us"
    if feishu_ws_config_key_value not in {"us", "hk"}:
        raise ValueError("feishu_ws_config_key must be us or hk")

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
                    deploy_user=systemd_user,
                    deploy_home=systemd_home,
                    exec_args=tick_args,
                ),
                install_path=f"/etc/systemd/system/{service_name}",
                kind="systemd_service",
                service_name=service_name,
            )
            add(
                f"systemd/{timer_name}",
                _systemd_timer(
                    description=f"Options Monitor {market.upper()} tick timer",
                    unit_name=service_name,
                    calendar=_systemd_tick_calendar(market),
                ),
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
                    deploy_user=systemd_user,
                    deploy_home=systemd_home,
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
                    calendar=AUTO_CLOSE_SYSTEMD_CALENDAR,
                ),
                install_path=f"/etc/systemd/system/{auto_close_timer}",
                kind="systemd_timer",
                service_name=auto_close_timer,
            )

        verify_service = "options-monitor-projection-verify.service"
        verify_timer = "options-monitor-projection-verify.timer"
        verify_args = [
            om,
            "option-positions",
            "--data-config",
            str(runtime_data_config),
            "verify-projection",
            "--mode",
            "auto",
        ]
        add(
            f"systemd/{verify_service}",
            _systemd_unit(
                description="Options Monitor option-position projection verification",
                repo_root=repo,
                runtime_root=runtime,
                env_file=env_file_path,
                deploy_user=systemd_user,
                deploy_home=systemd_home,
                exec_args=verify_args,
            ),
            install_path=f"/etc/systemd/system/{verify_service}",
            kind="systemd_service",
            service_name=verify_service,
        )
        add(
            f"systemd/{verify_timer}",
            _systemd_timer(
                description="Options Monitor option-position projection verification timer",
                unit_name=verify_service,
                calendar=PROJECTION_VERIFY_SYSTEMD_CALENDAR,
            ),
            install_path=f"/etc/systemd/system/{verify_timer}",
            kind="systemd_timer",
            service_name=verify_timer,
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
                deploy_user=systemd_user,
                deploy_home=systemd_home,
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
                deploy_user=systemd_user,
                deploy_home=systemd_home,
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
        if include_auto_upgrade:
            upgrade_service = "options-monitor-upgrade.service"
            upgrade_timer = "options-monitor-upgrade.timer"
            upgrade_args = [
                om,
                "update",
                "apply",
                "--repo-root",
                str(repo),
                "--runtime-root",
                str(runtime),
                "--auto",
                "--confirm",
            ]
            add(
                f"systemd/{upgrade_service}",
                _systemd_unit(
                    description="Options Monitor release upgrade",
                    repo_root=repo,
                    runtime_root=runtime,
                    env_file=env_file_path,
                    deploy_user=systemd_user,
                    deploy_home=systemd_home,
                    exec_args=upgrade_args,
                ),
                install_path=f"/etc/systemd/system/{upgrade_service}",
                kind="systemd_service",
                service_name=upgrade_service,
            )
            add(
                f"systemd/{upgrade_timer}",
                _systemd_timer(
                    description="Options Monitor release upgrade timer",
                    unit_name=upgrade_service,
                    calendar=AUTO_UPGRADE_SYSTEMD_CALENDAR,
                ),
                install_path=f"/etc/systemd/system/{upgrade_timer}",
                kind="systemd_timer",
                service_name=upgrade_timer,
            )

        if include_feishu_ws:
            ws_service = "options-monitor-feishu-ws.service"
            ws_args = [
                om,
                "inbound",
                "feishu-ws",
                "--config-key",
                feishu_ws_config_key_value,
                "--audit-db",
                str(inbound_audit_db),
                "--lock-path",
                str(lock_root / "feishu-ws.lock"),
            ]
            add(
                f"systemd/{ws_service}",
                _systemd_unit(
                    description="Options Monitor Feishu long-connection inbound",
                    repo_root=repo,
                    runtime_root=runtime,
                    env_file=env_file_path,
                    deploy_user=systemd_user,
                    deploy_home=systemd_home,
                    exec_args=ws_args,
                    service_type="simple",
                    restart="always",
                ),
                install_path=f"/etc/systemd/system/{ws_service}",
                kind="systemd_service",
                service_name=ws_service,
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
                    env_file=env_file_path,
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
                    env_file=env_file_path,
                    start_calendar_interval=AUTO_CLOSE_LAUNCHD_CALENDAR,
                ),
                install_path=f"~/Library/LaunchAgents/{auto_label}.plist",
                kind="launchd_plist",
                service_name=auto_label,
            )

        verify_label = "com.options-monitor.projection-verify"
        verify_args = [
            om,
            "option-positions",
            "--data-config",
            str(runtime_data_config),
            "verify-projection",
            "--mode",
            "auto",
        ]
        add(
            f"launchd/{verify_label}.plist",
            _launchd_plist(
                label=verify_label,
                repo_root=repo,
                runtime_root=runtime,
                program_args=verify_args,
                log_root=log_root,
                env_file=env_file_path,
                start_calendar_interval=PROJECTION_VERIFY_LAUNCHD_CALENDAR,
            ),
            install_path=f"~/Library/LaunchAgents/{verify_label}.plist",
            kind="launchd_plist",
            service_name=verify_label,
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
                env_file=env_file_path,
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
                env_file=env_file_path,
                start_interval=900,
            ),
            install_path=f"~/Library/LaunchAgents/{status_label}.plist",
            kind="launchd_plist",
            service_name=status_label,
        )
        if include_auto_upgrade:
            upgrade_label = "com.options-monitor.upgrade"
            upgrade_args = [
                om,
                "update",
                "apply",
                "--repo-root",
                str(repo),
                "--runtime-root",
                str(runtime),
                "--auto",
                "--confirm",
            ]
            add(
                f"launchd/{upgrade_label}.plist",
                _launchd_plist(
                    label=upgrade_label,
                    repo_root=repo,
                    runtime_root=runtime,
                    program_args=upgrade_args,
                    log_root=log_root,
                    env_file=env_file_path,
                    start_calendar_interval=AUTO_UPGRADE_LAUNCHD_CALENDAR,
                ),
                install_path=f"~/Library/LaunchAgents/{upgrade_label}.plist",
                kind="launchd_plist",
                service_name=upgrade_label,
            )

        if include_feishu_ws:
            ws_label = "com.options-monitor.feishu-ws"
            ws_args = [
                om,
                "inbound",
                "feishu-ws",
                "--config-key",
                feishu_ws_config_key_value,
                "--audit-db",
                str(inbound_audit_db),
                "--lock-path",
                str(lock_root / "feishu-ws.lock"),
            ]
            add(
                f"launchd/{ws_label}.plist",
                _launchd_plist(
                    label=ws_label,
                    repo_root=repo,
                    runtime_root=runtime,
                    program_args=ws_args,
                    log_root=log_root,
                    env_file=env_file_path,
                    keep_alive=True,
                ),
                install_path=f"~/Library/LaunchAgents/{ws_label}.plist",
                kind="launchd_plist",
                service_name=ws_label,
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
        deploy_user=systemd_user,
        deploy_home=systemd_home,
        auto_upgrade_enabled=bool(include_auto_upgrade),
        feishu_ws={
            "enabled": True,
            "config_key": feishu_ws_config_key_value,
            "audit_db": str(inbound_audit_db),
            "lock_path": str(lock_root / "feishu-ws.lock"),
        } if include_feishu_ws else None,
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
        **({"deploy_user": str(systemd_user)} if systemd_user else {}),
        **({"deploy_home": str(systemd_home)} if systemd_home is not None else {}),
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
            if item.kind == "systemd_service"
            and ("trade-intake" in item.install_path or "feishu-ws" in item.install_path)
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


def _status_from_checks(checks: list[dict[str, Any]]) -> dict[str, Any]:
    error_count = sum(1 for item in checks if item.get("status") == "error")
    warn_count = sum(1 for item in checks if item.get("status") == "warn")
    return {"ok": error_count == 0, "error_count": error_count, "warning_count": warn_count}


def _check_env_file(path: Path) -> dict[str, Any]:
    if path.exists() and path.is_file():
        return {"name": "env_file", "status": "ok", "message": "environment file exists", "value": str(path)}
    if path.exists() and path.is_dir():
        return {
            "name": "env_file",
            "status": "error",
            "message": "environment path is a directory; expected a file",
            "value": str(path),
        }
    return {"name": "env_file", "status": "error", "message": "environment file is missing", "value": str(path)}


def _check_writable_dir(path: Path, *, name: str) -> dict[str, Any]:
    if not path.exists():
        return {
            "name": name,
            "status": "error",
            "message": "directory is missing",
            "value": {"path": str(path), "repair": f"mkdir -p {shlex.quote(str(path))}"},
        }
    if not path.is_dir():
        return {"name": name, "status": "error", "message": "path exists but is not a directory", "value": str(path)}
    perms = {
        "readable": os.access(path, os.R_OK),
        "writable": os.access(path, os.W_OK),
        "executable": os.access(path, os.X_OK),
    }
    ok = all(perms.values())
    return {
        "name": name,
        "status": "ok" if ok else "error",
        "message": "directory permissions ok" if ok else "directory is not readable/writable/executable by current user",
        "value": {"path": str(path), **perms},
    }


def _json_parse_error_details(exc: JSONDecodeError) -> dict[str, Any]:
    return {
        "error": str(exc),
        "line": int(exc.lineno),
        "column": int(exc.colno),
        "position": int(exc.pos),
    }


def _check_runtime_config(path: Path, *, market: str) -> dict[str, Any]:
    if not path.exists():
        return {"name": f"runtime_config_{market}", "status": "error", "message": "runtime config is missing", "value": str(path)}
    if not path.is_file():
        return {"name": f"runtime_config_{market}", "status": "error", "message": "runtime config path is not a file", "value": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except JSONDecodeError as exc:
        return {
            "name": f"runtime_config_{market}",
            "status": "error",
            "message": "runtime config JSON parse failed",
            "value": {"path": str(path), **_json_parse_error_details(exc)},
        }
    except Exception as exc:
        return {
            "name": f"runtime_config_{market}",
            "status": "error",
            "message": "runtime config read failed",
            "value": {"path": str(path), "error": f"{type(exc).__name__}: {exc}"},
        }
    if not isinstance(payload, dict):
        return {"name": f"runtime_config_{market}", "status": "error", "message": "runtime config must be a JSON object", "value": str(path)}
    if not isinstance(payload.get("_generated"), dict):
        return {
            "name": f"runtime_config_{market}",
            "status": "error",
            "message": "runtime config is missing generation metadata",
            "value": {"path": str(path), "repair": f"./om config build --market {market} --output {shlex.quote(str(path))}"},
        }
    return {"name": f"runtime_config_{market}", "status": "ok", "message": "runtime config metadata exists", "value": str(path)}


def service_preflight(
    *,
    runtime_root: str | Path,
    env_file: str | Path | None = None,
    accounts: list[str] | tuple[str, ...] | None = None,
    config_paths: dict[str, str | Path] | None = None,
    default_account: str | None = None,
) -> dict[str, Any]:
    runtime = Path(runtime_root).expanduser().resolve()
    account_values = normalize_accounts(accounts)
    default_account_value = str(default_account or (account_values[0] if account_values else DEFAULT_ACCOUNTS[0])).strip()
    checks: list[dict[str, Any]] = []
    commands: list[str] = []

    if env_file is not None and str(env_file).strip():
        checks.append(_check_env_file(Path(env_file).expanduser()))

    for name, path in (
        ("runtime_root", runtime),
        ("locks", runtime / "locks"),
        ("output_accounts", runtime / "output_accounts"),
        ("output_shared", runtime / "output_shared"),
    ):
        checks.append(_check_writable_dir(path, name=name))

    output = runtime / "output"
    repair_cmd = (
        "./om service repair-output "
        f"--runtime-root {shlex.quote(str(runtime))} "
        f"--default-account {shlex.quote(default_account_value)} --confirm"
    )
    if output.is_symlink():
        target = output.resolve()
        status = "ok" if target.exists() else "warn"
        checks.append(
            {
                "name": "output_symlink",
                "status": status,
                "message": "output is a symlink" if status == "ok" else "output symlink target is missing",
                "value": {"path": str(output), "target": str(target)},
            }
        )
    elif output.exists():
        checks.append(
            {
                "name": "output_symlink",
                "status": "error",
                "message": "multi-account runtime requires output to be a symlink, but it is a real path",
                "value": {"path": str(output), "repair": repair_cmd},
            }
        )
        commands.append(repair_cmd)
    else:
        checks.append(
            {
                "name": "output_symlink",
                "status": "warn",
                "message": "output symlink is missing",
                "value": {"path": str(output), "repair": repair_cmd},
            }
        )
        commands.append(repair_cmd)

    for market, raw_path in sorted((config_paths or {}).items()):
        if raw_path is not None and str(raw_path).strip():
            checks.append(_check_runtime_config(Path(raw_path).expanduser(), market=str(market)))

    summary = _status_from_checks(checks)
    return {
        "runtime_root": str(runtime),
        "accounts": account_values,
        "checks": checks,
        "repair_commands": commands,
        "summary": summary,
    }


def _copy_tree_contents(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir() and not item.is_symlink():
            shutil.copytree(item, target, symlinks=True)
        else:
            shutil.copy2(item, target, follow_symlinks=False)


def repair_output_symlink(
    *,
    runtime_root: str | Path,
    default_account: str,
    confirm: bool = False,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    runtime = Path(runtime_root).expanduser().resolve()
    account = str(default_account or "").strip()
    if not account:
        raise ValueError("default_account is required")
    output = runtime / "output"
    account_root = runtime / "output_accounts" / account
    timestamp = (now_fn or (lambda: datetime.now(timezone.utc)))().strftime("%Y%m%d%H%M%S")
    backup = runtime / f"output.backup.{timestamp}"
    operations: list[str] = []

    if output.is_symlink():
        return {
            "changed": False,
            "confirmed": bool(confirm),
            "runtime_root": str(runtime),
            "output": str(output),
            "target": str(output.resolve()),
            "operations": ["output already symlink"],
        }
    if output.exists() and not output.is_dir():
        raise ValueError(f"runtime output exists but is not a directory or symlink: {output}")

    operations.extend(
        [
            f"mkdir -p {account_root}",
            f"backup {output} -> {backup}" if output.exists() else "no existing output directory to back up",
            f"link {output} -> {account_root}",
        ]
    )
    if not confirm:
        return {
            "changed": False,
            "confirmed": False,
            "runtime_root": str(runtime),
            "output": str(output),
            "target": str(account_root),
            "backup": str(backup) if output.exists() else None,
            "operations": operations,
        }

    account_root.mkdir(parents=True, exist_ok=True)
    if output.exists():
        conflicts = [item.name for item in output.iterdir() if (account_root / item.name).exists()]
        if conflicts:
            raise ValueError(
                "cannot migrate output because output_accounts/"
                f"{account} already has conflicting entries: {', '.join(conflicts)}"
            )
        _copy_tree_contents(output, backup)
        for item in output.iterdir():
            shutil.move(str(item), str(account_root / item.name))
        output.rmdir()
    output.symlink_to(account_root, target_is_directory=True)
    return {
        "changed": True,
        "confirmed": True,
        "runtime_root": str(runtime),
        "output": str(output),
        "target": str(account_root),
        "backup": str(backup) if backup.exists() else None,
        "operations": operations,
    }


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
    "repair_output_symlink",
    "render_service_bundle",
    "service_preflight",
    "service_status_from_profile",
    "write_service_bundle",
]
