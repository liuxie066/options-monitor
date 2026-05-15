from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from src.application.runtime_config_freshness import RuntimeConfigFreshnessError, ensure_runtime_config_freshness


@dataclass(frozen=True)
class TickCronPlan:
    market: str
    config_path: str
    accounts: list[str]
    timeout_seconds: int
    lock_path: str
    trigger_env: dict[str, str]
    tick_argv: list[str]


_MARKET_DEFAULTS = {
    "hk": {
        "config_path": "config.hk.json",
        "lock_path": "/tmp/om-tick-hk.lock",
        "trigger_job_id": "om-tick-hk",
        "trigger_job_name": "options-monitor hk tick",
        "trigger_timezone": "Asia/Hong_Kong",
    },
    "us": {
        "config_path": "config.us.json",
        "lock_path": "/tmp/om-tick-us.lock",
        "trigger_job_id": "om-tick-us",
        "trigger_job_name": "options-monitor us tick",
        "trigger_timezone": "America/New_York",
    },
}


def _normalize_market(market: str) -> str:
    out = str(market or "").strip().lower()
    if out not in _MARKET_DEFAULTS:
        raise ValueError(f"unsupported tick-cron market: {market}")
    return out


def _normalize_accounts(accounts: list[str] | tuple[str, ...] | None) -> list[str]:
    out: list[str] = []
    for item in accounts or []:
        account = str(item or "").strip()
        if account:
            out.append(account)
    return out


def _normalize_timeout(timeout_seconds: int | str | None) -> int:
    try:
        out = int(timeout_seconds or 600)
    except (TypeError, ValueError):
        out = 600
    return max(1, out)


def build_tick_cron_plan(
    *,
    market: str,
    accounts: list[str] | tuple[str, ...] | None = None,
    timeout_seconds: int | str | None = 600,
    config_path: str | None = None,
    lock_path: str | None = None,
    trigger_job_id: str | None = None,
    trigger_job_name: str | None = None,
    trigger_schedule: str | None = None,
    no_send: bool = False,
    force: bool = False,
    debug: bool = False,
    allow_stale_config: bool = False,
) -> TickCronPlan:
    market_key = _normalize_market(market)
    defaults = _MARKET_DEFAULTS[market_key]
    account_values = _normalize_accounts(accounts)
    timeout_value = _normalize_timeout(timeout_seconds)
    resolved_config = str(config_path or defaults["config_path"])
    resolved_lock = str(lock_path or defaults["lock_path"])

    tick_argv = [
        "./om",
        "run",
        "tick",
        "--config",
        resolved_config,
        "--market-config",
        market_key,
    ]
    if account_values:
        tick_argv.extend(["--accounts", *account_values])
    if no_send:
        tick_argv.append("--no-send")
    if force:
        tick_argv.append("--force")
    if debug:
        tick_argv.append("--debug")
    if allow_stale_config:
        tick_argv.append("--allow-stale-config")

    trigger_env = {
        "OM_TRIGGER_SOURCE": "cron",
        "OM_TRIGGER_JOB_ID": str(trigger_job_id or defaults["trigger_job_id"]),
        "OM_TRIGGER_JOB_NAME": str(trigger_job_name or defaults["trigger_job_name"]),
        "OM_TRIGGER_TIMEZONE": str(defaults["trigger_timezone"]),
        "OM_TIMEOUT_SECONDS": str(timeout_value),
    }
    schedule = str(trigger_schedule or "").strip()
    if schedule:
        trigger_env["OM_TRIGGER_SCHEDULE"] = schedule

    return TickCronPlan(
        market=market_key,
        config_path=resolved_config,
        accounts=account_values,
        timeout_seconds=timeout_value,
        lock_path=resolved_lock,
        trigger_env=trigger_env,
        tick_argv=tick_argv,
    )


def _write_line(stream: Any, text: str) -> None:
    try:
        stream.write(text + "\n")
        stream.flush()
    except Exception:
        pass


def _resolve_config_for_preflight(plan: TickCronPlan, *, cwd: str | Path | None) -> Path:
    config_path = Path(plan.config_path).expanduser()
    if config_path.is_absolute():
        return config_path.resolve()
    base = Path(cwd).expanduser() if cwd is not None else Path.cwd()
    return (base / config_path).resolve()


def _preflight_runtime_config(
    *,
    plan: TickCronPlan,
    cwd: str | Path | None,
    allow_stale_config: bool,
) -> dict[str, Any] | None:
    if allow_stale_config:
        return None
    config_path = _resolve_config_for_preflight(plan, cwd=cwd)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"[CONFIG_ERROR] failed to read runtime config for preflight: {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit(f"[CONFIG_ERROR] runtime config must be a JSON object: {config_path}")
    repo_root = Path(__file__).resolve().parents[2]
    try:
        return ensure_runtime_config_freshness(
            raw,
            repo_root=repo_root,
            market=plan.market,
            runtime_config_path=config_path,
        )
    except RuntimeConfigFreshnessError as exc:
        raise SystemExit(str(exc)) from exc


def run_tick_cron(
    *,
    market: str,
    accounts: list[str] | tuple[str, ...] | None = None,
    timeout_seconds: int | str | None = 600,
    config_path: str | None = None,
    lock_path: str | None = None,
    trigger_job_id: str | None = None,
    trigger_job_name: str | None = None,
    trigger_schedule: str | None = None,
    no_send: bool = False,
    force: bool = False,
    debug: bool = False,
    allow_stale_config: bool = False,
    cwd: str | Path | None = None,
    dry_run_command: bool = False,
    run_cmd: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    preflight_config_fn: Callable[..., Any] | None = _preflight_runtime_config,
    stdout: Any = None,
    stderr: Any = None,
    environ: dict[str, str] | None = None,
) -> int | dict[str, Any]:
    plan = build_tick_cron_plan(
        market=market,
        accounts=accounts,
        timeout_seconds=timeout_seconds,
        config_path=config_path,
        lock_path=lock_path,
        trigger_job_id=trigger_job_id,
        trigger_job_name=trigger_job_name,
        trigger_schedule=trigger_schedule,
        no_send=no_send,
        force=force,
        debug=debug,
        allow_stale_config=allow_stale_config,
    )
    if dry_run_command:
        return {
            "market": plan.market,
            "config_path": plan.config_path,
            "accounts": plan.accounts,
            "timeout_seconds": plan.timeout_seconds,
            "lock_path": plan.lock_path,
            "trigger_env": dict(plan.trigger_env),
            "command": list(plan.tick_argv),
        }
    if stdout is None:
        stdout = sys.stdout
    if stderr is None:
        stderr = sys.stderr

    lock_file = Path(plan.lock_path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("a+", encoding="utf-8") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _write_line(stdout, "SKIP_LOCKED")
            return 0

        if preflight_config_fn is not None:
            try:
                preflight_config_fn(
                    plan=plan,
                    cwd=cwd,
                    allow_stale_config=allow_stale_config,
                )
            except SystemExit as exc:
                _write_line(stderr, str(exc))
                return 1

        env = dict(environ if environ is not None else os.environ)
        env.update(plan.trigger_env)
        try:
            proc = run_cmd(
                list(plan.tick_argv),
                cwd=str(cwd) if cwd is not None else None,
                env=env,
                timeout=plan.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            _write_line(stderr, "EXEC_TIMEOUT_RC_124")
            return 124
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass

    rc = int(getattr(proc, "returncode", 1))
    if rc != 0:
        _write_line(stderr, f"EXEC_FAILED_RC_{rc}")
    return rc


__all__ = ["TickCronPlan", "build_tick_cron_plan", "run_tick_cron"]
