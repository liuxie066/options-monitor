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

from domain.domain.multi_tick import (
    DEFAULT_NOTIFICATION_PROVIDER,
    FEISHU_APP_NOTIFICATION_PROVIDER,
    WECHAT_CLAWBOT_NOTIFICATION_PROVIDER,
)
from domain.storage.repositories import state_repo
from domain.storage.json_io import atomic_write_private_json as atomic_write_json
from src.application.channels.wechat_clawbot.state import (
    default_wechat_clawbot_state_root,
    load_wechat_clawbot_binding,
    load_wechat_clawbot_state,
)
from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore
from src.application.notification_delivery_adapter import (
    normalize_notification_delivery_result,
    select_notification_delivery_adapter,
)
from src.application.notification_delivery_route import resolve_notification_delivery_route
from src.application.notification_shells import render_system_notice
from src.application.secret_resolver import resolve_feishu_bot_config

_MAX_INCIDENTS = 512
_SILENCE_SECONDS = 600


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


def _fallback_route(base: Path, primary_provider: str) -> dict | None:
    if primary_provider == WECHAT_CLAWBOT_NOTIFICATION_PROVIDER:
        bot = resolve_feishu_bot_config(notifications={})
        if bot.send_missing_fields:
            return None
        return {"provider": FEISHU_APP_NOTIFICATION_PROVIDER, "channel": FEISHU_APP_NOTIFICATION_PROVIDER,
                "target": bot.user_open_id, "notifications": {}}
    if primary_provider == FEISHU_APP_NOTIFICATION_PROVIDER:
        root = default_wechat_clawbot_state_root(base)
        targets = []
        for state_dir in root.iterdir() if root.is_dir() else ():
            if state_dir.is_symlink() or not state_dir.is_dir():
                continue
            label = state_dir.name
            try:
                load_wechat_clawbot_state(base=base, label=label, notifications={})
                bindings = WechatClawbotStateStore(state_dir).load_bindings().get("bindings", {})
                for name in bindings if isinstance(bindings, dict) else ():
                    target = f"wechat:{label}:{name}"
                    try:
                        load_wechat_clawbot_binding(base=base, target=target, notifications={})
                    except (OSError, ValueError):
                        continue
                    else:
                        targets.append(target)
            except (OSError, ValueError):
                continue
        if len(targets) == 1:
            return {"provider": WECHAT_CLAWBOT_NOTIFICATION_PROVIDER,
                    "channel": WECHAT_CLAWBOT_NOTIFICATION_PROVIDER, "target": targets[0], "notifications": {}}
    return None


def _send(base: Path, config: dict | None, message: str, key: str) -> dict:
    try:
        route = resolve_notification_delivery_route(config=config or {})
    except Exception:
        notifications = (config or {}).get("notifications") if isinstance(config, dict) else None
        provider = notifications.get("provider") if isinstance(notifications, dict) else None
        route = {"provider": provider or DEFAULT_NOTIFICATION_PROVIDER, "target": None}
    primary_provider = str(route.get("provider") or "")

    def attempt(selected: dict, delivery_key: str, *, fallback_used: bool) -> dict:
        adapter = select_notification_delivery_adapter(selected.get("provider"))
        result = adapter.send_fn(
            base=base, channel=str(selected.get("channel") or ""), target=str(selected["target"]),
            message=message, notifications=selected.get("notifications") or {},
            idempotency_key="om-" + delivery_key[:32],
        )
        normalized = normalize_notification_delivery_result(result, normalize_fn=adapter.normalize_fn)
        return {"delivery_confirmed": normalized.get("delivery_confirmed") is True,
                "provider": selected["provider"], "fallback_used": fallback_used,
                "attempted": True, "explicit_pre_acceptance_failure":
                    normalized.get("explicit_pre_acceptance_failure") is True}

    if str(route.get("target") or "").strip():
        try:
            primary = attempt(route, key, fallback_used=False)
            if primary["delivery_confirmed"] or not primary["explicit_pre_acceptance_failure"]:
                return primary
        except ValueError as exc:
            if not str(exc).startswith(("Feishu bot env missing required fields:",
                                         "Feishu bot user open_id is required",
                                         "WeChat ClawBot bot_token is missing",
                                         "WeChat ClawBot binding not found:",
                                         "WeChat ClawBot binding is incomplete:")):
                return {"delivery_confirmed": False, "provider": primary_provider,
                        "fallback_used": False, "attempted": True}
        except Exception:
            return {"delivery_confirmed": False, "provider": primary_provider,
                    "fallback_used": False, "attempted": True}
    try:
        fallback = _fallback_route(base, primary_provider)
        if fallback is not None:
            fallback_key = hashlib.sha256((key + ":fallback:" + fallback["provider"]).encode()).hexdigest()
            try:
                return attempt(fallback, fallback_key, fallback_used=True)
            except Exception:
                return {"delivery_confirmed": False, "provider": fallback["provider"],
                        "fallback_used": True, "attempted": True}
    except Exception:
        pass
    return {"delivery_confirmed": False, "provider": None, "fallback_used": False, "attempted": False}


