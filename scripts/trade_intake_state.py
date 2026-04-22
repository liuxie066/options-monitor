from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.io_utils import atomic_write_json, ensure_dir, read_json, utc_now


STATE_BUCKETS = ("processed_deal_ids", "failed_deal_ids", "unresolved_deal_ids")


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
    key = str(deal_id or "").strip()
    if not key or not isinstance(state, dict):
        return None
    for bucket_name in STATE_BUCKETS:
        bucket = state.get(bucket_name)
        if isinstance(bucket, dict) and isinstance(bucket.get(key), dict):
            return dict(bucket[key])
    return None


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
    cur[bucket][key] = item
    return cur


def append_trade_intake_audit(path: str | Path, payload: dict[str, Any]) -> Path:
    p = Path(path)
    ensure_dir(p.parent)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return p
