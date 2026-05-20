from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.application.inbound.audit import default_audit_db_path, utc_now_iso


class InboundOperationStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser().resolve() if path else default_audit_db_path()

    def save_preview(
        self,
        *,
        operation_id: str,
        command_id: str,
        channel: str,
        sender_id: str,
        operation_type: str,
        payload_hash: str,
        payload: dict[str, Any],
        preview: dict[str, Any],
        ttl_seconds: int,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_schema()
        now = created_at or utc_now_iso()
        expires_at = (datetime.fromisoformat(now) + timedelta(seconds=max(1, int(ttl_seconds)))).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inbound_pending_operations (
                    operation_id,
                    command_id,
                    channel,
                    sender_id,
                    operation_type,
                    status,
                    payload_hash,
                    payload_json,
                    preview_json,
                    created_at,
                    expires_at
                )
                VALUES (?, ?, ?, ?, ?, 'previewed', ?, ?, ?, ?, ?)
                ON CONFLICT(operation_id) DO NOTHING
                """,
                (
                    str(operation_id),
                    str(command_id),
                    str(channel),
                    str(sender_id),
                    str(operation_type),
                    str(payload_hash),
                    _json(payload),
                    _json(preview),
                    str(now),
                    str(expires_at),
                ),
            )
        existing = self.get(operation_id)
        if existing is None:
            raise RuntimeError(f"failed to save inbound operation: {operation_id}")
        return existing

    def get(self, operation_id: str) -> dict[str, Any] | None:
        normalized = str(operation_id or "").strip()
        if not normalized:
            return None
        self._ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM inbound_pending_operations
                WHERE operation_id = ?
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
        return _row_to_operation(row)

    def mark_confirmed(self, operation_id: str) -> None:
        self._set_status(operation_id, "confirmed", confirmed_at=utc_now_iso())

    def mark_applied(self, operation_id: str, *, result: dict[str, Any]) -> None:
        self._set_status(operation_id, "applied", applied_at=utc_now_iso(), result_json=_json(result))

    def mark_cancelled(self, operation_id: str, *, result: dict[str, Any]) -> None:
        self._set_status(operation_id, "cancelled", cancelled_at=utc_now_iso(), result_json=_json(result))

    def mark_expired(self, operation_id: str, *, result: dict[str, Any]) -> None:
        self._set_status(operation_id, "expired", result_json=_json(result))

    def mark_failed(self, operation_id: str, *, result: dict[str, Any]) -> None:
        self._set_status(operation_id, "failed", result_json=_json(result))

    def _set_status(
        self,
        operation_id: str,
        status: str,
        *,
        confirmed_at: str | None = None,
        applied_at: str | None = None,
        cancelled_at: str | None = None,
        result_json: str | None = None,
    ) -> None:
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE inbound_pending_operations
                SET status = ?,
                    confirmed_at = COALESCE(?, confirmed_at),
                    applied_at = COALESCE(?, applied_at),
                    cancelled_at = COALESCE(?, cancelled_at),
                    result_json = COALESCE(?, result_json)
                WHERE operation_id = ?
                """,
                (
                    str(status),
                    confirmed_at,
                    applied_at,
                    cancelled_at,
                    result_json,
                    str(operation_id),
                ),
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
                CREATE TABLE IF NOT EXISTS inbound_pending_operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL UNIQUE,
                    command_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    operation_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    preview_json TEXT NOT NULL,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    applied_at TEXT,
                    cancelled_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_inbound_operations_status
                ON inbound_pending_operations(status, expires_at)
                """
            )


def operation_is_expired(operation: dict[str, Any], *, now: datetime | None = None) -> bool:
    raw_expires_at = str(operation.get("expires_at") or "").strip()
    if not raw_expires_at:
        return True
    try:
        expires_at = datetime.fromisoformat(raw_expires_at)
    except Exception:
        return True
    effective_now = now or datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return effective_now >= expires_at


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _loads(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _row_to_operation(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = {key: row[key] for key in row.keys()}
    out["payload"] = _loads(out.get("payload_json"))
    out["preview"] = _loads(out.get("preview_json"))
    out["result"] = _loads(out.get("result_json"))
    return out
