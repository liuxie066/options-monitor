from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.application.multi_tick.assistant_perception_event import (
    NOTIFICATION_PERCEPTION_EVENT_SCHEMA_VERSION,
    NOTIFICATION_PERCEPTION_EVENT_TYPE,
)


NOTIFICATION_PERCEPTION_READ_SCHEMA_VERSION = "notification_perception_read.v1"


def read_notification_perception_events(
    *,
    repo_root: Path,
    run_id: str | None = None,
    conversation_id: str | None = None,
    event_kind: str | None = None,
    limit: int = 10,
    audit_path: str | Path | None = None,
) -> dict[str, Any]:
    base = repo_root.resolve()
    paths = _audit_paths(base=base, run_id=run_id, audit_path=audit_path)
    rows: list[dict[str, Any]] = []
    read_statuses: list[dict[str, Any]] = []
    for path in paths:
        file_rows, read_status = _read_jsonl(path, base=base)
        rows.extend(file_rows)
        read_statuses.append(read_status)
    filtered = [
        row
        for row in rows
        if row.get("event_type") == NOTIFICATION_PERCEPTION_EVENT_TYPE
        and _matches_event(row, conversation_id=conversation_id, event_kind=event_kind)
    ]
    filtered.sort(key=lambda row: str(row.get("event_at_utc") or row.get("created_at_utc") or ""), reverse=True)
    max_rows = max(0, min(int(limit or 10), 50))
    events = [_public_event(row) for row in filtered[:max_rows]]
    malformed_count = sum(
        int(item.get("malformed_count") or 0)
        for item in read_statuses
    )
    unreadable_count = sum(
        1 for item in read_statuses if item.get("status") == "unreadable"
    )
    missing_count = sum(
        1 for item in read_statuses if item.get("status") == "missing"
    )
    if unreadable_count:
        read_status = "failed"
    elif malformed_count:
        read_status = "partial"
    elif missing_count == len(read_statuses):
        read_status = "missing"
    elif not rows:
        read_status = "valid_empty"
    else:
        read_status = "ok"
    return {
        "schema_version": NOTIFICATION_PERCEPTION_READ_SCHEMA_VERSION,
        "summary": {
            "ok": read_status not in {"failed", "partial"},
            "status": read_status,
            "total_count": len(filtered),
            "returned_count": len(events),
            "limit": max_rows,
            "run_id": str(run_id or "").strip() or None,
            "conversation_id": str(conversation_id or "").strip() or None,
            "event_kind": str(event_kind or "").strip() or None,
            "malformed_count": malformed_count,
            "unreadable_count": unreadable_count,
            "missing_count": missing_count,
        },
        "audit_paths": [str(path) for path in paths],
        "read_statuses": read_statuses,
        "events": events,
    }


def _audit_paths(*, base: Path, run_id: str | None, audit_path: str | Path | None) -> list[Path]:
    if audit_path:
        return [_resolve_path(audit_path, base=base)]
    if str(run_id or "").strip():
        runs_root = base / "output_runs"
        run_dir = runs_root / _safe_run_id(run_id)
        # 与 position_advice_runner / opend_symbol_outputs 先例一致：
        # containment 边界上的目录不允许是符号链接，避免经 resolve() 逃逸出仓。
        state_dir = run_dir / "state"
        if runs_root.is_symlink() or run_dir.is_symlink() or state_dir.is_symlink():
            raise ValueError("audit path must stay under output_runs")
        return [
            _resolve_path(
                state_dir / "audit_events.jsonl",
                base=base,
                containment=base / "output_runs",
            )
        ]
    shared_root = base / "output_shared"
    shared_state = shared_root / "state"
    if shared_root.is_symlink() or shared_state.is_symlink():
        raise ValueError("audit path must stay under output_shared")
    return [(shared_state / "audit_events.jsonl").resolve()]


def _safe_run_id(value: str | None) -> str:
    # run_id 作为单一路径组件校验；语义与 tick_run_workspace._identity_component 等价，
    # 在此独立实现以避免 reader 依赖 tick 编排模块。
    text = str(value or "").strip()
    if (
        not text
        or text in {".", ".."}
        or Path(text).name != text
        or "/" in text
        or "\\" in text
    ):
        raise ValueError("run_id is not a safe path component")
    return text


def _read_jsonl(
    path: Path,
    *,
    base: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    display_path = str(_display_path(path, base=base))
    if not path.exists() or not path.is_file():
        return [], {
            "path": display_path,
            "status": "missing",
            "line_count": 0,
            "parsed_count": 0,
            "malformed_count": 0,
        }
    out: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [], {
            "path": display_path,
            "status": "unreadable",
            "line_count": None,
            "parsed_count": 0,
            "malformed_count": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
    malformed_count = 0
    nonempty_count = 0
    for line in lines:
        if not line.strip():
            continue
        nonempty_count += 1
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            malformed_count += 1
            continue
        if not isinstance(payload, dict):
            malformed_count += 1
            continue
        payload["_source_path"] = display_path
        out.append(payload)
    return out, {
        "path": display_path,
        "status": (
            "partially_corrupt"
            if malformed_count
            else "valid_empty"
            if not out
            else "ok"
        ),
        "line_count": nonempty_count,
        "parsed_count": len(out),
        "malformed_count": malformed_count,
    }


def _matches_event(row: dict[str, Any], *, conversation_id: str | None, event_kind: str | None) -> bool:
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    wanted_conversation = str(conversation_id or "").strip()
    if wanted_conversation:
        scope = extra.get("conversation_scope") if isinstance(extra.get("conversation_scope"), dict) else {}
        if str(scope.get("conversation_id") or "").strip() != wanted_conversation:
            return False
    wanted_kind = str(event_kind or "").strip()
    if wanted_kind and str(extra.get("event_kind") or row.get("action") or "").strip() != wanted_kind:
        return False
    return True


def _public_event(row: dict[str, Any]) -> dict[str, Any]:
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    event = dict(extra)
    event.setdefault("schema_version", NOTIFICATION_PERCEPTION_EVENT_SCHEMA_VERSION)
    event.setdefault("event_type", NOTIFICATION_PERCEPTION_EVENT_TYPE)
    event.setdefault("event_kind", row.get("action"))
    event.setdefault("run_id", row.get("run_id"))
    event.setdefault("created_at_utc", row.get("event_at_utc"))
    if row.get("_source_path"):
        event["source_path"] = row.get("_source_path")
    return _strip_sensitive(event)


def _strip_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key).lower()
            if text_key in {"target", "webhook", "token", "secret", "raw_message", "message_text"}:
                continue
            out[key] = _strip_sensitive(item)
        return out
    if isinstance(value, list):
        return [_strip_sensitive(item) for item in value]
    return value


def _resolve_path(value: str | Path, *, base: Path, containment: Path | None = None) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    resolved = path.resolve()
    root = (containment or base).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        if containment is None:
            raise ValueError("audit_path must be under repo_root") from exc
        raise ValueError(f"audit path must stay under {root.name}") from exc
    return resolved


def _display_path(path: Path, *, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path.resolve())


__all__ = [
    "NOTIFICATION_PERCEPTION_READ_SCHEMA_VERSION",
    "read_notification_perception_events",
]
