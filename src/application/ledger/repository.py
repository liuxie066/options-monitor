from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol, Sequence, cast

from domain.domain.ledger.position_fields import effective_expiration, now_ms
from src.application.ledger.event_codec import encode_trade_event_for_storage, trade_event_application_payload
from src.application.ledger.position_records import PositionLotRecord
from src.application.ledger.store_resolution import resolve_ledger_store
from src.infrastructure.feishu_bitable import parse_note_kv, safe_float


class OptionPositionsReadRepo(Protocol):
    def list_position_lots(self) -> list[dict[str, Any]]: ...


class OptionPositionsEventWriteRepo(OptionPositionsReadRepo, Protocol):
    def list_trade_events(self, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]: ...
    def upsert_trade_event(self, event: Any, *, conn: sqlite3.Connection | None = None) -> bool: ...
    def replace_position_lots(
        self,
        records: Sequence[PositionLotRecord],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int: ...


def _load_data_config(data_config: Path) -> dict[str, Any]:
    if not data_config.exists():
        return {}
    cfg = json.loads(data_config.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise SystemExit("data config must be a JSON object")
    return cfg


def _get_option_positions_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    raw = cfg.get("option_positions")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SystemExit("data config option_positions must be a JSON object")
    return raw


def _get_option_positions_bootstrap_from_legacy_sqlite_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    option_positions_cfg = _get_option_positions_cfg(cfg)
    raw = option_positions_cfg.get("bootstrap_from_legacy_sqlite")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SystemExit("data config option_positions.bootstrap_from_legacy_sqlite must be a JSON object")
    return raw


def option_positions_bootstrap_from_feishu_enabled(data_config: Path) -> bool:
    _load_data_config(data_config)
    return False


def option_positions_bootstrap_from_legacy_sqlite_enabled(data_config: Path) -> bool:
    cfg = _load_data_config(data_config)
    return _option_positions_bootstrap_from_legacy_sqlite_enabled_from_cfg(cfg)


def _option_positions_bootstrap_from_legacy_sqlite_enabled_from_cfg(cfg: dict[str, Any]) -> bool:
    bootstrap_cfg = _get_option_positions_bootstrap_from_legacy_sqlite_cfg(cfg)
    enabled = bootstrap_cfg.get("enabled")
    if enabled is None:
        return False
    if not isinstance(enabled, bool):
        raise SystemExit("data config option_positions.bootstrap_from_legacy_sqlite.enabled must be a boolean")
    return bool(enabled)

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


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _row_to_position_lot(row: sqlite3.Row) -> dict[str, Any]:
    fields = json.loads(str(row["fields_json"]) or "{}")
    if not isinstance(fields, dict):
        fields = {}
    if fields.get("expiration") in (None, "") and row["expiration"] not in (None, ""):
        fields["expiration"] = int(row["expiration"])
    if fields.get("strike") is None and row["strike"] is not None:
        fields["strike"] = float(row["strike"])
    if fields.get("multiplier") is None and row["multiplier"] is not None:
        raw_multiplier = float(row["multiplier"])
        fields["multiplier"] = int(raw_multiplier) if raw_multiplier.is_integer() else raw_multiplier
    return {
        "record_id": str(row["record_id"]),
        "fields": fields,
    }


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

    def count_legacy_records(self) -> int:
        if not self._table_exists("option_positions"):
            return 0
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM option_positions").fetchone()
        return int((row["cnt"] if row is not None else 0) or 0)

    def list_legacy_records(self) -> list[dict[str, Any]]:
        if not self._table_exists("option_positions"):
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT record_id, fields_json
                FROM option_positions
                ORDER BY updated_at_ms DESC, record_id DESC
                """
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            fields = json.loads(str(row["fields_json"]) or "{}")
            out.append(
                {
                    "record_id": str(row["record_id"]),
                    "fields": fields if isinstance(fields, dict) else {},
                }
            )
        return out

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
        return [_row_to_position_lot(row) for row in rows]

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
        return _row_to_position_lot(row)["fields"]

    def list_records(self, *, page_size: int = 500) -> list[dict[str, Any]]:
        return self.list_position_lots()

    def get_record_fields(self, record_id: str) -> dict[str, Any]:
        return self.get_position_lot_fields(record_id)


def with_sqlite_repo_transaction(repo: Any, fn: Any) -> Any:
    sqlite_repo = require_option_positions_event_write_repo(repo)
    conn = sqlite_repo._connect() if isinstance(sqlite_repo, SQLiteOptionPositionsRepository) else None
    try:
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


def require_option_positions_event_write_repo(repo: Any) -> OptionPositionsEventWriteRepo:
    candidate = require_option_positions_read_repo(repo)
    required = (
        "list_trade_events",
        "upsert_trade_event",
        "replace_position_lots",
    )
    if all(callable(getattr(candidate, name, None)) for name in required):
        return cast(OptionPositionsEventWriteRepo, candidate)
    raise TypeError("option_positions repo does not satisfy event write repository interface")
