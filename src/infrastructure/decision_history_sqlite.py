"""Immutable decision snapshots. Opening a reader never creates or repairs state."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterator, Mapping

from src.infrastructure.private_storage import connect_private_sqlite


# Serialize mixed read-only/writable handles in this process. SQLite still owns
# cross-process transactions; readers never acquire a filesystem write lock.
_CONNECTION_LOCK = RLock()


class DecisionHistoryError(ValueError):
    """Unavailable, incompatible or conflicting authoritative history."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def history_path(base: Path) -> Path:
    return Path(base) / "output_shared/state/decision_history.sqlite3"


_SCHEMA_STATEMENTS = (
    """CREATE TABLE decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account TEXT NOT NULL, market TEXT NOT NULL, market_date TEXT NOT NULL,
        run_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 0),
        successful INTEGER NOT NULL CHECK(successful IN (0,1)),
        scope_json TEXT NOT NULL, scope_hash TEXT NOT NULL,
        input_json TEXT NOT NULL, input_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
        UNIQUE(account, market, run_id), UNIQUE(account, market, market_date, revision))""",
    "CREATE INDEX decisions_scope_date ON decisions(account,market,market_date,id)",
    "CREATE TRIGGER decisions_no_update BEFORE UPDATE ON decisions BEGIN SELECT RAISE(ABORT, 'immutable_decision'); END",
    "CREATE TRIGGER decisions_no_delete BEFORE DELETE ON decisions BEGIN SELECT RAISE(ABORT, 'immutable_decision'); END",
    """CREATE TABLE history_imports (
        id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL, market TEXT NOT NULL,
        preview_hash TEXT NOT NULL UNIQUE, report_json TEXT NOT NULL, report_hash TEXT NOT NULL)""",
    "CREATE TRIGGER imports_no_update BEFORE UPDATE ON history_imports BEGIN SELECT RAISE(ABORT, 'immutable_import'); END",
    "CREATE TRIGGER imports_no_delete BEFORE DELETE ON history_imports BEGIN SELECT RAISE(ABORT, 'immutable_import'); END",
    "PRAGMA user_version=1",
)


