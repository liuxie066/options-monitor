from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import mask_path
from src.application.assistant.audit import connect_inbound_sqlite, default_audit_db_path, inbound_sqlite_error, utc_now_iso
from src.application.assistant.contracts import AssistantRequest


AGENT_SESSION_STORE_SCHEMA_VERSION = "om-agent-session-store-v1"
ASSISTANT_TRACE_SCHEMA_VERSION = "om-assistant-trace-v1"


class AgentSessionStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser().resolve() if path else default_audit_db_path()

    def upsert_snapshot(
        self,
        *,
        snapshot: dict[str, Any],
        command_id: str | None,
        request: AssistantRequest,
        response: dict[str, Any],
    ) -> bool:
        session_id = str(snapshot.get("session_id") or "").strip()
        if not session_id:
            return False
        request_payload = snapshot.get("request") if isinstance(snapshot.get("request"), dict) else {}
        evidence = snapshot.get("evidence_bundle") if isinstance(snapshot.get("evidence_bundle"), dict) else {}
        answer_trace = snapshot.get("answer_trace") if isinstance(snapshot.get("answer_trace"), dict) else {}
        final_response = answer_trace.get("final_response") if isinstance(answer_trace.get("final_response"), dict) else {}
        plan_revisions = snapshot.get("plan_revisions") if isinstance(snapshot.get("plan_revisions"), list) else []
        tool_transcript = snapshot.get("tool_transcript") if isinstance(snapshot.get("tool_transcript"), list) else []
        response_text = _response_text(response)
        now = utc_now_iso()
        self._ensure_schema()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO agent_sessions (
                        session_id,
                        command_id,
                        channel,
                        sender_id,
                        conversation_id,
                        message_id,
                        raw_text,
                        config_key,
                        goal,
                        task_state,
                        plan_revision_count,
                        tool_call_count,
                        fact_count,
                        dataset_count,
                        missing_data_count,
                        conflict_count,
                        response_status,
                        response_reason,
                        response_text,
                        snapshot_json,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        command_id = excluded.command_id,
                        channel = excluded.channel,
                        sender_id = excluded.sender_id,
                        conversation_id = excluded.conversation_id,
                        message_id = excluded.message_id,
                        raw_text = excluded.raw_text,
                        config_key = excluded.config_key,
                        goal = excluded.goal,
                        task_state = excluded.task_state,
                        plan_revision_count = excluded.plan_revision_count,
                        tool_call_count = excluded.tool_call_count,
                        fact_count = excluded.fact_count,
                        dataset_count = excluded.dataset_count,
                        missing_data_count = excluded.missing_data_count,
                        conflict_count = excluded.conflict_count,
                        response_status = excluded.response_status,
                        response_reason = excluded.response_reason,
                        response_text = excluded.response_text,
                        snapshot_json = excluded.snapshot_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        session_id,
                        _optional_str(command_id),
                        _first_text(request_payload.get("channel"), request.channel) or "local",
                        _first_text(request_payload.get("sender_id"), request.sender_id) or "",
                        _first_text(request_payload.get("conversation_id"), request.conversation_id),
                        _first_text(request_payload.get("message_id"), request.message_id),
                        request.text,
                        _first_text(request_payload.get("config_key"), request.config_key),
                        str(snapshot.get("goal") or "").strip(),
                        str(snapshot.get("task_state") or "").strip(),
                        len(plan_revisions),
                        len(tool_transcript),
                        _safe_int(evidence.get("fact_count")),
                        _safe_int(evidence.get("dataset_count")),
                        _safe_int(evidence.get("missing_data_count")),
                        _safe_int(evidence.get("conflict_count")),
                        _optional_str(final_response.get("status")),
                        _optional_str(final_response.get("reason")),
                        response_text,
                        _json(snapshot),
                        now,
                        now,
                    ),
                )
        except sqlite3.Error as exc:
            raise inbound_sqlite_error(self.path, exc) from exc
        return True

    def list_recent(
        self,
        *,
        session_id: str | None = None,
        command_id: str | None = None,
        channel: str | None = None,
        sender_id: str | None = None,
        conversation_id: str | None = None,
        message_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        if not self.has_schema():
            return []
        where: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("session_id", session_id),
            ("command_id", command_id),
            ("channel", str(channel or "").strip().lower() or None),
            ("sender_id", sender_id),
            ("conversation_id", conversation_id),
            ("message_id", message_id),
        ):
            text = str(value or "").strip()
            if not text:
                continue
            where.append(f"{column} = ?")
            params.append(text)
        params.append(_bounded_limit(limit))
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT *
                    FROM agent_sessions
                    {where_sql}
                    ORDER BY updated_at DESC, id DESC
                    LIMIT ?
                    """,
                    tuple(params),
                ).fetchall()
        except sqlite3.Error as exc:
            raise inbound_sqlite_error(self.path, exc) from exc
        return [_session_row_to_dict(row) for row in rows]

    def has_schema(self) -> bool:
        if not self.path.exists():
            return False
        try:
            with sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True) as conn:
                row = conn.execute(
                    """
                    SELECT 1
                    FROM sqlite_master
                    WHERE type = 'table' AND name = 'agent_sessions'
                    LIMIT 1
                    """
                ).fetchone()
        except sqlite3.Error as exc:
            raise inbound_sqlite_error(self.path, exc) from exc
        return row is not None

    def _connect(self) -> sqlite3.Connection:
        conn = connect_inbound_sqlite(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL UNIQUE,
                        command_id TEXT,
                        channel TEXT NOT NULL,
                        sender_id TEXT NOT NULL,
                        conversation_id TEXT,
                        message_id TEXT,
                        raw_text TEXT,
                        config_key TEXT,
                        goal TEXT,
                        task_state TEXT,
                        plan_revision_count INTEGER NOT NULL DEFAULT 0,
                        tool_call_count INTEGER NOT NULL DEFAULT 0,
                        fact_count INTEGER NOT NULL DEFAULT 0,
                        dataset_count INTEGER NOT NULL DEFAULT 0,
                        missing_data_count INTEGER NOT NULL DEFAULT 0,
                        conflict_count INTEGER NOT NULL DEFAULT 0,
                        response_status TEXT,
                        response_reason TEXT,
                        response_text TEXT,
                        snapshot_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_agent_sessions_command
                    ON agent_sessions(command_id)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_agent_sessions_scope
                    ON agent_sessions(channel, sender_id, conversation_id, updated_at)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_agent_sessions_message
                    ON agent_sessions(channel, message_id)
                    WHERE message_id IS NOT NULL AND message_id != ''
                    """
                )
        except sqlite3.Error as exc:
            raise inbound_sqlite_error(self.path, exc) from exc