def _audit_delivery(base: Path, *, run_id: str, incident: dict, recovery: bool = False) -> None:
    event = state_repo.normalize_audit_event({
        "event_type": "system_alert", "action": "recovery_delivery" if recovery else "failure_delivery",
        "status": "ok" if incident.get("delivery_confirmed") else "error", "run_id": run_id,
        "account": incident.get("account"), "error_code": incident.get("failure_code"),
        "fallback_used": incident.get("fallback_used", False),
        "extra": {"provider": incident.get("provider"),
                  "delivery_confirmed": incident.get("delivery_confirmed", False),
                  "stage": incident.get("stage")},
    })
    try:
        state_repo.append_shared_audit_jsonl(base, "audit_events.jsonl", event)
        runs_root = (base / "output_runs").resolve()
        run_dir = (runs_root / run_id).resolve()
        if run_id and run_dir.is_relative_to(runs_root) and run_dir.is_dir():
            state_repo.append_run_audit_jsonl(base, run_id, "audit_events.jsonl", event)
    except Exception as exc:
        print(f"<3>SYSTEM_ALERT_AUDIT_FAILED {type(exc).__name__}", file=sys.stderr)


def system_alert_delivery_status(base: Path, *, state_path: Path | None = None) -> dict:
    """Read the active alert delivery state without contacting either provider."""
    try:
        state = _read_state(state_path or base / "output_shared" / "state" / "system_alerts.json")
    except (OSError, ValueError):
        return {"status": "degraded", "reason_code": "SYSTEM_ALERT_STATE_UNREADABLE",
                "active_count": 0, "fallback_used": False, "provider": None}
    active = [value for value in state.values() if isinstance(value, dict) and value.get("status") == "failed"]
    latest = max(active, key=lambda item: str(item.get("last_attempt_at") or ""), default={})
    unconfirmed = any(item.get("delivery_confirmed") is not True for item in active)
    return {"status": "degraded" if unconfirmed else "confirmed" if active else "unknown",
            "reason_code": "SYSTEM_ALERT_DELIVERY_UNCONFIRMED" if unconfirmed else None,
            "active_count": len(active), "fallback_used": bool(latest.get("fallback_used")),
            "provider": latest.get("provider")}


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
    silence_seconds: int = _SILENCE_SECONDS,
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
        incident_at = str(previous.get("reserved_at") or now.isoformat()) if active else now.isoformat()
        state[key] = {
            "status": "failed", "reserved_at": incident_at, "last_attempt_at": now.isoformat(), "delivery": "unknown",
            "delivery_confirmed": False, "fallback_used": False, "provider": None,
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
        delivery = _send(base, config, message, hashlib.sha256((key + incident_at).encode()).hexdigest())
    except Exception:
        delivery = {"delivery_confirmed": False, "provider": None, "fallback_used": False, "attempted": False}
    with _state_lock(path):
        state = _read_state(path)
        if isinstance(state.get(key), dict) and state[key].get("reserved_at") == incident_at and state[key].get("last_attempt_at") == now.isoformat():
            state[key].update(delivery)
            state[key]["delivery"] = "confirmed" if delivery["delivery_confirmed"] else "unconfirmed" if delivery["attempted"] else "journal"
            atomic_write_json(path, state)
    _audit_delivery(base, run_id=run_id, incident={**state.get(key, {}), **delivery})
    if not delivery["delivery_confirmed"]:
        print(f"<3>SYSTEM_ALERT_UNCONFIRMED {failure_code} provider={delivery['provider'] or '-'}", file=sys.stderr)
    return "confirmed" if delivery["delivery_confirmed"] else "unconfirmed" if delivery["attempted"] else "unconfigured"


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
                if (now - last_attempt).total_seconds() < _SILENCE_SECONDS:
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
        delivery = _send(base, config, message, recovery_key)
    except Exception:
        delivery = {"delivery_confirmed": False, "provider": None, "fallback_used": False, "attempted": False}
    confirmed = delivery["delivery_confirmed"]
    with _state_lock(path):
        state = _read_state(path)
        if (isinstance(state.get(key), dict) and state[key].get("reserved_at") == incident_at
                and state[key].get("recovery_last_attempt_at") == attempt_at):
            for field in ("delivery_confirmed", "fallback_used", "provider"):
                state[key].setdefault("failure_" + field, state[key].get(field))
                state[key][field] = delivery[field]
            state[key].setdefault("failure_delivery", state[key].get("delivery"))
            state[key]["delivery"] = "confirmed" if confirmed else "unconfirmed" if delivery["attempted"] else "journal"
            state[key]["recovery_delivery"] = "confirmed" if confirmed else "unknown"
            state[key]["recovery_delivery_confirmed"] = confirmed
            state[key]["recovery_fallback_used"] = delivery["fallback_used"]
            state[key]["recovery_provider"] = delivery["provider"]
            atomic_write_json(path, state)
    _audit_delivery(base, run_id=str(previous.get("run_id") or ""),
                    incident={**previous, **delivery}, recovery=True)
    try:
        report_system_meta_signal(
            base=base, unit=unit, market=market, account=account,
            failure_code="SYSTEM_RECOVERY_DELIVERY_UNCONFIRMED", stage="recovery_delivery",
            run_id=str(previous.get("run_id") or ""), degraded=not confirmed,
            reason=f"{failure_code}:{stage}", external=False,
        )
    except Exception as exc:
        print(f"<3>SYSTEM_RECOVERY_META_FAILED {type(exc).__name__}", file=sys.stderr)
    return "confirmed" if confirmed else "unconfirmed"


def report_system_meta_signal(
    *, base: Path, unit: str, market: str, account: str, failure_code: str,
    stage: str, run_id: str, degraded: bool, reason: str = "",
    config: dict | None = None, external: bool = False,
) -> str:
    """Record a meta incident and attempt one external delivery per incident when configured."""
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
                "run_id": run_id, "reason": reason, "fallback_used": False,
                "provider": None, "delivery_confirmed": False,
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
    if external:
        notice = render_system_notice(component=unit, status="❌ 不可用" if degraded else "✅ 已恢复",
                                      fields=(("market", market), ("account", account),
                                              ("failure_code", failure_code), ("stage", stage), ("reason", reason)))
        try:
            delivery = _send(base, config, notice, hashlib.sha256((key + str(state[key].get("reserved_at") or now) + ("-recovery" if not degraded else "")).encode()).hexdigest())
        except Exception:
            delivery = {"delivery_confirmed": False, "provider": None, "fallback_used": False, "attempted": False}
        with _state_lock(path):
            current = _read_state(path)
            if isinstance(current.get(key), dict) and current[key].get("reserved_at") == state[key].get("reserved_at"):
                current[key].update(delivery)
                current[key]["delivery"] = "confirmed" if delivery["delivery_confirmed"] else "unconfirmed" if delivery["attempted"] else "journal"
                atomic_write_json(path, current)
        _audit_delivery(base, run_id=run_id, incident={"account": account, "failure_code": failure_code,
                                                    "stage": stage, **delivery}, recovery=not degraded)
    return "signaled" if degraded else "recovered"
