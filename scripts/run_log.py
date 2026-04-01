#!/usr/bin/env python3
"""Minimal JSONL run logger for checkpoint-style events."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_run_id() -> str:
    dt = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # Avoid os entropy blocking in constrained containers.
    suffix = f"{(time.time_ns() ^ os.getpid()) & 0xFFFFFF:06x}"
    return f"{dt}-{suffix}"


def _truncate_text(v: str, n: int) -> str:
    if len(v) <= n:
        return v
    return v[: max(0, n - 3)] + "..."


def _compact_scalar(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, str):
        return _truncate_text(v, 240)
    return _truncate_text(str(v), 240)


def _compact_data(data: Any, max_chars: int = 1200) -> dict[str, Any]:
    if data is None:
        return {}

    if not isinstance(data, dict):
        return {"value": _compact_scalar(data)}

    out: dict[str, Any] = {}
    # Keep event data intentionally small and shallow.
    for i, (k, v) in enumerate(data.items()):
        if i >= 20:
            out["_truncated_keys"] = True
            break

        key = _truncate_text(str(k), 60)
        if isinstance(v, dict):
            # keep one-level summary only
            out[key] = {
                "_type": "dict",
                "keys": list(v.keys())[:8],
                "size": len(v),
            }
        elif isinstance(v, (list, tuple)):
            sample = [_compact_scalar(x) for x in list(v)[:8]]
            out[key] = {
                "_type": "list",
                "size": len(v),
                "sample": sample,
            }
        else:
            out[key] = _compact_scalar(v)

    # Enforce payload budget.
    # Enforce payload budget.
    # IMPORTANT: avoid infinite loop when we always add a marker key.
    truncated_marked = False
    for _ in range(200):
        s = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
        if len(s) <= max_chars:
            return out
        if not out:
            return {}

        # Drop one key (prefer dropping non-marker keys).
        keys = [k for k in out.keys() if k not in ("_truncated", "_truncated_keys")]
        if keys:
            out.pop(keys[-1], None)
        else:
            # Nothing meaningful to drop; return a minimal marker.
            return {"_truncated": True}

        if not truncated_marked:
            out["_truncated"] = True
            truncated_marked = True

    # Failsafe
    return {"_truncated": True}


class RunLogger:
    def __init__(self, base_dir: Path, run_id: str | None = None, logs_rel_dir: str = "audit/run_logs") -> None:
        self.base_dir = Path(base_dir).resolve()
        self.run_id = run_id or create_run_id()
        self.logs_dir = (self.base_dir / logs_rel_dir).resolve()
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.log_path = self.logs_dir / f"{day}.jsonl"

    def event(
        self,
        step: str,
        status: str,
        *,
        duration_ms: int | None = None,
        error_code: str | None = None,
        message: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        rec: dict[str, Any] = {
            "ts": _utc_ts(),
            "run_id": self.run_id,
            "step": str(step),
            "status": str(status),
        }
        if duration_ms is not None:
            rec["duration_ms"] = int(duration_ms)
        if error_code:
            rec["error_code"] = _truncate_text(str(error_code), 80)
        if message:
            rec["message"] = _truncate_text(str(message), 500)
        if data:
            rec["data"] = _compact_data(data)

        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            # Logging must never break business flow.
            pass
