from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from om.storage import paths
from om.storage.repositories import run_repo
from scripts.io_utils import atomic_write_json as write_json
from scripts.io_utils import read_json


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
    out = run_state_dir(base, run_id) / "scheduler_decision.json"
    write_json(out, payload)
    return out


def write_tick_metrics(base: Path, run_id: str, payload: dict[str, Any]) -> dict[str, Path]:
    sdir = shared_state_dir(base)
    rdir = run_state_dir(base, run_id)
    p_shared = (sdir / "tick_metrics.json").resolve()
    p_run = (rdir / "tick_metrics.json").resolve()
    write_json(p_shared, payload)
    write_json(p_run, payload)
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
    return out


def write_shared_state(base: Path, name: str, payload: dict[str, Any]) -> Path:
    out = (shared_state_dir(base) / str(name)).resolve()
    write_json(out, payload)
    return out


def write_account_last_run(base: Path, account: str, payload: dict[str, Any]) -> Path:
    out = (account_state_dir(base, account) / "last_run.json").resolve()
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


def append_tool_execution_audit(base: Path, payload: dict[str, Any], *, run_id: str | None = None) -> dict[str, Path]:
    out: dict[str, Path] = {
        "shared": append_shared_audit_jsonl(base, "tool_execution_audit.jsonl", payload),
    }
    if run_id:
        out["run"] = append_run_audit_jsonl(base, run_id, "tool_execution_audit.jsonl", payload)
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
        os.write(fd, raw)
    finally:
        os.close(fd)
    return {"created": True, "path": p, "record": body}


def query_tool_execution_audit(
    base: Path,
    *,
    run_id: str | None = None,
    limit: int = 200,
    status: str | None = None,
    tool_name: str | None = None,
    since_utc: str | None = None,
) -> list[dict[str, Any]]:
    path = (
        (run_state_dir(base, run_id) / "tool_execution_audit.jsonl").resolve()
        if run_id
        else (shared_state_dir(base) / "tool_execution_audit.jsonl").resolve()
    )
    if not path.exists():
        return []

    since_dt: datetime | None = None
    if since_utc:
        try:
            since_dt = datetime.fromisoformat(str(since_utc).replace("Z", "+00:00"))
        except Exception:
            since_dt = None

    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for ln in f:
            txt = ln.strip()
            if not txt:
                continue
            try:
                row = json.loads(txt)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            if status and str(row.get("status") or "") != str(status):
                continue
            if tool_name and str(row.get("tool_name") or "") != str(tool_name):
                continue
            if since_dt is not None:
                ts = str(row.get("finished_at_utc") or row.get("updated_at_utc") or "")
                try:
                    row_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except Exception:
                    row_dt = None
                if row_dt is None or row_dt < since_dt:
                    continue
            out.append(row)

    if limit > 0:
        out = out[-int(limit) :]
    return out


def apply_tool_execution_audit_retention(
    base: Path,
    *,
    run_id: str | None = None,
    max_lines: int = 20000,
    max_age_days: int = 30,
) -> dict[str, Any]:
    """Conservative retention scaffold for tool_execution_audit.jsonl."""
    path = (
        (run_state_dir(base, run_id) / "tool_execution_audit.jsonl").resolve()
        if run_id
        else (shared_state_dir(base) / "tool_execution_audit.jsonl").resolve()
    )
    if not path.exists():
        return {"path": path, "kept": 0, "dropped": 0, "rewritten": False}

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(max_age_days)))
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for ln in f:
            txt = ln.strip()
            if not txt:
                continue
            try:
                row = json.loads(txt)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            ts = str(row.get("finished_at_utc") or row.get("updated_at_utc") or "")
            keep = False
            try:
                row_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                keep = row_dt >= cutoff
            except Exception:
                keep = True
            if keep:
                rows.append(row)

    if max_lines > 0:
        rows = rows[-int(max_lines) :]

    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    old = path.read_text(encoding="utf-8", errors="replace")
    rewritten = body != old
    if rewritten:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(path)

    old_lines = len([x for x in old.splitlines() if x.strip()])
    return {
        "path": path,
        "kept": len(rows),
        "dropped": max(0, old_lines - len(rows)),
        "rewritten": rewritten,
        "max_lines": int(max_lines),
        "max_age_days": int(max_age_days),
    }
