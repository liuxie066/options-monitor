from __future__ import annotations

import subprocess
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.application.account_config import accounts_from_config_path
from src.application.payload_helpers import parse_utc
from src.application.system_alerts import report_system_failure, report_system_recovery
from src.application.trades.account_mapping import resolve_trade_intake_config
from src.infrastructure.io_utils import read_json
from src.infrastructure.run_log import create_run_id

# The intake writes its status file once per work-loop iteration, so the
# no-heartbeat window has to cover one iteration. Measured on production
# 2026-09-24 over 16h of audit records (n=163 lx / 166 sy), from the interval
# between consecutive `backfill_check_finished` records, which land at the end of
# an iteration: 331s at p50, 424s at p90, and 583s at worst. 1200s is ~2x that
# worst case, so a healthy intake stays silent while a hung one is still caught
# within ~21 minutes. Re-measure when the loop's work changes — a longer backfill
# lookback, a slower broker, or a new per-iteration stage all widen the cycle.
_HEARTBEAT_STALE_SECONDS = 1200

# `status` is a stage label, not the liveness signal: it churns every iteration
# (the listener reports "starting" throughout receipt/backfill work), and every
# write refreshes the heartbeat, so gating on one healthy label (3.7.1 required
# `status == "listening"`) reported a healthy intake as stale. The whole
# vocabulary the writer produces is listening / starting / once for a working
# source and blocked / error / reconnecting / stopped for one that is not, so
# only the latter four are listed here. `reconnecting` is the one that would
# otherwise go silent: the retry loop refreshes the heartbeat while it backs off,
# so ignoring the label would report recovery for a dead OpenD connection.
_INTAKE_UNHEALTHY_STATUSES = frozenset({"blocked", "error", "reconnecting", "stopped"})


def alert_failed_service(*, unit: str, market: str, config_path: str, runtime_root: str | Path) -> int:
    config_file = Path(config_path)
    config = read_json(config_file, {})
    if not isinstance(config, dict) or not config:
        print("<3>SERVICE_FAILURE_ALERT_CONFIG_UNAVAILABLE")
        return 1
    try:
        accounts = accounts_from_config_path(config_file, fallback=()) or ["-"]
    except Exception:
        accounts = ["-"]
    base = Path(runtime_root)
    pending = read_json(base / "output_shared" / "state" / "opend_phone_verify_pending.json", {})
    login_state = "phone_verify_pending" if isinstance(pending, dict) and pending.get("pending") else "unknown"
    run_id = create_run_id()
    now = datetime.now(timezone.utc).isoformat()
    reason_by_account: dict[str, str] = {}
    try:
        intake = resolve_trade_intake_config(config)
        for source in intake.get("sources") or []:
            status_path = Path(source["status_path"])
            if not status_path.is_absolute():
                status_path = base / status_path
            status = read_json(status_path, {})
            if isinstance(status, dict) and status.get("status") == "blocked":
                reason = str(status.get("reason_code") or status.get("error_code") or "").strip()
                if reason:
                    reason_by_account[str(source.get("account") or "")] = reason
    except (KeyError, TypeError, ValueError):
        pass
    results = []
    for account in accounts:
        try:
            results.append(report_system_failure(
                base=base, config=config, unit=unit, market=market, account=account,
                failure_code=reason_by_account.get(account, "SERVICE_TERMINAL_FAILURE"), stage="unit_failed", run_id=run_id,
                rc=-1, first_error_at=now, opend_login_state=login_state,
            ))
        except Exception:
            print("<3>SERVICE_ALERT_INFRA_FAILED")
            return 1
    if all(item in {"confirmed", "suppressed"} for item in results):
        return 0
    print("<3>SERVICE_FAILURE_ALERT_UNCONFIRMED " + ",".join(results))
    return 1


