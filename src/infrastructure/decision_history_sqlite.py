"""Immutable decision snapshots. Opening a reader never creates or repairs state."""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import closing, contextmanager, nullcontext
from contextvars import ContextVar
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterator, Mapping

from src.infrastructure.private_storage import connect_private_sqlite


_LOGGER = logging.getLogger(__name__)


# Serialize mixed read-only/writable handles in this process. SQLite still owns
# cross-process transactions; readers never acquire a filesystem write lock.
_CONNECTION_LOCK = RLock()


class DecisionHistoryError(ValueError):
    """Unavailable, incompatible or conflicting authoritative history."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(value: Any) -> str:
    return _json_hash(canonical_json(value))


def _json_hash(encoded: str) -> str:
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


_CANONICAL_HISTORY_HASHES: ContextVar[set[str] | None] = ContextVar(
    "canonical_history_hashes", default=None,
)
_UNDECODED = object()


@contextmanager
def history_hash_read_scope() -> Iterator[None]:
    """Reuse canonical encodings verified within one tick, retaining only digests."""
    token = _CANONICAL_HISTORY_HASHES.set(set())
    try:
        yield
    finally:
        _CANONICAL_HISTORY_HASHES.reset(token)


def _decoded_content_hash(encoded: str, value: Any = _UNDECODED) -> str:
    known = _CANONICAL_HISTORY_HASHES.get()
    raw_hash = _json_hash(encoded) if known is not None and isinstance(encoded, str) else None
    if raw_hash is not None and raw_hash in known:
        return raw_hash
    canonical_hash = content_hash(json.loads(encoded) if value is _UNDECODED else value)
    if known is not None and raw_hash == canonical_hash:
        known.add(canonical_hash)
    return canonical_hash


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
                _LOGGER.exception(
                    "Decision history SQLite error (db=%s, write=%s, sqlite_errorname=%s, sqlite_errorcode=%s)",
                    self.path,
                    write,
                    getattr(exc, "sqlite_errorname", None),
                    getattr(exc, "sqlite_errorcode", None),
                )
                raise DecisionHistoryError("history_database_error") from exc

    @staticmethod
    def decode(row: sqlite3.Row | None, *, _payload_only: bool = False) -> dict[str, Any] | None:
        if row is None:
            return None
        try:
            result = dict(row)
            payload_json = result.pop("payload_json")
            input_json = result.pop("input_json")
            result["payload"] = json.loads(payload_json)
            payload_hash = _decoded_content_hash(payload_json, result["payload"])
            if payload_hash != result["payload_hash"]:
                raise DecisionHistoryError("history_content_corrupt")
            # Full reads return independently mutable input and payload. For a
            # payload-only read, the input hash still needs fresh verification.
            source = _UNDECODED if _payload_only else json.loads(input_json)
            input_hash = payload_hash if input_json == payload_json else _decoded_content_hash(input_json, source)
            if input_hash != result["input_hash"]:
                raise DecisionHistoryError("history_content_corrupt")
            if not _payload_only:
                result["input"] = source
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
            successful: bool = False, conn: sqlite3.Connection | None = None,
            _payload_only: bool = False) -> dict[str, Any] | None:
        conditions, values = ["account=?", "market=?"], [account, market]
        for key, val in (("market_date", market_date), ("revision", revision), ("run_id", run_id)):
            if val is not None:
                conditions.append(f"{key}=?")
                values.append(val)
        if successful:
            conditions.append("successful=1")
        with (self.connect() if conn is None else nullcontext(conn)) as connection:
            row = connection.execute("SELECT * FROM decisions WHERE " + " AND ".join(conditions) +
                                     " ORDER BY market_date DESC,revision DESC LIMIT 1", values).fetchone()
            return self.decode(row, _payload_only=_payload_only)

    def revisions(self, *, account: str, market: str, market_date: str,
                  conn: sqlite3.Connection | None = None) -> list[int]:
        with (self.connect() if conn is None else nullcontext(conn)) as connection:
            return [int(row[0]) for row in connection.execute(
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
        scope_json, input_json, payload_json = (canonical_json(item) for item in (scope, source, payload))
        conn.execute("""INSERT INTO decisions(account,market,market_date,run_id,revision,successful,scope_json,scope_hash,
                     input_json,input_hash,payload_json,payload_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (payload["account"], payload["market"], payload["market_trading_date"], payload["run_id"], payload["revision"],
                      int(successful), scope_json, _json_hash(scope_json), input_json, _json_hash(input_json),
                      payload_json, _json_hash(payload_json)))

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
