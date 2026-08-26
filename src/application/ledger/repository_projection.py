from __future__ import annotations

from .repository_schema import (
    _backfill_trade_event_pagination_schema,
    _position_lot_contract_scalars,
    json,
    sqlite3,
)

class PositionProjectionRepositoryMixin:
    def backfill_position_lot_contract_columns(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        updated = 0
        with self._optional_conn(conn, commit=True) as active_conn:
            updated = self._backfill_position_lot_contract_columns(active_conn)
        return updated

    def _backfill_position_lot_contract_columns(self, conn: sqlite3.Connection) -> int:
        updated = 0
        rows = conn.execute(
            """
            SELECT record_id, fields_json, expiration, strike, multiplier
            FROM position_lots
            """
        ).fetchall()
        for row in rows:
            fields = json.loads(str(row["fields_json"]) or "{}")
            if not isinstance(fields, dict):
                fields = {}
            expiration_ms, strike, multiplier = _position_lot_contract_scalars(fields)
            if (
                row["expiration"] == expiration_ms
                and (
                    (row["strike"] is None and strike is None)
                    or (row["strike"] is not None and strike is not None and abs(float(row["strike"]) - float(strike)) < 1e-9)
                )
                and (
                    (row["multiplier"] is None and multiplier is None)
                    or (
                        row["multiplier"] is not None
                        and multiplier is not None
                        and abs(float(row["multiplier"]) - float(multiplier)) < 1e-9
                    )
                )
            ):
                continue
            conn.execute(
                """
                UPDATE position_lots
                SET expiration = ?, strike = ?, multiplier = ?
                WHERE record_id = ?
                """,
                (
                    int(expiration_ms) if expiration_ms is not None else None,
                    float(strike) if strike is not None else None,
                    float(multiplier) if multiplier is not None else None,
                    str(row["record_id"]),
                ),
            )
            updated += 1
        return updated

    def backfill_position_projection_accounts(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, int]:
        """Explicitly backfill normalized accounts after validating every row."""

        with self._optional_conn(conn, commit=True) as active_conn:
            event_updates: list[tuple[str, str]] = []
            for row in active_conn.execute("SELECT event_id, account, event_json FROM trade_events ORDER BY event_id"):
                try:
                    payload = json.loads(str(row["event_json"] or "{}"))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"trade event JSON is invalid: event_id={row['event_id']}") from exc
                contract_key = payload.get("contract_key") if isinstance(payload, dict) else None
                account = str(
                    (contract_key.get("account") if isinstance(contract_key, dict) else None)
                    or (payload.get("account") if isinstance(payload, dict) else None)
                    or ""
                ).strip()
                if not account or account != account.lower():
                    raise ValueError(f"trade event account cannot be normalized: event_id={row['event_id']}")
                stored = str(row["account"] or "").strip()
                if stored and stored != account:
                    raise ValueError(f"trade event account conflicts with JSON: event_id={row['event_id']}")
                if not stored:
                    event_updates.append((account, str(row["event_id"])))

            lot_updates: list[tuple[str, str]] = []
            for row in active_conn.execute(
                "SELECT record_id, account, fields_json FROM position_lots ORDER BY record_id"
            ):
                try:
                    fields = json.loads(str(row["fields_json"] or "{}"))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"position lot JSON is invalid: record_id={row['record_id']}") from exc
                account = str(fields.get("account") if isinstance(fields, dict) else "").strip()
                if not account or account != account.lower():
                    raise ValueError(f"position lot account cannot be normalized: record_id={row['record_id']}")
                stored = str(row["account"] or "").strip()
                if stored and stored != account:
                    raise ValueError(f"position lot account conflicts with JSON: record_id={row['record_id']}")
                if not stored:
                    lot_updates.append((account, str(row["record_id"])))

            active_conn.executemany(
                "UPDATE trade_events SET account = ? WHERE event_id = ?",
                event_updates,
            )
            active_conn.executemany(
                "UPDATE position_lots SET account = ? WHERE record_id = ?",
                lot_updates,
            )
        return {
            "trade_events_updated": len(event_updates),
            "position_lots_updated": len(lot_updates),
        }

    def backfill_trade_event_pagination(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Run the controlled bounded-memory pagination backfill."""

        with self._optional_conn(conn, commit=True) as active_conn:
            return _backfill_trade_event_pagination_schema(active_conn)

    def build_position_projection_indexes(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[str, ...]:
        """Explicitly build normalized indexes for an already populated store."""

        definitions = (
            (
                "idx_trade_events_trade_time",
                "CREATE INDEX IF NOT EXISTS idx_trade_events_trade_time "
                "ON trade_events(trade_time_ms, event_id)",
            ),
            (
                "idx_trade_events_account_time",
                "CREATE INDEX IF NOT EXISTS idx_trade_events_account_time "
                "ON trade_events(account, trade_time_ms, event_id)",
            ),
            (
                "idx_position_lots_account_expiration",
                "CREATE INDEX IF NOT EXISTS idx_position_lots_account_expiration "
                "ON position_lots(account, expiration, record_id)",
            ),
            (
                "idx_position_lots_account_record",
                "CREATE INDEX IF NOT EXISTS idx_position_lots_account_record ON position_lots(account, record_id)",
            ),
        )
        with self._optional_conn(conn, commit=True) as active_conn:
            before = {
                str(row["name"]) for row in active_conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
            }
            for _name, create_sql in definitions:
                active_conn.execute(create_sql)
        return tuple(name for name, _sql in definitions if name not in before)
