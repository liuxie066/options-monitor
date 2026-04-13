from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any


SCHEMA_VERSION_V1 = "1.0"

SCHEMA_KIND_TOOL_EXECUTION = "tool_execution"
SCHEMA_KIND_SCHEDULER_DECISION = "scheduler_decision"
SCHEMA_KIND_SUBPROCESS_ADAPTER = "subprocess_adapter"

ALLOWED_TOOL_STATUS = {"cached", "fetched", "error", "skipped"}
ALLOWED_SUBPROCESS_STATUS = {"ok", "error"}


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


def resolve_notify_window_open(
    src: dict[str, Any] | Any,
    *,
    default: bool = False,
) -> bool:
    payload = src if isinstance(src, dict) else {}
    if "is_notify_window_open" in payload:
        return bool(payload.get("is_notify_window_open"))
    if "should_notify" in payload:
        return bool(payload.get("should_notify"))
    return bool(default)


def normalize_notify_window_aliases(
    src: dict[str, Any] | Any,
    *,
    default: bool = False,
) -> dict[str, Any]:
    payload = dict(src) if isinstance(src, dict) else {}
    if "is_notify_window_open" in payload:
        payload["is_notify_window_open"] = bool(payload.get("is_notify_window_open"))
        return payload
    if "should_notify" in payload:
        payload["is_notify_window_open"] = bool(payload.get("should_notify"))
        return payload
    payload["is_notify_window_open"] = bool(default)
    return payload


def normalize_scheduler_decision_payload(raw: dict[str, Any] | Any) -> dict[str, Any]:
    src = normalize_notify_window_aliases(raw)
    out = {
        "schema_kind": SCHEMA_KIND_SCHEDULER_DECISION,
        "schema_version": SCHEMA_VERSION_V1,
        "should_run_scan": bool(src.get("should_run_scan")),
        "is_notify_window_open": bool(src.get("is_notify_window_open")),
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


def _tail_text(raw: Any, *, max_lines: int = 60, max_chars: int = 4000) -> str:
    txt = str(raw or "").strip()
    if not txt:
        return ""
    lines = txt.splitlines()
    tail = "\n".join(lines[-max_lines:])
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail


def _extract_last_json_obj(raw: str) -> dict[str, Any] | None:
    txt = str(raw or "").strip()
    s = txt.find("{")
    e = txt.rfind("}")
    if s < 0 or e <= s:
        return None
    try:
        obj = json.loads(txt[s : e + 1])
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def normalize_subprocess_adapter_payload(
    *,
    adapter: str,
    tool_name: str,
    returncode: int | None,
    stdout: str | None,
    stderr: str | None,
    ok: bool | None = None,
    message: str = "",
    started_at_utc: str | None = None,
    finished_at_utc: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_ok = bool(ok) if ok is not None else (int(returncode or 0) == 0)
    status = "ok" if resolved_ok else "error"
    if status not in ALLOWED_SUBPROCESS_STATUS:
        status = "error"

    out = {
        "schema_kind": SCHEMA_KIND_SUBPROCESS_ADAPTER,
        "schema_version": SCHEMA_VERSION_V1,
        "adapter": str(adapter or "").strip().lower(),
        "tool_name": str(tool_name or "").strip(),
        "status": status,
        "ok": resolved_ok,
        "returncode": (None if returncode is None else int(returncode)),
        "message": str(message or ""),
        "stdout_tail": _tail_text(stdout),
        "stderr_tail": _tail_text(stderr),
        "started_at_utc": str(started_at_utc or _utc_now_iso()),
        "finished_at_utc": str(finished_at_utc or _utc_now_iso()),
    }
    if isinstance(extra, dict):
        for k, v in extra.items():
            if k not in out:
                out[k] = v
    return validate_schema_payload(out, kind=SCHEMA_KIND_SUBPROCESS_ADAPTER)


def normalize_watchdog_subprocess_output(*, returncode: int, stdout: str = "", stderr: str = "") -> dict[str, Any]:
    merged = ((stdout or "") + "\n" + (stderr or "")).strip()
    obj = _extract_last_json_obj(merged) or {}
    ok = bool(obj.get("ok")) if obj else (int(returncode) == 0)
    message = str(obj.get("message") or obj.get("error") or "").strip()
    return normalize_subprocess_adapter_payload(
        adapter="watchdog",
        tool_name="opend_watchdog",
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        ok=ok,
        message=message,
        extra={"watchdog_payload": obj if obj else None},
    )


def normalize_pipeline_subprocess_output(*, returncode: int, stdout: str = "", stderr: str = "") -> dict[str, Any]:
    msg_src = (stderr or stdout or "").strip()
    message = msg_src.splitlines()[-1] if msg_src else ""
    return normalize_subprocess_adapter_payload(
        adapter="pipeline",
        tool_name="run_pipeline",
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        message=message,
    )


def normalize_notify_subprocess_output(*, returncode: int, stdout: str = "", stderr: str = "") -> dict[str, Any]:
    obj = _extract_last_json_obj(stdout or "") or {}
    msg_id = obj.get("messageId")
    if msg_id is None and isinstance(obj.get("result"), dict):
        msg_id = obj.get("result", {}).get("messageId")
    message = str(stderr or "").strip()
    if not message and msg_id:
        message = f"message_id={msg_id}"
    return normalize_subprocess_adapter_payload(
        adapter="notify",
        tool_name="openclaw_message_send",
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        message=message,
        extra={"message_id": msg_id},
    )
