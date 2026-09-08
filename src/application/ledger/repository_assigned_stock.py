from __future__ import annotations

from .external_event_key import ensure_execution_writer_guard
from .repository_trade_schema import _execution_candidate_rows, validated_execution_identity_metadata

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


def _wheel_activation_scope(market: str, account: str) -> tuple[str, str]:
    market_value = str(market or "").strip().lower()
    account_value = str(account or "").strip()
    if market_value not in {"us", "hk"}:
        raise ValueError("wheel activation market must be us or hk")
    if not account_value or account_value != account_value.lower():
        raise ValueError("wheel activation account must be lowercase")
    return market_value, account_value


def _wheel_activation_hash(value: str, field: str) -> str:
    digest = str(value or "").strip()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"{field} must be a lowercase sha256")
    return digest


def _wheel_activation_request_id(value: str) -> str:
    request_id = str(value or "").strip()
    if not request_id:
        raise ValueError("wheel activation request_id is required")
    return request_id


def _wheel_activation_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "market": str(row["market"]),
        "account": str(row["account"]),
        "generation": int(row["generation"]),
        "activated_at_ms": int(row["activated_at_ms"]),
        "deactivated_at_ms": (
            int(row["deactivated_at_ms"])
            if row["deactivated_at_ms"] is not None
            else None
        ),
        "policy_hash": str(row["policy_hash"]),
        "activation_request_id": str(row["activation_request_id"]),
        "activation_request_hash": str(row["activation_request_hash"]),
        "deactivation_request_id": (
            str(row["deactivation_request_id"])
            if row["deactivation_request_id"] is not None
            else None
        ),
        "deactivation_request_hash": (
            str(row["deactivation_request_hash"])
            if row["deactivation_request_hash"] is not None
            else None
        ),
    }


