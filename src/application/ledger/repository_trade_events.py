from __future__ import annotations

from .repository_schema import (
    Any,
    Sequence,
    TradeEventPaginationUnavailable,
    _ensure_opend_trade_time_correction_guard,
    _trade_event_pagination_schema_ready,
    _trade_event_query_projections,
    encode_trade_event_for_storage,
    json,
    now_ms,
    sqlite3,
    trade_event_application_payload,
)

class TradeEventRepositoryMixin:
    def count_position_lots(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM position_lots").fetchone()
        return int((row["cnt"] if row is not None else 0) or 0)

    def count_trade_events(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM trade_events").fetchone()
        return int((row["cnt"] if row is not None else 0) or 0)

    def upsert_trade_event(self, event: Any, *, conn: sqlite3.Connection | None = None) -> bool:
        encoded = encode_trade_event_for_storage(event)
        account, market, position_effect = _trade_event_query_projections(
            encoded.event_json
        )
        ts = int(now_ms())
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                """
                SELECT account, event_json, ingest_seq, market, position_effect
                FROM trade_events
                WHERE event_id = ?
                """,
                (encoded.event_id,),
            ).fetchone()
            if existing is not None:
                try:
                    existing_payload = json.loads(str(existing["event_json"]) or "{}")
                except json.JSONDecodeError as exc:
                    raise ValueError(f"existing trade event JSON is invalid: event_id={encoded.event_id}") from exc
                existing_encoded = encode_trade_event_for_storage(existing_payload)
                if existing_encoded.event_json != encoded.event_json:
                    raise ValueError(f"trade event conflict for event_id={encoded.event_id}")
                if (
                    str(existing["account"] or "").strip() != account
                    or existing["ingest_seq"] is None
                    or str(existing["market"] or "").strip().upper() != market
                    or str(existing["position_effect"] or "").strip().lower()
                    != position_effect
                ):
                    raise ValueError(
                        "existing trade event pagination projection is incomplete: "
                        f"event_id={encoded.event_id}"
                    )
                return False
            active_conn.execute(
                """
                UPDATE trade_event_ingest_sequence
                SET last_value = last_value + 1
                WHERE singleton_id = 1
                """
            )
            sequence_row = active_conn.execute(
                """
                SELECT last_value
                FROM trade_event_ingest_sequence
                WHERE singleton_id = 1
                """
            ).fetchone()
            if sequence_row is None:
                raise RuntimeError("trade event ingest sequence allocator is unavailable")
            active_conn.execute(
                """
                INSERT INTO trade_events (
                  event_id, account, event_json, trade_time_ms,
                  created_at_ms, updated_at_ms, ingest_seq, market,
                  position_effect
                ) VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    encoded.event_id,
                    account,
                    encoded.event_json,
                    encoded.event_time_ms,
                    ts,
                    ts,
                    int(sequence_row["last_value"]),
                    market,
                    position_effect,
                ),
            )
        return True

    def compare_and_swap_trade_event_order_identity_json(
        self,
        *,
        event_id: str,
        expected_event_json: str,
        replacement_event_json: str,
        updated_at_ms: int,
        conn: sqlite3.Connection,
    ) -> bool:
        if conn is None or not conn.in_transaction:
            raise ValueError("order identity binding requires an active transaction")
        updated = conn.execute(
            """
            UPDATE trade_events
            SET event_json = ?, updated_at_ms = ?
            WHERE event_id = ? AND event_json = ?
            """,
            (
                str(replacement_event_json),
                int(updated_at_ms),
                str(event_id),
                str(expected_event_json),
            ),
        )
        return int(updated.rowcount or 0) == 1

    def compare_and_swap_trade_event_time(
        self,
        *,
        event_id: str,
        expected_event_json: str,
        expected_trade_time_ms: int,
        replacement_event_json: str,
        replacement_trade_time_ms: int,
        updated_at_ms: int,
        conn: sqlite3.Connection,
    ) -> bool:
        if conn is None or not conn.in_transaction:
            raise ValueError("trade time correction requires an active transaction")
        _ensure_opend_trade_time_correction_guard(conn)
        updated = conn.execute(
            """
            UPDATE trade_events
            SET event_json = ?, trade_time_ms = ?, updated_at_ms = ?
            WHERE event_id = ? AND event_json = ? AND trade_time_ms = ?
            """,
            (
                str(replacement_event_json),
                int(replacement_trade_time_ms),
                int(updated_at_ms),
                str(event_id),
                str(expected_event_json),
                int(expected_trade_time_ms),
            ),
        )
        return int(updated.rowcount or 0) == 1

    def list_trade_events(self, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT event_json
                FROM trade_events
                ORDER BY trade_time_ms ASC, event_id ASC
                """
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = json.loads(str(row["event_json"]) or "{}")
            if isinstance(item, dict):
                out.append(trade_event_application_payload(item))
        return out

    def list_trade_events_page(
        self,
        *,
        limit: int = 10,
        snapshot_max_ingest_seq: int | None = None,
        last_trade_time_ms: int | None = None,
        last_event_id: str | None = None,
        account: str | None = None,
        broker: str | None = None,
        symbol: str | None = None,
        option_type: str | None = None,
        strike: float | None = None,
        expiration_ymd: str | None = None,
        market: str | None = None,
        position_effect: str | None = None,
        authorized_accounts: Sequence[str] = (),
        include_total: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        page_limit = int(limit)
        if not 1 <= page_limit <= 20:
            raise ValueError("events limit must be between 1 and 20")
        normalized_market = str(market or "").strip().upper()
        if normalized_market not in {"US", "HK"}:
            raise ValueError("events market must be US or HK")
        if (last_trade_time_ms is None) != (last_event_id is None):
            raise ValueError("events keyset boundary must be complete")
        if snapshot_max_ingest_seq is not None and int(snapshot_max_ingest_seq) < 0:
            raise ValueError("events snapshot boundary must be non-negative")
        authority_accounts = tuple(
            sorted(
                {
                    str(item or "").strip().lower()
                    for item in authorized_accounts
                    if str(item or "").strip()
                }
            )
        )
        if not authority_accounts:
            raise ValueError("events authority account scope is required")
        normalized_account = str(account or "").strip().lower() or None
        if normalized_account and normalized_account not in authority_accounts:
            raise ValueError("events account is outside the authority scope")

        owned = conn is None
        with self._optional_conn(conn) as active_conn:
            if owned:
                active_conn.execute("BEGIN DEFERRED")
            elif not active_conn.in_transaction:
                raise ValueError(
                    "events page connection must already own a transaction"
                )
            if not _trade_event_pagination_schema_ready(active_conn):
                raise TradeEventPaginationUnavailable(
                    "trade event pagination migration is required"
                )
            fence = snapshot_max_ingest_seq
            if fence is None:
                row = active_conn.execute(
                    "SELECT COALESCE(MAX(ingest_seq), 0) AS max_seq FROM trade_events"
                ).fetchone()
                fence = int(row["max_seq"] if row is not None else 0)

            base_clauses = ["ingest_seq <= ?", "market = ?"]
            base_params: list[Any] = [int(fence), normalized_market]
            if normalized_account:
                base_clauses.append("account = ?")
                base_params.append(normalized_account)
            else:
                placeholders = ", ".join("?" for _ in authority_accounts)
                base_clauses.append(f"account IN ({placeholders})")
                base_params.extend(authority_accounts)
            if position_effect:
                base_clauses.append("position_effect = ?")
                base_params.append(str(position_effect).strip().lower())
            if broker:
                base_clauses.append(
                    "json_extract(event_json, '$.contract_key.broker') = ?"
                )
                base_params.append(str(broker))
            if symbol:
                base_clauses.append(
                    "json_extract(event_json, '$.contract_key.underlying_symbol') = ?"
                )
                base_params.append(str(symbol).strip().upper())
            if option_type:
                base_clauses.append(
                    "json_extract(event_json, '$.contract_key.option_type') = ?"
                )
                base_params.append(str(option_type).strip().lower())
            if expiration_ymd:
                base_clauses.append(
                    "json_extract(event_json, '$.contract_key.expiration_ymd') = ?"
                )
                base_params.append(str(expiration_ymd).strip())
            if strike is not None:
                base_clauses.append(
                    "ABS(CAST(json_extract(event_json, '$.contract_key.strike') AS REAL) - ?) < 1e-9"
                )
                base_params.append(float(strike))

            page_clauses = list(base_clauses)
            page_params = list(base_params)
            if last_trade_time_ms is not None and last_event_id is not None:
                page_clauses.append(
                    "(trade_time_ms, event_id) < (?, ?)"
                )
                page_params.extend(
                    [int(last_trade_time_ms), str(last_event_id)]
                )
            page_where = " AND ".join(page_clauses)
            rows = active_conn.execute(
                f"""
                SELECT event_id, event_json, trade_time_ms
                FROM trade_events
                WHERE {page_where}
                ORDER BY trade_time_ms DESC, event_id DESC
                LIMIT ?
                """,
                (*page_params, page_limit + 1),
            ).fetchall()

            total = None
            if include_total:
                base_where = " AND ".join(base_clauses)
                count_row = active_conn.execute(
                    f"SELECT COUNT(*) AS row_count FROM trade_events WHERE {base_where}",
                    base_params,
                ).fetchone()
                total = int(count_row["row_count"] if count_row is not None else 0)

        page = rows[:page_limit]
        return {
            "rows": [
                trade_event_application_payload(json.loads(str(row["event_json"])))
                for row in page
            ],
            "snapshot_max_ingest_seq": int(fence),
            "has_more": len(rows) > page_limit,
            "total_count": total,
            "last_trade_time_ms": (
                int(page[-1]["trade_time_ms"]) if page else None
            ),
            "last_event_id": str(page[-1]["event_id"]) if page else None,
        }

    def list_position_projection_event_rows(
        self,
        *,
        after: tuple[int, str] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        with self._optional_conn(conn) as active_conn:
            if after is None:
                rows = active_conn.execute(
                    """
                    SELECT event_id, account, event_json, trade_time_ms
                    FROM trade_events
                    ORDER BY trade_time_ms ASC, event_id ASC
                    """
                ).fetchall()
            else:
                rows = active_conn.execute(
                    """
                    SELECT event_id, account, event_json, trade_time_ms
                    FROM trade_events
                    WHERE trade_time_ms > ?
                       OR (trade_time_ms = ? AND event_id > ?)
                    ORDER BY trade_time_ms ASC, event_id ASC
                    """,
                    (int(after[0]), int(after[0]), str(after[1])),
                ).fetchall()
        return [
            {
                "event_id": str(row["event_id"]),
                "account": row["account"],
                "event_json": str(row["event_json"]),
                "trade_time_ms": int(row["trade_time_ms"]),
            }
            for row in rows
        ]

    def get_trade_events_by_ids(
        self,
        event_ids: Sequence[str],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        normalized = tuple(dict.fromkeys(str(item or "").strip() for item in event_ids))
        if not normalized or any(not item for item in normalized):
            return []
        placeholders = ",".join("?" for _item in normalized)
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT event_json
                FROM trade_events
                WHERE event_id IN ({placeholders})
                ORDER BY trade_time_ms ASC, event_id ASC
                """,
                normalized,
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = json.loads(str(row["event_json"]) or "{}")
            if isinstance(item, dict):
                out.append(trade_event_application_payload(item))
        return out
