from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from domain.storage.json_io import atomic_write_private_json as atomic_write_json
from src.application.notification_delivery_adapter import (
    normalize_notification_delivery_result,
    select_notification_delivery_adapter,
)
from src.application.notification_delivery_route import resolve_notification_delivery_route
from src.application.notification_shells import render_system_notice

_MAX_INCIDENTS = 512


@contextmanager
def _state_lock(path: Path):
    lock_path = path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("system alert lock is not a regular file")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_state(path: Path) -> dict:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("system alert state is invalid")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            state = json.load(stream)
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(state, dict):
        raise ValueError("system alert state must be an object")
    return state


def _prune_state(state: dict) -> dict:
    if len(state) <= _MAX_INCIDENTS:
        return state
    return dict(sorted(
        state.items(),
        key=lambda item: str(item[1].get("last_attempt_at") or item[1].get("recovered_at") or item[1].get("reserved_at") or "")
        if isinstance(item[1], dict) else "",
    )[-_MAX_INCIDENTS:])


def _fingerprint(unit: str, market: str, account: str, failure_code: str, stage: str) -> str:
    fields = [unit, market, account.lower(), failure_code, stage]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _send(base: Path, config: dict, message: str, key: str) -> bool:
    route = resolve_notification_delivery_route(config=config)
    target = str(route.get("target") or "").strip()
    if not target:
        return False
    adapter = select_notification_delivery_adapter(route.get("provider"))
    result = adapter.send_fn(
        base=base,
        channel=str(route.get("channel") or ""),
        target=target,
        message=message,
        notifications=route.get("notifications") if isinstance(route.get("notifications"), dict) else {},
        idempotency_key="om-" + key[:32],
    )
    return normalize_notification_delivery_result(result, normalize_fn=adapter.normalize_fn).get("delivery_confirmed") is True


def report_system_failure(
    *,
    base: Path,
    config: dict,
    unit: str,
    market: str,
    account: str,
    failure_code: str,
    stage: str,
    run_id: str,
    rc: int,
    first_error_at: str,
    opend_login_state: str,
    silence_seconds: int = 600,
    message: str | None = None,
) -> str:
    """Reserve an incident before delivery; an uncertain send is never retried inside the silence window."""
    key = _fingerprint(unit, market, account, failure_code, stage)
    path = base / "output_shared" / "state" / "system_alerts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _state_lock(path):
        state = _read_state(path)
        now = datetime.now(timezone.utc)
        previous = state.get(key)
        active = isinstance(previous, dict) and previous.get("status") == "failed"
        if active:
            try:
                prior = datetime.fromisoformat(str(previous.get("last_attempt_at") or previous["reserved_at"]))
                if (now - prior).total_seconds() < max(1, silence_seconds):
                    return "suppressed"
            except (KeyError, TypeError, ValueError):
                pass
        route = resolve_notification_delivery_route(config=config)
        if not route.get("target"):
            return "unconfigured"
        incident_at = str(previous.get("reserved_at") or now.isoformat()) if active else now.isoformat()
        state[key] = {
            "status": "failed", "reserved_at": incident_at, "last_attempt_at": now.isoformat(), "delivery": "unknown",
            "unit": unit, "market": market, "account": account, "failure_code": failure_code,
            "stage": stage, "run_id": run_id, "rc": rc,
            "first_error_at": previous.get("first_error_at", first_error_at) if active else first_error_at,
            "opend_login_state": opend_login_state,
        }
        atomic_write_json(path, _prune_state(state))
    message = message or render_system_notice(
        component=unit,
        status="❌ 不可用",
        fields=(("run_id", run_id), ("market", market), ("account", account),
                ("failure_code", failure_code), ("stage", stage), ("rc", rc),
                ("first_error_at", first_error_at), ("opend_login_state", opend_login_state)),
    )
    try:
        confirmed = _send(base, config, message, hashlib.sha256((key + incident_at).encode()).hexdigest())
    except Exception:
        confirmed = False
    if confirmed:
        with _state_lock(path):
            state = _read_state(path)
            if isinstance(state.get(key), dict) and state[key].get("reserved_at") == incident_at and state[key].get("last_attempt_at") == now.isoformat():
                state[key]["delivery"] = "confirmed"
                atomic_write_json(path, state)
    return "confirmed" if confirmed else "unconfirmed"


