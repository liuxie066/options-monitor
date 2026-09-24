from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timezone
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
    start_utc: str | None = None,
    end_utc: str | None = None,
) -> dict[str, Any]:
    from src.application.agent_tools.project_reader import ProjectReaderError, decode_cursor, digest, set_continuation
    from src.application.agent_tool_contracts import AgentToolError

    base = repo_root.resolve()
    if start_utc or end_utc:
        return _read_window(
            base=base, run_id=run_id, conversation_id=conversation_id,
            event_kind=event_kind, limit=limit, audit_path=audit_path, cursor=cursor,
            start_utc=start_utc, end_utc=end_utc,
            deadline_monotonic=deadline_monotonic, cancelled=cancelled,
        )
    paths = _audit_paths(base=base, run_id=run_id, audit_path=audit_path)
    rows: list[dict[str, Any]] = []
    read_statuses: list[dict[str, Any]] = []
    for path in paths:
        file_rows, read_status = _read_jsonl(path, base=base,
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
        "truncated": len(filtered) > len(events) or any(item.get("tail_truncated") for item in read_statuses),
        "read_statuses": read_statuses,
        "summary": {
            "status": "failed" if unreadable else "partial" if malformed or any(item.get("tail_truncated") for item in read_statuses) else "missing" if missing else "ok",
            "malformed_count": malformed,
            "unreadable_count": unreadable,
            "missing_count": missing,
        },
    }