class DecisionHistoryStore:
    def __init__(self, path: Path):
        self.path = Path(path).absolute()

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with _CONNECTION_LOCK:
            try:
                if write:
                    conn = connect_private_sqlite(self.path, timeout=10)
                else:
                    if not self.path.is_file():
                        raise DecisionHistoryError("history_missing")
                    conn = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True, timeout=5)
                with closing(conn):
                    conn.row_factory = sqlite3.Row
                    if not write:
                        conn.execute("PRAGMA query_only=ON")
                    conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
                    if write and version == 0:
                        if conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                            raise DecisionHistoryError("history_schema_invalid")
                        # execute statements separately: executescript would commit the transaction.
                        for statement in _SCHEMA_STATEMENTS:
                            conn.execute(statement)
                    elif version != 1:
                        raise DecisionHistoryError("history_schema_invalid")
                    yield conn
                    if write:
                        conn.commit()
            except sqlite3.Error as exc:
                raise DecisionHistoryError("history_database_error") from exc

    @staticmethod
    def decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        try:
            result = dict(row)
            for name in ("payload", "input"):
                result[name] = json.loads(result.pop(name + "_json"))
                if content_hash(result[name]) != result[name + "_hash"]:
                    raise DecisionHistoryError("history_content_corrupt")
            result["scope"] = json.loads(result.pop("scope_json"))
            if content_hash(result["scope"]) != result["scope_hash"]:
                raise DecisionHistoryError("history_scope_corrupt")
            payload = result["payload"]
            expected = (result["account"], result["market"], result["market_date"], result["run_id"], result["revision"])
            actual = (payload["account"], payload["market"], payload["market_trading_date"], payload["run_id"], payload["revision"])
            if actual != expected:
                raise DecisionHistoryError("history_identity_corrupt")
            return result
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise DecisionHistoryError("history_content_corrupt") from exc

    def get(self, *, account: str, market: str, market_date: str | None = None,
            revision: int | None = None, run_id: str | None = None,
            successful: bool = False) -> dict[str, Any] | None:
        conditions, values = ["account=?", "market=?"], [account, market]
        for key, val in (("market_date", market_date), ("revision", revision), ("run_id", run_id)):
            if val is not None:
                conditions.append(f"{key}=?")
                values.append(val)
        if successful:
            conditions.append("successful=1")
        with self.connect() as conn:
            return self.decode(conn.execute("SELECT * FROM decisions WHERE " + " AND ".join(conditions) +
                                            " ORDER BY market_date DESC,revision DESC LIMIT 1", values).fetchone())

    def revisions(self, *, account: str, market: str, market_date: str) -> list[int]:
        with self.connect() as conn:
            return [int(row[0]) for row in conn.execute(
                "SELECT revision FROM decisions WHERE account=? AND market=? AND market_date=? ORDER BY revision",
                (account, market, market_date))]

    def append(self, source: Mapping[str, Any], *, successful: bool,
               build: Callable[[int, dict[str, Any] | None], dict[str, Any]],
               legacy_present: bool = False) -> tuple[dict[str, Any], dict[str, Any] | None]:
        account, market, day, run_id = (source[k] for k in ("account", "market", "market_trading_date", "run_id"))
        if legacy_present and not self.path.exists():
            raise DecisionHistoryError("history_migration_required")
        with self.connect(write=True) as conn:
            existing = self.decode(conn.execute("SELECT * FROM decisions WHERE account=? AND market=? AND run_id=?",
                                                (account, market, run_id)).fetchone())
            if existing is not None:
                if existing["input_hash"] != content_hash(source) or existing["successful"] != int(successful):
                    raise DecisionHistoryError("history_run_conflict")
                previous = self.decode(conn.execute(
                    "SELECT * FROM decisions WHERE account=? AND market=? AND successful=1 AND (market_date,revision)<(?,?) ORDER BY market_date DESC,revision DESC LIMIT 1",
                    (account, market, existing["market_date"], existing["revision"])).fetchone())
                return existing["payload"], previous["payload"] if previous else None
            any_row = conn.execute("SELECT 1 FROM decisions WHERE account=? AND market=? LIMIT 1", (account, market)).fetchone()
            imports = self.import_reports(conn, account=account, market=market)
            if not any_row and not imports and legacy_present:
                raise DecisionHistoryError("history_migration_required")
            previous = self.decode(conn.execute(
                "SELECT * FROM decisions WHERE account=? AND market=? AND successful=1 ORDER BY market_date DESC,revision DESC LIMIT 1",
                (account, market)).fetchone())
            previous_payload = previous["payload"] if previous else None
            revision = conn.execute("SELECT COALESCE(MAX(revision),-1)+1 FROM decisions WHERE account=? AND market=? AND market_date=?",
                                    (account, market, day)).fetchone()[0]
            reserved = max((int(report.get("reserved_revisions", {}).get(day, -1)) for report in imports), default=-1)
            revision = max(revision, reserved + 1)
            payload = build(revision, previous_payload)
            self.insert(conn, source=source, payload=payload, successful=successful, scope=source.get("decision_scope") or {})
            return payload, previous_payload

    @staticmethod
    def insert(conn: sqlite3.Connection, *, source: Mapping[str, Any], payload: Mapping[str, Any],
               successful: bool, scope: Mapping[str, Any]) -> None:
        conn.execute("""INSERT INTO decisions(account,market,market_date,run_id,revision,successful,scope_json,scope_hash,
                     input_json,input_hash,payload_json,payload_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (payload["account"], payload["market"], payload["market_trading_date"], payload["run_id"], payload["revision"],
                      int(successful), canonical_json(scope), content_hash(scope), canonical_json(source), content_hash(source),
                      canonical_json(payload), content_hash(payload)))

    @staticmethod
    def import_reports(conn: sqlite3.Connection, *, account: str, market: str) -> list[dict[str, Any]]:
        reports = []
        for row in conn.execute("SELECT report_json, report_hash FROM history_imports WHERE account=? AND market=? ORDER BY id", (account, market)):
            try:
                report = json.loads(row[0])
            except (TypeError, ValueError) as exc:
                raise DecisionHistoryError("history_import_receipt_corrupt") from exc
            if content_hash(report) != row[1]:
                raise DecisionHistoryError("history_import_receipt_corrupt")
            reports.append(report)
        return reports
