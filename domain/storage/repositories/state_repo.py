from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from domain.storage import paths
from domain.storage.json_io import atomic_write_json as write_json
from domain.storage.json_io import read_json
from domain.storage.repositories import run_repo
from domain.domain.intermediate_objects import SnapshotDTO


AUDIT_SCHEMA_KIND = "audit_event"
AUDIT_SCHEMA_VERSION = "1.0"


def shared_state_dir(base: Path) -> Path:
    p = paths.shared_state_dir(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def run_state_dir(base: Path, run_id: str) -> Path:
    return run_repo.ensure_run_state_dir(base, run_id)


def account_state_dir(base: Path, account: str) -> Path:
    p = paths.account_state_dir(base, account)
    p.mkdir(parents=True, exist_ok=True)
    return p


def run_account_state_dir(base: Path, run_id: str, account: str) -> Path:
    return run_repo.ensure_run_account_state_dir(base, run_id, account)


def write_scheduler_decision(base: Path, run_id: str, payload: dict[str, Any]) -> Path:
    normalized = SnapshotDTO.from_payload(payload).to_payload()
    out = run_state_dir(base, run_id) / "scheduler_decision.json"
    write_json(out, normalized)
    write_shared_current_read_model(base, "scheduler_decision.current.json", normalized)
    return out


def write_tick_metrics(base: Path, run_id: str, payload: dict[str, Any]) -> dict[str, Path]:
    sdir = shared_state_dir(base)
    rdir = run_state_dir(base, run_id)
    p_shared = (sdir / "tick_metrics.json").resolve()
    p_run = (rdir / "tick_metrics.json").resolve()
    write_json(p_shared, payload)
    write_json(p_run, payload)
    write_shared_current_read_model(base, "tick_metrics.current.json", payload)
    return {"shared": p_shared, "run": p_run}


def append_tick_metrics_history(base: Path, run_id: str, payload: dict[str, Any]) -> dict[str, Path]:
    sdir = shared_state_dir(base)
    rdir = run_state_dir(base, run_id)
    p_shared = (sdir / "tick_metrics_history.json").resolve()
    p_run = (rdir / "tick_metrics_history.json").resolve()

    def _append(path: Path) -> None:
        cur = read_json(path, [])
        if not isinstance(cur, list):
            cur = []
        cur.append(payload)
        write_json(path, cur)

    _append(p_shared)
    _append(p_run)
    return {"shared": p_shared, "run": p_run}


def write_shared_last_run(base: Path, payload: dict[str, Any]) -> Path:
    out = (shared_state_dir(base) / "last_run.json").resolve()
    write_json(out, payload)
    write_shared_current_read_model(base, "last_run.current.json", payload)
    return out


def write_shared_state(base: Path, name: str, payload: dict[str, Any]) -> Path:
    out = (shared_state_dir(base) / str(name)).resolve()
    write_json(out, payload)
    write_shared_current_read_model(base, f"{str(name)}.current.json", payload)
    return out


def write_account_last_run(base: Path, account: str, payload: dict[str, Any]) -> Path:
    out = (account_state_dir(base, account) / "last_run.json").resolve()
    write_json(out, payload)
    return out


def shared_current_read_model_dir(base: Path) -> Path:
    out = (shared_state_dir(base) / "current").resolve()
    out.mkdir(parents=True, exist_ok=True)
    return out


def write_shared_current_read_model(base: Path, name: str, payload: dict[str, Any]) -> Path:
    out = (shared_current_read_model_dir(base) / str(name)).resolve()
    write_json(out, payload)
    return out


def write_account_state_json_text(base: Path, account: str, name: str, payload: dict[str, Any]) -> Path:
    out = (account_state_dir(base, account) / str(name)).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def write_run_account_last_run(base: Path, run_id: str, account: str, payload: dict[str, Any]) -> Path:
    out = (run_account_state_dir(base, run_id, account) / "last_run.json").resolve()
    write_json(out, payload)
    return out


def write_account_run_state(base: Path, run_id: str, account: str, name: str, payload: dict[str, Any]) -> Path:
    out = (run_account_state_dir(base, run_id, account) / str(name)).resolve()
    write_json(out, payload)
    return out


def write_last_run_dir_pointer(base: Path, run_id: str) -> Path:
    p = (shared_state_dir(base) / "last_run_dir.txt").resolve()
    p.write_text(str(run_repo.get_run_dir(base, run_id)) + "\n", encoding="utf-8")
    return p


def append_run_audit_jsonl(base: Path, run_id: str, name: str, payload: dict[str, Any]) -> Path:
    out = (run_state_dir(base, run_id) / str(name)).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return out


def append_shared_audit_jsonl(base: Path, name: str, payload: dict[str, Any]) -> Path:
    out = (shared_state_dir(base) / str(name)).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return out


def normalize_audit_event(payload: dict[str, Any] | Any) -> dict[str, Any]:
    src = payload if isinstance(payload, dict) else {}
    event_type = str(src.get("event_type") or "").strip()
    action = str(src.get("action") or "").strip()
    if not event_type:
        raise ValueError("audit event requires event_type")
    if not action:
        raise ValueError("audit event requires action")
    out = {
        "schema_kind": AUDIT_SCHEMA_KIND,
        "schema_version": AUDIT_SCHEMA_VERSION,
        "event_type": event_type,
        "action": action,
        "status": str(src.get("status") or "ok"),
        "event_at_utc": str(src.get("event_at_utc") or datetime.now(timezone.utc).isoformat()),
    }
    for key in (
        "run_id",
        "account",
        "idempotency_key",
        "tool_name",
        "target",
        "message",
        "error_code",
        "fallback_used",
    ):
        if key in src:
            out[key] = src.get(key)
    extra = src.get("extra")
    if isinstance(extra, dict):
        out["extra"] = extra
    return out


def append_audit_event(base: Path, payload: dict[str, Any], *, run_id: str | None = None) -> dict[str, Path]:
    normalized = normalize_audit_event(payload)
    out = {
        "shared": append_shared_audit_jsonl(base, "audit_events.jsonl", normalized),
    }
    if run_id:
        out["run"] = append_run_audit_jsonl(base, run_id, "audit_events.jsonl", normalized)
    try:
        write_shared_current_read_model(
            base,
            "audit_event_latest.current.json",
            normalized,
        )
    except Exception:
        pass
    return out


def append_tool_execution_audit(base: Path, payload: dict[str, Any], *, run_id: str | None = None) -> dict[str, Path]:
    out: dict[str, Path] = {
        "shared": append_shared_audit_jsonl(base, "tool_execution_audit.jsonl", payload),
    }
    if run_id:
        out["run"] = append_run_audit_jsonl(base, run_id, "tool_execution_audit.jsonl", payload)
    return out


def append_source_snapshot_event(
    base: Path,
    payload: dict[str, Any],
    *,
    run_id: str | None = None,
) -> dict[str, Path]:
    out: dict[str, Path] = {
        "shared": append_shared_audit_jsonl(base, "source_snapshots.events.jsonl", payload),
    }
    if run_id:
        out["run"] = append_run_audit_jsonl(base, run_id, "source_snapshots.events.jsonl", payload)

    source_name = str((payload or {}).get("source_name") or "").strip().lower()
    if source_name:
        write_shared_current_read_model(
            base,
            f"source_snapshot.{source_name}.current.json",
            payload,
        )

    aggregated_path = (shared_current_read_model_dir(base) / "source_snapshots.current.json").resolve()
    aggregated = read_json(aggregated_path, {})
    if not isinstance(aggregated, dict):
        aggregated = {}
    if source_name:
        aggregated[source_name] = payload
    aggregated["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    write_json(aggregated_path, aggregated)
    out["current"] = aggregated_path
    return out


def _idempotency_scope_dir(base: Path, scope: str) -> Path:
    scope_norm = str(scope or "tool_execution").strip().lower().replace("/", "_")
    p = (shared_state_dir(base) / "idempotency" / scope_norm).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _idempotency_path(base: Path, *, scope: str, key: str) -> Path:
    key_norm = sha256(str(key or "").encode("utf-8")).hexdigest()
    return (_idempotency_scope_dir(base, scope) / f"{key_norm}.json").resolve()


def read_idempotency_record(base: Path, *, scope: str, key: str) -> dict[str, Any] | None:
    p = _idempotency_path(base, scope=scope, key=key)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def write_idempotency_record(base: Path, *, scope: str, key: str, payload: dict[str, Any]) -> Path:
    p = _idempotency_path(base, scope=scope, key=key)
    body = dict(payload or {})
    body["idempotency_key"] = str(key)
    body["scope"] = str(scope or "tool_execution")
    body["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    write_json(p, body)
    return p


def put_idempotency_success(
    base: Path,
    *,
    scope: str,
    key: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a successful idempotency record.

    Uses O_EXCL first-write semantics to keep writes retry-safe under contention.
    """
    p = _idempotency_path(base, scope=scope, key=key)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "idempotency_key": str(key),
        "scope": str(scope or "tool_execution"),
        "ok": True,
        "status": "fetched",
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    if isinstance(payload, dict):
        body.update(payload)
    raw = (json.dumps(body, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        prev = read_idempotency_record(base, scope=scope, key=key) or {}
        return {"created": False, "path": p, "record": prev}
    try:
        written = os.write(fd, raw)
        if written != len(raw):
            raise OSError(f"short write: {written}/{len(raw)} bytes")
    finally:
        os.close(fd)
    return {"created": True, "path": p, "record": body}


def claim_idempotency_record(
    base: Path,
    *,
    scope: str,
    key: str,
    payload: dict[str, Any] | None = None,
    stale_after_sec: int = 900,
) -> dict[str, Any]:
    """Claim an idempotency key without recording success prematurely."""
    p = _idempotency_path(base, scope=scope, key=key)
    p.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    body = {
        "idempotency_key": str(key),
        "scope": str(scope or "tool_execution"),
        "ok": False,
        "status": "in_progress",
        "updated_at_utc": now.isoformat(),
    }
    if isinstance(payload, dict):
        body.update(payload)
        body["ok"] = False
        body["status"] = str(body.get("status") or "in_progress")
    raw = (json.dumps(body, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        prev = read_idempotency_record(base, scope=scope, key=key) or {}
        status = str(prev.get("status") or "").strip().lower()
        if bool(prev.get("ok")) or status in {"success", "completed", "fetched"}:
            return {"claimed": False, "created": False, "stale": False, "path": p, "record": prev}

        stale = False
        pid_raw = prev.get("pid")
        try:
            pid = int(pid_raw)
            if pid > 0:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    stale = True
                except PermissionError:
                    stale = False
        except Exception:
            pass

        updated_raw = str(prev.get("updated_at_utc") or "").strip()
        if not stale:
            try:
                updated = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                stale = (now - updated.astimezone(timezone.utc)).total_seconds() >= max(1, int(stale_after_sec))
            except Exception:
                stale = True
        if not stale:
            return {"claimed": False, "created": False, "stale": False, "path": p, "record": prev}

        write_json(p, body)
        return {"claimed": True, "created": False, "stale": True, "path": p, "record": body}
    try:
        written = os.write(fd, raw)
        if written != len(raw):
            raise OSError(f"short write: {written}/{len(raw)} bytes")
    finally:
        os.close(fd)
    return {"claimed": True, "created": True, "stale": False, "path": p, "record": body}