def check_trade_intake_heartbeat(
    *, unit: str, market: str, config_path: str, runtime_root: str | Path,
    unit_active_fn: Callable[[str], bool] | None = None,
    now: datetime | None = None,
    disk_usage_fn: Callable[[str], Any] = shutil.disk_usage,
) -> int:
    config = read_json(Path(config_path), {})
    if not isinstance(config, dict) or not config:
        print("<3>INTAKE_HEARTBEAT_CONFIG_UNAVAILABLE")
        return 1
    base = Path(runtime_root)
    try:
        sources = [source for source in resolve_trade_intake_config(config).get("sources") or []
                   if source.get("enabled", True)]
        if not sources:
            raise ValueError("no enabled trade intake sources")
        active = unit_active_fn(unit) if unit_active_fn is not None else subprocess.run(
            ["systemctl", "is-active", "--quiet", unit], capture_output=True, timeout=5,
        ).returncode == 0
        checked_at = now or datetime.now(timezone.utc)
        usage = disk_usage_fn("/")
        if usage.total <= 0:
            raise ValueError("root disk size is unavailable")
        results: list[str] = []
        for threshold in (85, 90):
            code = f"ROOT_DISK_USAGE_{threshold}"
            fields = dict(base=base, config=config, unit="options-monitor-root-disk",
                          market="host", account="system", failure_code=code, stage="disk_capacity")
            if usage.used * 100 >= usage.total * threshold:
                outcome = report_system_failure(
                    **fields, run_id=create_run_id(), rc=0,
                    first_error_at=checked_at.isoformat(), opend_login_state="not_applicable",
                )
                if outcome != "suppressed":
                    print(f"<{'3' if threshold == 90 else '4'}>{code} used={usage.used} total={usage.total} delivery={outcome}")
            else:
                outcome = report_system_recovery(**fields)
            results.append(outcome)
        for source in sources:
            status_path = Path(source["status_path"])
            if not status_path.is_absolute():
                status_path = base / status_path
            status = read_json(status_path, {})
            status = status if isinstance(status, dict) else {}
            account = str(source.get("account") or "").strip().lower()
            heartbeat_raw = str(status.get("last_heartbeat_utc") or "")
            heartbeat = parse_utc(heartbeat_raw)
            age = float("inf") if heartbeat is None else (checked_at - heartbeat).total_seconds()
            status_label = str(status.get("status") or "").strip().lower()
            healthy = (
                active
                and status_label not in _INTAKE_UNHEALTHY_STATUSES
                and -60 <= age <= _HEARTBEAT_STALE_SECONDS
            )
            if healthy:
                for failure_code, stage in (
                    ("TRADE_INTAKE_PROCESS_DOWN", "heartbeat"),
                    ("TRADE_INTAKE_HEARTBEAT_STALE", "heartbeat"),
                    ("SERVICE_TERMINAL_FAILURE", "unit_failed"),
                    ("OPEND_LOGIN_INVALID", "unit_failed"),
                    ("OPEND_NEEDS_PHONE_VERIFY", "unit_failed"),
                    ("OPEND_NEEDS_PIC_VERIFY", "unit_failed"),
                ):
                    results.append(report_system_recovery(
                        base=base, config=config, unit=unit, market=market, account=account,
                        failure_code=failure_code, stage=stage,
                    ))
                continue
            failure_code = "TRADE_INTAKE_HEARTBEAT_STALE" if active else "TRADE_INTAKE_PROCESS_DOWN"
            result = report_system_failure(
                base=base, config=config, unit=unit, market=market, account=account,
                failure_code=failure_code, stage="heartbeat", run_id=create_run_id(),
                rc=0 if active else -1, first_error_at=heartbeat_raw or checked_at.isoformat(),
                opend_login_state=str(status.get("reason_code") or "unknown"),
            )
            results.append(result)
            if result != "suppressed":
                print(f"<4>{failure_code} account={account} delivery={result}")
        if all(result in {"confirmed", "suppressed", "no_incident"} for result in results):
            return 0
        print("<3>INTAKE_HEARTBEAT_ALERT_UNCONFIRMED")
        return 1
    except Exception as error:
        # Name the cause: an internal error here used to surface as a bare
        # "infrastructure failure", which reads like the monitored system's fault.
        print(f"<3>INTAKE_HEARTBEAT_ALERT_INFRA_FAILED error={error!r}")
        return 1
