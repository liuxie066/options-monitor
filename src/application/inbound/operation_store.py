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
        conversation_id: str | None = None,
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
                    conversation_id,
                    operation_type,
                    status,
                    payload_hash,
                    payload_json,
                    preview_json,
                    created_at,
                    expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'previewed', ?, ?, ?, ?, ?)
                ON CONFLICT(operation_id) DO NOTHING
                """,
                (
                    str(operation_id),
                    str(command_id),
                    str(channel),
                    str(sender_id),
                    _optional_str(conversation_id),
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

    def list_pending_operations(
        self,
        *,
        channel: str | None = None,
        sender_id: str | None = None,
        conversation_id: str | None = None,
        operation_types: set[str] | frozenset[str] | None = None,
        include_expired: bool = False,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        normalized_types = {str(item).strip() for item in (operation_types or set()) if str(item).strip()}
        operations = self._list_previewed_operations(
            channel=channel,
            sender_id=sender_id,
            conversation_id=conversation_id,
            operation_types=normalized_types,
            limit=limit,
        )
        out: list[dict[str, Any]] = []
        for operation in operations:
            expired = operation_is_expired(operation, now=now)
            if expired and not include_expired:
                continue
            summary = _operation_summary(operation)
            summary["status"] = "expired" if expired else str(operation.get("status") or "previewed")
            out.append(summary)
        return out

    def resolve_pending_operation(
        self,
        *,
        channel: str,
        sender_id: str,
        operation_types: set[str] | frozenset[str],
        conversation_id: str | None = None,
        explicit_operation_id: str | None = None,
        allow_expired: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        normalized_types = {str(item).strip() for item in operation_types if str(item).strip()}
        operation_id = str(explicit_operation_id or "").strip()
        if operation_id:
            operation = self.get(operation_id)
            resolution = {
                "operation_resolution": "explicit",
                "operation_id": operation_id,
                "candidate_operations": [],
            }
            if operation is None:
                return {**resolution, "status": "not_found", "operation": None}
            validation = _validate_pending_operation(
                operation,
                channel=channel,
                sender_id=sender_id,
                conversation_id=conversation_id,
                operation_types=normalized_types,
                allow_expired=allow_expired,
                now=now,
            )
            return {**resolution, **validation, "operation": operation}

        candidates = self._list_previewed_operations(
            channel=channel,
            sender_id=sender_id,
            conversation_id=conversation_id,
            operation_types=normalized_types,
        )
        active: list[dict[str, Any]] = []
        expired: list[dict[str, Any]] = []
        for operation in candidates:
            if operation_is_expired(operation, now=now):
                if allow_expired:
                    active.append(operation)
                else:
                    expired.append(operation)
            else:
                active.append(operation)

        resolution = {
            "operation_resolution": "latest_pending",
            "operation_id": None,
            "candidate_operations": [_operation_summary(item) for item in active],
        }
        if len(active) == 1:
            operation = active[0]
            return {
                **resolution,
                "status": "resolved",
                "operation_id": str(operation.get("operation_id") or ""),
                "operation": operation,
            }
        if len(active) > 1:
            return {**resolution, "status": "ambiguous", "operation": None}
        if expired:
            operation = expired[0]
            return {
                **resolution,
                "status": "expired",
                "operation_id": str(operation.get("operation_id") or ""),
                "operation": operation,
                "candidate_operations": [_operation_summary(item) for item in expired],
            }
        return {**resolution, "status": "none", "operation": None}

    def mark_confirmed(self, operation_id: str) -> bool:
        self._ensure_schema()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE inbound_pending_operations
                SET status = 'confirmed',
                    confirmed_at = COALESCE(confirmed_at, ?)
                WHERE operation_id = ?
                  AND status = 'previewed'
                """,
                (utc_now_iso(), str(operation_id)),
            )
            return cursor.rowcount == 1

    def update_preview(
        self,
        operation_id: str,
        *,
        payload_hash: str,
        payload: dict[str, Any],
        preview: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = str(operation_id or "").strip()
        if not normalized:
            raise ValueError("operation_id is required")
        self._ensure_schema()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE inbound_pending_operations
                SET payload_hash = ?,
                    payload_json = ?,
                    preview_json = ?,
                    result_json = NULL
                WHERE operation_id = ?
                  AND status = 'previewed'
                """,
                (str(payload_hash), _json(payload), _json(preview), normalized),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"pending operation is not previewed: {normalized}")
        updated = self.get(normalized)
        if updated is None:
            raise RuntimeError(f"failed to load updated inbound operation: {normalized}")
        return updated

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

    def _list_previewed_operations(
        self,
        *,
        channel: str | None,
        sender_id: str | None,
        conversation_id: str | None,
        operation_types: set[str],
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_schema()
        normalized_channel = str(channel or "").strip().lower()
        normalized_sender = str(sender_id or "").strip()
        normalized_conversation = str(conversation_id or "").strip()
        limit_value = max(1, min(int(limit or 100), 500))
        where = ["status = 'previewed'"]
        params: list[Any] = []
        if normalized_channel:
            where.append("channel = ?")
            params.append(normalized_channel)
        if normalized_sender:
            where.append("sender_id = ?")
            params.append(normalized_sender)
        if normalized_conversation and normalized_sender:
            where.append("(conversation_id = ? OR (conversation_id IS NULL OR conversation_id = ''))")
            params.append(normalized_conversation)
        elif normalized_conversation:
            where.append("conversation_id = ?")
            params.append(normalized_conversation)
        params.append(limit_value)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM inbound_pending_operations
                WHERE {' AND '.join(where)}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            operation = _row_to_operation(row)
            if operation is None:
                continue
            if operation_types and str(operation.get("operation_type") or "") not in operation_types:
                continue
            out.append(operation)
        return out

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
                    conversation_id TEXT,
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
            _ensure_column(conn, "conversation_id", "TEXT")


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


def _validate_pending_operation(
    operation: dict[str, Any],
    *,
    channel: str,
    sender_id: str,
    conversation_id: str | None,
    operation_types: set[str],
    allow_expired: bool,
    now: datetime | None,
) -> dict[str, Any]:
    stored_conversation = str(operation.get("conversation_id") or "").strip()
    normalized_conversation = str(conversation_id or "").strip()
    if stored_conversation and normalized_conversation and stored_conversation != normalized_conversation:
        return {"status": "forbidden"}
    if str(operation.get("channel") or "") != (str(channel or "").strip().lower() or "local") or str(operation.get("sender_id") or "") != str(sender_id or "").strip():
        return {"status": "forbidden"}
    operation_type = str(operation.get("operation_type") or "")
    if operation_types and operation_type not in operation_types:
        return {"status": "wrong_family"}
    status = str(operation.get("status") or "").strip()
    if status != "previewed":
        return {"status": "invalid_status"}
    if not allow_expired and operation_is_expired(operation, now=now):
        return {"status": "expired"}
    return {"status": "resolved"}


def _operation_summary(operation: dict[str, Any]) -> dict[str, Any]:
    operation_type = str(operation.get("operation_type") or "")
    return {
        "operation_id": operation.get("operation_id"),
        "operation_type": operation_type,
        "summary": _operation_summary_text(operation_type, operation),
        "conversation_id": operation.get("conversation_id"),
        "created_at": operation.get("created_at"),
        "expires_at": operation.get("expires_at"),
    }


def _operation_summary_text(operation_type: str, operation: dict[str, Any]) -> str:
    payload = operation.get("payload")
    payload_map = payload if isinstance(payload, dict) else {}
    args_raw = payload_map.get("arguments")
    args = args_raw if isinstance(args_raw, dict) else {}
    if operation_type == "manual_open":
        return _manual_open_summary(args)
    if operation_type == "manual_close":
        return _manual_close_summary(args)
    if operation_type.startswith("symbol_"):
        return _symbol_operation_summary(operation_type, args, operation)
    return operation_type or "-"


def _manual_open_summary(args: dict[str, Any]) -> str:
    return " ".join(
        part
        for part in (
            str(args.get("account") or "-"),
            str(args.get("symbol") or "-"),
            _option_contract_text(args),
            f"{args.get('side') or '-'} {args.get('option_type') or '-'}",
            f"{args.get('contracts') or '-'}张",
            f"premium {args.get('premium_per_share') if args.get('premium_per_share') is not None else '-'}",
        )
        if part
    )


def _manual_close_summary(args: dict[str, Any]) -> str:
    record_id = str(args.get("record_id") or "").strip()
    base = f"record_id {record_id}" if record_id else " ".join(
        part
        for part in (
            str(args.get("account") or "-"),
            str(args.get("symbol") or "-"),
            _option_contract_text(args),
            f"{args.get('side') or args.get('position_side') or '-'} {args.get('option_type') or '-'}",
        )
        if part
    )
    return f"{base} close {args.get('contracts_to_close') or '-'}张 @ {args.get('close_price') if args.get('close_price') is not None else '-'}"


def _option_contract_text(args: dict[str, Any]) -> str:
    expiration = str(args.get("expiration_ymd") or "-")
    strike = args.get("strike")
    option_type = str(args.get("option_type") or "").lower()
    suffix = "P" if option_type == "put" else ("C" if option_type == "call" else "")
    strike_text = "-" if strike is None else str(strike)
    return f"{expiration} {strike_text}{suffix}"


def _symbol_operation_summary(operation_type: str, args: dict[str, Any], operation: dict[str, Any]) -> str:
    preview = operation.get("preview")
    preview_map = preview if isinstance(preview, dict) else {}
    summary = preview_map.get("summary")
    summary_map = summary if isinstance(summary, dict) else {}
    symbol = str(summary_map.get("canonical_symbol") or args.get("symbol") or "-")
    if operation_type == "symbol_add":
        modes = []
        if args.get("sell_put_enabled"):
            modes.append("put")
        if args.get("sell_call_enabled"):
            modes.append("call")
        return f"add {symbol} {'/'.join(modes) if modes else '-'}"
    if operation_type == "symbol_edit":
        sets = args.get("set")
        fields = sorted(str(key) for key in sets) if isinstance(sets, dict) else []
        return f"edit {symbol} {','.join(fields) if fields else '-'}"
    if operation_type == "symbol_remove":
        return f"remove {symbol}"
    return f"{operation_type} {symbol}"


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _loads(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _optional_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _ensure_column(conn: sqlite3.Connection, name: str, column_type: str) -> None:
    rows = conn.execute("PRAGMA table_info(inbound_pending_operations)").fetchall()
    existing = {str(row[1]) for row in rows}
    if name not in existing:
        conn.execute(f"ALTER TABLE inbound_pending_operations ADD COLUMN {name} {column_type}")


def _row_to_operation(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = {key: row[key] for key in row.keys()}
    out["payload"] = _loads(out.get("payload_json"))
    out["preview"] = _loads(out.get("preview_json"))
    out["result"] = _loads(out.get("result_json"))
    return out