def _read_window(
    *, base: Path, run_id: str | None, conversation_id: str | None,
    event_kind: str | None, limit: int, audit_path: str | Path | None,
    cursor: str | None, start_utc: str | None, end_utc: str | None,
    deadline_monotonic: float | None, cancelled: Callable[[], bool] | None,
) -> dict[str, Any]:
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.agent_tools.project_reader import (
        ProjectReaderError, _check, _directory, decode_cursor, digest, set_continuation,
    )

    try:
        if not start_utc or not end_utc:
            raise ValueError("both start_utc and end_utc are required")
        start = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_utc.replace("Z", "+00:00"))
        if start.tzinfo is None or end.tzinfo is None or start > end:
            raise ValueError("invalid UTC time window")
        start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    except ValueError as exc:
        raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from None
    current = _audit_paths(base=base, run_id=run_id, audit_path=audit_path)[0]
    paths = [current]
    first_segment_at: datetime | None = None
    if not run_id and audit_path is None and current.parent.is_dir() and not current.parent.is_symlink():
        segments = sorted(current.parent.glob("audit_events.????????.??????.jsonl"))
        paths = segments + paths
        if segments:
            try:
                first_segment_at = datetime.strptime(segments[0].name.split(".")[1], "%Y%m%d").replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    if len(paths) > 64:
        paths = paths[-64:]
        omitted_segments = True
    else:
        omitted_segments = False
    max_rows = max(1, min(int(limit or 10), 50))
    binding = digest({"tool": "notification_perception_read.window", "root": str(base),
        "run_id": run_id, "conversation_id": conversation_id, "event_kind": event_kind,
        "start_utc": start.isoformat(), "end_utc": end.isoformat(), "limit": max_rows})
    try:
        state = decode_cursor(cursor, binding)
        offset = state.get("index", 0) if state else 0
        if type(offset) is not int or not 0 <= offset <= 5000:
            raise ProjectReaderError("cursor_invalidated")
    except ProjectReaderError as exc:
        raise AgentToolError(code="INPUT_ERROR", message=exc.code, details={"reason": exc.code}) from None

    # ponytail: one bounded sequential scan; add an index only if historical queries need >64 MiB.
    budget = 64 * 1024 * 1024
    read_statuses: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    top: list[tuple[float, int, dict[str, Any]]] = []
    scanned_bytes = matched = malformed = 0
    stop_reason: str | None = "segments_omitted" if omitted_segments else (
        "history_before_first_segment" if first_segment_at and start < first_segment_at else None
    )
    seq = 0
    for path in paths:
        _check(deadline_monotonic, cancelled)
        display = str(_display_path(path, base=base))
        if path.is_symlink():
            read_statuses.append({"path": display, "status": "unreadable", "reason": "symlink"})
            stop_reason = stop_reason or "unreadable"
            continue
        parent_fd = None
        try:
            relative = path.relative_to(base)
            parent_fd = _directory(base, str(relative.parent) if relative.parent != Path(".") else "",
                                   deadline_monotonic, cancelled)
            fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent_fd)
        except FileNotFoundError:
            read_statuses.append({"path": display, "status": "missing"})
            if state:
                raise AgentToolError(code="INPUT_ERROR", message="source_changed") from None
            continue
        except ProjectReaderError as exc:
            if exc.code in {"cancelled", "time_deadline"}:
                raise
            read_statuses.append({"path": display, "status": "missing" if exc.code == "not_found" else "unreadable"})
            stop_reason = stop_reason or ("missing" if exc.code == "not_found" else "unreadable")
            continue
        except (OSError, ValueError):
            read_statuses.append({"path": display, "status": "unreadable"})
            stop_reason = stop_reason or "unreadable"
            continue
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                read_statuses.append({"path": display, "status": "unreadable", "reason": "unsupported"})
                stop_reason = stop_reason or "unreadable"
                continue
            size = info.st_size
            head = os.pread(fd, min(4096, size), 0)
            tail = os.pread(fd, min(4096, size), max(0, size - 4096))
            snapshot = {"path": display, "dev": info.st_dev, "ino": info.st_ino,
                "size": size, "edge": hashlib.sha256(head + tail).hexdigest()}
            if state:
                previous = next((item for item in state.get("sources", []) if item.get("path") == display), None)
                if previous is None or previous["dev"] != info.st_dev or previous["ino"] != info.st_ino or size < previous["size"]:
                    raise AgentToolError(code="INPUT_ERROR", message="source_changed") from None
                old_size = previous["size"]
                old_edge = hashlib.sha256(os.pread(fd, min(4096, old_size), 0)
                    + os.pread(fd, min(4096, old_size), max(0, old_size - 4096))).hexdigest()
                if old_edge != previous["edge"]:
                    raise AgentToolError(code="INPUT_ERROR", message="source_changed") from None
                size = old_size
                snapshot = previous
            snapshots.append(snapshot)
            line_count = 0
            while handle.tell() < size:
                _check(deadline_monotonic, cancelled)
                remaining = min(size - handle.tell(), budget - scanned_bytes)
                if remaining <= 0:
                    stop_reason = stop_reason or "scan_budget"
                    break
                raw = handle.readline(min(remaining, 1024 * 1024 + 1))
                scanned_bytes += len(raw)
                if len(raw) > 1024 * 1024:
                    stop_reason = stop_reason or "line_too_large"
                    break
                if not raw.endswith(b"\n"):
                    stop_reason = stop_reason or "incomplete_line"
                    break
                line_count += 1
                try:
                    row = json.loads(raw)
                    if not isinstance(row, dict):
                        raise ValueError("not an object")
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    malformed += 1
                    continue
                try:
                    at = datetime.fromisoformat(str(row.get("event_at_utc") or row.get("created_at_utc") or "").replace("Z", "+00:00"))
                    if at.tzinfo is None:
                        raise ValueError("naive time")
                    at = at.astimezone(timezone.utc)
                except ValueError:
                    malformed += 1
                    continue
                if not start <= at <= end or row.get("event_type") != NOTIFICATION_PERCEPTION_EVENT_TYPE:
                    continue
                if not _matches_event(row, conversation_id=conversation_id, event_kind=event_kind):
                    continue
                if run_id and str(row.get("run_id") or "") != str(run_id).strip():
                    continue
                matched += 1
                row["_source_path"] = display
                top.append((at.timestamp(), seq, row))
                seq += 1
                if len(top) > offset + max_rows + 1:
                    top.sort(key=lambda item: (item[0], item[1]), reverse=True)
                    top.pop()
            read_statuses.append({"path": display, "status": "ok" if handle.tell() >= size else "partial", "line_count": line_count})
    if state and len(snapshots) != len(state.get("sources", [])):
        raise AgentToolError(code="INPUT_ERROR", message="source_changed") from None
    top.sort(key=lambda item: (item[0], item[1]), reverse=True)
    events = [_public_event(row) for _, _, row in top[offset:offset + max_rows]]
    while events and len(json.dumps(events, ensure_ascii=False).encode()) > 6500:
        if len(events) == 1:
            raise AgentToolError(code="INPUT_ERROR", message="detail_too_large")
        events.pop()
    complete = stop_reason is None and malformed == 0 and all(item["status"] != "unreadable" for item in read_statuses)
    status = "ok" if complete else "partial"
    if not snapshots and all(item["status"] == "missing" for item in read_statuses):
        status = "missing"
    has_more = matched > offset + len(events)
    result = {"schema_version": NOTIFICATION_PERCEPTION_READ_SCHEMA_VERSION,
        "summary": {"ok": complete, "status": status, "total_count": matched if complete else None,
            "returned_count": len(events), "limit": max_rows, "run_id": run_id,
            "conversation_ref": conversation_reference(conversation_id), "event_kind": event_kind,
            "malformed_count": malformed, "unreadable_count": sum(item["status"] == "unreadable" for item in read_statuses),
            "missing_count": sum(item["status"] == "missing" for item in read_statuses)},
        "audit_paths": [str(path) for path in paths], "read_statuses": read_statuses,
        "events": events, "source_hash": digest(snapshots),
        "scope": {"run_id": run_id, "conversation_ref": conversation_reference(conversation_id),
            "event_kind": event_kind, "start_utc": start.isoformat(), "end_utc": end.isoformat()},
        "pagination": {"total_count": matched if complete else None, "matched_count": matched if complete else None,
            "returned_count": len(events), "scanned_count": matched, "has_more": has_more},
        "coverage": {"status": "complete" if complete else "partial", "complete_for": "requested_window",
            "included_count": len(events), "total_count": matched if complete else None,
            "omitted_count": matched - len(events) if complete else None, "has_more": has_more,
            "scanned_bytes": scanned_bytes, "stop_reason": stop_reason or ("malformed" if malformed else None),
            "available_from_utc": first_segment_at.isoformat() if first_segment_at else None},
    }
    set_continuation(result, {"binding": binding, "index": offset + len(events), "sources": snapshots} if has_more else None)
    if conversation_id:
        for item in read_statuses:
            item["line_count"] = None
    return result


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
    deadline_monotonic: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    display_path = str(_display_path(path, base=base))
    source_hash = None
    out: list[dict[str, Any]] = []
    try:
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
    except FileNotFoundError:
        return [], {"path": display_path, "status": "missing", "line_count": 0,
            "parsed_count": 0, "malformed_count": 0}
    except (OSError, UnicodeDecodeError) as exc:
        return [], {"path": display_path, "status": "unreadable", "line_count": None,
            "parsed_count": 0, "malformed_count": 0, "reason": type(exc).__name__}
    malformed_count = 0
    nonempty_count = 0
    for line in lines:
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
            if tail_truncated
            else "valid_empty"
            if not out
            else "ok"
        ),
        "source_hash": source_hash,
        "tail_truncated": tail_truncated,
        "size_bytes": source_size,
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