def report_system_recovery(
    *, base: Path, config: dict, unit: str, market: str, account: str,
    failure_code: str, stage: str, message: str | None = None,
    notify: bool = True, allow_without_incident: bool = False,
) -> str:
    key = _fingerprint(unit, market, account, failure_code, stage)
    path = base / "output_shared" / "state" / "system_alerts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _state_lock(path):
        state = _read_state(path)
        previous = state.get(key)
        if previous is None and allow_without_incident:
            previous = {"status": "failed", "reserved_at": datetime.now(timezone.utc).isoformat()}
        if not isinstance(previous, dict):
            return "no_incident"
        retry = previous.get("status") == "recovered" and previous.get("recovery_delivery") == "unknown" and notify
        if previous.get("status") != "failed" and not retry:
            return "no_incident"
        now = datetime.now(timezone.utc)
        if retry:
            try:
                last_attempt = datetime.fromisoformat(str(previous["recovery_last_attempt_at"]))
                if (now - last_attempt).total_seconds() < 60:
                    return "suppressed"
            except (KeyError, TypeError, ValueError):
                pass
        incident_at = str(previous.get("reserved_at") or "")
        attempt_at = now.isoformat()
        state[key] = {**previous, "status": "recovered",
                      "recovered_at": previous.get("recovered_at") or attempt_at,
                      "recovery_last_attempt_at": attempt_at,
                      "recovery_delivery": "unknown" if notify else "disabled"}
        atomic_write_json(path, _prune_state(state))
    if not notify:
        return "suppressed"
    message = message or render_system_notice(component=unit, status="✅ 已恢复", fields=(("market", market), ("account", account), ("failure_code", failure_code), ("stage", stage)))
    try:
        recovery_key = hashlib.sha256((key + incident_at + "-recovery").encode()).hexdigest()
        confirmed = _send(base, config, message, recovery_key)
    except Exception:
        confirmed = False
    if confirmed:
        with _state_lock(path):
            state = _read_state(path)
            if (isinstance(state.get(key), dict) and state[key].get("reserved_at") == incident_at
                    and state[key].get("recovery_last_attempt_at") == attempt_at):
                state[key]["recovery_delivery"] = "confirmed"
                atomic_write_json(path, state)
    try:
        report_system_meta_signal(
            base=base, unit=unit, market=market, account=account,
            failure_code="SYSTEM_RECOVERY_DELIVERY_UNCONFIRMED", stage="recovery_delivery",
            run_id=str(previous.get("run_id") or ""), degraded=not confirmed,
            reason=f"{failure_code}:{stage}",
        )
    except Exception as exc:
        print(f"<3>SYSTEM_RECOVERY_META_FAILED {type(exc).__name__}", file=sys.stderr)
    return "confirmed" if confirmed else "unconfirmed"


def report_system_meta_signal(
    *, base: Path, unit: str, market: str, account: str, failure_code: str,
    stage: str, run_id: str, degraded: bool, reason: str = "",
) -> str:
    """Record a journal-only failure or recovery in the existing incident state."""
    key = _fingerprint(unit, market, account, failure_code, stage)
    path = base / "output_shared" / "state" / "system_alerts.json"
    now = datetime.now(timezone.utc).isoformat()
    with _state_lock(path):
        state = _read_state(path)
        previous = state.get(key)
        active = isinstance(previous, dict) and previous.get("status") == "failed"
        if degraded == active:
            return "suppressed" if degraded else "no_incident"
        if degraded:
            state[key] = {
                "status": "failed", "reserved_at": now, "last_attempt_at": now,
                "delivery": "journal", "unit": unit, "market": market,
                "account": account, "failure_code": failure_code, "stage": stage,
                "run_id": run_id, "reason": reason,
            }
        else:
            state[key]["status"] = "recovered"
            state[key]["recovered_at"] = now
        atomic_write_json(path, _prune_state(state))
    event = "SYSTEM_META_ALERT" if degraded else "SYSTEM_META_RECOVERY"
    priority = "<3>" if degraded else "<4>"
    print(priority + event + " " + json.dumps({
        "unit": unit, "market": market, "account": account,
        "failure_code": failure_code, "stage": stage, "run_id": run_id,
        "reason": reason,
    }, ensure_ascii=False, separators=(",", ":")), file=sys.stderr)
    return "signaled" if degraded else "recovered"
