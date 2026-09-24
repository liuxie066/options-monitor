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

from domain.storage.repositories import state_repo
from src.application.account_config import accounts_from_config_path
from src.application.runtime_config_freshness import (
    RuntimeConfigFreshnessError,
    RuntimeConfigIdentityError,
    ensure_runtime_config_freshness,
    ensure_runtime_config_identity,
)
from src.application.runtime_paths import resolve_runtime_root
from src.application.system_alerts import report_system_failure, report_system_recovery
from src.infrastructure.io_utils import read_json
from src.infrastructure.run_log import create_run_id


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


def _record_failure(
    *,
    base: Path,
    run_id: str,
    plan: TickCronPlan,
    code: str,
    stage: str,
    rc: int,
    message: str,
    stderr: Any,
    cwd: str | Path | None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    pending = read_json(base / "output_shared" / "state" / "opend_phone_verify_pending.json", {})
    login_state = "phone_verify_pending" if isinstance(pending, dict) and pending.get("pending") else "unknown"
    event = state_repo.normalize_audit_event({
        "event_type": "tick_cron",
        "action": "failed",
        "status": "error",
        "run_id": run_id,
        "error_code": code,
        "message": message[:500],
        "event_at_utc": now,
        "extra": {
            "market": plan.market,
            "accounts": plan.accounts,
            "account": plan.accounts[0] if len(plan.accounts) == 1 else None,
            "failure_code": code,
            "stage": stage,
            "trigger_source": plan.trigger_env["OM_TRIGGER_SOURCE"],
            "rc": rc,
            "first_error_at": now,
            "opend_login_state": login_state,
        },
    })
    incomplete: list[str] = []
    try:
        state_repo.append_run_audit_jsonl(base, run_id, "audit_events.jsonl", event)
    except Exception:
        incomplete.append("run")
    try:
        state_repo.append_shared_audit_jsonl(base, "audit_events.jsonl", event)
    except Exception:
        incomplete.append("shared")
    try:
        state_repo.write_shared_current_read_model(
            base,
            f"tick_cron_last_result.{plan.market}.current.json",
            {"status": "evidence_incomplete" if incomplete else "failed", "run_id": run_id,
             "market": plan.market, "accounts": plan.accounts, "error_code": code,
             "stage": stage, "rc": rc, "event_at_utc": now, "incomplete": incomplete},
        )
    except Exception:
        incomplete.append("latest")
    if incomplete:
        _write_line(stderr, "<3>FAILURE_RECORD_WRITE_FAILED stages=" + ",".join(incomplete))
    config = read_json(_resolve_config_for_preflight(plan, cwd=cwd), {})
    if isinstance(config, dict):
        for account in plan.accounts or ["-"]:
            try:
                report_system_failure(
                    base=base, config=config, unit=f"options-monitor-tick-{plan.market}.service",
                    market=plan.market, account=account, failure_code=code, stage=stage,
                    run_id=run_id, rc=rc, first_error_at=now, opend_login_state=login_state,
                )
            except Exception as exc:
                _write_line(stderr, f"<3>ALERT_RECORD_FAILED {type(exc).__name__}")


def _completed_receipt(base: Path, run_id: str, plan: TickCronPlan) -> dict[str, Any] | None:
    path = base / "output_runs" / run_id / "state" / "child_tick_completion.json"
    try:
        receipt = read_json(path, {})
    except Exception:
        return None
    if not isinstance(receipt, dict):
        return None
    if (
        receipt.get("status") != "ok"
        or receipt.get("wrapper_run_id") != run_id
        or receipt.get("market") != plan.market
        or sorted(receipt.get("accounts") or []) != sorted(plan.accounts)
        or not receipt.get("inner_run_id")
    ):
        return None
    return receipt


def _full_account_scope(plan: TickCronPlan, *, cwd: str | Path | None) -> bool:
    try:
        configured = accounts_from_config_path(_resolve_config_for_preflight(plan, cwd=cwd), fallback=())
    except Exception:
        return False
    return bool(configured) and len(plan.accounts) == len(configured) and set(plan.accounts) == set(configured)




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
    stdout: Any = None,
    stderr: Any = None,
    environ: dict[str, str] | None = None,
    runtime_root: str | Path | None = None,
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

        env = dict(environ if environ is not None else os.environ)
        base = resolve_runtime_root(
            repo_root=Path(cwd).resolve() if cwd is not None else Path.cwd(),
            runtime_root=runtime_root,
            environ=env,
        ).runtime_root
        run_id = create_run_id()
        env.update(plan.trigger_env)
        env["OM_TICK_CRON_RUN_ID"] = run_id

        if preflight_config_fn is not None:
            try:
                preflight_config_fn(
                    plan=plan,
                    cwd=cwd,
                    allow_stale_config=allow_stale_config,
                )
            except Exception as exc:
                _record_failure(base=base, run_id=run_id, plan=plan,
                                code="TICK_PREFLIGHT_FAILED", stage="preflight", rc=1,
                                message=f"{type(exc).__name__}: {exc}", stderr=stderr, cwd=cwd)
                _write_line(stderr, "<3>EXEC_PREFLIGHT_FAILED_RC_1")
                return 1
            except SystemExit as exc:
                _record_failure(base=base, run_id=run_id, plan=plan,
                                code="TICK_PREFLIGHT_FAILED", stage="preflight", rc=1,
                                message=str(exc), stderr=stderr, cwd=cwd)
                _write_line(stderr, "<3>" + str(exc))
                return 1

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
            _record_failure(base=base, run_id=run_id, plan=plan,
                            code="TICK_TIMEOUT", stage="timeout", rc=124,
                            message="tick child timed out", stderr=stderr, cwd=cwd)
            _write_line(stderr, "<3>EXEC_TIMEOUT_RC_124")
            return 124
        except Exception as exc:
            _record_failure(base=base, run_id=run_id, plan=plan,
                            code="TICK_START_FAILED", stage="start", rc=1,
                            message=f"{type(exc).__name__}: {exc}", stderr=stderr, cwd=cwd)
            _write_line(stderr, "<3>EXEC_START_FAILED_RC_1")
            return 1
        rc = int(getattr(proc, "returncode", 1))
        if rc != 0:
            _record_failure(base=base, run_id=run_id, plan=plan,
                            code="TICK_EXEC_FAILED", stage="child_exit", rc=rc,
                            message=f"tick child exited with rc={rc}", stderr=stderr, cwd=cwd)
            _write_line(stderr, f"<3>EXEC_FAILED_RC_{rc}")
        elif _full_account_scope(plan, cwd=cwd) and not plan.symbols and not no_send and _completed_receipt(base, run_id, plan):
            config = read_json(_resolve_config_for_preflight(plan, cwd=cwd), {})
            if isinstance(config, dict):
                for account in plan.accounts or ["-"]:
                    for code, stage in (("TICK_PREFLIGHT_FAILED", "preflight"),
                                        ("TICK_TIMEOUT", "timeout"),
                                        ("TICK_START_FAILED", "start"),
                                        ("TICK_EXEC_FAILED", "child_exit")):
                        try:
                            report_system_recovery(
                                base=base, config=config, unit=f"options-monitor-tick-{plan.market}.service",
                                market=plan.market, account=account, failure_code=code, stage=stage,
                            )
                        except Exception as exc:
                            _write_line(stderr, f"<3>ALERT_RECOVERY_RECORD_FAILED {type(exc).__name__}")
            try:
                state_repo.write_shared_current_read_model(
                    base, f"tick_cron_last_result.{plan.market}.current.json",
                    {"status": "ok", "run_id": run_id, "market": plan.market,
                     "accounts": plan.accounts, "event_at_utc": datetime.now(timezone.utc).isoformat()},
                )
            except Exception:
                _write_line(stderr, "<3>FAILURE_RECORD_WRITE_FAILED stages=latest_success")
                return 1
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