def collect_assistant_trace(
    *,
    audit_db: str | None = None,
    session_id: str | None = None,
    command_id: str | None = None,
    channel: str | None = None,
    sender_id: str | None = None,
    conversation_id: str | None = None,
    message_id: str | None = None,
    limit: int = 10,
    include_snapshot: bool = False,
) -> dict[str, Any]:
    store = AgentSessionStore(audit_db)
    filters = {
        "session_id": _optional_str(session_id),
        "command_id": _optional_str(command_id),
        "channel": _optional_str(channel),
        "sender_id": _optional_str(sender_id),
        "conversation_id": _optional_str(conversation_id),
        "message_id": _optional_str(message_id),
        "limit": _bounded_limit(limit),
        "include_snapshot": bool(include_snapshot),
    }
    warnings: list[str] = []
    if not store.path.exists():
        warnings.append("audit_db_missing")
        traces: list[dict[str, Any]] = []
        audit_db_exists = False
    elif not store.has_schema():
        warnings.append("agent_session_store_missing")
        traces = []
        audit_db_exists = True
    else:
        rows = store.list_recent(
            session_id=session_id,
            command_id=command_id,
            channel=channel,
            sender_id=sender_id,
            conversation_id=conversation_id,
            message_id=message_id,
            limit=limit,
        )
        traces = [_trace_from_row(row, include_snapshot=include_snapshot) for row in rows]
        audit_db_exists = True
        if not traces:
            warnings.append("agent_session_not_found")
    return {
        "schema_version": ASSISTANT_TRACE_SCHEMA_VERSION,
        "audit_db": mask_path(store.path),
        "audit_db_exists": audit_db_exists,
        "filters": {key: value for key, value in filters.items() if value not in (None, "", [], {})},
        "trace_count": len(traces),
        "traces": traces,
        "warnings": warnings,
        "response_text": format_assistant_trace(traces, filters=filters, warnings=warnings),
    }


def format_assistant_trace(traces: list[dict[str, Any]], *, filters: dict[str, Any], warnings: list[str]) -> str:
    scope = _scope_text(filters)
    if not traces:
        return f"Assistant trace：0 条\nscope：{scope}\n没有匹配的 Agent session。"
    lines = [f"Assistant trace：{len(traces)} 条", f"scope：{scope}"]
    if warnings:
        lines.append("warnings：" + ",".join(sorted(set(warnings))))
    for trace in traces:
        identity = trace.get("identity") if isinstance(trace.get("identity"), dict) else {}
        task = trace.get("task") if isinstance(trace.get("task"), dict) else {}
        plan = trace.get("plan") if isinstance(trace.get("plan"), dict) else {}
        evidence = trace.get("evidence") if isinstance(trace.get("evidence"), dict) else {}
        answer = trace.get("answer") if isinstance(trace.get("answer"), dict) else {}
        lines.append(
            "- "
            f"{task.get('updated_at') or '-'} "
            f"{task.get('state') or '-'} "
            f"session={identity.get('session_id') or '-'} "
            f"command={identity.get('command_id') or '-'}"
        )
        goal = str(task.get("goal") or "").strip()
        if goal:
            lines.append(f"  goal: {_clip(goal, 180)}")
        lines.append(
            "  "
            f"plan_revisions={plan.get('revision_count', 0)} "
            f"tools={len(trace.get('tools') or [])} "
            f"facts={evidence.get('fact_count', 0)} "
            f"missing={evidence.get('missing_data_count', 0)} "
            f"conflicts={evidence.get('conflict_count', 0)}"
        )
        answer_reason = _first_text(answer.get("synthesis_reason"), answer.get("response_reason"), answer.get("fallback"))
        if answer_reason:
            lines.append(f"  answer: {answer.get('response_status') or '-'} reason={answer_reason}")
    return "\n".join(lines)


