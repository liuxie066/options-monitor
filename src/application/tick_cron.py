from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.application.research.formal_corpus import seal_profile_formal_expectations
from src.application.runtime_config_freshness import (
    RuntimeConfigFreshnessError,
    RuntimeConfigIdentityError,
    ensure_runtime_config_freshness,
    ensure_runtime_config_identity,
)
from src.application.runtime_paths import resolve_runtime_root


@dataclass(frozen=True)
class TickCronPlan:
    market: str
    config_path: str
    accounts: list[str]
    symbols: list[str]
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


def _normalize_symbols(symbols: list[str] | tuple[str, ...] | None) -> list[str]:
    out: list[str] = []
    for item in symbols or []:
        symbol = str(item or "").strip()
        if symbol:
            out.append(symbol)
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
    symbols: list[str] | tuple[str, ...] | None = None,
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
    symbol_values = _normalize_symbols(symbols)
    timeout_value = _normalize_timeout(timeout_seconds)
    resolved_config = str(config_path or defaults["config_path"])
    resolved_lock = str(lock_path or defaults["lock_path"])
    no_send = bool(no_send or symbol_values)

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
    if symbol_values:
        tick_argv.extend(["--symbols", ",".join(symbol_values)])
    if no_send:
        tick_argv.append("--no-send")
    if force:
        tick_argv.append("--force")
    if debug:
        tick_argv.append("--debug")
    if allow_stale_config:
        tick_argv.append("--allow-stale-config")

    trigger_env = {
        "OM_TRIGGER_SOURCE": "diagnostic" if symbol_values else "cron",
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
        symbols=symbol_values,
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
        ensure_runtime_config_identity(
            raw,
            explicit_market=plan.market,
            runtime_config_path=config_path,
        )
        return ensure_runtime_config_freshness(
            raw,
            repo_root=repo_root,
            market=plan.market,
            runtime_config_path=config_path,
        )
    except RuntimeConfigIdentityError as exc:
        raise SystemExit(str(exc)) from exc
    except RuntimeConfigFreshnessError as exc:
        raise SystemExit(str(exc)) from exc


def run_tick_cron(
    *,
    market: str,
    accounts: list[str] | tuple[str, ...] | None = None,
    symbols: list[str] | tuple[str, ...] | None = None,
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
    run_cmd: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
    preflight_config_fn: Callable[..., Any] | None = _preflight_runtime_config,
    seal_formal_expectations_fn: Callable[..., dict[str, Any]] | None = seal_profile_formal_expectations,
    stdout: Any = None,
    stderr: Any = None,
    environ: dict[str, str] | None = None,
) -> int | dict[str, Any]:
    plan = build_tick_cron_plan(
        market=market,
        accounts=accounts,
        symbols=symbols,
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
            "symbols": plan.symbols,
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
        if (
            seal_formal_expectations_fn is not None
            and str(env.get("OM_RUNTIME_ROOT") or "").strip()
            and not plan.symbols
            and (not plan.accounts or "lx" in {item.lower() for item in plan.accounts})
        ):
            try:
                repo_root = Path(cwd).expanduser() if cwd is not None else Path.cwd()
                runtime_root = resolve_runtime_root(
                    repo_root=repo_root,
                    environ=env,
                ).runtime_root
                expectation = seal_formal_expectations_fn(
                    runtime_root,
                    profile={
                        "markets": [plan.market],
                        "accounts": ["lx"],
                        "config_paths": {plan.market: str(_resolve_config_for_preflight(plan, cwd=cwd))},
                    },
                    artifact_root=(runtime_root / "output_shared" / "research" / "strategy_lab"),
                    occurred_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                )
                if expectation.get("status") != "ok":
                    _write_line(stderr, "FORMAL_EXPECTATION_DEGRADED")
            except Exception as exc:
                reason = str(getattr(exc, "reason_code", "formal_expectation_failed"))
                _write_line(stderr, f"FORMAL_EXPECTATION_DEGRADED_{reason}")
        try:
            if run_cmd is None:
                proc = _run_tick_process_group(
                    command=list(plan.tick_argv),
                    cwd=cwd,
                    env=env,
                    timeout_seconds=plan.timeout_seconds,
                )
            else:
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


def _run_tick_process_group(
    *,
    command: list[str],
    cwd: str | Path | None,
    env: dict[str, str],
    timeout_seconds: int,
    terminate_grace_seconds: float = 5.0,
) -> subprocess.CompletedProcess[Any]:
    proc = subprocess.Popen(
        command,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        start_new_session=True,
    )
    try:
        returncode = proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=max(0.1, float(terminate_grace_seconds)))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        raise
    return subprocess.CompletedProcess(command, int(returncode))


__all__ = ["TickCronPlan", "build_tick_cron_plan", "run_tick_cron"]
