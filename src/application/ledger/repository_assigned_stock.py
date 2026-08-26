from __future__ import annotations

from .repository_schema import (
    Any,
    Mapping,
    _json_object,
    _json_text,
    json,
    normalize_wheel_event,
    now_ms,
    sqlite3,
)

class AssignedStockRepositoryMixin:
    def upsert_assigned_stock_event(self, event: dict[str, Any], *, conn: sqlite3.Connection | None = None) -> bool:
        if not isinstance(event, dict):
            raise TypeError("assigned stock event must be a JSON object")
        stock_event_id = str(event.get("stock_event_id") or event.get("event_id") or "").strip()
        if not stock_event_id:
            raise ValueError("assigned stock event requires stock_event_id")
        account = str(event.get("account") or "").strip()
        if not account or account != account.lower():
            raise ValueError("assigned stock event requires lowercase account")
        try:
            trade_time_ms = int(event.get("trade_time_ms") or event.get("event_time_ms") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("assigned stock event requires numeric trade_time_ms") from exc
        if trade_time_ms <= 0:
            raise ValueError("assigned stock event requires trade_time_ms > 0")
        payload = dict(event)
        payload["stock_event_id"] = stock_event_id
        payload["account"] = account
        payload["trade_time_ms"] = trade_time_ms
        event_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        ts = int(now_ms())
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                "SELECT event_json FROM assigned_stock_events WHERE stock_event_id = ?",
                (stock_event_id,),
            ).fetchone()
            if existing is not None:
                existing_json = str(existing["event_json"] or "")
                if existing_json != event_json:
                    raise ValueError(f"assigned stock event conflict for stock_event_id={stock_event_id}")
                return False
            active_conn.execute(
                """
                INSERT INTO assigned_stock_events (
                  stock_event_id, account, event_json, trade_time_ms,
                  created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (stock_event_id, account, event_json, trade_time_ms, ts, ts),
            )
        return True

    def list_assigned_stock_events(self, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        if not self._table_exists("assigned_stock_events"):
            return []
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT event_json
                FROM assigned_stock_events
                ORDER BY trade_time_ms ASC, stock_event_id ASC
                """
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = json.loads(str(row["event_json"]) or "{}")
            if isinstance(item, dict):
                out.append(item)
        return out

    def list_assigned_stock_events_for_account(
        self,
        account: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        account_value = str(account or "").strip()
        if not account_value or account_value != account_value.lower():
            raise ValueError("assigned stock account must be lowercase")
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT event_json
                FROM assigned_stock_events
                WHERE account = ?
                ORDER BY trade_time_ms ASC, stock_event_id ASC
                """,
                (account_value,),
            ).fetchall()
        return [_json_object(row["event_json"]) for row in rows]

    def append_wheel_event_once(
        self,
        event: Mapping[str, Any],
        *,
        conn: sqlite3.Connection,
    ) -> bool:
        if conn is None or not conn.in_transaction:
            raise ValueError("wheel event append requires an active transaction")
        payload = normalize_wheel_event(event)
        existing = conn.execute(
            "SELECT payload_hash FROM wheel_events WHERE event_id = ?",
            (payload["event_id"],),
        ).fetchone()
        if existing is not None:
            if str(existing["payload_hash"] or "") != payload["payload_hash"]:
                raise ValueError(
                    f"wheel event conflict for event_id={payload['event_id']}"
                )
            return False
        conn.execute(
            """
            INSERT INTO wheel_events (
              event_id, account, stock_lot_id, event_type,
              occurred_at_ms, recorded_at_ms, intent_id,
              source_trade_event_id, payload_json, payload_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["event_id"],
                payload["account"],
                payload["stock_lot_id"],
                payload["event_type"],
                payload["occurred_at_ms"],
                payload["recorded_at_ms"],
                payload["intent_id"],
                payload["source_trade_event_id"],
                _json_text(payload["payload"]),
                payload["payload_hash"],
            ),
        )
        return True

    def list_wheel_events(
        self,
        *,
        account: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        account_value = str(account or "").strip().lower()
        with self._optional_conn(conn) as active_conn:
            if account_value:
                rows = active_conn.execute(
                    """
                    SELECT * FROM wheel_events
                    WHERE account = ?
                    ORDER BY occurred_at_ms ASC, event_id ASC
                    """,
                    (account_value,),
                ).fetchall()
            else:
                rows = active_conn.execute(
                    """
                    SELECT * FROM wheel_events
                    ORDER BY occurred_at_ms ASC, event_id ASC
                    """
                ).fetchall()
        return [
            normalize_wheel_event(
                {
                    "event_id": row["event_id"],
                    "account": row["account"],
                    "stock_lot_id": row["stock_lot_id"],
                    "event_type": row["event_type"],
                    "occurred_at_ms": row["occurred_at_ms"],
                    "recorded_at_ms": row["recorded_at_ms"],
                    "intent_id": row["intent_id"],
                    "source_trade_event_id": row["source_trade_event_id"],
                    "payload": _json_object(row["payload_json"]),
                    "payload_hash": row["payload_hash"],
                }
            )
            for row in rows
        ]
