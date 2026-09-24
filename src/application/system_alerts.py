from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
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
        # ponytail: 1 MiB state cap; prune old incidents if alert cardinality grows.
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024:
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
) -> str:
    """Reserve an incident before delivery; an uncertain send is never retried inside the silence window."""
    key = _fingerprint(unit, market, account, failure_code, stage)
    path = base / "output_shared" / "state" / "system_alerts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _state_lock(path):
        state = _read_state(path)
        now = datetime.now(timezone.utc)
        previous = state.get(key)
        if isinstance(previous, dict) and previous.get("status") == "failed":
            try:
                prior = datetime.fromisoformat(str(previous["reserved_at"]))
                if (now - prior).total_seconds() < max(1, silence_seconds):
                    return "suppressed"
            except (KeyError, TypeError, ValueError):
                pass
        route = resolve_notification_delivery_route(config=config)
        if not route.get("target"):
            return "unconfigured"
        state[key] = {
            "status": "failed", "reserved_at": now.isoformat(), "delivery": "unknown",
            "unit": unit, "market": market, "account": account, "failure_code": failure_code,
            "stage": stage, "run_id": run_id, "rc": rc, "first_error_at": first_error_at,
            "opend_login_state": opend_login_state,
        }
        atomic_write_json(path, state)
    message = render_system_notice(
        component=unit,
        status="❌ 不可用",
        fields=(("run_id", run_id), ("market", market), ("account", account),
                ("failure_code", failure_code), ("stage", stage), ("rc", rc),
                ("first_error_at", first_error_at), ("opend_login_state", opend_login_state)),
    )
    try:
        confirmed = _send(base, config, message, hashlib.sha256((key + now.isoformat()).encode()).hexdigest())
    except Exception:
        confirmed = False
    if confirmed:
        with _state_lock(path):
            state = _read_state(path)
            if isinstance(state.get(key), dict) and state[key].get("reserved_at") == now.isoformat():
                state[key]["delivery"] = "confirmed"
                atomic_write_json(path, state)
    return "confirmed" if confirmed else "unconfirmed"


def report_system_recovery(
    *, base: Path, config: dict, unit: str, market: str, account: str,
    failure_code: str, stage: str,
) -> str:
    key = _fingerprint(unit, market, account, failure_code, stage)
    path = base / "output_shared" / "state" / "system_alerts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _state_lock(path):
        state = _read_state(path)
        if not isinstance(state.get(key), dict):
            return "no_incident"
        if state[key].get("status") != "failed":
            return "no_incident"
        incident_at = str(state[key].get("reserved_at") or "")
        state[key]["status"] = "recovered"
        state[key]["recovered_at"] = datetime.now(timezone.utc).isoformat()
        state[key]["recovery_delivery"] = "unknown"
        atomic_write_json(path, state)
    message = render_system_notice(component=unit, status="✅ 已恢复", fields=(("market", market), ("account", account), ("failure_code", failure_code), ("stage", stage)))
    try:
        recovery_key = hashlib.sha256((key + incident_at + "-recovery").encode()).hexdigest()
        confirmed = _send(base, config, message, recovery_key)
    except Exception:
        confirmed = False
    if confirmed:
        with _state_lock(path):
            state = _read_state(path)
            if isinstance(state.get(key), dict) and state[key].get("reserved_at") == incident_at:
                state[key]["recovery_delivery"] = "confirmed"
                atomic_write_json(path, state)
    return "confirmed" if confirmed else "unconfirmed"