class AssignedStockRepositoryMixin:
    def compare_and_swap_assigned_stock_order_identity_json(
        self,
        *,
        event_id: str,
        expected_event_json: str,
        replacement_event_json: str,
        updated_at_ms: int,
        conn: sqlite3.Connection,
    ) -> bool:
        if conn is None or not conn.in_transaction:
            raise ValueError("stock order identity binding requires an active transaction")
        validated_execution_identity_metadata(json.loads(replacement_event_json))
        updated = conn.execute(
            "UPDATE assigned_stock_events SET event_json = ?, updated_at_ms = ? "
            "WHERE stock_event_id = ? AND event_json = ?",
            (replacement_event_json, int(updated_at_ms), event_id, expected_event_json),
        )
        return int(updated.rowcount or 0) == 1

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
            execution_id = validated_execution_identity_metadata(payload)
            if execution_id:
                if payload.get("execution_id") != execution_id:
                    raise ValueError("trade_execution_identity_metadata_mismatch")
                ensure_execution_writer_guard(active_conn)
                duplicate_execution = active_conn.execute(
                    "SELECT stock_event_id FROM assigned_stock_events WHERE json_extract(event_json, '$.execution_id') = ? AND stock_event_id != ?",
                    (execution_id, stock_event_id),
                ).fetchone()
                if duplicate_execution is not None:
                    raise ValueError("trade execution already has an assigned stock event")
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

    def list_assigned_stock_events_for_execution(
        self, execution_id: str, *, conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        with self._optional_conn(conn) as active_conn:
            if not self._table_exists("assigned_stock_events", conn=active_conn):
                return []
            rows = _execution_candidate_rows(active_conn, "assigned_stock_events", execution_id)
            if rows is None:
                return self.list_assigned_stock_events(conn=active_conn)
        return [json.loads(row["event_json"]) for row in rows]

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
            """
            SELECT event_schema_version, wheel_branch_id, payload_hash
            FROM wheel_events
            WHERE event_id = ?
            """,
            (payload["event_id"],),
        ).fetchone()
        if existing is not None:
            existing_identity = (
                str(existing["event_schema_version"] or ""),
                str(existing["wheel_branch_id"] or ""),
                str(existing["payload_hash"] or ""),
            )
            payload_identity = (
                payload["event_schema_version"],
                payload["wheel_branch_id"],
                payload["payload_hash"],
            )
            if existing_identity != payload_identity:
                raise ValueError(
                    f"wheel event conflict for event_id={payload['event_id']}"
                )
            return False
        conn.execute(
            """
            INSERT INTO wheel_events (
              event_id, event_schema_version, account, wheel_branch_id,
              stock_lot_id, event_type, occurred_at_ms, recorded_at_ms,
              intent_id, source_trade_event_id, payload_json, payload_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["event_id"],
                payload["event_schema_version"],
                payload["account"],
                payload["wheel_branch_id"],
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
                    "event_schema_version": row["event_schema_version"],
                    "account": row["account"],
                    "wheel_branch_id": row["wheel_branch_id"],
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

    def list_wheel_activation_windows(
        self,
        *,
        market: str,
        account: str,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        market_value, account_value = _wheel_activation_scope(market, account)
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT *
                FROM wheel_activation_windows
                WHERE market = ? AND account = ?
                ORDER BY generation ASC
                """,
                (market_value, account_value),
            ).fetchall()
        return [_wheel_activation_row(row) for row in rows]

    def get_current_wheel_activation_window(
        self,
        *,
        market: str,
        account: str,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        market_value, account_value = _wheel_activation_scope(market, account)
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT *
                FROM wheel_activation_windows
                WHERE market = ? AND account = ? AND deactivated_at_ms IS NULL
                ORDER BY generation DESC
                """,
                (market_value, account_value),
            ).fetchall()
        if len(rows) > 1:
            raise RuntimeError("wheel activation open-window uniqueness violated")
        return _wheel_activation_row(rows[0]) if rows else None

    def get_wheel_activation_window_for_event(
        self,
        *,
        market: str,
        account: str,
        occurred_at_ms: int,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        market_value, account_value = _wheel_activation_scope(market, account)
        try:
            event_time_ms = int(occurred_at_ms)
        except (TypeError, ValueError):
            raise ValueError("wheel activation occurred_at_ms must be positive") from None
        if event_time_ms <= 0:
            raise ValueError("wheel activation occurred_at_ms must be positive")
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT *
                FROM wheel_activation_windows
                WHERE market = ?
                  AND account = ?
                  AND activated_at_ms <= ?
                  AND (deactivated_at_ms IS NULL OR ? < deactivated_at_ms)
                ORDER BY generation ASC
                """,
                (market_value, account_value, event_time_ms, event_time_ms),
            ).fetchall()
        if len(rows) > 1:
            raise RuntimeError("wheel activation historical-window uniqueness violated")
        return _wheel_activation_row(rows[0]) if rows else None

    def _wheel_activation_request_replay(
        self,
        *,
        market: str,
        account: str,
        action: str,
        request_id: str,
        request_hash: str,
        policy_hash: str,
        conn: sqlite3.Connection,
    ) -> dict[str, Any] | None:
        rows = conn.execute(
            """
            SELECT *
            FROM wheel_activation_windows
            WHERE market = ?
              AND account = ?
              AND (activation_request_id = ? OR deactivation_request_id = ?)
            ORDER BY generation ASC
            """,
            (market, account, request_id, request_id),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError("wheel activation request identity is not unique")
        row = rows[0]
        stored_action = (
            "activate" if row["activation_request_id"] == request_id else "deactivate"
        )
        stored_hash = (
            str(row["activation_request_hash"])
            if stored_action == "activate"
            else str(row["deactivation_request_hash"])
        )
        if (
            stored_action != action
            or stored_hash != request_hash
            or str(row["policy_hash"]) != policy_hash
        ):
            raise ValueError(f"wheel activation request conflict for request_id={request_id}")
        return {
            "action": action,
            "write_applied": False,
            "idempotent": True,
            "window": _wheel_activation_row(row),
        }

    def open_wheel_activation_window(
        self,
        *,
        market: str,
        account: str,
        expected_current_generation: int,
        policy_hash: str,
        request_id: str,
        request_hash: str,
        conn: sqlite3.Connection,
    ) -> dict[str, Any]:
        if conn is None or not conn.in_transaction:
            raise ValueError("wheel activation requires an active transaction")
        market_value, account_value = _wheel_activation_scope(market, account)
        policy_hash_value = _wheel_activation_hash(policy_hash, "policy_hash")
        request_id_value = _wheel_activation_request_id(request_id)
        request_hash_value = _wheel_activation_hash(request_hash, "request_hash")
        replay = self._wheel_activation_request_replay(
            market=market_value,
            account=account_value,
            action="activate",
            request_id=request_id_value,
            request_hash=request_hash_value,
            policy_hash=policy_hash_value,
            conn=conn,
        )
        if replay is not None:
            return replay
        latest = conn.execute(
            """
            SELECT *
            FROM wheel_activation_windows
            WHERE market = ? AND account = ?
            ORDER BY generation DESC
            LIMIT 1
            """,
            (market_value, account_value),
        ).fetchone()
        current_generation = int(latest["generation"]) if latest is not None else 0
        if int(expected_current_generation) != current_generation:
            raise ValueError("wheel activation generation conflict")
        if latest is not None and latest["deactivated_at_ms"] is None:
            raise ValueError("wheel activation window is already open")
        activated_at_ms = max(
            int(now_ms()),
            (int(latest["activated_at_ms"]) + 1) if latest is not None else 1,
            int(latest["deactivated_at_ms"] or 0) if latest is not None else 0,
        )
        generation = current_generation + 1
        conn.execute(
            """
            INSERT INTO wheel_activation_windows (
              market, account, generation, activated_at_ms, deactivated_at_ms,
              policy_hash, activation_request_id, activation_request_hash,
              deactivation_request_id, deactivation_request_hash
            ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL)
            """,
            (
                market_value,
                account_value,
                generation,
                activated_at_ms,
                policy_hash_value,
                request_id_value,
                request_hash_value,
            ),
        )
        row = conn.execute(
            """
            SELECT * FROM wheel_activation_windows
            WHERE market = ? AND account = ? AND generation = ?
            """,
            (market_value, account_value, generation),
        ).fetchone()
        if row is None:
            raise RuntimeError("wheel activation write readback failed")
        return {
            "action": "activate",
            "write_applied": True,
            "idempotent": False,
            "window": _wheel_activation_row(row),
        }

    def close_wheel_activation_window(
        self,
        *,
        market: str,
        account: str,
        expected_current_generation: int,
        policy_hash: str,
        request_id: str,
        request_hash: str,
        conn: sqlite3.Connection,
    ) -> dict[str, Any]:
        if conn is None or not conn.in_transaction:
            raise ValueError("wheel deactivation requires an active transaction")
        market_value, account_value = _wheel_activation_scope(market, account)
        policy_hash_value = _wheel_activation_hash(policy_hash, "policy_hash")
        request_id_value = _wheel_activation_request_id(request_id)
        request_hash_value = _wheel_activation_hash(request_hash, "request_hash")
        replay = self._wheel_activation_request_replay(
            market=market_value,
            account=account_value,
            action="deactivate",
            request_id=request_id_value,
            request_hash=request_hash_value,
            policy_hash=policy_hash_value,
            conn=conn,
        )
        if replay is not None:
            return replay
        row = conn.execute(
            """
            SELECT *
            FROM wheel_activation_windows
            WHERE market = ? AND account = ? AND deactivated_at_ms IS NULL
            """,
            (market_value, account_value),
        ).fetchone()
        if row is None:
            raise ValueError("wheel activation window is not open")
        generation = int(row["generation"])
        if int(expected_current_generation) != generation:
            raise ValueError("wheel activation generation conflict")
        if str(row["policy_hash"]) != policy_hash_value:
            raise ValueError("wheel activation policy hash conflict")
        deactivated_at_ms = max(int(now_ms()), int(row["activated_at_ms"]) + 1)
        updated = conn.execute(
            """
            UPDATE wheel_activation_windows
            SET deactivated_at_ms = ?,
                deactivation_request_id = ?,
                deactivation_request_hash = ?
            WHERE market = ?
              AND account = ?
              AND generation = ?
              AND deactivated_at_ms IS NULL
            """,
            (
                deactivated_at_ms,
                request_id_value,
                request_hash_value,
                market_value,
                account_value,
                generation,
            ),
        )
        if int(updated.rowcount or 0) != 1:
            raise ValueError("wheel activation close compare-and-swap failed")
        readback = conn.execute(
            """
            SELECT * FROM wheel_activation_windows
            WHERE market = ? AND account = ? AND generation = ?
            """,
            (market_value, account_value, generation),
        ).fetchone()
        if readback is None:
            raise RuntimeError("wheel deactivation write readback failed")
        return {
            "action": "deactivate",
            "write_applied": True,
            "idempotent": False,
            "window": _wheel_activation_row(readback),
        }
