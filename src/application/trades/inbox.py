from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from src.application.ledger.api import (
    canonical_source_economic_payload,
    canonical_source_payload_hash,
)


def enqueue_trade_payload(
    path: str | Path,
    *,
    payload: dict[str, Any],
    source: str,
    broker_deal_key: str | None = None,
) -> str:
    inbox_path = Path(path)
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    deal_id = _payload_deal_id(payload)
    source_text = str(source or "unknown").strip().lower() or "unknown"
    canonical_key = str(broker_deal_key or "").strip()
    economic_payload_hash = (
        _canonical_inbox_economic_hash(
            canonical_key,
            payload,
        )
        if canonical_key
        else None
    )
    identity_status = "bound" if canonical_key else "identity_needs_review"
    identity = canonical_key or (
        f"identity-needs-review|{source_text}|"
        f"{hashlib.sha256(payload_json.encode('utf-8')).hexdigest()}"
    )
    inbox_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    now_ms = int(time.time() * 1000)
    with closing(_connect(inbox_path)) as conn:
        with conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO trade_inbox (
                    inbox_id, source, deal_id, broker_deal_key, identity_status,
                    payload_json, economic_payload_hash, status,
                    attempt_count, received_at_ms, updated_at_ms,
                    last_error, result_status, result_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                ON CONFLICT(inbox_id) DO NOTHING
                """,
                (
                    inbox_id,
                    source_text,
                    deal_id or None,
                    canonical_key or None,
                    identity_status,
                    payload_json,
                    economic_payload_hash,
                    "pending" if canonical_key else "identity_needs_review",
                    now_ms,
                    now_ms,
                    None if canonical_key else "canonical_broker_identity_missing",
                    None if canonical_key else "identity_needs_review",
                    None if canonical_key else "canonical_broker_identity_missing",
                ),
            )
            if canonical_key:
                row = conn.execute(
                    """
                    SELECT payload_json, economic_payload_hash, status
                    FROM trade_inbox
                    WHERE inbox_id = ?
                    """,
                    (inbox_id,),
                ).fetchone()
                if row is None:
                    raise ValueError(
                        "trade inbox row disappeared after enqueue"
                    )
                existing_hash = str(
                    row["economic_payload_hash"] or ""
                ).strip()
                if not existing_hash:
                    existing_payload = json.loads(
                        str(row["payload_json"]) or "{}"
                    )
                    existing_hash = (
                        _canonical_inbox_economic_hash(
                            canonical_key,
                            (
                                existing_payload
                                if isinstance(
                                    existing_payload,
                                    dict,
                                )
                                else {}
                            ),
                        )
                    )
                    conn.execute(
                        """
                        UPDATE trade_inbox
                        SET economic_payload_hash = ?
                        WHERE inbox_id = ?
                        """,
                        (existing_hash, inbox_id),
                    )
                if existing_hash != economic_payload_hash:
                    conn.execute(
                        """
                        UPDATE trade_inbox
                        SET status = 'conflict',
                            updated_at_ms = ?,
                            last_error = ?,
                            result_status = 'conflict',
                            result_reason = ?
                        WHERE inbox_id = ?
                        """,
                        (
                            now_ms,
                            "broker_economic_payload_conflict",
                            "broker_economic_payload_conflict",
                            inbox_id,
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
    lifecycle_pending_or_review = str(
        result_payload.get("reason") or ""
    ).strip().lower() in {
        "waiting_settlement_evidence",
        "awaiting_out_of_order_pair",
        "awaiting_settlement_evidence",
        "lifecycle_conflict_requires_review",
    }
    retryable = (
        str(result_payload.get("status") or "").strip().lower() == "unresolved"
        and bool(diagnostics.get("retryable"))
        and not bool(diagnostics.get("broker_evidence_accepted"))
        and not lifecycle_pending_or_review
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
            "identity_needs_review_count": 0,
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
        "identity_needs_review_count": counts.get(
            "identity_needs_review",
            0,
        ),
        "conflict_count": counts.get("conflict", 0),
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
            broker_deal_key TEXT,
            identity_status TEXT NOT NULL DEFAULT 'bound',
            payload_json TEXT NOT NULL,
            economic_payload_hash TEXT,
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
    _add_column_if_missing(conn, "trade_inbox", "broker_deal_key", "TEXT")
    _add_column_if_missing(
        conn,
        "trade_inbox",
        "identity_status",
        "TEXT NOT NULL DEFAULT 'bound'",
    )
    _add_column_if_missing(
        conn,
        "trade_inbox",
        "economic_payload_hash",
        "TEXT",
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


def _canonical_inbox_economic_hash(
    source_key: str,
    payload: dict[str, Any],
) -> str:
    _broker, account, futu_account_id, _deal_id = (
        str(source_key).split(":", 3)
    )
    source = {
        **dict(payload or {}),
        "account": account,
        "futu_account_id": futu_account_id,
        "symbol": (
            payload.get("symbol")
            or payload.get("code")
            or payload.get("stock_code")
        ),
        "contracts": (
            payload.get("contracts")
            if payload.get("contracts") is not None
            else payload.get("qty")
            if payload.get("qty") is not None
            else payload.get("quantity")
        ),
        "event_time_ms": (
            payload.get("event_time_ms")
            or payload.get("trade_time_ms")
            or payload.get("execution_time_ms")
        ),
    }
    role = (
        "option_anchor"
        if (
            source.get("option_type")
            or source.get("optionType")
            or source.get("strike")
        )
        else "stock_settlement"
    )
    canonical = canonical_source_economic_payload(
        source_key=source_key,
        source_role=role,
        payload=source,
    )
    return canonical_source_payload_hash(canonical)


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    sql_type: str,
) -> None:
    columns = {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")


__all__ = [
    "enqueue_trade_payload",
    "list_retryable_trade_payloads",
    "mark_trade_payload_handled",
    "mark_trade_payload_retryable",
    "settle_trade_payload_result",
    "trade_inbox_summary",
]
