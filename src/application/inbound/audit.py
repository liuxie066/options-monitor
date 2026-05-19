from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.application.agent_tool_config import repo_base


def default_audit_db_path() -> Path:
    raw = str(os.environ.get("OM_INBOUND_AUDIT_DB") or "").strip()
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else (repo_base() / path).resolve()
    return (repo_base() / "output_shared" / "state" / "inbound_control.sqlite3").resolve()


def build_command_id(*, channel: str, sender_id: str, message_id: str | None, text: str) -> str:
    source = "\x1f".join(
        [
            str(channel or "").strip().lower(),
            str(sender_id or "").strip(),
            str(message_id or "").strip(),
            str(text or "").strip(),
        ]
    )
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]
    return f"in_{digest}"


class InboundAuditStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser().resolve() if path else default_audit_db_path()

    def find_by_message(self, *, channel: str, message_id: str | None) -> dict[str, Any] | None:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return None
        self._ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM inbound_command_audit
                WHERE channel = ? AND message_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (str(channel or "").strip().lower() or "local", normalized_message_id),
            ).fetchone()
        return _row_to_dict(row)

    def record_result(self, record: dict[str, Any]) -> None:
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inbound_command_audit (
                    command_id,
                    channel,
                    sender_id,
                    message_id,
                    raw_text,
                    parser,
                    intent_name,
                    tool_name,
                    tool_payload_json,
                    decision,
                    result_ok,
                    error_code,
                    response_json,
                    created_at,
                    finished_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record.get("command_id") or ""),
                    str(record.get("channel") or ""),
                    str(record.get("sender_id") or ""),
                    _optional_str(record.get("message_id")),
                    str(record.get("raw_text") or ""),
                    _optional_str(record.get("parser")),
                    _optional_str(record.get("intent_name")),
                    _optional_str(record.get("tool_name")),
                    _json(record.get("tool_payload")),
                    str(record.get("decision") or ""),
                    1 if bool(record.get("result_ok")) else 0,
                    _optional_str(record.get("error_code")),
                    _json(record.get("response")),
                    str(record.get("created_at") or utc_now_iso()),
                    str(record.get("finished_at") or utc_now_iso()),
                ),
            )

    def mark_duplicate(self, *, command_id: str, sender_id: str | None = None, decision: str = "idempotent_replay") -> None:
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE inbound_command_audit
                SET duplicate_count = duplicate_count + 1,
                    last_duplicate_at = ?,
                    last_duplicate_sender_id = ?,
                    last_duplicate_decision = ?
                WHERE command_id = ?
                """,
                (utc_now_iso(), _optional_str(sender_id), str(decision or "idempotent_replay"), str(command_id)),
            )

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inbound_command_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_id TEXT NOT NULL UNIQUE,
                    channel TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    message_id TEXT,
                    raw_text TEXT NOT NULL,
                    parser TEXT,
                    intent_name TEXT,
                    tool_name TEXT,
                    tool_payload_json TEXT,
                    decision TEXT NOT NULL,
                    result_ok INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    response_json TEXT,
                    duplicate_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    last_duplicate_at TEXT,
                    last_duplicate_sender_id TEXT,
                    last_duplicate_decision TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_inbound_audit_message
                ON inbound_command_audit(channel, message_id)
                WHERE message_id IS NOT NULL AND message_id != ''
                """
            )
            _ensure_column(conn, "last_duplicate_sender_id", "TEXT")
            _ensure_column(conn, "last_duplicate_decision", "TEXT")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _optional_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _ensure_column(conn: sqlite3.Connection, name: str, column_type: str) -> None:
    rows = conn.execute("PRAGMA table_info(inbound_command_audit)").fetchall()
    existing = {str(row[1]) for row in rows}
    if name not in existing:
        conn.execute(f"ALTER TABLE inbound_command_audit ADD COLUMN {name} {column_type}")
