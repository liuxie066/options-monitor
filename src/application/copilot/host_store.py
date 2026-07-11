from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.application.copilot.contracts import (
    AppEvent,
    AppResult,
    ExecutionContract,
    contract_from_payload,
    contract_to_payload,
    new_id,
    utc_now_iso,
)
from src.application.copilot.event_store import public_progress_event


REPLY_DELIVERY_LEASE_SECONDS = 300


class CopilotHostStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()

    def session_messages(self, session_key: str) -> tuple[dict[str, Any], ...]:
        self._ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT messages_json FROM copilot_sessions WHERE session_key = ?",
                (session_key,),
            ).fetchone()
        if row is None:
            return ()
        try:
            messages = json.loads(str(row[0] or "[]"))
        except Exception:
            return ()
        return tuple(dict(item) for item in messages if isinstance(item, dict))

    def record_session_turn(
        self,
        session_key: str,
        user_message: str,
        assistant_message: str,
        *,
        max_messages: int,
        tool_uses: tuple[dict[str, Any], ...] = (),
        warnings: tuple[str, ...] = (),
        errors: tuple[str, ...] = (),
    ) -> None:
        messages = list(self.session_messages(session_key))
        messages.extend(
            (
                {"role": "user", "content": str(user_message)},
                {"role": "assistant", "content": str(assistant_message)},
            )
        )
        if max_messages and len(messages) > max_messages:
            messages = messages[-max_messages:]
        now = utc_now_iso()
        turns = list(self.session_turns(session_key))
        turns.append(
            {
                "turn_id": new_id("turn"),
                "user_text": str(user_message),
                "assistant_final": str(assistant_message),
                "tool_uses": [dict(item) for item in tool_uses],
                "warnings": list(warnings),
                "errors": list(errors),
                "created_at": now,
            }
        )
        turns = turns[-100:]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO copilot_sessions (session_key, messages_json, turns_json, memory_json, updated_at)
                VALUES (?, ?, ?, '{}', ?)
                ON CONFLICT(session_key) DO UPDATE SET
                    messages_json = excluded.messages_json,
                    turns_json = excluded.turns_json,
                    updated_at = excluded.updated_at
                """,
                (
                    session_key,
                    json.dumps(messages, ensure_ascii=False),
                    json.dumps(turns, ensure_ascii=False, default=str),
                    now,
                ),
            )

    def session_turns(self, session_key: str) -> tuple[dict[str, Any], ...]:
        return self._session_json_list(session_key, "turns_json")

    def session_memory(self, session_key: str) -> dict[str, Any]:
        self._ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT memory_json FROM copilot_sessions WHERE session_key = ?",
                (session_key,),
            ).fetchone()
        default = {"version": 1, "compacted_turn_count": 0, "pinned_state": {}, "episodes": []}
        if row is None:
            return default
        try:
            payload = json.loads(str(row[0] or "{}"))
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            return default
        return {
            "version": int(payload.get("version") or 1),
            "compacted_turn_count": max(0, int(payload.get("compacted_turn_count") or 0)),
            "pinned_state": dict(payload.get("pinned_state") or {}),
            "episodes": [dict(item) for item in payload.get("episodes") or () if isinstance(item, dict)],
        }

    def update_session_memory(
        self,
        session_key: str,
        memory: dict[str, Any],
        *,
        expected_compacted_turn_count: int | None = None,
    ) -> bool:
        self._ensure_schema()
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if expected_compacted_turn_count is not None:
                row = conn.execute(
                    "SELECT memory_json FROM copilot_sessions WHERE session_key = ?",
                    (session_key,),
                ).fetchone()
                current = _json_object(row)
                if max(0, int(current.get("compacted_turn_count") or 0)) != max(
                    0, int(expected_compacted_turn_count)
                ):
                    return False
            conn.execute(
                """
                INSERT INTO copilot_sessions (session_key, messages_json, turns_json, memory_json, updated_at)
                VALUES (?, '[]', '[]', ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                    memory_json = excluded.memory_json,
                    updated_at = excluded.updated_at
                """,
                (session_key, json.dumps(memory, ensure_ascii=False, default=str), now),
            )
        return True

    def _session_json_list(self, session_key: str, column: str) -> tuple[dict[str, Any], ...]:
        if column not in {"messages_json", "turns_json"}:
            raise ValueError("unsupported session column")
        self._ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {column} FROM copilot_sessions WHERE session_key = ?",
                (session_key,),
            ).fetchone()
        if row is None:
            return ()
        try:
            items = json.loads(str(row[0] or "[]"))
        except Exception:
            return ()
        return tuple(dict(item) for item in items if isinstance(item, dict))

    def acquire_session_run(self, session_key: str, run_id: str, *, ttl_seconds: int) -> bool:
        self._ensure_schema()
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=max(1, ttl_seconds))).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM copilot_session_runs WHERE session_key = ? AND expires_at <= ?",
                (session_key, now.isoformat()),
            )
            try:
                conn.execute(
                    "INSERT INTO copilot_session_runs (session_key, run_id, expires_at) VALUES (?, ?, ?)",
                    (session_key, run_id, expires_at),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def release_session_run(self, session_key: str, run_id: str) -> None:
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM copilot_session_runs WHERE session_key = ? AND run_id = ?",
                (session_key, run_id),
            )

    def start_run(
        self,
        run_id: str,
        *,
        contract: ExecutionContract,
        session_key: str | None,
        resumed_from: str | None = None,
    ) -> None:
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO copilot_runs (
                    run_id, request_id, contract_id, session_key, status, cancel_requested,
                    events_json, started_at, finished_at, response_json, contract_json,
                    resumed_from, resume_attempts
                ) VALUES (?, ?, ?, ?, 'running', 0, '[]', ?, NULL, NULL, ?, ?, 0)
                """,
                (
                    run_id,
                    contract.request_id,
                    contract.contract_id,
                    session_key,
                    utc_now_iso(),
                    json.dumps(contract_to_payload(contract), ensure_ascii=False, default=str),
                    resumed_from,
                ),
            )

    def append_event(self, event: AppEvent) -> None:
        self._ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT events_json FROM copilot_runs WHERE run_id = ?",
                (event.run_id,),
            ).fetchone()
            if row is None:
                return
            try:
                events = json.loads(str(row[0] or "[]"))
            except Exception:
                events = []
            events.append(
                {
                    "event_id": event.event_id,
                    "run_id": event.run_id,
                    "type": event.type,
                    "timestamp": event.timestamp,
                    "payload": event.payload,
                    "visible_ref": event.visible_ref,
                }
            )
            conn.execute(
                "UPDATE copilot_runs SET events_json = ? WHERE run_id = ?",
                (json.dumps(events, ensure_ascii=False, default=str), event.run_id),
            )
            state = {
                "model_turn_started": "waiting_model",
                "tool_call": "waiting_tool",
                "tool_result": "running",
            }.get(event.type)
            if state:
                conn.execute("UPDATE copilot_runs SET status = ? WHERE run_id = ?", (state, event.run_id))
            termination_reason = _termination_reason(event)
            if termination_reason:
                conn.execute(
                    "UPDATE copilot_runs SET termination_reason = ? WHERE run_id = ?",
                    (termination_reason, event.run_id),
                )
            if event.type == "model_turn_completed":
                metrics = _json_object(
                    conn.execute("SELECT metrics_json FROM copilot_runs WHERE run_id = ?", (event.run_id,)).fetchone()
                )
                metrics["model_turn_count"] = int(metrics.get("model_turn_count") or 0) + 1
                metrics["model_retry_count"] = max(
                    int(metrics.get("model_retry_count") or 0),
                    int(event.payload.get("model_retry_count") or 0),
                )
                metrics["usage"] = dict(event.payload.get("usage_total") or event.payload.get("usage") or {})
                conn.execute(
                    "UPDATE copilot_runs SET metrics_json = ? WHERE run_id = ?",
                    (json.dumps(metrics, ensure_ascii=False, default=str), event.run_id),
                )
            elif event.type == "tool_result":
                metrics = _json_object(
                    conn.execute("SELECT metrics_json FROM copilot_runs WHERE run_id = ?", (event.run_id,)).fetchone()
                )
                metrics["tool_call_count"] = int(metrics.get("tool_call_count") or 0) + 1
                conn.execute(
                    "UPDATE copilot_runs SET metrics_json = ? WHERE run_id = ?",
                    (json.dumps(metrics, ensure_ascii=False, default=str), event.run_id),
                )

    def finish_run(self, result: AppResult) -> None:
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE copilot_runs
                SET status = ?, finished_at = ?, response_json = ?
                WHERE run_id = ?
                """,
                (
                    result.status,
                    utc_now_iso(),
                    json.dumps(
                        {
                            "status": result.status,
                            "ok": result.ok,
                            "user_response": result.user_response,
                            "error": result.error,
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                    result.run_id,
                ),
            )

    def request_cancel(self, run_id: str) -> bool:
        self._ensure_schema()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE copilot_runs SET cancel_requested = 1
                WHERE run_id = ? AND status IN ('running', 'waiting_model', 'waiting_tool')
                """,
                (run_id,),
            )
        return bool(cursor.rowcount)

    def is_cancel_requested(self, run_id: str) -> bool:
        self._ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM copilot_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return bool(row and row[0])

    def run_record(self, run_id: str) -> dict[str, Any] | None:
        self._ensure_schema()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM copilot_runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_runs(self, *, limit: int = 20) -> tuple[dict[str, Any], ...]:
        self._ensure_schema()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM copilot_runs ORDER BY started_at DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def run_events(self, run_id: str, *, after_event_id: str | None = None) -> tuple[dict[str, Any], ...]:
        record = self.run_record(run_id)
        if record is None:
            return ()
        try:
            events = [dict(item) for item in json.loads(str(record.get("events_json") or "[]")) if isinstance(item, dict)]
        except Exception:
            return ()
        if not after_event_id:
            return tuple(events)
        for index, item in enumerate(events):
            if str(item.get("event_id") or "") == str(after_event_id):
                return tuple(events[index + 1 :])
        return tuple(events)

    def run_progress(self, run_id: str, *, after_event_id: str | None = None) -> tuple[dict[str, Any], ...]:
        return tuple(
            progress
            for item in self.run_events(run_id, after_event_id=after_event_id)
            if (progress := public_progress_event(item)) is not None
        )

    def resume_source(self, run_id: str, *, max_attempts: int = 3) -> tuple[ExecutionContract, list[dict[str, Any]], str | None] | None:
        self._ensure_schema()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM copilot_runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            status = str(row["status"] or "")
            attempts = int(row["resume_attempts"] or 0)
            if status not in {"failed", "interrupted"} or attempts >= max(1, int(max_attempts)):
                return None
            try:
                contract_payload = json.loads(str(row["contract_json"] or "{}"))
                events = json.loads(str(row["events_json"] or "[]"))
                contract = contract_from_payload(contract_payload)
            except Exception:
                return None
            if not contract.contract_id or contract.policy.get("read_only") is not True:
                return None
            conn.execute(
                "UPDATE copilot_runs SET resume_attempts = resume_attempts + 1 WHERE run_id = ?",
                (run_id,),
            )
        return contract, [dict(item) for item in events if isinstance(item, dict)], row["session_key"]

    def mark_stale_runs_interrupted(self, *, older_than_seconds: int = 600) -> int:
        self._ensure_schema()
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(1, int(older_than_seconds)))).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE copilot_runs
                SET status = 'interrupted', finished_at = ?, termination_reason = 'host_restart_or_stale_run'
                WHERE status IN ('running', 'waiting_model', 'waiting_tool') AND started_at < ?
                """,
                (utc_now_iso(), cutoff),
            )
        return int(cursor.rowcount)

    def enqueue_reply(
        self,
        *,
        delivery_key: str,
        channel: str,
        payload: dict[str, Any],
        session_key: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_schema()
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO copilot_reply_outbox (
                    delivery_key, channel, session_key, run_id, payload_json, status,
                    attempt_count, next_attempt_at, last_error, created_at, updated_at, delivered_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, NULL, ?, ?, NULL)
                ON CONFLICT(delivery_key) DO NOTHING
                """,
                (
                    str(delivery_key),
                    str(channel),
                    session_key,
                    run_id,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    now,
                    now,
                    now,
                ),
            )
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM copilot_reply_outbox WHERE delivery_key = ?",
                (str(delivery_key),),
            ).fetchone()
        return dict(row) if row is not None else {}

    def claim_reply(
        self,
        *,
        delivery_key: str | None = None,
        channel: str | None = None,
        before: str | None = None,
    ) -> dict[str, Any] | None:
        self._ensure_schema()
        now = before or utc_now_iso()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            delivery_cutoff = (
                datetime.fromisoformat(str(now).replace("Z", "+00:00"))
                - timedelta(seconds=REPLY_DELIVERY_LEASE_SECONDS)
            ).isoformat()
            conn.execute(
                """
                UPDATE copilot_reply_outbox
                SET status = 'retryable_failed', next_attempt_at = ?,
                    last_error = 'delivery lease expired', updated_at = ?
                WHERE status = 'delivering' AND updated_at <= ?
                """,
                (now, now, delivery_cutoff),
            )
            if delivery_key:
                row = conn.execute(
                    """
                    SELECT * FROM copilot_reply_outbox
                    WHERE delivery_key = ?
                      AND status IN ('pending', 'retryable_failed')
                      AND next_attempt_at <= ?
                    """,
                    (str(delivery_key), now),
                ).fetchone()
            elif channel:
                row = conn.execute(
                    """
                    SELECT * FROM copilot_reply_outbox
                    WHERE channel = ?
                      AND status IN ('pending', 'retryable_failed')
                      AND next_attempt_at <= ?
                    ORDER BY created_at, delivery_key LIMIT 1
                    """,
                    (str(channel), now),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM copilot_reply_outbox
                    WHERE status IN ('pending', 'retryable_failed') AND next_attempt_at <= ?
                    ORDER BY created_at, delivery_key LIMIT 1
                    """,
                    (now,),
                ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE copilot_reply_outbox
                SET status = 'delivering', attempt_count = attempt_count + 1, updated_at = ?
                WHERE delivery_key = ?
                """,
                (utc_now_iso(), row["delivery_key"]),
            )
            claimed = dict(row)
            claimed["status"] = "delivering"
            claimed["attempt_count"] = int(row["attempt_count"] or 0) + 1
            return claimed

    def mark_reply_delivered(self, delivery_key: str) -> bool:
        self._ensure_schema()
        now = utc_now_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE copilot_reply_outbox
                SET status = 'delivered', delivered_at = ?, updated_at = ?, last_error = NULL
                WHERE delivery_key = ? AND status = 'delivering'
                """,
                (now, now, str(delivery_key)),
            )
        return bool(cursor.rowcount)

    def mark_reply_failed(
        self,
        delivery_key: str,
        *,
        error: str,
        retryable: bool,
        retry_after_seconds: int = 30,
    ) -> bool:
        self._ensure_schema()
        now = datetime.now(timezone.utc)
        next_attempt = now + timedelta(seconds=max(1, int(retry_after_seconds)))
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE copilot_reply_outbox
                SET status = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE delivery_key = ? AND status = 'delivering'
                """,
                (
                    "retryable_failed" if retryable else "terminal_failed",
                    next_attempt.isoformat(),
                    str(error)[:1000],
                    now.isoformat(),
                    str(delivery_key),
                ),
            )
        return bool(cursor.rowcount)

    def list_replies(self, *, limit: int = 50) -> tuple[dict[str, Any], ...]:
        self._ensure_schema()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM copilot_reply_outbox ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def acquire_lane(self, lane: str, lease_id: str, *, limit: int, ttl_seconds: int) -> bool:
        self._ensure_schema()
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=max(1, int(ttl_seconds)))).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM copilot_lane_leases WHERE expires_at <= ?", (now.isoformat(),))
            active = conn.execute(
                "SELECT COUNT(*) FROM copilot_lane_leases WHERE lane = ?",
                (str(lane),),
            ).fetchone()
            if int(active[0] if active else 0) >= max(1, int(limit)):
                return False
            conn.execute(
                "INSERT INTO copilot_lane_leases (lane, lease_id, expires_at) VALUES (?, ?, ?)",
                (str(lane), str(lease_id), expires_at),
            )
        return True

    def release_lane(self, lane: str, lease_id: str) -> None:
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM copilot_lane_leases WHERE lane = ? AND lease_id = ?",
                (str(lane), str(lease_id)),
            )

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.path, timeout=10)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS copilot_sessions (
                    session_key TEXT PRIMARY KEY,
                    messages_json TEXT NOT NULL,
                    turns_json TEXT NOT NULL DEFAULT '[]',
                    memory_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS copilot_session_runs (
                    session_key TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS copilot_runs (
                    run_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    contract_id TEXT NOT NULL,
                    session_key TEXT,
                    status TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    events_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    response_json TEXT,
                    contract_json TEXT NOT NULL DEFAULT '{}',
                    resumed_from TEXT,
                    resume_attempts INTEGER NOT NULL DEFAULT 0,
                    termination_reason TEXT,
                    metrics_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS copilot_reply_outbox (
                    delivery_key TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    session_key TEXT,
                    run_id TEXT,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    delivered_at TEXT
                );
                CREATE TABLE IF NOT EXISTS copilot_lane_leases (
                    lane TEXT NOT NULL,
                    lease_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    PRIMARY KEY (lane, lease_id)
                );
                """
            )
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(copilot_sessions)")}
            if "turns_json" not in columns:
                conn.execute("ALTER TABLE copilot_sessions ADD COLUMN turns_json TEXT NOT NULL DEFAULT '[]'")
            if "memory_json" not in columns:
                conn.execute("ALTER TABLE copilot_sessions ADD COLUMN memory_json TEXT NOT NULL DEFAULT '{}'")
            run_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(copilot_runs)")}
            if "contract_json" not in run_columns:
                conn.execute("ALTER TABLE copilot_runs ADD COLUMN contract_json TEXT NOT NULL DEFAULT '{}'")
            if "resumed_from" not in run_columns:
                conn.execute("ALTER TABLE copilot_runs ADD COLUMN resumed_from TEXT")
            if "resume_attempts" not in run_columns:
                conn.execute("ALTER TABLE copilot_runs ADD COLUMN resume_attempts INTEGER NOT NULL DEFAULT 0")
            if "termination_reason" not in run_columns:
                conn.execute("ALTER TABLE copilot_runs ADD COLUMN termination_reason TEXT")
            if "metrics_json" not in run_columns:
                conn.execute("ALTER TABLE copilot_runs ADD COLUMN metrics_json TEXT NOT NULL DEFAULT '{}'")


def _termination_reason(event: AppEvent) -> str | None:
    if event.type == "agent_terminated":
        return str(event.payload.get("reason") or "completed")
    if event.type == "run_cancelled":
        return "cancelled"
    if event.type == "budget_exhausted":
        return "budget_exhausted"
    if event.type == "model_error":
        return str(event.payload.get("error_category") or "model_error")
    return None


def _json_object(row: tuple[Any, ...] | sqlite3.Row | None) -> dict[str, Any]:
    raw = row[0] if row else "{}"
    try:
        value = json.loads(str(raw or "{}"))
    except Exception:
        return {}
    return dict(value) if isinstance(value, dict) else {}


__all__ = ["CopilotHostStore"]
