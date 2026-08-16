from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from src.application.ledger.event_codec import trade_event_application_payload
from src.application.ledger.sqlite_row_codec import (
    read_current_decision_projection_inputs_from_conn,
)


def open_trade_reconciliation_evidence_repo(
    sqlite_path: str | Path,
) -> Any:
    """Open the minimal ledger evidence surface in SQLite query-only mode."""
    return _ReadOnlyTradeReconciliationEvidenceRepository(Path(sqlite_path))


class _ReadOnlyTradeReconciliationEvidenceRepository:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def _connect(self) -> sqlite3.Connection:
        uri = f"{self.path.as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    def list_trade_events(self) -> list[dict[str, Any]]:
        return [
            trade_event_application_payload(item)
            for item in self._read_json_column("trade_events", "event_json")
        ]

    def list_assigned_stock_events(self) -> list[dict[str, Any]]:
        return self._read_json_column("assigned_stock_events", "event_json")

    def list_position_lots(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = self._read_position_lots(conn)
        return rows

    def read_current_decision_projection_inputs(
        self,
        account: str,
        *,
        conn: sqlite3.Connection | None = None,
        include_identities: bool = True,
    ) -> dict[str, Any]:
        if conn is not None:
            return read_current_decision_projection_inputs_from_conn(
                conn,
                account,
                include_identities=include_identities,
            )
        with closing(self._connect()) as active_conn:
            active_conn.execute("BEGIN")
            return read_current_decision_projection_inputs_from_conn(
                active_conn,
                account,
                include_identities=include_identities,
            )

    def _read_position_lots(
        self,
        conn: sqlite3.Connection,
        *,
        strict: bool = False,
    ) -> list[dict[str, Any]]:
        if not self._table_exists(conn, "position_lots"):
            return []
        rows = conn.execute(
            """
            SELECT record_id, fields_json
            FROM position_lots
            ORDER BY updated_at_ms DESC, record_id DESC
            """
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                fields = json.loads(str(row["fields_json"]) or "{}")
            except (TypeError, ValueError):
                if strict:
                    raise
                continue
            if not isinstance(fields, dict):
                if strict:
                    fields = {}
                else:
                    continue
            out.append(
                {
                    "record_id": str(row["record_id"] or ""),
                    "fields": fields,
                }
            )
        return out

    def read_lifecycle_account_rows(
        self,
        *,
        account: str,
    ) -> dict[str, Any]:
        """Read one account's lifecycle inputs from a single SQLite snapshot."""

        account_value = str(account or "").strip().lower()
        if not account_value:
            raise ValueError("lifecycle account reader requires account")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            cases = (
                self._read_json_query_from_conn(
                    conn,
                    """
                    SELECT raw_json
                    FROM trade_lifecycle_cases
                    WHERE account = ?
                    ORDER BY updated_at_ms DESC, case_id DESC
                    """,
                    (account_value,),
                    strict=True,
                )
                if self._table_exists(conn, "trade_lifecycle_cases")
                else []
            )
            evidence = (
                self._read_json_query_from_conn(
                    conn,
                    """
                    SELECT item.raw_json, item.created_at_ms
                    FROM trade_lifecycle_evidence AS item
                    JOIN trade_lifecycle_cases AS lifecycle_case
                      ON lifecycle_case.case_id = item.case_id
                    WHERE lifecycle_case.account = ?
                    ORDER BY item.created_at_ms ASC, item.evidence_id ASC
                    """,
                    (account_value,),
                    created_at_field="_ledger_created_at_ms",
                    strict=True,
                )
                if self._tables_exist(
                    conn,
                    "trade_lifecycle_cases",
                    "trade_lifecycle_evidence",
                )
                else []
            )
            allocations = (
                self._read_json_query_from_conn(
                    conn,
                    """
                    SELECT item.raw_json
                    FROM trade_lifecycle_allocations AS item
                    JOIN trade_lifecycle_cases AS lifecycle_case
                      ON lifecycle_case.case_id = item.case_id
                    WHERE lifecycle_case.account = ?
                    ORDER BY item.created_at_ms ASC, item.allocation_id ASC
                    """,
                    (account_value,),
                    strict=True,
                )
                if self._tables_exist(
                    conn,
                    "trade_lifecycle_cases",
                    "trade_lifecycle_allocations",
                )
                else []
            )
            source_claims = (
                self._read_json_query_from_conn(
                    conn,
                    """
                    SELECT item.raw_json
                    FROM trade_lifecycle_source_consumptions AS item
                    JOIN trade_lifecycle_cases AS lifecycle_case
                      ON lifecycle_case.case_id = item.case_id
                    WHERE lifecycle_case.account = ?
                    ORDER BY item.created_at_ms ASC, item.source_key ASC
                    """,
                    (account_value,),
                    strict=True,
                )
                if self._tables_exist(
                    conn,
                    "trade_lifecycle_cases",
                    "trade_lifecycle_source_consumptions",
                )
                else []
            )
            timing_policies = (
                self._read_json_query_from_conn(
                    conn,
                    """
                    SELECT item.raw_json
                    FROM trade_lifecycle_timing_policies AS item
                    JOIN trade_lifecycle_cases AS lifecycle_case
                      ON lifecycle_case.case_id = item.case_id
                    WHERE lifecycle_case.account = ?
                    ORDER BY item.case_id ASC
                    """,
                    (account_value,),
                    strict=True,
                )
                if self._tables_exist(
                    conn,
                    "trade_lifecycle_cases",
                    "trade_lifecycle_timing_policies",
                )
                else []
            )
            trade_events = self._read_trade_events(conn, strict=True)
            lots = self._read_position_lots(conn, strict=True)
            conn.commit()
        return {
            "account": account_value,
            "trade_events": trade_events,
            "stored_position_lots": lots,
            "account_position_lots": [
                item
                for item in lots
                if str((item.get("fields") or {}).get("account") or "")
                .strip()
                .lower()
                == account_value
            ],
            "account_lifecycle_cases": cases,
            "account_lifecycle_evidence": evidence,
            "account_lifecycle_evidence_received_at_ms_by_id": {
                str(item.get("evidence_id") or "").strip(): int(
                    item.get("_ledger_created_at_ms") or 0
                )
                for item in evidence
                if str(item.get("evidence_id") or "").strip()
                and int(item.get("_ledger_created_at_ms") or 0) > 0
            },
            "account_lifecycle_allocations": allocations,
            "account_lifecycle_source_consumptions": source_claims,
            "account_lifecycle_timing_policies": timing_policies,
        }

    def list_trade_lifecycle_cases(self) -> list[dict[str, Any]]:
        return self._read_json_column("trade_lifecycle_cases", "raw_json")

    def get_trade_lifecycle_case(
        self,
        case_id: str,
    ) -> dict[str, Any] | None:
        wanted = str(case_id or "").strip()
        return next(
            (
                item
                for item in self.list_trade_lifecycle_cases()
                if str(item.get("case_id") or "").strip() == wanted
            ),
            None,
        )

    def list_trade_lifecycle_evidence(
        self,
        *,
        case_id: str | None = None,
        account: str | None = None,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._read_json_column(
            "trade_lifecycle_evidence",
            "raw_json",
            created_at_field="_ledger_created_at_ms",
        )
        if case_id:
            rows = [item for item in rows if str(item.get("case_id") or "") == str(case_id)]
        if account:
            rows = [
                item
                for item in rows
                if str(item.get("account") or "").strip().lower()
                == str(account).strip().lower()
            ]
        if symbol:
            rows = [
                item
                for item in rows
                if str(item.get("symbol") or "").strip().upper()
                == str(symbol).strip().upper()
            ]
        return rows

    def list_trade_lifecycle_allocations(
        self,
        *,
        case_id: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._read_json_column(
            "trade_lifecycle_allocations",
            "raw_json",
        )
        if case_id:
            wanted = str(case_id).strip()
            rows = [
                item
                for item in rows
                if str(item.get("case_id") or "").strip() == wanted
            ]
        return rows

    def list_trade_lifecycle_source_consumptions(
        self,
        *,
        case_id: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._read_json_column(
            "trade_lifecycle_source_consumptions",
            "raw_json",
        )
        if case_id:
            wanted = str(case_id).strip()
            rows = [
                item
                for item in rows
                if str(item.get("case_id") or "").strip() == wanted
            ]
        return rows

    def list_trade_lifecycle_timing_policies(
        self,
    ) -> list[dict[str, Any]]:
        return self._read_json_column(
            "trade_lifecycle_timing_policies",
            "raw_json",
        )

    def get_trade_lifecycle_timing_policy(
        self,
        case_id: str,
    ) -> dict[str, Any] | None:
        wanted = str(case_id or "").strip()
        return next(
            (
                item
                for item in self.list_trade_lifecycle_timing_policies()
                if str(item.get("case_id") or "").strip() == wanted
            ),
            None,
        )

    def _read_json_column(
        self,
        table: str,
        column: str,
        *,
        created_at_field: str | None = None,
    ) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            return self._read_json_column_from_conn(
                conn,
                table,
                column,
                created_at_field=created_at_field,
            )

    def _read_json_column_from_conn(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        *,
        created_at_field: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self._table_exists(conn, table):
            return []
        selected = (
            f"{column}, created_at_ms"
            if created_at_field
            else column
        )
        return self._read_json_query_from_conn(
            conn,
            f"SELECT {selected} FROM {table}",
            (),
            column=column,
            created_at_field=created_at_field,
        )

    @staticmethod
    def _read_json_query_from_conn(
        conn: sqlite3.Connection,
        query: str,
        params: tuple[Any, ...],
        *,
        column: str = "raw_json",
        created_at_field: str | None = None,
        strict: bool = False,
    ) -> list[dict[str, Any]]:
        rows = conn.execute(query, params).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(str(row[column]) or "{}")
            except (TypeError, ValueError):
                if strict:
                    raise
                continue
            if not isinstance(payload, dict):
                if strict:
                    raise ValueError("stored ledger JSON value must be an object")
                continue
            if created_at_field:
                payload[created_at_field] = int(row["created_at_ms"])
            out.append(payload)
        return out

    def _read_trade_events(
        self,
        conn: sqlite3.Connection,
        *,
        strict: bool = False,
    ) -> list[dict[str, Any]]:
        if not self._table_exists(conn, "trade_events"):
            return []
        rows = conn.execute(
            """
            SELECT event_json
            FROM trade_events
            ORDER BY trade_time_ms ASC, event_id ASC
            """
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(str(row["event_json"]) or "{}")
            except (TypeError, ValueError):
                if strict:
                    raise
                continue
            if isinstance(payload, dict):
                out.append(trade_event_application_payload(payload))
        return out

    @classmethod
    def _tables_exist(
        cls,
        conn: sqlite3.Connection,
        *tables: str,
    ) -> bool:
        return all(cls._table_exists(conn, table) for table in tables)

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            is not None
        )


__all__ = ["open_trade_reconciliation_evidence_repo"]
