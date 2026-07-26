from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from src.application.ledger.event_codec import trade_event_application_payload


def open_trade_reconciliation_evidence_repo(
    sqlite_path: str | Path,
) -> Any:
    """Open the minimal ledger evidence surface in SQLite query-only mode."""
    return _ReadOnlyTradeReconciliationEvidenceRepository(Path(sqlite_path))


class _ReadOnlyTradeReconciliationEvidenceRepository:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def list_trade_events(self) -> list[dict[str, Any]]:
        return [
            trade_event_application_payload(item)
            for item in self._read_json_column("trade_events", "event_json")
        ]

    def list_assigned_stock_events(self) -> list[dict[str, Any]]:
        return self._read_json_column("assigned_stock_events", "event_json")

    def list_trade_lifecycle_cases(self) -> list[dict[str, Any]]:
        return self._read_json_column("trade_lifecycle_cases", "raw_json")

    def list_trade_lifecycle_evidence(
        self,
        *,
        case_id: str | None = None,
        account: str | None = None,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._read_json_column("trade_lifecycle_evidence", "raw_json")
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

    def _read_json_column(self, table: str, column: str) -> list[dict[str, Any]]:
        uri = f"{self.path.as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if exists is None:
                return []
            rows = conn.execute(f"SELECT {column} FROM {table}").fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(str(row[column]) or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                out.append(payload)
        return out


__all__ = ["open_trade_reconciliation_evidence_repo"]
