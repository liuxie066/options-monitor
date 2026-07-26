from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any


def enqueue_trade_payload(
    path: str | Path,
    *,
    payload: dict[str, Any],
    source: str,
) -> str:
    inbox_path = Path(path)
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    deal_id = _payload_deal_id(payload)
    source_text = str(source or "unknown").strip().lower() or "unknown"
    identity = deal_id or f"{source_text}|{hashlib.sha256(payload_json.encode('utf-8')).hexdigest()}"
    inbox_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    now_ms = int(time.time() * 1000)
    with closing(_connect(inbox_path)) as conn:
        with conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO trade_inbox (
                    inbox_id, source, deal_id, payload_json, status,
                    attempt_count, received_at_ms, updated_at_ms,
                    last_error, result_status, result_reason
                )
                VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, NULL, NULL, NULL)
                ON CONFLICT(inbox_id) DO UPDATE SET
                    payload_json = CASE
                        WHEN trade_inbox.status = 'handled' THEN trade_inbox.payload_json
                        ELSE excluded.payload_json
                    END,
                    updated_at_ms = CASE
                        WHEN trade_inbox.status = 'handled' THEN trade_inbox.updated_at_ms
                        ELSE excluded.updated_at_ms
                    END
                """,
                (
                    inbox_id,
                    source_text,
                    deal_id or None,
                    payload_json,
                    now_ms,
                    now_ms,
                ),
            )
    return inbox_id


def list_retryable_trade_payloads(
    path: str | Path,
    *,
    limit: int = 100,
    retry_delay_sec: float = 60.0,
    max_attempts: int = 20,
) -> list[dict[str, Any]]:
    inbox_path = Path(path)
    if not inbox_path.exists():
        return []
    cutoff_ms = int(time.time() * 1000 - max(0.0, retry_delay_sec) * 1000)
    with closing(_connect(inbox_path)) as conn:
        with conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT inbox_id, source, deal_id, payload_json, attempt_count,
                       received_at_ms, updated_at_ms, last_error
                FROM trade_inbox
                WHERE status = 'pending'
                  AND attempt_count < ?
                  AND (attempt_count = 0 OR updated_at_ms <= ?)
                ORDER BY received_at_ms ASC, inbox_id ASC
                LIMIT ?
                """,
                (int(max_attempts), cutoff_ms, max(1, int(limit))),
            ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]) or "{}")
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        out.append(
            {
                "inbox_id": str(row["inbox_id"]),
                "source": str(row["source"]),
                "deal_id": str(row["deal_id"] or ""),
                "payload": payload,
                "attempt_count": int(row["attempt_count"] or 0),
                "received_at_ms": int(row["received_at_ms"] or 0),
                "updated_at_ms": int(row["updated_at_ms"] or 0),
                "last_error": str(row["last_error"] or ""),
            }
        )
    return out


def mark_trade_payload_handled(
    path: str | Path,
    *,
    inbox_id: str,
    result: dict[str, Any] | None,
) -> None:
    inbox_path = Path(path)
    now_ms = int(time.time() * 1000)
    result_payload = result if isinstance(result, dict) else {}
    with closing(_connect(inbox_path)) as conn:
        with conn:
            _ensure_schema(conn)
            conn.execute(
                """
                UPDATE trade_inbox
                SET status = 'handled',
                    attempt_count = attempt_count + 1,
                    updated_at_ms = ?,
                    last_error = NULL,
                    result_status = ?,
                    result_reason = ?
                WHERE inbox_id = ? AND status != 'handled'
                """,
                (
                    now_ms,
                    str(result_payload.get("status") or "").strip() or None,
                    str(result_payload.get("reason") or "").strip() or None,
                    str(inbox_id),
                ),
            )


def mark_trade_payload_retryable(
    path: str | Path,
    *,
    inbox_id: str,
    error: str | None,
    result: dict[str, Any] | None = None,
) -> None:
    inbox_path = Path(path)
    now_ms = int(time.time() * 1000)
    result_payload = result if isinstance(result, dict) else {}
    with closing(_connect(inbox_path)) as conn:
        with conn:
            _ensure_schema(conn)
            conn.execute(
                """
                UPDATE trade_inbox
                SET status = 'pending',
                attempt_count = attempt_count + 1,
                updated_at_ms = ?,
                last_error = ?,
                result_status = ?,
                result_reason = ?
            WHERE inbox_id = ? AND status != 'handled'
            """,
                (
                    now_ms,
                    str(error) if error else None,
                    str(result_payload.get("status") or "exception"),
                    str(result_payload.get("reason") or "callback_exception"),
                    str(inbox_id),
                ),
            )


def settle_trade_payload_result(
    path: str | Path,
    *,
    inbox_id: str,
    result: dict[str, Any] | None,
) -> None:
    result_payload = result if isinstance(result, dict) else {}
    diagnostics = (
        result_payload.get("diagnostics")
        if isinstance(result_payload.get("diagnostics"), dict)
        else {}
    )
    retryable = (
        str(result_payload.get("status") or "").strip().lower() == "unresolved"
        and bool(diagnostics.get("retryable"))
    )
    if retryable:
        mark_trade_payload_retryable(
            path,
            inbox_id=inbox_id,
            error=None,
            result=result_payload,
        )
        return
    mark_trade_payload_handled(
        path,
        inbox_id=inbox_id,
        result=result_payload,
    )


def trade_inbox_summary(path: str | Path) -> dict[str, Any]:
    inbox_path = Path(path)
    if not inbox_path.exists():
        return {
            "path": str(inbox_path),
            "pending_count": 0,
            "handled_count": 0,
            "max_attempt_count": 0,
        }
    with closing(_connect(inbox_path)) as conn:
        with conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS item_count, MAX(attempt_count) AS max_attempt_count
                FROM trade_inbox
                GROUP BY status
                """
            ).fetchall()
    counts = {str(row["status"]): int(row["item_count"] or 0) for row in rows}
    return {
        "path": str(inbox_path),
        "pending_count": counts.get("pending", 0),
        "handled_count": counts.get("handled", 0),
        "max_attempt_count": max(
            (int(row["max_attempt_count"] or 0) for row in rows),
            default=0,
        ),
    }


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_inbox (
            inbox_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            deal_id TEXT,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            received_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL,
            last_error TEXT,
            result_status TEXT,
            result_reason TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trade_inbox_retry
        ON trade_inbox(status, updated_at_ms, received_at_ms)
        """
    )


def _payload_deal_id(payload: dict[str, Any]) -> str:
    for key in ("deal_id", "dealID", "dealId", "id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


__all__ = [
    "enqueue_trade_payload",
    "list_retryable_trade_payloads",
    "mark_trade_payload_handled",
    "mark_trade_payload_retryable",
    "settle_trade_payload_result",
    "trade_inbox_summary",
]
