from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any

from src.application.ledger.api import (
    LIFECYCLE_ATTEMPT_RUN_SEAL_SCHEMA,
    build_lifecycle_attempt_run_seal,
    validate_lifecycle_attempt_run_seal,
)
from src.infrastructure.io_utils import atomic_write_json, ensure_dir, read_json, utc_now


STATE_BUCKETS = ("processed_deal_ids", "failed_deal_ids", "unresolved_deal_ids")
_AUDIT_TAIL_SCAN_BYTES = 64 * 1024


def empty_trade_intake_state() -> dict[str, Any]:
    return {name: {} for name in STATE_BUCKETS}


def load_trade_intake_state(path: str | Path) -> dict[str, Any]:
    raw = read_json(path, default={})
    if not isinstance(raw, dict):
        return empty_trade_intake_state()
    out = empty_trade_intake_state()
    for key in STATE_BUCKETS:
        bucket = raw.get(key)
        out[key] = dict(bucket) if isinstance(bucket, dict) else {}
    return out


def write_trade_intake_state(path: str | Path, state: dict[str, Any]) -> Path:
    p = Path(path)
    ensure_dir(p.parent)
    body = empty_trade_intake_state()
    if isinstance(state, dict):
        for key in STATE_BUCKETS:
            body[key] = dict(state.get(key) or {})
    atomic_write_json(p, body)
    return p


def lookup_deal_state(state: dict[str, Any] | None, deal_id: str | None) -> dict[str, Any] | None:
    item = lookup_deal_state_entry(state, deal_id)
    if item is None:
        return None
    _bucket, payload = item
    return payload


def lookup_deal_state_entry(state: dict[str, Any] | None, deal_id: str | None) -> tuple[str, dict[str, Any]] | None:
    key = str(deal_id or "").strip()
    if not key or not isinstance(state, dict):
        return None
    for bucket_name in STATE_BUCKETS:
        bucket = state.get(bucket_name)
        if isinstance(bucket, dict) and isinstance(bucket.get(key), dict):
            return bucket_name, dict(bucket[key])
    return None


def is_retryable_unresolved_deal(state: dict[str, Any] | None, deal_id: str | None) -> bool:
    item = lookup_deal_state_entry(state, deal_id)
    if item is None:
        return False
    bucket, payload = item
    return bucket == "unresolved_deal_ids" and str(payload.get("status") or "").strip().lower() == "unresolved" and bool(payload.get("retryable"))


def is_failed_deal(state: dict[str, Any] | None, deal_id: str | None) -> bool:
    item = lookup_deal_state_entry(state, deal_id)
    if item is None:
        return False
    bucket, payload = item
    return bucket == "failed_deal_ids" and str(payload.get("status") or "").strip().lower() == "failed"


def upsert_deal_state(
    state: dict[str, Any] | None,
    *,
    bucket: str,
    deal_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if bucket not in STATE_BUCKETS:
        raise ValueError(f"unknown state bucket: {bucket}")
    key = str(deal_id or "").strip()
    if not key:
        raise ValueError("deal_id is required")
    cur = empty_trade_intake_state()
    if isinstance(state, dict):
        for name in STATE_BUCKETS:
            cur[name] = dict(state.get(name) or {})
    item = dict(payload or {})
    item.setdefault("updated_at", utc_now())
    for name in STATE_BUCKETS:
        cur[name].pop(key, None)
    cur[bucket][key] = item
    return cur


def append_trade_intake_audit(
    path: str | Path,
    payload: dict[str, Any],
    *,
    durable: bool = False,
) -> Path:
    p = Path(path)
    ensure_dir(p.parent)
    line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    with p.open("a+b", buffering=0) as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            if durable:
                _truncate_torn_audit_tail(handle)
            else:
                handle.seek(0, os.SEEK_END)
                if handle.tell():
                    handle.seek(-1, os.SEEK_END)
                    if handle.read(1) != b"\n":
                        raise OSError("trade intake audit has an unterminated tail")
            if handle.write(line) != len(line):
                raise OSError("trade intake audit append was incomplete")
            if durable:
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return p


def append_lifecycle_attempt_checkpoint_seal(
    path: str | Path,
    repo: Any,
    *,
    account: str,
    source_id: str,
    completed_at_ms: int,
    reason: str,
) -> dict[str, Any]:
    seal = build_lifecycle_attempt_run_seal(
        account=account,
        source_id=source_id,
        completed_at_ms=completed_at_ms,
        heads=repo.list_trade_lifecycle_attempt_audit_heads_for_account(
            account=account,
        ),
        seal_scope="all_heads_checkpoint",
        reason=reason,
    )
    append_trade_intake_audit(path, seal, durable=True)
    return seal


def _truncate_torn_audit_tail(handle: Any) -> None:
    handle.seek(0, os.SEEK_END)
    end = handle.tell()
    if end == 0:
        return
    handle.seek(end - 1)
    if handle.read(1) == b"\n":
        handle.seek(0, os.SEEK_END)
        return
    scan_end = end
    while scan_end:
        start = max(0, scan_end - _AUDIT_TAIL_SCAN_BYTES)
        handle.seek(start)
        newline = handle.read(scan_end - start).rfind(b"\n")
        if newline >= 0:
            handle.truncate(start + newline + 1)
            break
        scan_end = start
    else:
        handle.truncate(0)
    handle.seek(0, os.SEEK_END)


def read_latest_lifecycle_attempt_run_seal(
    path: str | Path,
    *,
    account: str | None = None,
    source_id: str | None = None,
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower() or None
    source_value = str(source_id or "").strip() or None
    latest: dict[str, Any] | None = None
    seal_count = 0
    torn_tail_ignored = False
    try:
        handle = Path(path).open("rb")
    except FileNotFoundError:
        handle = None
    if handle is not None:
        with handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                for line_number, raw_line in enumerate(handle, start=1):
                    terminated = raw_line.endswith(b"\n")
                    try:
                        payload = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        if not terminated:
                            torn_tail_ignored = True
                            break
                        raise ValueError(
                            f"malformed trade intake audit line {line_number}"
                        ) from exc
                    if not terminated:
                        raise ValueError(
                            f"unterminated trade intake audit line {line_number}"
                        )
                    if type(payload) is not dict:
                        raise ValueError(
                            f"trade intake audit line {line_number} must be an object"
                        )
                    if payload.get("schema_version") != LIFECYCLE_ATTEMPT_RUN_SEAL_SCHEMA:
                        continue
                    seal = validate_lifecycle_attempt_run_seal(payload)
                    seal_count += 1
                    if account_value is not None and seal["account"] != account_value:
                        continue
                    if source_value is not None and seal["source_id"] != source_value:
                        continue
                    latest = seal
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return {
        "schema_version": "trade_lifecycle_attempt_run_seal_reader.v1",
        "seal_count": seal_count,
        "last_seal": latest,
        "torn_tail_ignored": torn_tail_ignored,
    }
