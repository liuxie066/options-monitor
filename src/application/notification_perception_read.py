from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from src.application.multi_tick.assistant_perception_event import (
    NOTIFICATION_PERCEPTION_EVENT_SCHEMA_VERSION,
    NOTIFICATION_PERCEPTION_EVENT_TYPE,
)
from src.application.conversation_scope import conversation_reference


NOTIFICATION_PERCEPTION_READ_SCHEMA_VERSION = "notification_perception_read.v1"


def read_notification_perception_events(
    *,
    repo_root: Path,
    run_id: str | None = None,
    conversation_id: str | None = None,
    event_kind: str | None = None,
    limit: int = 10,
    audit_path: str | Path | None = None,
    cursor: str | None = None,
    deadline_monotonic: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    from src.application.agent_tools.project_reader import ProjectReaderError, decode_cursor, digest, set_continuation
    from src.application.agent_tool_contracts import AgentToolError

    base = repo_root.resolve()
    paths = _audit_paths(base=base, run_id=run_id, audit_path=audit_path)
    rows: list[dict[str, Any]] = []
    read_statuses: list[dict[str, Any]] = []
    for path in paths:
        file_rows, read_status = _read_jsonl(path, base=base, bounded=True,
            deadline_monotonic=deadline_monotonic, cancelled=cancelled)
        rows.extend(file_rows)
        read_statuses.append(read_status)
    visible = [row for row in rows if row.get("event_type") == NOTIFICATION_PERCEPTION_EVENT_TYPE
               and _matches_event(row, conversation_id=conversation_id, event_kind=None)
               and (not run_id or str(row.get("run_id") or "") == str(run_id).strip())]
    filtered = [row for row in visible if _matches_event(row, conversation_id=conversation_id, event_kind=event_kind)]
    filtered.sort(key=lambda row: str(row.get("event_at_utc") or row.get("created_at_utc") or ""), reverse=True)
    max_rows = max(1, min(int(limit or 10), 50))
    binding = digest({"tool": "notification_perception_read", "root": str(base),
        "paths": [str(path) for path in paths], "run_id": run_id, "conversation_id": conversation_id,
        "event_kind": event_kind, "limit": max_rows})
    source_hash = digest(read_statuses)
    try:
        state = decode_cursor(cursor, binding)
        if state and state.get("source_hash") != source_hash:
            raise ProjectReaderError("source_changed")
        offset = state.get("index", 0) if state else 0
        if type(offset) is not int or not 0 <= offset <= len(filtered):
            raise ProjectReaderError("cursor_invalidated")
        events = []
        for row in filtered[offset:offset + max_rows]:
            event = _public_event(row)
            if len(json.dumps([*events, event], ensure_ascii=False).encode()) > 6500:
                if not events:
                    raise ProjectReaderError("detail_too_large")
                break
            events.append(event)
    except ProjectReaderError as exc:
        raise AgentToolError(code="INPUT_ERROR", message=exc.code,
            hint="Repeat the query without cursor after a source change; narrow run_id or event_kind.",
            details={"reason": exc.code}) from None
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
    elif malformed_count or any(item.get("tail_truncated") for item in read_statuses):
        read_status = "partial"
    elif missing_count == len(read_statuses):
        read_status = "missing"
    elif not rows:
        read_status = "valid_empty"
    else:
        read_status = "ok"
    result = {
        "schema_version": NOTIFICATION_PERCEPTION_READ_SCHEMA_VERSION,
        "summary": {
            "ok": read_status not in {"failed", "partial"},
            "status": read_status,
            "total_count": len(filtered),
            "returned_count": len(events),
            "limit": max_rows,
            "run_id": str(run_id or "").strip() or None,
            "conversation_ref": conversation_reference(conversation_id),
            "event_kind": str(event_kind or "").strip() or None,
            "malformed_count": malformed_count,
            "unreadable_count": unreadable_count,
            "missing_count": missing_count,
        },
        "audit_paths": [str(path) for path in paths],
        "read_statuses": read_statuses,
        "events": events,
    }
    complete = read_status in {"ok", "valid_empty"}
    matched_count = len(filtered) if complete else None
    has_more = offset + len(events) < len(filtered)
    result["summary"]["total_count"] = matched_count
    result["source_hash"] = source_hash
    result["scope"] = {"run_id": run_id, "conversation_ref": conversation_reference(conversation_id),
        "event_kind": event_kind, "source_hash": source_hash, "page_range": {"start": offset, "end": offset + len(events)}}
    result["pagination"] = {"total_count": len(visible) if complete else None,
        "matched_count": matched_count, "returned_count": len(events),
        "scanned_count": len(visible), "has_more": has_more}
    result["coverage"] = {"status": "complete" if complete else "partial" if events else "unknown",
        "complete_for": "requested_page", "scope": result["scope"], "included_count": len(events), "total_count": matched_count,
        "omitted_count": matched_count - len(events) if matched_count is not None else None, "has_more": has_more}
    set_continuation(result, {"binding": binding, "source_hash": source_hash,
        "index": offset + len(events)} if has_more else None)
    if conversation_id:
        # A scoped reader must not expose other conversations' source row counts.
        for status in read_statuses:
            status["line_count"] = None
            status["parsed_count"] = None
    return result


def iter_notification_perception_events(
    *,
    repo_root: Path,
    conversation_id: str | None = None,
    event_kind: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Read shared notification perception events for internal resolution.

    Unlike ``read_notification_perception_events`` (a public tool surface capped
    at 50 rows), this helper is for internal consumers such as run resolution
    that must scan beyond the public preview window. The scan remains bounded:
    ``limit`` defaults to 5000 and may not exceed 5000.

    Returns events, total_count, truncated and read_statuses/summary so callers
    can distinguish no match, a bounded-window gap and damaged audit evidence.
    """

    base = repo_root.resolve()
    paths = _audit_paths(base=base, run_id=None, audit_path=None)
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
    max_rows = max(0, min(int(limit if limit is not None else 5000), 5000))
    events = [_public_event(row) for row in filtered[:max_rows]]
    malformed = sum(item["malformed_count"] for item in read_statuses)
    unreadable = sum(item["status"] == "unreadable" for item in read_statuses)
    missing = sum(item["status"] == "missing" for item in read_statuses)
    return {
        "events": events,
        "total_count": len(filtered),
        "truncated": len(filtered) > len(events),
        "read_statuses": read_statuses,
        "summary": {
            "status": "failed" if unreadable else "partial" if malformed else "missing" if missing else "ok",
            "malformed_count": malformed,
            "unreadable_count": unreadable,
            "missing_count": missing,
        },
    }


def _audit_paths(*, base: Path, run_id: str | None, audit_path: str | Path | None) -> list[Path]:
    if audit_path:
        return [_resolve_path(audit_path, base=base)]
    if str(run_id or "").strip():
        runs_root = base / "output_runs"
        run_dir = runs_root / _safe_run_id(run_id)
        # 与其他运行时 artifact reader 一致：
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
    return [shared_state / "audit_events.jsonl"]


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
    bounded: bool = False,
    deadline_monotonic: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    display_path = str(_display_path(path, base=base))
    source_hash = None
    out: list[dict[str, Any]] = []
    try:
        if bounded:
            from src.application.agent_tools.project_reader import ProjectReaderError, _check, read_tail_bytes
            try:
                raw, tail_truncated, source_size = read_tail_bytes(base, str(path.relative_to(base)),
                    deadline_monotonic=deadline_monotonic, cancelled=cancelled)
            except ProjectReaderError as exc:
                if exc.code in {"cancelled", "time_deadline"}:
                    raise
                return [], {"path": display_path, "status": "missing" if exc.code == "not_found" else "unreadable",
                    "line_count": None, "parsed_count": 0, "malformed_count": 0, "reason": exc.code}
            source_hash = hashlib.sha256(raw).hexdigest()
            if tail_truncated:
                raw = raw.partition(b"\n")[2]
            lines = raw.decode("utf-8").splitlines()
        else:
            # Existing internal notification-run resolution keeps its original scan contract.
            lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return [], {"path": display_path, "status": "missing", "line_count": 0,
            "parsed_count": 0, "malformed_count": 0}
    except (OSError, UnicodeDecodeError) as exc:
        return [], {"path": display_path, "status": "unreadable", "line_count": None,
            "parsed_count": 0, "malformed_count": 0, "reason": type(exc).__name__}
    malformed_count = 0
    nonempty_count = 0
    for line in lines:
        if bounded:
            _check(deadline_monotonic, cancelled)
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
            else "tail_only"
            if bounded and tail_truncated
            else "valid_empty"
            if not out
            else "ok"
        ),
        "source_hash": source_hash,
        "tail_truncated": bool(bounded and tail_truncated),
        "size_bytes": source_size if bounded else None,
        "line_count": nonempty_count,
        "parsed_count": len(out),
        "malformed_count": malformed_count,
    }


def _matches_event(row: dict[str, Any], *, conversation_id: str | None, event_kind: str | None) -> bool:
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    if row.get("run_id") and extra.get("run_id") and row["run_id"] != extra["run_id"]:
        return False
    wanted_conversation = str(conversation_id or "").strip()
    if wanted_conversation:
        scope = extra.get("conversation_scope") if isinstance(extra.get("conversation_scope"), dict) else {}
        stored_ref = str(scope.get("conversation_ref") or "").strip()
        legacy_id = str(scope.get("conversation_id") or "").strip()
        if not stored_ref and not legacy_id:
            return False
        if stored_ref and stored_ref != conversation_reference(wanted_conversation):
            return False
        if legacy_id and legacy_id != wanted_conversation:
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
            if text_key in {
                "target",
                "webhook",
                "token",
                "secret",
                "raw_message",
                "message_text",
                "sender_id",
                "conversation_id",
                "to_user_id",
                "group_id",
                "chat_key",
            }:
                continue
            out[key] = _strip_sensitive(item)
        return out
    if isinstance(value, list):
        return [_strip_sensitive(item) for item in value]
    return value


def _resolve_path(value: str | Path, *, base: Path, containment: Path | None = None) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    resolved = path.absolute()
    root = (containment or base).resolve()
    try:
        if ".." in resolved.relative_to(root).parts:
            raise ValueError("audit path contains parent traversal")
    except ValueError as exc:
        if containment is None:
            raise ValueError("audit_path must be under repo_root") from exc
        raise ValueError(f"audit path must stay under {root.name}") from exc
    return resolved


def _display_path(path: Path, *, base: Path) -> str:
    try:
        return str(path.absolute().relative_to(base.resolve()))
    except ValueError:
        return str(path.absolute())


__all__ = [
    "NOTIFICATION_PERCEPTION_READ_SCHEMA_VERSION",
    "iter_notification_perception_events",
    "read_notification_perception_events",
]
