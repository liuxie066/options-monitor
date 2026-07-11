from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.application.copilot.contracts import AppEvent, AppResult, utc_now_iso


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
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO copilot_sessions (session_key, messages_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                    messages_json = excluded.messages_json,
                    updated_at = excluded.updated_at
                """,
                (session_key, json.dumps(messages, ensure_ascii=False), now),
            )

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

    def start_run(self, run_id: str, *, request_id: str, contract_id: str, session_key: str | None) -> None:
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO copilot_runs (
                    run_id, request_id, contract_id, session_key, status, cancel_requested,
                    events_json, started_at, finished_at, response_json
                ) VALUES (?, ?, ?, ?, 'running', 0, '[]', ?, NULL, NULL)
                """,
                (run_id, request_id, contract_id, session_key, utc_now_iso()),
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

    def request_cancel(self, run_id: str) -> None:
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute("UPDATE copilot_runs SET cancel_requested = 1 WHERE run_id = ?", (run_id,))

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
                    response_json TEXT
                );
                """
            )


__all__ = ["CopilotHostStore"]
