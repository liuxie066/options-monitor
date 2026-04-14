#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scripts.io_utils import atomic_write_json, ensure_dir, read_json
except ModuleNotFoundError:
    from io_utils import atomic_write_json, ensure_dir, read_json

ROOT_DEV = Path("/home/node/.openclaw/workspace/options-monitor")
DEFAULT_STATE_PATH = ROOT_DEV / "output_shared" / "state" / "deploy_observability.json"
DEFAULT_HISTORY_LIMIT = 50
DEFAULT_FAILURE_COOLDOWN_SECONDS = 1800


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_failure_key(text: str) -> str:
    if not text:
        return "unknown"
    x = re.sub(r"\s+", " ", text.strip().lower())
    x = re.sub(r"\b[0-9a-f]{8,40}\b", "<sha>", x)
    x = re.sub(r"\d+", "<n>", x)
    return x[:160] if len(x) > 160 else x


def _parse_iso_utc(text: str) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _seconds_between(old_ts: str, new_ts: str) -> float | None:
    old = _parse_iso_utc(old_ts)
    new = _parse_iso_utc(new_ts)
    if old is None or new is None:
        return None
    return (new - old).total_seconds()


def default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": None,
        "last_event": None,
        "last_success": None,
        "last_failure": None,
        "history": [],
        "failure_aggregate": {},
    }


def load_state(path: Path = DEFAULT_STATE_PATH) -> dict[str, Any]:
    obj = read_json(path, default=None)
    if not isinstance(obj, dict):
        return default_state()
    out = default_state()
    out.update(obj)
    if not isinstance(out.get("history"), list):
        out["history"] = []
    if not isinstance(out.get("failure_aggregate"), dict):
        out["failure_aggregate"] = {}
    return out


def save_state(state: dict[str, Any], path: Path = DEFAULT_STATE_PATH) -> None:
    ensure_dir(path.parent)
    atomic_write_json(path, state)


def enrich_event(event: dict[str, Any], state: dict[str, Any], cooldown_seconds: int = DEFAULT_FAILURE_COOLDOWN_SECONDS) -> dict[str, Any]:
    ts = str(event.get("timestamp_utc") or utc_now())
    status = str(event.get("status") or "failed")
    previous = state.get("last_event") if isinstance(state.get("last_event"), dict) else None
    previous_status = str(previous.get("status")) if previous else None
    status_changed = previous_status != status

    failure_reason = str(event.get("failure_reason") or "").strip()
    failure_key = str(event.get("failure_key") or _normalize_failure_key(failure_reason)) if status == "failed" else ""
    agg = state.get("failure_aggregate") if isinstance(state.get("failure_aggregate"), dict) else {}
    old_item = agg.get(failure_key) if isinstance(agg.get(failure_key), dict) else None

    new_failure_type = False
    cooldown_expired = True
    if status == "failed":
        last_failure = state.get("last_failure") if isinstance(state.get("last_failure"), dict) else None
        last_key = str(last_failure.get("failure_key") or "") if last_failure else ""
        new_failure_type = failure_key != last_key
        if old_item:
            seconds = _seconds_between(str(old_item.get("last_seen_at") or ""), ts)
            if seconds is not None and seconds < float(cooldown_seconds):
                cooldown_expired = False

    should_alert = False
    if status == "failed":
        should_alert = status_changed or new_failure_type or cooldown_expired
    elif status == "success":
        should_alert = status_changed

    out = dict(event)
    out["timestamp_utc"] = ts
    out["status"] = status
    out["failure_reason"] = failure_reason if status == "failed" else ""
    out["failure_key"] = failure_key if status == "failed" else ""
    out["status_changed"] = status_changed
    out["should_alert"] = should_alert
    out["suppressed"] = status == "failed" and not should_alert
    out["cooldown_seconds"] = int(cooldown_seconds)
    return out


def append_event(
    event: dict[str, Any],
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    cooldown_seconds: int = DEFAULT_FAILURE_COOLDOWN_SECONDS,
) -> dict[str, Any]:
    state = load_state(state_path)
    ev = enrich_event(event, state, cooldown_seconds=cooldown_seconds)
    ts = str(ev.get("timestamp_utc") or utc_now())

    hist = state.get("history") if isinstance(state.get("history"), list) else []
    hist.append(ev)
    state["history"] = hist[-max(1, int(history_limit)) :]
    state["updated_at"] = ts
    state["last_event"] = ev

    if ev.get("status") == "success":
        state["last_success"] = ev
    else:
        state["last_failure"] = ev
        fk = str(ev.get("failure_key") or "unknown")
        agg = state.get("failure_aggregate") if isinstance(state.get("failure_aggregate"), dict) else {}
        item = agg.get(fk) if isinstance(agg.get(fk), dict) else {}
        agg[fk] = {
            "count": int(item.get("count") or 0) + 1,
            "first_seen_at": str(item.get("first_seen_at") or ts),
            "last_seen_at": ts,
            "last_reason": str(ev.get("failure_reason") or ""),
        }
        state["failure_aggregate"] = agg

    save_state(state, state_path)
    return ev


def build_summary(event: dict[str, Any]) -> str:
    op = str(event.get("operation") or "deploy")
    status = str(event.get("status") or "failed").upper()
    dev = str(event.get("dev_commit") or "unknown")
    prod_after = str(event.get("prod_commit_after") or event.get("prod_commit") or "unknown")
    merged = event.get("merged_to_target")
    target = str(event.get("target_branch") or "")
    lag = event.get("lag")
    lag_txt = ""
    if isinstance(lag, dict):
        lag_txt = f" lagging={lag.get('is_lagging')} ahead_by={lag.get('ahead_by')}"
    summary = f"[{op}] {status} dev={dev[:12]} prod={prod_after[:12]} merged={merged}"
    if target:
        summary += f" target={target}"
    summary += lag_txt
    if status == "FAILED":
        reason = str(event.get("failure_reason") or "unknown")
        summary += f" reason={reason}"
    if bool(event.get("suppressed")):
        summary += " (alert-suppressed)"
    return summary


def make_machine_json(event: dict[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False, sort_keys=True)


def classify_lag(
    *,
    dev_head: str,
    prod_head: str,
    prod_is_ancestor_of_dev: bool,
    dev_is_ancestor_of_prod: bool,
    ahead_by: int,
    behind_by: int,
) -> dict[str, Any]:
    same = bool(dev_head and prod_head and dev_head == prod_head)
    diverged = False
    is_lagging = False
    if not same:
        if prod_is_ancestor_of_dev and not dev_is_ancestor_of_prod:
            is_lagging = True
        elif not prod_is_ancestor_of_dev and not dev_is_ancestor_of_prod:
            diverged = True
    return {
        "dev_head": dev_head,
        "prod_head": prod_head,
        "is_lagging": is_lagging,
        "is_same": same,
        "is_diverged": diverged,
        "ahead_by": int(max(0, ahead_by)),
        "behind_by": int(max(0, behind_by)),
    }
