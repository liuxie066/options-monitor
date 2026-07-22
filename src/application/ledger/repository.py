from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol, Sequence, cast

from domain.domain.ledger.position_fields import effective_expiration, now_ms
from src.application.ledger.event_codec import encode_trade_event_for_storage, trade_event_application_payload
from src.application.ledger.position_records import PositionLotRecord
from src.application.ledger.sqlite_row_codec import position_lot_row_to_record
from src.application.ledger.store_resolution import resolve_ledger_store
from src.infrastructure.feishu_bitable import parse_note_kv, safe_float


class OptionPositionsReadRepo(Protocol):
    def list_position_lots(self) -> list[dict[str, Any]]: ...


class OptionPositionsEventReadRepo(OptionPositionsReadRepo, Protocol):
    def list_trade_events(self, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]: ...


class OptionPositionsEventWriteRepo(OptionPositionsEventReadRepo, Protocol):
    def upsert_trade_event(self, event: Any, *, conn: sqlite3.Connection | None = None) -> bool: ...
    def replace_position_lots(
        self,
        records: Sequence[PositionLotRecord],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int: ...


class AssignedStockEventRepo(Protocol):
    def list_assigned_stock_events(self, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]: ...
    def upsert_assigned_stock_event(self, event: dict[str, Any], *, conn: sqlite3.Connection | None = None) -> bool: ...


def _load_data_config(data_config: Path) -> dict[str, Any]:
    if not data_config.exists():
        return {}
    cfg = json.loads(data_config.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise SystemExit("data config must be a JSON object")
    return cfg


def option_positions_bootstrap_from_feishu_enabled(data_config: Path) -> bool:
    _load_data_config(data_config)
    return False


def resolve_option_positions_sqlite_path(data_config: Path) -> Path:
    path = resolve_ledger_store(data_config).sqlite_path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _validate_position_lot_fields(*, record_id: str, fields: dict[str, Any]) -> None:
    option_type = str(fields.get("option_type") or "").strip().lower()
    if option_type not in {"put", "call"}:
        return
    expiration = fields.get("expiration")
    strike = safe_float(fields.get("strike"))
    missing: list[str] = []
    if expiration in (None, ""):
        missing.append("expiration")
    if strike is None:
        missing.append("strike")
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"incomplete option position lot {record_id}: missing {joined}")


def _position_lot_contract_scalars(fields: dict[str, Any]) -> tuple[int | None, float | None, float | None]:
    expiration_ms, _ = effective_expiration(fields)
    strike = safe_float(fields.get("strike"))
    multiplier = safe_float(fields.get("multiplier"))
    if multiplier is None:
        multiplier = safe_float(parse_note_kv(fields.get("note") or "", "multiplier"))
    return expiration_ms, strike, multiplier


def _same_lifecycle_evidence_source(existing_raw_json: Any, payload: dict[str, Any]) -> bool:
    try:
        existing = json.loads(str(existing_raw_json or "{}"))
    except Exception:
        return False
    if not isinstance(existing, dict):
        return False
    for key in ("source_type", "source_event_id", "evidence_type"):
        if str(existing.get(key) or "").strip() != str(payload.get(key) or "").strip():
            return False
    return True


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


class SQLiteOptionPositionsRepository:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_config_path: Path | None = None
        self.bootstrap_status = "not_started"
        self.bootstrap_message: str | None = None
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def _optional_conn(self, conn: sqlite3.Connection | None, *, commit: bool = False):
        owned = conn is None
        if conn is None:
            conn = self._connect()
        try:
            yield conn
            if owned and commit:
                conn.commit()
        finally:
            if owned:
                conn.close()

    def _table_exists(self, name: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
                (str(name),),
            ).fetchone()
        return row is not None

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_events (
                  event_id TEXT PRIMARY KEY,
                  event_json TEXT NOT NULL,
                  trade_time_ms INTEGER NOT NULL,
                  created_at_ms INTEGER NOT NULL,
                  updated_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trade_events_trade_time ON trade_events(trade_time_ms, event_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS position_lots (
                  record_id TEXT PRIMARY KEY,
                  fields_json TEXT NOT NULL,
                  source_event_id TEXT,
                  expiration INTEGER,
                  strike REAL,
                  multiplier REAL,
                  updated_at_ms INTEGER NOT NULL
                )
                """
            )
            _add_column_if_missing(conn, "position_lots", "expiration", "INTEGER")
            _add_column_if_missing(conn, "position_lots", "strike", "REAL")
            _add_column_if_missing(conn, "position_lots", "multiplier", "REAL")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_position_lots_expiration ON position_lots(expiration, record_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assigned_stock_events (
                  stock_event_id TEXT PRIMARY KEY,
                  event_json TEXT NOT NULL,
                  trade_time_ms INTEGER NOT NULL,
                  created_at_ms INTEGER NOT NULL,
                  updated_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_assigned_stock_events_trade_time
                ON assigned_stock_events(trade_time_ms, stock_event_id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_lifecycle_cases (
                  case_id TEXT PRIMARY KEY,
                  case_key TEXT NOT NULL UNIQUE,
                  account TEXT NOT NULL,
                  symbol TEXT NOT NULL,
                  option_type TEXT,
                  position_side TEXT,
                  strike REAL,
                  expiration_ymd TEXT,
                  status TEXT NOT NULL,
                  decision_type TEXT,
                  target_lot_ids_json TEXT,
                  pending_until_ms INTEGER,
                  created_at_ms INTEGER NOT NULL,
                  updated_at_ms INTEGER NOT NULL,
                  raw_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_trade_lifecycle_cases_lookup
                ON trade_lifecycle_cases(account, symbol, option_type, strike, expiration_ymd, status)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_lifecycle_evidence (
                  evidence_id TEXT PRIMARY KEY,
                  case_id TEXT,
                  source_type TEXT NOT NULL,
                  source_event_id TEXT,
                  evidence_type TEXT NOT NULL,
                  account TEXT,
                  symbol TEXT,
                  raw_json TEXT NOT NULL,
                  created_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_trade_lifecycle_evidence_case
                ON trade_lifecycle_evidence(case_id, created_at_ms, evidence_id)
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_lifecycle_evidence_source
                ON trade_lifecycle_evidence(source_type, source_event_id, evidence_type)
                WHERE source_event_id IS NOT NULL AND source_event_id != ''
                """
            )
            self._backfill_position_lot_contract_columns(conn)
            conn.commit()

    def _backfill_position_lot_contract_columns(self, conn: sqlite3.Connection) -> None:
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
        ts = int(now_ms())
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                "SELECT event_json FROM trade_events WHERE event_id = ?",
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
                return False
            active_conn.execute(
                """
                INSERT INTO trade_events (
                  event_id, event_json, trade_time_ms, created_at_ms, updated_at_ms
                ) VALUES (
                  ?, ?, ?, ?, ?
                )
                """,
                (
                    encoded.event_id,
                    encoded.event_json,
                    encoded.event_time_ms,
                    ts,
                    ts,
                ),
            )
        return True

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

    def upsert_assigned_stock_event(self, event: dict[str, Any], *, conn: sqlite3.Connection | None = None) -> bool:
        if not isinstance(event, dict):
            raise TypeError("assigned stock event must be a JSON object")
        stock_event_id = str(event.get("stock_event_id") or event.get("event_id") or "").strip()
        if not stock_event_id:
            raise ValueError("assigned stock event requires stock_event_id")
        try:
            trade_time_ms = int(event.get("trade_time_ms") or event.get("event_time_ms") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("assigned stock event requires numeric trade_time_ms") from exc
        if trade_time_ms <= 0:
            raise ValueError("assigned stock event requires trade_time_ms > 0")
        payload = dict(event)
        payload["stock_event_id"] = stock_event_id
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
                  stock_event_id, event_json, trade_time_ms, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (stock_event_id, event_json, trade_time_ms, ts, ts),
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

    def replace_position_lots(
        self,
        records: Sequence[PositionLotRecord],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        ts = int(now_ms())
        inserted = 0
        with self._optional_conn(conn, commit=True) as active_conn:
            active_conn.execute("DELETE FROM position_lots")
            for record in records:
                if not isinstance(record, PositionLotRecord):
                    raise TypeError("replace_position_lots requires PositionLotRecord records")
                record_id = record.record_id
                fields = record.fields
                _validate_position_lot_fields(record_id=record_id, fields=fields)
                expiration_ms, strike, multiplier = _position_lot_contract_scalars(fields)
                active_conn.execute(
                    """
                    INSERT INTO position_lots (
                      record_id, fields_json, source_event_id, expiration, strike, multiplier, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_id,
                        json.dumps(fields, ensure_ascii=False, sort_keys=True),
                        (str(fields.get("source_event_id")) if fields.get("source_event_id") else None),
                        int(expiration_ms) if expiration_ms is not None else None,
                        float(strike) if strike is not None else None,
                        float(multiplier) if multiplier is not None else None,
                        ts,
                    ),
                )
                inserted += 1
        return inserted

    def list_position_lots(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT record_id, fields_json, expiration, strike, multiplier
                FROM position_lots
                ORDER BY updated_at_ms DESC, record_id DESC
                """
            ).fetchall()
        return [position_lot_row_to_record(row) for row in rows]

    def get_position_lot_fields(self, record_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT record_id, fields_json, expiration, strike, multiplier
                FROM position_lots
                WHERE record_id = ?
                """,
                (str(record_id),),
            ).fetchone()
        if row is None:
            raise ValueError(f"position lot not found: {record_id}")
        return position_lot_row_to_record(row)["fields"]

    def list_records(self, *, page_size: int = 500) -> list[dict[str, Any]]:
        return self.list_position_lots()

    def get_record_fields(self, record_id: str) -> dict[str, Any]:
        return self.get_position_lot_fields(record_id)

    def upsert_trade_lifecycle_case(
        self,
        case: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = dict(case or {})
        case_id = str(payload.get("case_id") or "").strip()
        case_key = str(payload.get("case_key") or "").strip()
        if not case_id or not case_key:
            raise ValueError("trade lifecycle case requires case_id and case_key")
        ts = int(now_ms())
        raw_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                "SELECT raw_json, created_at_ms FROM trade_lifecycle_cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            created_at_ms = int(existing["created_at_ms"]) if existing is not None else ts
            changed = existing is None or str(existing["raw_json"] or "") != raw_json
            active_conn.execute(
                """
                INSERT INTO trade_lifecycle_cases (
                  case_id, case_key, account, symbol, option_type, position_side,
                  strike, expiration_ymd, status, decision_type, target_lot_ids_json,
                  pending_until_ms, created_at_ms, updated_at_ms, raw_json
                ) VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(case_id) DO UPDATE SET
                  case_key = excluded.case_key,
                  account = excluded.account,
                  symbol = excluded.symbol,
                  option_type = excluded.option_type,
                  position_side = excluded.position_side,
                  strike = excluded.strike,
                  expiration_ymd = excluded.expiration_ymd,
                  status = excluded.status,
                  decision_type = excluded.decision_type,
                  target_lot_ids_json = excluded.target_lot_ids_json,
                  pending_until_ms = excluded.pending_until_ms,
                  updated_at_ms = excluded.updated_at_ms,
                  raw_json = excluded.raw_json
                """,
                (
                    case_id,
                    case_key,
                    str(payload.get("account") or "").strip().lower(),
                    str(payload.get("symbol") or "").strip().upper(),
                    (str(payload.get("option_type") or "").strip().lower() or None),
                    (str(payload.get("position_side") or "").strip().lower() or None),
                    float(payload["strike"]) if payload.get("strike") is not None else None,
                    (str(payload.get("expiration_ymd") or "").strip() or None),
                    str(payload.get("status") or "pending").strip().lower(),
                    (str(payload.get("decision_type") or "").strip().lower() or None),
                    json.dumps(list(payload.get("target_lot_ids") or []), ensure_ascii=False, sort_keys=True),
                    int(payload["pending_until_ms"]) if payload.get("pending_until_ms") is not None else None,
                    created_at_ms,
                    ts,
                    raw_json,
                ),
            )
        return changed

    def get_trade_lifecycle_case(self, case_id: str, *, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                "SELECT raw_json FROM trade_lifecycle_cases WHERE case_id = ?",
                (str(case_id or "").strip(),),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["raw_json"]) or "{}")
        return dict(payload) if isinstance(payload, dict) else None

    def get_trade_lifecycle_case_by_key(
        self,
        case_key: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                "SELECT raw_json FROM trade_lifecycle_cases WHERE case_key = ?",
                (str(case_key or "").strip(),),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["raw_json"]) or "{}")
        return dict(payload) if isinstance(payload, dict) else None

    def list_trade_lifecycle_cases(
        self,
        *,
        status: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if status:
            where = "WHERE status = ?"
            params.append(str(status).strip().lower())
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT raw_json
                FROM trade_lifecycle_cases
                {where}
                ORDER BY updated_at_ms DESC, case_id DESC
                """,
                params,
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(str(row["raw_json"]) or "{}")
            if isinstance(payload, dict):
                out.append(dict(payload))
        return out

    def upsert_trade_lifecycle_evidence(
        self,
        evidence: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = dict(evidence or {})
        evidence_id = str(payload.get("evidence_id") or "").strip()
        if not evidence_id:
            raise ValueError("trade lifecycle evidence requires evidence_id")
        ts = int(now_ms())
        raw_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                "SELECT raw_json FROM trade_lifecycle_evidence WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
            if existing is not None:
                if not _same_lifecycle_evidence_source(existing["raw_json"], payload):
                    raise ValueError(f"trade lifecycle evidence conflict for evidence_id={evidence_id}")
                if str(existing["raw_json"] or "") == raw_json:
                    return False
                active_conn.execute(
                    """
                    UPDATE trade_lifecycle_evidence
                    SET case_id = ?, account = ?, symbol = ?, raw_json = ?
                    WHERE evidence_id = ?
                    """,
                    (
                        (str(payload.get("case_id") or "").strip() or None),
                        (str(payload.get("account") or "").strip().lower() or None),
                        (str(payload.get("symbol") or "").strip().upper() or None),
                        raw_json,
                        evidence_id,
                    ),
                )
                return True
            active_conn.execute(
                """
                INSERT INTO trade_lifecycle_evidence (
                  evidence_id, case_id, source_type, source_event_id, evidence_type,
                  account, symbol, raw_json, created_at_ms
                ) VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    evidence_id,
                    (str(payload.get("case_id") or "").strip() or None),
                    str(payload.get("source_type") or "").strip(),
                    (str(payload.get("source_event_id") or "").strip() or None),
                    str(payload.get("evidence_type") or "").strip(),
                    (str(payload.get("account") or "").strip().lower() or None),
                    (str(payload.get("symbol") or "").strip().upper() or None),
                    raw_json,
                    ts,
                ),
            )
        return True

    def list_trade_lifecycle_evidence(
        self,
        *,
        case_id: str | None = None,
        account: str | None = None,
        symbol: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if case_id:
            clauses.append("case_id = ?")
            params.append(str(case_id).strip())
        if account:
            clauses.append("account = ?")
            params.append(str(account).strip().lower())
        if symbol:
            clauses.append("symbol = ?")
            params.append(str(symbol).strip().upper())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT raw_json
                FROM trade_lifecycle_evidence
                {where}
                ORDER BY created_at_ms ASC, evidence_id ASC
                """,
                params,
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(str(row["raw_json"]) or "{}")
            if isinstance(payload, dict):
                out.append(dict(payload))
        return out


def with_sqlite_repo_transaction(repo: Any, fn: Any) -> Any:
    sqlite_repo = require_option_positions_event_write_repo(repo)
    conn = sqlite_repo._connect() if isinstance(sqlite_repo, SQLiteOptionPositionsRepository) else None
    try:
        if conn is not None:
            conn.execute("BEGIN IMMEDIATE")
        result = fn(sqlite_repo, conn)
        if conn is not None:
            conn.commit()
        return result
    except Exception:
        if conn is not None:
            conn.rollback()
        raise
    finally:
        if conn is not None:
            conn.close()


def require_option_positions_read_repo(repo: Any) -> OptionPositionsReadRepo:
    candidate = getattr(repo, "primary_repo", repo)
    if callable(getattr(candidate, "list_position_lots", None)):
        return candidate
    raise TypeError("option_positions repo does not satisfy read repository interface")


def require_option_positions_event_read_repo(repo: Any) -> OptionPositionsEventReadRepo:
    candidate = require_option_positions_read_repo(repo)
    if callable(getattr(candidate, "list_trade_events", None)):
        return cast(OptionPositionsEventReadRepo, candidate)
    raise TypeError("option_positions repo does not satisfy event read repository interface")


def require_option_positions_event_write_repo(repo: Any) -> OptionPositionsEventWriteRepo:
    candidate = require_option_positions_event_read_repo(repo)
    required = (
        "upsert_trade_event",
        "replace_position_lots",
    )
    if all(callable(getattr(candidate, name, None)) for name in required):
        return cast(OptionPositionsEventWriteRepo, candidate)
    raise TypeError("option_positions repo does not satisfy event write repository interface")
