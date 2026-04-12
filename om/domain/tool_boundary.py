from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any


SCHEMA_VERSION_V1 = "1.0"

SCHEMA_KIND_TOOL_EXECUTION = "tool_execution"
SCHEMA_KIND_SCHEDULER_DECISION = "scheduler_decision"

ALLOWED_TOOL_STATUS = {"cached", "fetched", "error", "skipped"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_schema_payload(payload: dict[str, Any], *, kind: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("schema payload must be a dict")
    if str(payload.get("schema_kind") or "") != str(kind):
        raise ValueError(f"schema_kind must be {kind}")
    if str(payload.get("schema_version") or "") != SCHEMA_VERSION_V1:
        raise ValueError(f"unsupported schema_version: {payload.get('schema_version')}")
    return payload


def normalize_scheduler_decision_payload(raw: dict[str, Any] | Any) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    out = {
        "schema_kind": SCHEMA_KIND_SCHEDULER_DECISION,
        "schema_version": SCHEMA_VERSION_V1,
        "should_run_scan": bool(src.get("should_run_scan")),
        "is_notify_window_open": bool(src.get("is_notify_window_open", src.get("should_notify"))),
        "reason": str(src.get("reason") or ""),
    }
    for key in (
        "now_utc",
        "next_run_utc",
        "in_market_hours",
        "interval_min",
        "notify_cooldown_min",
        "should_notify",
    ):
        if key in src:
            out[key] = src.get(key)
    return validate_schema_payload(out, kind=SCHEMA_KIND_SCHEDULER_DECISION)


def build_tool_idempotency_key(*, tool_name: str, symbol: str, source: str, limit_exp: int) -> str:
    raw = f"{tool_name}|{symbol.strip().upper()}|{source.strip().lower()}|{int(limit_exp)}"
    return sha256(raw.encode("utf-8")).hexdigest()


def normalize_tool_execution_payload(
    *,
    tool_name: str,
    symbol: str,
    source: str,
    limit_exp: int,
    status: str,
    ok: bool,
    message: str,
    returncode: int | None = None,
    idempotency_key: str | None = None,
    started_at_utc: str | None = None,
    finished_at_utc: str | None = None,
) -> dict[str, Any]:
    status_norm = str(status or "").strip().lower() or "error"
    if status_norm not in ALLOWED_TOOL_STATUS:
        status_norm = "error"

    key = idempotency_key or build_tool_idempotency_key(
        tool_name=tool_name,
        symbol=symbol,
        source=source,
        limit_exp=limit_exp,
    )

    out = {
        "schema_kind": SCHEMA_KIND_TOOL_EXECUTION,
        "schema_version": SCHEMA_VERSION_V1,
        "tool_name": str(tool_name or "").strip(),
        "symbol": str(symbol or "").strip().upper(),
        "source": str(source or "").strip().lower(),
        "limit_exp": int(limit_exp),
        "idempotency_key": str(key),
        "status": status_norm,
        "ok": bool(ok),
        "message": str(message or ""),
        "returncode": (None if returncode is None else int(returncode)),
        "started_at_utc": str(started_at_utc or _utc_now_iso()),
        "finished_at_utc": str(finished_at_utc or _utc_now_iso()),
    }
    return validate_schema_payload(out, kind=SCHEMA_KIND_TOOL_EXECUTION)