def _trace_from_row(row: dict[str, Any], *, include_snapshot: bool) -> dict[str, Any]:
    snapshot = row.get("snapshot") if isinstance(row.get("snapshot"), dict) else {}
    evidence = snapshot.get("evidence_bundle") if isinstance(snapshot.get("evidence_bundle"), dict) else {}
    answer_trace = snapshot.get("answer_trace") if isinstance(snapshot.get("answer_trace"), dict) else {}
    final_response = answer_trace.get("final_response") if isinstance(answer_trace.get("final_response"), dict) else {}
    synthesis = answer_trace.get("synthesis") if isinstance(answer_trace.get("synthesis"), dict) else {}
    permission_state = snapshot.get("permission_state") if isinstance(snapshot.get("permission_state"), dict) else {}
    trace: dict[str, Any] = {
        "schema_version": "om-assistant-trace-entry-v1",
        "identity": {
            "session_id": row.get("session_id"),
            "command_id": row.get("command_id"),
            "channel": row.get("channel"),
            "sender_id": row.get("sender_id"),
            "conversation_id": row.get("conversation_id"),
            "message_id": row.get("message_id"),
            "config_key": row.get("config_key"),
        },
        "task": {
            "goal": row.get("goal"),
            "state": row.get("task_state"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "raw_text": row.get("raw_text"),
        },
        "plan": {
            "revision_count": int(row.get("plan_revision_count") or 0),
            "revisions": _compact_plan_revisions(snapshot.get("plan_revisions")),
        },
        "tools": _compact_tools(snapshot.get("tool_transcript")),
        "evidence": {
            "fact_count": int(row.get("fact_count") or 0),
            "dataset_count": int(row.get("dataset_count") or 0),
            "missing_data_count": int(row.get("missing_data_count") or 0),
            "conflict_count": int(row.get("conflict_count") or 0),
            "sources": list(evidence.get("sources") or []),
            "tools": list(evidence.get("tools") or []),
            "guard_profiles": list(evidence.get("guard_profiles") or []),
        },
        "answer": {
            "response_status": row.get("response_status") or final_response.get("status"),
            "response_reason": row.get("response_reason") or final_response.get("reason"),
            "synthesis_reason": synthesis.get("reason"),
            "fallback": synthesis.get("fallback"),
            "answer_guard": synthesis.get("answer_guard") if isinstance(synthesis.get("answer_guard"), dict) else None,
            "response_text_chars": len(str(row.get("response_text") or "")),
        },
        "permission_state": permission_state,
    }
    if include_snapshot:
        trace["snapshot"] = snapshot
    return trace


def _compact_plan_revisions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        plan = item.get("plan") if isinstance(item.get("plan"), dict) else {}
        steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
        out.append(
            {
                "revision": item.get("revision"),
                "reason": item.get("reason"),
                "goal": plan.get("goal"),
                "steps": [
                    {
                        "tool_name": step.get("tool_name"),
                        "arguments": dict(step.get("arguments") or {}) if isinstance(step, dict) else {},
                        "purpose": step.get("purpose") if isinstance(step, dict) else None,
                    }
                    for step in steps
                    if isinstance(step, dict)
                ],
            }
        )
    return out


def _compact_tools(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "index": item.get("index"),
                "tool_name": item.get("tool_name"),
                "payload": dict(item.get("payload") or {}) if isinstance(item.get("payload"), dict) else {},
                "authorized": bool(item.get("authorized", False)),
                "authorization_reason": item.get("authorization_reason"),
                "ok": bool(item.get("ok", False)),
                "error_code": item.get("error_code"),
                "summary": dict(item.get("summary") or {}) if isinstance(item.get("summary"), dict) else {},
            }
        )
    return out


def _session_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = {key: row[key] for key in row.keys()}
    item["snapshot"] = _loads_object(item.get("snapshot_json"))
    return item


def _response_text(response: dict[str, Any]) -> str | None:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    text = str(data.get("response_text") or "").strip()
    return text or None


def _loads_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        loaded = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return dict(loaded) if isinstance(loaded, dict) else {}


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _optional_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _bounded_limit(value: Any) -> int:
    try:
        return max(1, min(int(value or 10), 50))
    except Exception:
        return 10


def _scope_text(filters: dict[str, Any]) -> str:
    parts = []
    for key in ("session_id", "command_id", "channel", "sender_id", "conversation_id", "message_id"):
        value = filters.get(key)
        if value:
            parts.append(f"{key}={value}")
    parts.append(f"limit={filters.get('limit') or 10}")
    return ", ".join(parts)


def _clip(value: str, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "..."


__all__ = [
    "AGENT_SESSION_STORE_SCHEMA_VERSION",
    "ASSISTANT_TRACE_SCHEMA_VERSION",
    "AgentSessionStore",
    "collect_assistant_trace",
    "format_assistant_trace",
]
