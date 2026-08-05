from __future__ import annotations

import hashlib
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
    def list_position_lots(self, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]: ...


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


def _ensure_notification_outbox_v2(conn: sqlite3.Connection) -> None:
    table = "trade_lifecycle_notification_outbox"
    existing = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    create_sql = """
        CREATE TABLE {table_name} (
          outbox_id TEXT PRIMARY KEY,
          case_id TEXT NOT NULL,
          transition_type TEXT NOT NULL,
          resolution_revision INTEGER NOT NULL,
          delivery_revision INTEGER NOT NULL DEFAULT 0,
          transition_key TEXT NOT NULL,
          state_fingerprint TEXT NOT NULL,
          status TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          payload_hash TEXT NOT NULL,
          provider_message_id TEXT,
          claim_id TEXT,
          claimed_at_ms INTEGER,
          send_started_at_ms INTEGER,
          attempt_count INTEGER NOT NULL DEFAULT 0,
          next_attempt_at_ms INTEGER,
          last_error TEXT,
          provider_receipt_json TEXT,
          created_at_ms INTEGER NOT NULL,
          updated_at_ms INTEGER NOT NULL,
          confirmed_at_ms INTEGER,
          UNIQUE(transition_key, delivery_revision),
          UNIQUE(case_id, transition_type, resolution_revision, delivery_revision),
          UNIQUE(case_id, transition_type, state_fingerprint, delivery_revision)
        )
    """
    if existing is None:
        conn.execute(create_sql.format(table_name=table))
        return

    columns = {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    required = {
        "delivery_revision",
        "transition_key",
        "state_fingerprint",
    }
    unique_indexes: set[tuple[str, ...]] = set()
    for index in conn.execute(f"PRAGMA index_list({table})").fetchall():
        if not bool(index["unique"]):
            continue
        name = str(index["name"])
        unique_indexes.add(
            tuple(
                str(row["name"])
                for row in conn.execute(f"PRAGMA index_info({name})").fetchall()
            )
        )
    expected_unique = {
        ("transition_key", "delivery_revision"),
        (
            "case_id",
            "transition_type",
            "resolution_revision",
            "delivery_revision",
        ),
        (
            "case_id",
            "transition_type",
            "state_fingerprint",
            "delivery_revision",
        ),
    }
    if required.issubset(columns) and expected_unique.issubset(unique_indexes):
        return

    replacement = f"{table}_v2_rebuild"
    conn.execute(f"DROP TABLE IF EXISTS {replacement}")
    conn.execute(create_sql.format(table_name=replacement))
    conn.execute(
        f"""
        INSERT INTO {replacement} (
          outbox_id, case_id, transition_type, resolution_revision,
          delivery_revision, transition_key, state_fingerprint, status,
          payload_json, payload_hash, provider_message_id, claim_id,
          claimed_at_ms, send_started_at_ms, attempt_count,
          next_attempt_at_ms, last_error, provider_receipt_json,
          created_at_ms, updated_at_ms, confirmed_at_ms
        )
        SELECT
          outbox_id, case_id, transition_type, resolution_revision,
          0, 'legacy:' || outbox_id, payload_hash, status,
          payload_json, payload_hash, provider_message_id, claim_id,
          claimed_at_ms, send_started_at_ms, attempt_count,
          next_attempt_at_ms, last_error, provider_receipt_json,
          created_at_ms, updated_at_ms, confirmed_at_ms
        FROM {table}
        """
    )
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {replacement} RENAME TO {table}")


def _ensure_notification_delivery_batches_v1(
    conn: sqlite3.Connection,
) -> None:
    _add_column_if_missing(
        conn,
        "trade_lifecycle_notification_outbox",
        "delivery_batch_id",
        "TEXT",
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS
        trade_lifecycle_notification_delivery_batches (
          batch_id TEXT PRIMARY KEY,
          route_fingerprint TEXT NOT NULL,
          provider TEXT NOT NULL,
          channel TEXT NOT NULL,
          target_fingerprint TEXT NOT NULL,
          renderer_version TEXT NOT NULL,
          status TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          payload_hash TEXT NOT NULL,
          member_count INTEGER NOT NULL CHECK(member_count > 0),
          first_intent_created_at_ms INTEGER NOT NULL,
          last_intent_created_at_ms INTEGER NOT NULL,
          provider_message_id TEXT,
          claim_id TEXT,
          claimed_at_ms INTEGER,
          send_started_at_ms INTEGER,
          attempt_count INTEGER NOT NULL DEFAULT 0,
          next_attempt_at_ms INTEGER,
          last_error TEXT,
          provider_receipt_json TEXT,
          created_at_ms INTEGER NOT NULL,
          updated_at_ms INTEGER NOT NULL,
          confirmed_at_ms INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_trade_lifecycle_delivery_batches_dispatch
        ON trade_lifecycle_notification_delivery_batches(
          status, next_attempt_at_ms, created_at_ms, batch_id
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_trade_lifecycle_delivery_batches_route
        ON trade_lifecycle_notification_delivery_batches(
          route_fingerprint, send_started_at_ms, created_at_ms, batch_id
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_trade_lifecycle_outbox_delivery_batch
        ON trade_lifecycle_notification_outbox(
          delivery_batch_id, created_at_ms, outbox_id
        )
        """
    )


def _ensure_lifecycle_delivery_status_revision_v1(
    conn: sqlite3.Connection,
) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_lifecycle_status_revisions (
          scope TEXT PRIMARY KEY,
          revision INTEGER NOT NULL CHECK(revision >= 0)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO trade_lifecycle_status_revisions (scope, revision)
        VALUES ('delivery', 0)
        ON CONFLICT(scope) DO NOTHING
        """
    )
    status_tables = {
        "cases": "trade_lifecycle_cases",
        "evidence": "trade_lifecycle_evidence",
        "timing": "trade_lifecycle_timing_policies",
        "outbox": "trade_lifecycle_notification_outbox",
        "batches": "trade_lifecycle_notification_delivery_batches",
        "receipts": "trade_lifecycle_migration_receipts",
    }
    for alias, table in status_tables.items():
        for operation in ("INSERT", "UPDATE", "DELETE"):
            conn.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS
                trg_lifecycle_delivery_status_{alias}_{operation.lower()}
                AFTER {operation} ON {table}
                BEGIN
                  INSERT INTO trade_lifecycle_status_revisions (
                    scope, revision
                  ) VALUES ('delivery', 1)
                  ON CONFLICT(scope) DO UPDATE SET
                    revision = revision + 1;
                END
                """
            )


def initialize_ledger_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    row = conn.execute("PRAGMA foreign_keys").fetchone()
    enabled = int(row[0]) if row is not None else 0
    if enabled != 1:
        raise RuntimeError("SQLite foreign key enforcement is required for the option ledger")
    return conn


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
        initialize_ledger_connection(conn)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def _optional_conn(self, conn: sqlite3.Connection | None, *, commit: bool = False):
        owned = conn is None
        if conn is None:
            conn = self._connect()
        else:
            initialize_ledger_connection(conn)
        try:
            yield conn
            if owned and commit:
                conn.commit()
        finally:
            if owned:
                conn.close()

    def _table_exists(self, name: str, *, conn: sqlite3.Connection | None = None) -> bool:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
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
            _add_column_if_missing(conn, "trade_lifecycle_cases", "broker", "TEXT")
            _add_column_if_missing(conn, "trade_lifecycle_cases", "contract_key", "TEXT")
            _add_column_if_missing(
                conn,
                "trade_lifecycle_cases",
                "target_contracts_by_lot_json",
                "TEXT",
            )
            _add_column_if_missing(conn, "trade_lifecycle_cases", "observation_start_ms", "INTEGER")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_trade_lifecycle_cases_lookup
                ON trade_lifecycle_cases(account, symbol, option_type, strike, expiration_ymd, status)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_trade_lifecycle_cases_due
                ON trade_lifecycle_cases(
                  account, updated_at_ms DESC, case_id DESC
                )
                WHERE status NOT IN (
                  'ledger_written', 'conflict', 'superseded'
                )
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
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_lifecycle_evidence_case_id
                ON trade_lifecycle_evidence(case_id, evidence_id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_trade_lifecycle_evidence_settlement_latest
                ON trade_lifecycle_evidence(
                  case_id, source_type, created_at_ms, evidence_id
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_lifecycle_evidence_revisions (
                  case_id TEXT PRIMARY KEY,
                  revision INTEGER NOT NULL CHECK(revision >= 0)
                )
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS
                trg_trade_lifecycle_evidence_revision_insert
                AFTER INSERT ON trade_lifecycle_evidence
                WHEN NEW.case_id IS NOT NULL AND NEW.case_id != ''
                BEGIN
                  INSERT INTO trade_lifecycle_evidence_revisions (
                    case_id, revision
                  ) VALUES (NEW.case_id, 1)
                  ON CONFLICT(case_id) DO UPDATE SET
                    revision = revision + 1;
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS
                trg_trade_lifecycle_evidence_revision_update_old
                AFTER UPDATE OF case_id ON trade_lifecycle_evidence
                WHEN OLD.case_id IS NOT NEW.case_id
                  AND OLD.case_id IS NOT NULL
                  AND OLD.case_id != ''
                BEGIN
                  INSERT INTO trade_lifecycle_evidence_revisions (
                    case_id, revision
                  ) VALUES (OLD.case_id, 1)
                  ON CONFLICT(case_id) DO UPDATE SET
                    revision = revision + 1;
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS
                trg_trade_lifecycle_evidence_revision_update_new
                AFTER UPDATE OF case_id ON trade_lifecycle_evidence
                WHEN OLD.case_id IS NOT NEW.case_id
                  AND NEW.case_id IS NOT NULL
                  AND NEW.case_id != ''
                BEGIN
                  INSERT INTO trade_lifecycle_evidence_revisions (
                    case_id, revision
                  ) VALUES (NEW.case_id, 1)
                  ON CONFLICT(case_id) DO UPDATE SET
                    revision = revision + 1;
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS
                trg_trade_lifecycle_evidence_revision_delete
                AFTER DELETE ON trade_lifecycle_evidence
                WHEN OLD.case_id IS NOT NULL AND OLD.case_id != ''
                BEGIN
                  INSERT INTO trade_lifecycle_evidence_revisions (
                    case_id, revision
                  ) VALUES (OLD.case_id, 1)
                  ON CONFLICT(case_id) DO UPDATE SET
                    revision = revision + 1;
                END
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_lifecycle_settlement_admission_heads (
                  case_id TEXT PRIMARY KEY,
                  semantic_schema TEXT NOT NULL,
                  semantic_fingerprint TEXT NOT NULL,
                  evidence_id TEXT NOT NULL,
                  evidence_created_at_ms INTEGER NOT NULL,
                  updated_at_ms INTEGER NOT NULL,
                  FOREIGN KEY(case_id) REFERENCES trade_lifecycle_cases(case_id),
                  FOREIGN KEY(case_id, evidence_id)
                    REFERENCES trade_lifecycle_evidence(case_id, evidence_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_lifecycle_source_consumptions (
                  source_key TEXT PRIMARY KEY,
                  case_id TEXT NOT NULL,
                  owner_evidence_id TEXT NOT NULL,
                  source_role TEXT NOT NULL,
                  source_payload_hash TEXT NOT NULL,
                  created_at_ms INTEGER NOT NULL,
                  raw_json TEXT NOT NULL,
                  FOREIGN KEY(case_id, owner_evidence_id)
                    REFERENCES trade_lifecycle_evidence(case_id, evidence_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_trade_lifecycle_source_owner
                ON trade_lifecycle_source_consumptions(
                  case_id, owner_evidence_id, source_role, source_key
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_lifecycle_allocations (
                  allocation_id TEXT PRIMARY KEY,
                  case_id TEXT NOT NULL,
                  evidence_id TEXT NOT NULL,
                  target_lot_id TEXT NOT NULL,
                  terminal_type TEXT NOT NULL,
                  contracts_allocated INTEGER NOT NULL CHECK(contracts_allocated > 0),
                  canonical_terminal_event_id TEXT NOT NULL,
                  created_at_ms INTEGER NOT NULL,
                  raw_json TEXT NOT NULL,
                  UNIQUE(case_id, evidence_id, target_lot_id),
                  FOREIGN KEY(case_id) REFERENCES trade_lifecycle_cases(case_id),
                  FOREIGN KEY(case_id, evidence_id)
                    REFERENCES trade_lifecycle_evidence(case_id, evidence_id),
                  FOREIGN KEY(canonical_terminal_event_id)
                    REFERENCES trade_events(event_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_trade_lifecycle_allocations_case
                ON trade_lifecycle_allocations(case_id, target_lot_id, allocation_id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_lifecycle_timing_policies (
                  case_id TEXT PRIMARY KEY,
                  policy_schema TEXT NOT NULL,
                  market TEXT NOT NULL,
                  timezone TEXT NOT NULL,
                  settlement_style TEXT NOT NULL,
                  underlying_security_type TEXT NOT NULL,
                  last_trade_cutoff_ms INTEGER NOT NULL,
                  last_trade_cutoff_source TEXT NOT NULL,
                  settlement_deadline_ms INTEGER NOT NULL,
                  trading_days_json TEXT NOT NULL,
                  calendar_source TEXT NOT NULL,
                  calendar_observed_at_ms INTEGER NOT NULL,
                  calendar_hash TEXT NOT NULL,
                  created_at_ms INTEGER NOT NULL,
                  raw_json TEXT NOT NULL,
                  FOREIGN KEY(case_id) REFERENCES trade_lifecycle_cases(case_id)
                )
                """
            )
            _ensure_notification_outbox_v2(conn)
            _ensure_notification_delivery_batches_v1(conn)
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_trade_lifecycle_outbox_dispatch
                ON trade_lifecycle_notification_outbox(
                  status, next_attempt_at_ms, created_at_ms, outbox_id
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_lifecycle_migration_receipts (
                  target_key TEXT PRIMARY KEY,
                  migration_schema TEXT NOT NULL,
                  manifest_hash TEXT NOT NULL,
                  row_hash TEXT NOT NULL,
                  applied_at_ms INTEGER NOT NULL,
                  raw_json TEXT NOT NULL
                )
                """
            )
            _ensure_lifecycle_delivery_status_revision_v1(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_group_identities (
                  group_id TEXT PRIMARY KEY,
                  schema_version TEXT NOT NULL,
                  strategy TEXT NOT NULL,
                  account TEXT NOT NULL,
                  symbol TEXT NOT NULL,
                  funding_put_record_id TEXT NOT NULL,
                  funding_put_open_event_id TEXT NOT NULL,
                  funding_put_contract_key TEXT NOT NULL,
                  participation_call_record_id TEXT NOT NULL,
                  participation_call_open_event_id TEXT NOT NULL,
                  participation_call_contract_key TEXT NOT NULL,
                  original_contracts INTEGER NOT NULL CHECK(original_contracts > 0),
                  created_at_ms INTEGER NOT NULL,
                  identity_hash TEXT NOT NULL,
                  raw_json TEXT NOT NULL,
                  FOREIGN KEY(funding_put_open_event_id) REFERENCES trade_events(event_id),
                  FOREIGN KEY(participation_call_open_event_id) REFERENCES trade_events(event_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_strategy_group_identities_account
                ON strategy_group_identities(account, symbol, group_id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS combo_pair_inferences (
                  inference_id TEXT PRIMARY KEY,
                  schema_version TEXT NOT NULL,
                  algorithm_version TEXT NOT NULL,
                  account TEXT NOT NULL,
                  symbol TEXT NOT NULL,
                  market TEXT NOT NULL,
                  market_date TEXT NOT NULL,
                  put_record_id TEXT NOT NULL,
                  put_open_event_id TEXT NOT NULL,
                  call_record_id TEXT NOT NULL,
                  call_open_event_id TEXT NOT NULL,
                  evidence_grade TEXT NOT NULL,
                  candidate_occurrence_ids_json TEXT NOT NULL,
                  candidate_exposure_ids_json TEXT NOT NULL,
                  input_snapshot_hash TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN (
                    'proposal_ready', 'ambiguous', 'user_confirmed',
                    'user_rejected', 'expired_unresolved', 'superseded'
                  )),
                  proposal_expires_at_ms INTEGER NOT NULL,
                  evidence_json TEXT NOT NULL,
                  alternatives_json TEXT NOT NULL,
                  strategy_group_id TEXT NOT NULL,
                  identity_hash TEXT,
                  put_adoption_event_id TEXT,
                  call_adoption_event_id TEXT,
                  put_void_event_id TEXT,
                  call_void_event_id TEXT,
                  decision_at_ms INTEGER,
                  decision_by TEXT,
                  decision_reason TEXT,
                  created_at_ms INTEGER NOT NULL,
                  updated_at_ms INTEGER NOT NULL,
                  raw_json TEXT NOT NULL,
                  FOREIGN KEY(put_open_event_id) REFERENCES trade_events(event_id),
                  FOREIGN KEY(call_open_event_id) REFERENCES trade_events(event_id),
                  FOREIGN KEY(put_adoption_event_id) REFERENCES trade_events(event_id),
                  FOREIGN KEY(call_adoption_event_id) REFERENCES trade_events(event_id),
                  FOREIGN KEY(put_void_event_id) REFERENCES trade_events(event_id),
                  FOREIGN KEY(call_void_event_id) REFERENCES trade_events(event_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_combo_pair_inferences_account_status
                ON combo_pair_inferences(
                  account, status, market_date, symbol, updated_at_ms, inference_id
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_combo_pair_confirmed_put
                ON combo_pair_inferences(put_open_event_id)
                WHERE status = 'user_confirmed'
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_combo_pair_confirmed_call
                ON combo_pair_inferences(call_open_event_id)
                WHERE status = 'user_confirmed'
                """
            )
            self._backfill_position_lot_contract_columns(conn)
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"SQLite foreign key check failed: {len(violations)} violation(s)")
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

    def list_position_lots(self, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT record_id, fields_json, expiration, strike, multiplier
                FROM position_lots
                ORDER BY updated_at_ms DESC, record_id DESC
                """
            ).fetchall()
        return [position_lot_row_to_record(row) for row in rows]

    def get_position_lot_fields(
        self,
        record_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
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
                  case_id, case_key, account, broker, symbol, option_type, position_side,
                  strike, expiration_ymd, contract_key, status, decision_type,
                  target_lot_ids_json, target_contracts_by_lot_json,
                  observation_start_ms, pending_until_ms, created_at_ms, updated_at_ms,
                  raw_json
                ) VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(case_id) DO UPDATE SET
                  case_key = excluded.case_key,
                  account = excluded.account,
                  broker = excluded.broker,
                  symbol = excluded.symbol,
                  option_type = excluded.option_type,
                  position_side = excluded.position_side,
                  strike = excluded.strike,
                  expiration_ymd = excluded.expiration_ymd,
                  contract_key = excluded.contract_key,
                  status = excluded.status,
                  decision_type = excluded.decision_type,
                  target_lot_ids_json = excluded.target_lot_ids_json,
                  target_contracts_by_lot_json = excluded.target_contracts_by_lot_json,
                  observation_start_ms = excluded.observation_start_ms,
                  pending_until_ms = excluded.pending_until_ms,
                  updated_at_ms = excluded.updated_at_ms,
                  raw_json = excluded.raw_json
                """,
                (
                    case_id,
                    case_key,
                    str(payload.get("account") or "").strip().lower(),
                    str(payload.get("broker") or "").strip().lower() or None,
                    str(payload.get("symbol") or "").strip().upper(),
                    (str(payload.get("option_type") or "").strip().lower() or None),
                    (str(payload.get("position_side") or "").strip().lower() or None),
                    float(payload["strike"]) if payload.get("strike") is not None else None,
                    (str(payload.get("expiration_ymd") or "").strip() or None),
                    (str(payload.get("contract_key") or "").strip() or None),
                    str(payload.get("status") or "pending").strip().lower(),
                    (str(payload.get("decision_type") or "").strip().lower() or None),
                    json.dumps(list(payload.get("target_lot_ids") or []), ensure_ascii=False, sort_keys=True),
                    json.dumps(
                        dict(payload.get("target_contracts_by_lot") or {}),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    (
                        int(payload["observation_start_ms"])
                        if payload.get("observation_start_ms") is not None
                        else None
                    ),
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
        account: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(str(status).strip().lower())
        if account:
            clauses.append("account = ?")
            params.append(str(account).strip().lower())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
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

    def list_trade_lifecycle_due_candidates(
        self,
        *,
        account: str,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        """Return compact case/timing/evidence invalidation facts only."""

        account_value = str(account or "").strip().lower()
        if not account_value:
            raise ValueError("due lifecycle candidate account is required")
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT
                  lifecycle_case.raw_json AS case_raw_json,
                  lifecycle_case.updated_at_ms AS case_updated_at_ms,
                  timing.raw_json AS timing_raw_json,
                  COALESCE(evidence_revision.revision, 0)
                    AS evidence_revision
                FROM trade_lifecycle_cases AS lifecycle_case
                LEFT JOIN trade_lifecycle_timing_policies AS timing
                  ON timing.case_id = lifecycle_case.case_id
                LEFT JOIN trade_lifecycle_evidence_revisions
                  AS evidence_revision
                  ON evidence_revision.case_id = lifecycle_case.case_id
                WHERE lifecycle_case.account = ?
                  AND lifecycle_case.status NOT IN (
                    'ledger_written', 'conflict', 'superseded'
                  )
                ORDER BY lifecycle_case.updated_at_ms DESC,
                         lifecycle_case.case_id DESC
                """,
                (account_value,),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            lifecycle_case = _json_object(row["case_raw_json"])
            timing_policy = (
                _json_object(row["timing_raw_json"])
                if row["timing_raw_json"] is not None
                else None
            )
            output.append(
                {
                    "lifecycle_case": lifecycle_case,
                    "case_updated_at_ms": int(
                        row["case_updated_at_ms"] or 0
                    ),
                    "timing_policy": timing_policy,
                    "evidence_revision": int(
                        row["evidence_revision"] or 0
                    ),
                }
            )
        return output

    def get_trade_lifecycle_delivery_status_revision(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT revision
                FROM trade_lifecycle_status_revisions
                WHERE scope = 'delivery'
                """
            ).fetchone()
        if row is None:
            raise RuntimeError("lifecycle delivery status revision is missing")
        return int(row["revision"] or 0)

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
                if str(existing["raw_json"] or "") == raw_json:
                    return False
                raise ValueError(
                    "trade lifecycle evidence is immutable for "
                    f"evidence_id={evidence_id}"
                )
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

    def insert_trade_lifecycle_case_once(
        self,
        case: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = dict(case or {})
        case_id = str(payload.get("case_id") or "").strip()
        case_key = str(payload.get("case_key") or "").strip()
        account = str(payload.get("account") or "").strip().lower()
        target_contracts = payload.get("target_contracts_by_lot")
        if not case_id or not case_key or not account or not isinstance(target_contracts, dict) or not target_contracts:
            raise ValueError("lifecycle_case.v2 requires case id, key, account and target manifest")
        immutable = _lifecycle_case_immutable_payload(payload)
        ts = int(now_ms())
        raw_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                "SELECT raw_json FROM trade_lifecycle_cases WHERE case_id = ? OR case_key = ?",
                (case_id, case_key),
            ).fetchone()
            if existing is not None:
                existing_payload = json.loads(str(existing["raw_json"]) or "{}")
                if not isinstance(existing_payload, dict) or _lifecycle_case_immutable_payload(existing_payload) != immutable:
                    raise ValueError(f"lifecycle case immutable conflict for case_id={case_id}")
                return False
            active_conn.execute(
                """
                INSERT INTO trade_lifecycle_cases (
                  case_id, case_key, account, broker, symbol, option_type, position_side,
                  strike, expiration_ymd, contract_key, status, decision_type,
                  target_lot_ids_json, target_contracts_by_lot_json, observation_start_ms,
                  pending_until_ms, created_at_ms, updated_at_ms, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    case_id,
                    case_key,
                    account,
                    str(payload.get("broker") or "").strip().lower() or None,
                    str(payload.get("symbol") or "").strip().upper(),
                    str(payload.get("option_type") or "").strip().lower() or None,
                    str(payload.get("position_side") or "").strip().lower() or None,
                    float(payload["strike"]) if payload.get("strike") is not None else None,
                    str(payload.get("expiration_ymd") or "").strip() or None,
                    _json_text(payload.get("contract_key")),
                    str(payload.get("status") or "waiting_settlement_evidence").strip().lower(),
                    str(payload.get("decision_type") or "").strip().lower() or None,
                    json.dumps(sorted(str(item) for item in target_contracts), ensure_ascii=False),
                    json.dumps(target_contracts, ensure_ascii=False, sort_keys=True),
                    int(payload["observation_start_ms"]) if payload.get("observation_start_ms") is not None else None,
                    int(payload["pending_until_ms"]) if payload.get("pending_until_ms") is not None else None,
                    ts,
                    ts,
                    raw_json,
                ),
            )
        return True

    def update_trade_lifecycle_case_derived_status(
        self,
        *,
        case_id: str,
        status: str,
        derived_summary: dict[str, Any],
        expected_state_fingerprint: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        case_id_value = str(case_id or "").strip()
        status_value = str(status or "").strip().lower()
        if not case_id_value or not status_value:
            raise ValueError("case id and derived status are required")
        with self._optional_conn(conn, commit=True) as active_conn:
            row = active_conn.execute(
                "SELECT raw_json, status FROM trade_lifecycle_cases WHERE case_id = ?",
                (case_id_value,),
            ).fetchone()
            if row is None:
                raise ValueError(f"lifecycle case not found: {case_id_value}")
            payload = json.loads(str(row["raw_json"]) or "{}")
            if not isinstance(payload, dict):
                raise ValueError(f"lifecycle case JSON invalid: {case_id_value}")
            if expected_state_fingerprint is not None:
                current_summary = (
                    dict(payload.get("derived_summary") or {})
                    if isinstance(payload.get("derived_summary"), dict)
                    else {}
                )
                if str(
                    current_summary.get("state_fingerprint") or ""
                ).strip() != str(expected_state_fingerprint or "").strip():
                    raise ValueError(
                        "lifecycle case state fingerprint compare-and-set failed"
                    )
            updated = {
                **payload,
                "status": status_value,
                "derived_summary": dict(derived_summary or {}),
            }
            updated_json = json.dumps(updated, ensure_ascii=False, sort_keys=True)
            if str(row["status"] or "") == status_value and str(row["raw_json"] or "") == updated_json:
                return False
            active_conn.execute(
                """
                UPDATE trade_lifecycle_cases
                SET status = ?, updated_at_ms = ?, raw_json = ?
                WHERE case_id = ?
                """,
                (status_value, int(now_ms()), updated_json, case_id_value),
            )
        return True

    def bind_trade_lifecycle_case_futu_account_once(
        self,
        *,
        case_id: str,
        futu_account_id: str,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        case_id_value = str(case_id or "").strip()
        account_id_value = str(futu_account_id or "").strip()
        if not case_id_value or not account_id_value:
            raise ValueError(
                "lifecycle case and Futu account identity are required"
            )
        with self._optional_conn(conn, commit=True) as active_conn:
            row = active_conn.execute(
                "SELECT raw_json FROM trade_lifecycle_cases WHERE case_id = ?",
                (case_id_value,),
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"lifecycle case not found: {case_id_value}"
                )
            payload = json.loads(str(row["raw_json"]) or "{}")
            if not isinstance(payload, dict):
                raise ValueError(
                    f"lifecycle case JSON invalid: {case_id_value}"
                )
            existing = str(
                payload.get("futu_account_id") or ""
            ).strip()
            if existing:
                if existing != account_id_value:
                    raise ValueError(
                        "lifecycle case Futu account immutable conflict"
                    )
                return False
            payload["futu_account_id"] = account_id_value
            active_conn.execute(
                """
                UPDATE trade_lifecycle_cases
                SET updated_at_ms = ?, raw_json = ?
                WHERE case_id = ?
                """,
                (
                    int(now_ms()),
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    case_id_value,
                ),
            )
        return True

    def supersede_trade_lifecycle_case_once(
        self,
        *,
        case_id: str,
        superseded_by_case_id: str,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        case_id_value = str(case_id or "").strip()
        successor_id = str(superseded_by_case_id or "").strip()
        if (
            not case_id_value
            or not successor_id
            or case_id_value == successor_id
        ):
            raise ValueError(
                "legacy lifecycle supersession identity is invalid"
            )
        with self._optional_conn(conn, commit=True) as active_conn:
            row = active_conn.execute(
                "SELECT raw_json FROM trade_lifecycle_cases WHERE case_id = ?",
                (case_id_value,),
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"lifecycle case not found: {case_id_value}"
                )
            payload = json.loads(str(row["raw_json"]) or "{}")
            if not isinstance(payload, dict):
                raise ValueError(
                    f"lifecycle case JSON invalid: {case_id_value}"
                )
            existing_successor = str(
                payload.get("superseded_by_case_id") or ""
            ).strip()
            existing_status = str(
                payload.get("status") or ""
            ).strip().lower()
            if existing_successor:
                if (
                    existing_successor != successor_id
                    or existing_status != "superseded"
                ):
                    raise ValueError(
                        "legacy lifecycle supersession conflict"
                    )
                return False
            payload["status"] = "superseded"
            payload["superseded_by_case_id"] = successor_id
            active_conn.execute(
                """
                UPDATE trade_lifecycle_cases
                SET status = ?, updated_at_ms = ?, raw_json = ?
                WHERE case_id = ?
                """,
                (
                    "superseded",
                    int(now_ms()),
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    case_id_value,
                ),
            )
        return True

    def insert_trade_lifecycle_evidence_once(
        self,
        evidence: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = dict(evidence or {})
        evidence_id = str(payload.get("evidence_id") or "").strip()
        source_type = str(payload.get("source_type") or "").strip()
        evidence_type = str(payload.get("evidence_type") or "").strip()
        if not evidence_id or not source_type or not evidence_type:
            raise ValueError("lifecycle evidence requires evidence_id, source_type and evidence_type")
        raw_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                "SELECT raw_json FROM trade_lifecycle_evidence WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["raw_json"] or "") != raw_json:
                    raise ValueError(f"lifecycle evidence immutable conflict for evidence_id={evidence_id}")
                return False
            active_conn.execute(
                """
                INSERT INTO trade_lifecycle_evidence (
                  evidence_id, case_id, source_type, source_event_id, evidence_type,
                  account, symbol, raw_json, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    str(payload.get("case_id") or "").strip() or None,
                    source_type,
                    str(payload.get("source_event_id") or "").strip() or None,
                    evidence_type,
                    str(payload.get("account") or "").strip().lower() or None,
                    str(payload.get("symbol") or "").strip().upper() or None,
                    raw_json,
                    int(now_ms()),
                ),
            )
        return True

    def bind_trade_lifecycle_evidence_case_once(
        self,
        *,
        evidence_id: str,
        case_id: str,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        evidence_id_value = str(evidence_id or "").strip()
        case_id_value = str(case_id or "").strip()
        if not evidence_id_value or not case_id_value:
            raise ValueError("evidence_id and case_id are required")
        with self._optional_conn(conn, commit=True) as active_conn:
            row = active_conn.execute(
                "SELECT case_id, raw_json FROM trade_lifecycle_evidence WHERE evidence_id = ?",
                (evidence_id_value,),
            ).fetchone()
            if row is None:
                raise ValueError(f"lifecycle evidence not found: {evidence_id_value}")
            existing_case = str(row["case_id"] or "").strip()
            if existing_case:
                if existing_case != case_id_value:
                    raise ValueError(f"lifecycle evidence already bound to another case: {evidence_id_value}")
                return False
            payload = json.loads(str(row["raw_json"]) or "{}")
            if not isinstance(payload, dict):
                raise ValueError(f"lifecycle evidence JSON invalid: {evidence_id_value}")
            payload["case_id"] = case_id_value
            active_conn.execute(
                """
                UPDATE trade_lifecycle_evidence
                SET case_id = ?, raw_json = ?
                WHERE evidence_id = ? AND case_id IS NULL
                """,
                (
                    case_id_value,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    evidence_id_value,
                ),
            )
        return True

    def get_trade_lifecycle_evidence(
        self,
        evidence_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                "SELECT raw_json FROM trade_lifecycle_evidence WHERE evidence_id = ?",
                (str(evidence_id or "").strip(),),
            ).fetchone()
        return _json_object(row["raw_json"]) if row is not None else None

    def get_latest_trade_lifecycle_settlement_evidence(
        self,
        *,
        case_id: str,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        case_value = str(case_id or "").strip()
        if not case_value:
            raise ValueError("settlement evidence case_id is required")
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT rowid, raw_json, created_at_ms
                FROM trade_lifecycle_evidence
                WHERE case_id = ?
                  AND source_type = 'broker_settlement_observation'
                  AND json_type(raw_json, '$.observation') = 'object'
                ORDER BY created_at_ms DESC, rowid DESC
                LIMIT 1
                """,
                (case_value,),
            ).fetchone()
        if row is None:
            return None
        payload = _json_object(row["raw_json"])
        return {
            **payload,
            "_created_at_ms": int(row["created_at_ms"] or 0),
            "_rowid": int(row["rowid"] or 0),
        }

    def get_trade_lifecycle_settlement_admission_head(
        self,
        *,
        case_id: str,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT case_id, semantic_schema, semantic_fingerprint,
                       evidence_id, evidence_created_at_ms, updated_at_ms
                FROM trade_lifecycle_settlement_admission_heads
                WHERE case_id = ?
                """,
                (str(case_id or "").strip(),),
            ).fetchone()
        return dict(row) if row is not None else None

    def upsert_trade_lifecycle_settlement_admission_head(
        self,
        *,
        case_id: str,
        semantic_schema: str,
        semantic_fingerprint: str,
        evidence_id: str,
        evidence_created_at_ms: int,
        updated_at_ms: int,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        values = (
            str(case_id or "").strip(),
            str(semantic_schema or "").strip(),
            str(semantic_fingerprint or "").strip(),
            str(evidence_id or "").strip(),
        )
        if not all(values) or int(evidence_created_at_ms or 0) <= 0:
            raise ValueError("settlement admission head is incomplete")
        with self._optional_conn(conn, commit=True) as active_conn:
            active_conn.execute(
                """
                INSERT INTO trade_lifecycle_settlement_admission_heads (
                  case_id, semantic_schema, semantic_fingerprint, evidence_id,
                  evidence_created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                  semantic_schema = excluded.semantic_schema,
                  semantic_fingerprint = excluded.semantic_fingerprint,
                  evidence_id = excluded.evidence_id,
                  evidence_created_at_ms = excluded.evidence_created_at_ms,
                  updated_at_ms = excluded.updated_at_ms
                """,
                (
                    *values,
                    int(evidence_created_at_ms),
                    int(updated_at_ms),
                ),
            )

    def insert_trade_lifecycle_source_consumption_once(
        self,
        claim: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = dict(claim or {})
        source_key = str(payload.get("source_key") or "").strip()
        case_id = str(payload.get("case_id") or "").strip()
        evidence_id = str(payload.get("owner_evidence_id") or "").strip()
        role = str(payload.get("source_role") or "").strip().lower()
        payload_hash = str(
            payload.get("source_payload_hash") or ""
        ).strip()
        if (
            str(payload.get("schema_version") or "").strip()
            != "trade_lifecycle_source_consumption.v1"
            or not source_key
            or not case_id
            or not evidence_id
            or role not in {"option_anchor", "stock_settlement"}
            or not payload_hash
        ):
            raise ValueError("lifecycle source consumption claim is incomplete")
        raw_json = _json_text(payload)
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                """
                SELECT raw_json
                FROM trade_lifecycle_source_consumptions
                WHERE source_key = ?
                """,
                (source_key,),
            ).fetchone()
            if existing is not None:
                if str(existing["raw_json"] or "") != raw_json:
                    raise ValueError(
                        "lifecycle_source_event_already_consumed"
                    )
                return False
            active_conn.execute(
                """
                INSERT INTO trade_lifecycle_source_consumptions (
                  source_key, case_id, owner_evidence_id, source_role,
                  source_payload_hash, created_at_ms, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_key,
                    case_id,
                    evidence_id,
                    role,
                    payload_hash,
                    int(now_ms()),
                    raw_json,
                ),
            )
        return True

    def get_trade_lifecycle_source_consumption(
        self,
        source_key: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT raw_json
                FROM trade_lifecycle_source_consumptions
                WHERE source_key = ?
                """,
                (str(source_key or "").strip(),),
            ).fetchone()
        return _json_object(row["raw_json"]) if row is not None else None

    def list_trade_lifecycle_source_consumptions(
        self,
        *,
        case_id: str | None = None,
        owner_evidence_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if case_id:
            clauses.append("case_id = ?")
            params.append(str(case_id).strip())
        if owner_evidence_id:
            clauses.append("owner_evidence_id = ?")
            params.append(str(owner_evidence_id).strip())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT raw_json
                FROM trade_lifecycle_source_consumptions
                {where}
                ORDER BY created_at_ms ASC, source_key ASC
                """,
                params,
            ).fetchall()
        return [_json_object(row["raw_json"]) for row in rows]

    def insert_trade_lifecycle_allocation(
        self,
        allocation: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = dict(allocation or {})
        required = (
            "allocation_id",
            "case_id",
            "evidence_id",
            "target_lot_id",
            "terminal_type",
            "canonical_terminal_event_id",
        )
        values = {field: str(payload.get(field) or "").strip() for field in required}
        if any(not value for value in values.values()):
            raise ValueError("lifecycle allocation is missing required identity")
        contracts = int(payload.get("contracts_allocated") or 0)
        if contracts <= 0 or contracts != float(payload.get("contracts_allocated")):
            raise ValueError("lifecycle allocation contracts must be a positive integer")
        raw_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                "SELECT raw_json FROM trade_lifecycle_allocations WHERE allocation_id = ?",
                (values["allocation_id"],),
            ).fetchone()
            if existing is not None:
                if str(existing["raw_json"] or "") != raw_json:
                    raise ValueError(f"lifecycle allocation conflict for allocation_id={values['allocation_id']}")
                return False
            active_conn.execute(
                """
                INSERT INTO trade_lifecycle_allocations (
                  allocation_id, case_id, evidence_id, target_lot_id, terminal_type,
                  contracts_allocated, canonical_terminal_event_id, created_at_ms, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["allocation_id"],
                    values["case_id"],
                    values["evidence_id"],
                    values["target_lot_id"],
                    values["terminal_type"].lower(),
                    contracts,
                    values["canonical_terminal_event_id"],
                    int(now_ms()),
                    raw_json,
                ),
            )
        return True

    def list_trade_lifecycle_allocations(
        self,
        *,
        case_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        where = "WHERE case_id = ?" if case_id else ""
        params = (str(case_id).strip(),) if case_id else ()
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT raw_json
                FROM trade_lifecycle_allocations
                {where}
                ORDER BY created_at_ms ASC, allocation_id ASC
                """,
                params,
            ).fetchall()
        return [_json_object(row["raw_json"]) for row in rows]

    def insert_trade_lifecycle_timing_policy_once(
        self,
        policy: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = dict(policy or {})
        payload.pop("pairing_until_ms", None)
        case_id = str(payload.get("case_id") or "").strip()
        if (
            not case_id
            or str(payload.get("policy_schema") or "").strip()
            != "lifecycle_timing_policy.v1"
        ):
            raise ValueError(
                "lifecycle timing policy requires case_id and v1 schema"
            )
        raw_json = _json_text(payload)
        ts = int(now_ms())
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                """
                SELECT raw_json
                FROM trade_lifecycle_timing_policies
                WHERE case_id = ?
                """,
                (case_id,),
            ).fetchone()
            if existing is not None:
                existing_payload = _json_object(existing["raw_json"])
                existing_payload.pop("pairing_until_ms", None)
                if _json_text(existing_payload) != raw_json:
                    raise ValueError(
                        "lifecycle timing policy immutable conflict "
                        f"for case_id={case_id}"
                    )
                return False
            columns = {
                str(row["name"])
                for row in active_conn.execute(
                    "PRAGMA table_info(trade_lifecycle_timing_policies)"
                ).fetchall()
            }
            names = [
                "case_id",
                "policy_schema",
                "market",
                "timezone",
                "settlement_style",
                "underlying_security_type",
                "last_trade_cutoff_ms",
                "last_trade_cutoff_source",
            ]
            values: list[Any] = [
                case_id,
                str(payload["policy_schema"]),
                str(payload.get("market") or "").strip().upper(),
                str(payload.get("timezone") or "").strip(),
                str(payload.get("settlement_style") or "").strip().lower(),
                str(
                    payload.get("underlying_security_type") or ""
                ).strip().lower(),
                int(payload.get("last_trade_cutoff_ms") or 0),
                str(payload.get("last_trade_cutoff_source") or "").strip(),
            ]
            if "pairing_until_ms" in columns:
                # Compatibility with databases initialized by the pre-v2 draft.
                names.append("pairing_until_ms")
                values.append(0)
            names.extend(
                [
                    "settlement_deadline_ms",
                    "trading_days_json",
                    "calendar_source",
                    "calendar_observed_at_ms",
                    "calendar_hash",
                    "created_at_ms",
                    "raw_json",
                ]
            )
            values.extend(
                [
                    int(payload.get("settlement_deadline_ms") or 0),
                    _json_text(payload.get("trading_days") or []),
                    str(payload.get("calendar_source") or "").strip(),
                    int(payload.get("calendar_observed_at_ms") or 0),
                    str(payload.get("calendar_hash") or "").strip(),
                    ts,
                    raw_json,
                ]
            )
            placeholders = ", ".join("?" for _ in names)
            active_conn.execute(
                f"""
                INSERT INTO trade_lifecycle_timing_policies (
                  {", ".join(names)}
                ) VALUES ({placeholders})
                """,
                values,
            )
        return True

    def get_trade_lifecycle_timing_policy(
        self,
        case_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT raw_json
                FROM trade_lifecycle_timing_policies
                WHERE case_id = ?
                """,
                (str(case_id or "").strip(),),
            ).fetchone()
        return _json_object(row["raw_json"]) if row is not None else None

    def list_trade_lifecycle_timing_policies(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT raw_json
                FROM trade_lifecycle_timing_policies
                ORDER BY case_id ASC
                """
            ).fetchall()
        return [_json_object(row["raw_json"]) for row in rows]

    def insert_trade_lifecycle_notification_once(
        self,
        intent: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = dict(intent.get("payload") or {})
        payload_json = _json_text(payload)
        payload_hash = hashlib.sha256(
            payload_json.encode("utf-8")
        ).hexdigest()
        supplied_hash = str(intent.get("payload_hash") or "").strip()
        if supplied_hash and supplied_hash != payload_hash:
            raise ValueError("notification outbox payload hash mismatch")
        outbox_id = str(intent.get("outbox_id") or "").strip()
        case_id = str(intent.get("case_id") or "").strip()
        transition_type = str(
            intent.get("transition_type") or ""
        ).strip().lower()
        revision = int(intent.get("resolution_revision") or 0)
        delivery_revision = int(intent.get("delivery_revision") or 0)
        transition_key = str(intent.get("transition_key") or "").strip()
        state_fingerprint = str(
            intent.get("state_fingerprint") or ""
        ).strip()
        status = str(intent.get("status") or "pending").strip().lower()
        if (
            not outbox_id
            or not case_id
            or not transition_type
            or revision <= 0
            or delivery_revision < 0
            or not transition_key
            or not state_fingerprint
            or status not in {"pending", "suppressed"}
        ):
            raise ValueError("notification outbox intent is incomplete")
        ts = int(now_ms())
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                """
                SELECT *
                FROM trade_lifecycle_notification_outbox
                WHERE transition_key = ? AND delivery_revision = ?
                """,
                (transition_key, delivery_revision),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["outbox_id"] or "") != outbox_id
                    or str(existing["case_id"] or "") != case_id
                    or str(existing["transition_type"] or "")
                    != transition_type
                    or int(existing["resolution_revision"] or 0)
                    != revision
                    or str(existing["state_fingerprint"] or "")
                    != state_fingerprint
                    or str(existing["payload_hash"] or "") != payload_hash
                    or str(existing["payload_json"] or "") != payload_json
                ):
                    raise ValueError(
                        "notification outbox immutable intent conflict"
                    )
                return False
            active_conn.execute(
                """
                INSERT INTO trade_lifecycle_notification_outbox (
                  outbox_id, case_id, transition_type, resolution_revision,
                  delivery_revision, transition_key, state_fingerprint,
                  status, payload_json, payload_hash, attempt_count,
                  next_attempt_at_ms, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    outbox_id,
                    case_id,
                    transition_type,
                    revision,
                    delivery_revision,
                    transition_key,
                    state_fingerprint,
                    status,
                    payload_json,
                    payload_hash,
                    ts if status == "pending" else None,
                    ts,
                    ts,
                ),
            )
        return True

    def insert_trade_lifecycle_migration_receipt_once(
        self,
        receipt: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = dict(receipt or {})
        target_key = str(payload.get("target_key") or "").strip()
        migration_schema = str(
            payload.get("migration_schema") or ""
        ).strip()
        manifest_hash = str(
            payload.get("manifest_hash") or ""
        ).strip()
        row_hash = str(payload.get("row_hash") or "").strip()
        if (
            not target_key
            or not migration_schema
            or not manifest_hash
            or not row_hash
        ):
            raise ValueError(
                "lifecycle migration receipt identity is incomplete"
            )
        raw_json = _json_text(payload)
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                """
                SELECT row_hash
                FROM trade_lifecycle_migration_receipts
                WHERE target_key = ?
                """,
                (target_key,),
            ).fetchone()
            if existing is not None:
                if str(existing["row_hash"] or "") != row_hash:
                    raise ValueError(
                        "lifecycle migration receipt row conflict"
                    )
                return False
            active_conn.execute(
                """
                INSERT INTO trade_lifecycle_migration_receipts (
                  target_key, migration_schema, manifest_hash,
                  row_hash, applied_at_ms, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    target_key,
                    migration_schema,
                    manifest_hash,
                    row_hash,
                    int(now_ms()),
                    raw_json,
                ),
            )
        return True

    def list_trade_lifecycle_migration_receipts(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT raw_json
                FROM trade_lifecycle_migration_receipts
                ORDER BY target_key ASC
                """
            ).fetchall()
        return [_json_object(row["raw_json"]) for row in rows]

    def get_trade_lifecycle_notification(
        self,
        outbox_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT *
                FROM trade_lifecycle_notification_outbox
                WHERE outbox_id = ?
                """,
                (str(outbox_id or "").strip(),),
            ).fetchone()
        return _notification_outbox_row(row) if row is not None else None

    def get_trade_lifecycle_notification_by_transition(
        self,
        *,
        transition_key: str,
        delivery_revision: int = 0,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        transition_key_value = str(transition_key or "").strip()
        delivery_revision_value = int(delivery_revision)
        if not transition_key_value or delivery_revision_value < 0:
            raise ValueError("notification transition identity is incomplete")
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT *
                FROM trade_lifecycle_notification_outbox
                WHERE transition_key = ? AND delivery_revision = ?
                """,
                (transition_key_value, delivery_revision_value),
            ).fetchone()
        return _notification_outbox_row(row) if row is not None else None

    def list_trade_lifecycle_notifications(
        self,
        *,
        status: str | None = None,
        case_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(str(status).strip().lower())
        if case_id:
            clauses.append("case_id = ?")
            params.append(str(case_id).strip())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT *
                FROM trade_lifecycle_notification_outbox
                {where}
                ORDER BY created_at_ms ASC, outbox_id ASC
                """,
                params,
            ).fetchall()
        return [_notification_outbox_row(row) for row in rows]

    def compare_and_set_trade_lifecycle_notification(
        self,
        *,
        outbox_id: str,
        expected_status: str,
        new_status: str,
        claim_id: str | None = None,
        expected_claim_id: str | None = None,
        fields: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        allowed_fields = {
            "provider_message_id",
            "claim_id",
            "claimed_at_ms",
            "send_started_at_ms",
            "attempt_count",
            "next_attempt_at_ms",
            "last_error",
            "provider_receipt_json",
            "confirmed_at_ms",
        }
        updates = dict(fields or {})
        invalid = sorted(set(updates) - allowed_fields)
        if invalid:
            raise ValueError(
                "unsupported notification outbox fields: "
                + ",".join(invalid)
            )
        if claim_id is not None:
            updates["claim_id"] = claim_id
        assignments = ["status = ?", "updated_at_ms = ?"]
        values: list[Any] = [
            str(new_status or "").strip().lower(),
            int(now_ms()),
        ]
        for key in sorted(updates):
            value = updates[key]
            if key == "provider_receipt_json" and isinstance(value, dict):
                value = _json_text(value)
            assignments.append(f"{key} = ?")
            values.append(value)
        clauses = ["outbox_id = ?", "status = ?"]
        values.extend(
            (
                str(outbox_id or "").strip(),
                str(expected_status or "").strip().lower(),
            )
        )
        if expected_claim_id is not None:
            clauses.append("claim_id = ?")
            values.append(str(expected_claim_id))
        with self._optional_conn(conn, commit=True) as active_conn:
            cursor = active_conn.execute(
                f"""
                UPDATE trade_lifecycle_notification_outbox
                SET {', '.join(assignments)}
                WHERE {' AND '.join(clauses)}
                """,
                values,
            )
        return int(cursor.rowcount or 0) == 1

    def insert_trade_lifecycle_notification_batch_once(
        self,
        batch: dict[str, Any],
        *,
        member_outbox_ids: Sequence[str],
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = dict(batch.get("payload") or {})
        payload_json = _json_text(payload)
        payload_hash = hashlib.sha256(
            payload_json.encode("utf-8")
        ).hexdigest()
        supplied_hash = str(batch.get("payload_hash") or "").strip()
        if supplied_hash and supplied_hash != payload_hash:
            raise ValueError(
                "notification delivery batch payload hash mismatch"
            )
        batch_id = str(batch.get("batch_id") or "").strip()
        route_fingerprint = str(
            batch.get("route_fingerprint") or ""
        ).strip()
        provider = str(batch.get("provider") or "").strip().lower()
        channel = str(batch.get("channel") or "").strip().lower()
        target_fingerprint = str(
            batch.get("target_fingerprint") or ""
        ).strip()
        renderer_version = str(
            batch.get("renderer_version") or ""
        ).strip()
        status = str(batch.get("status") or "pending").strip().lower()
        member_ids = tuple(
            str(value or "").strip() for value in member_outbox_ids
        )
        if not member_ids or any(not value for value in member_ids):
            raise ValueError(
                "notification delivery batch members are incomplete"
            )
        if len(set(member_ids)) != len(member_ids):
            raise ValueError(
                "notification delivery batch members must be unique"
            )
        payload_members_raw = payload.get("members")
        payload_members = (
            list(payload_members_raw)
            if isinstance(payload_members_raw, list)
            else []
        )
        payload_route = (
            dict(payload.get("route") or {})
            if isinstance(payload.get("route"), dict)
            else {}
        )
        payload_member_ids = tuple(
            str(item.get("outbox_id") or "").strip()
            for item in payload_members
            if isinstance(item, dict)
        )
        member_count = int(batch.get("member_count") or 0)
        first_created = int(
            batch.get("first_intent_created_at_ms") or 0
        )
        last_created = int(
            batch.get("last_intent_created_at_ms") or 0
        )
        created_at = int(batch.get("created_at_ms") or 0)
        attempts = int(batch.get("attempt_count") or 0)
        next_attempt = batch.get("next_attempt_at_ms")
        if (
            not batch_id
            or not route_fingerprint
            or not provider
            or not channel
            or not target_fingerprint
            or not renderer_version
            or status != "pending"
            or member_count != len(member_ids)
            or first_created <= 0
            or last_created < first_created
            or created_at <= 0
            or attempts < 0
            or str(payload.get("batch_id") or "").strip() != batch_id
            or str(payload.get("schema_version") or "").strip()
            != renderer_version
            or payload_member_ids != member_ids
            or len(payload_members) != len(member_ids)
            or str(payload_route.get("provider") or "").strip().lower()
            != provider
            or str(payload_route.get("channel") or "").strip().lower()
            != channel
            or str(
                payload_route.get("target_fingerprint") or ""
            ).strip()
            != target_fingerprint
            or str(
                payload_route.get("route_fingerprint") or ""
            ).strip()
            != route_fingerprint
        ):
            raise ValueError(
                "notification delivery batch is incomplete"
            )
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                """
                SELECT *
                FROM trade_lifecycle_notification_delivery_batches
                WHERE batch_id = ?
                """,
                (batch_id,),
            ).fetchone()
            if existing is not None:
                stored = _notification_delivery_batch_row(existing)
                immutable_conflict = any(
                    stored[key] != value
                    for key, value in {
                        "route_fingerprint": route_fingerprint,
                        "provider": provider,
                        "channel": channel,
                        "target_fingerprint": target_fingerprint,
                        "renderer_version": renderer_version,
                        "payload_hash": payload_hash,
                        "member_count": member_count,
                        "first_intent_created_at_ms": first_created,
                        "last_intent_created_at_ms": last_created,
                    }.items()
                )
                if immutable_conflict or stored["payload"] != payload:
                    raise ValueError(
                        "notification delivery batch immutable conflict"
                    )
                bound = active_conn.execute(
                    """
                    SELECT outbox_id
                    FROM trade_lifecycle_notification_outbox
                    WHERE delivery_batch_id = ?
                    ORDER BY created_at_ms ASC, outbox_id ASC
                    """,
                    (batch_id,),
                ).fetchall()
                if {str(row["outbox_id"]) for row in bound} != set(
                    member_ids
                ):
                    raise ValueError(
                        "notification delivery batch membership conflict"
                    )
                return False
            placeholders = ",".join("?" for _ in member_ids)
            member_rows = active_conn.execute(
                f"""
                SELECT *
                FROM trade_lifecycle_notification_outbox
                WHERE outbox_id IN ({placeholders})
                """,
                member_ids,
            ).fetchall()
            if len(member_rows) != len(member_ids):
                raise ValueError(
                    "notification delivery batch member not found"
                )
            members_by_id = {
                str(row["outbox_id"]): _notification_outbox_row(row)
                for row in member_rows
            }
            for envelope in payload_members:
                if not isinstance(envelope, dict):
                    raise ValueError(
                        "notification delivery batch member payload is invalid"
                    )
                outbox_id = str(
                    envelope.get("outbox_id") or ""
                ).strip()
                row = members_by_id.get(outbox_id)
                if not isinstance(row, dict):
                    raise ValueError(
                        "notification delivery batch member not found"
                    )
                if row["delivery_batch_id"] is not None or str(
                    row["status"] or ""
                ) not in {"pending", "explicit_failed"}:
                    raise ValueError(
                        "notification delivery batch member is not bindable"
                    )
                expected_envelope = {
                    "outbox_id": str(row["outbox_id"]),
                    "case_id": str(row["case_id"]),
                    "transition_type": str(row["transition_type"]),
                    "resolution_revision": int(
                        row["resolution_revision"]
                    ),
                    "delivery_revision": int(
                        row.get("delivery_revision") or 0
                    ),
                    "transition_key": str(row["transition_key"]),
                    "state_fingerprint": str(
                        row["state_fingerprint"]
                    ),
                    "payload_hash": str(row["payload_hash"]),
                    "created_at_ms": int(row["created_at_ms"]),
                    "payload": dict(row.get("payload") or {}),
                }
                if envelope != expected_envelope:
                    raise ValueError(
                        "notification delivery batch member payload mismatch"
                    )
            actual_created = [
                int(row["created_at_ms"])
                for row in members_by_id.values()
            ]
            if (
                min(actual_created) != first_created
                or max(actual_created) != last_created
            ):
                raise ValueError(
                    "notification delivery batch member time range mismatch"
                )
            active_conn.execute(
                """
                INSERT INTO trade_lifecycle_notification_delivery_batches (
                  batch_id, route_fingerprint, provider, channel,
                  target_fingerprint, renderer_version, status,
                  payload_json, payload_hash, member_count,
                  first_intent_created_at_ms,
                  last_intent_created_at_ms, attempt_count,
                  next_attempt_at_ms, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    route_fingerprint,
                    provider,
                    channel,
                    target_fingerprint,
                    renderer_version,
                    status,
                    payload_json,
                    payload_hash,
                    member_count,
                    first_created,
                    last_created,
                    attempts,
                    next_attempt,
                    created_at,
                    created_at,
                ),
            )
            cursor = active_conn.execute(
                f"""
                UPDATE trade_lifecycle_notification_outbox
                SET delivery_batch_id = ?, status = 'batched',
                    claim_id = NULL, claimed_at_ms = NULL,
                    send_started_at_ms = NULL,
                    next_attempt_at_ms = NULL,
                    updated_at_ms = ?
                WHERE outbox_id IN ({placeholders})
                  AND delivery_batch_id IS NULL
                  AND status IN ('pending', 'explicit_failed')
                """,
                (batch_id, created_at, *member_ids),
            )
            if int(cursor.rowcount or 0) != len(member_ids):
                raise ValueError(
                    "notification delivery batch binding lost"
                )
        return True

    def get_trade_lifecycle_notification_batch(
        self,
        batch_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT *
                FROM trade_lifecycle_notification_delivery_batches
                WHERE batch_id = ?
                """,
                (str(batch_id or "").strip(),),
            ).fetchone()
        return (
            _notification_delivery_batch_row(row)
            if row is not None
            else None
        )

    def list_trade_lifecycle_notification_batches(
        self,
        *,
        status: str | None = None,
        route_fingerprint: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(str(status).strip().lower())
        if route_fingerprint:
            clauses.append("route_fingerprint = ?")
            params.append(str(route_fingerprint).strip())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT *
                FROM trade_lifecycle_notification_delivery_batches
                {where}
                ORDER BY created_at_ms ASC, batch_id ASC
                """,
                params,
            ).fetchall()
        return [_notification_delivery_batch_row(row) for row in rows]

    def list_trade_lifecycle_notification_batch_members(
        self,
        batch_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT *
                FROM trade_lifecycle_notification_outbox
                WHERE delivery_batch_id = ?
                ORDER BY created_at_ms ASC, outbox_id ASC
                """,
                (str(batch_id or "").strip(),),
            ).fetchall()
        return [_notification_outbox_row(row) for row in rows]

    def compare_and_set_trade_lifecycle_notification_batch(
        self,
        *,
        batch_id: str,
        expected_status: str,
        new_status: str,
        claim_id: str | None = None,
        expected_claim_id: str | None = None,
        fields: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        allowed_fields = {
            "provider_message_id",
            "claim_id",
            "claimed_at_ms",
            "send_started_at_ms",
            "attempt_count",
            "next_attempt_at_ms",
            "last_error",
            "provider_receipt_json",
            "confirmed_at_ms",
        }
        updates = dict(fields or {})
        invalid = sorted(set(updates) - allowed_fields)
        if invalid:
            raise ValueError(
                "unsupported notification delivery batch fields: "
                + ",".join(invalid)
            )
        if claim_id is not None:
            updates["claim_id"] = claim_id
        assignments = ["status = ?", "updated_at_ms = ?"]
        values: list[Any] = [
            str(new_status or "").strip().lower(),
            int(now_ms()),
        ]
        for key in sorted(updates):
            value = updates[key]
            if key == "provider_receipt_json" and isinstance(value, dict):
                value = _json_text(value)
            assignments.append(f"{key} = ?")
            values.append(value)
        clauses = ["batch_id = ?", "status = ?"]
        values.extend(
            (
                str(batch_id or "").strip(),
                str(expected_status or "").strip().lower(),
            )
        )
        if expected_claim_id is not None:
            clauses.append("claim_id = ?")
            values.append(str(expected_claim_id))
        with self._optional_conn(conn, commit=True) as active_conn:
            cursor = active_conn.execute(
                f"""
                UPDATE trade_lifecycle_notification_delivery_batches
                SET {', '.join(assignments)}
                WHERE {' AND '.join(clauses)}
                """,
                values,
            )
        return int(cursor.rowcount or 0) == 1

    def update_trade_lifecycle_notification_batch_members(
        self,
        *,
        batch_id: str,
        expected_statuses: Sequence[str],
        new_status: str,
        fields: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        statuses = tuple(
            str(value or "").strip().lower()
            for value in expected_statuses
            if str(value or "").strip()
        )
        if not statuses:
            raise ValueError(
                "notification batch member expected status is required"
            )
        allowed_fields = {
            "attempt_count",
            "next_attempt_at_ms",
            "last_error",
            "confirmed_at_ms",
        }
        updates = dict(fields or {})
        invalid = sorted(set(updates) - allowed_fields)
        if invalid:
            raise ValueError(
                "unsupported notification batch member fields: "
                + ",".join(invalid)
            )
        assignments = ["status = ?", "updated_at_ms = ?"]
        values: list[Any] = [
            str(new_status or "").strip().lower(),
            int(now_ms()),
        ]
        for key in sorted(updates):
            assignments.append(f"{key} = ?")
            values.append(updates[key])
        placeholders = ",".join("?" for _ in statuses)
        values.append(str(batch_id or "").strip())
        values.extend(statuses)
        with self._optional_conn(conn, commit=True) as active_conn:
            cursor = active_conn.execute(
                f"""
                UPDATE trade_lifecycle_notification_outbox
                SET {', '.join(assignments)}
                WHERE delivery_batch_id = ?
                  AND status IN ({placeholders})
                """,
                values,
            )
        return int(cursor.rowcount or 0)

    def insert_strategy_group_identity(
        self,
        identity: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = dict(identity or {})
        group_id = str(payload.get("group_id") or "").strip()
        identity_hash = str(payload.get("identity_hash") or "").strip()
        if not group_id or not identity_hash:
            raise ValueError("strategy group identity requires group_id and identity_hash")
        raw_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                "SELECT identity_hash FROM strategy_group_identities WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["identity_hash"] or "") != identity_hash:
                    raise ValueError(f"strategy group identity conflict for group_id={group_id}")
                return False
            active_conn.execute(
                """
                INSERT INTO strategy_group_identities (
                  group_id, schema_version, strategy, account, symbol,
                  funding_put_record_id, funding_put_open_event_id, funding_put_contract_key,
                  participation_call_record_id, participation_call_open_event_id,
                  participation_call_contract_key, original_contracts, created_at_ms,
                  identity_hash, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    str(payload.get("schema_version") or "").strip(),
                    str(payload.get("strategy") or "").strip().lower(),
                    str(payload.get("account") or "").strip().lower(),
                    str(payload.get("symbol") or "").strip().upper(),
                    str(payload.get("funding_put_record_id") or "").strip(),
                    str(payload.get("funding_put_open_event_id") or "").strip(),
                    _json_text(payload.get("funding_put_contract_key")),
                    str(payload.get("participation_call_record_id") or "").strip(),
                    str(payload.get("participation_call_open_event_id") or "").strip(),
                    _json_text(payload.get("participation_call_contract_key")),
                    int(payload.get("original_contracts") or 0),
                    int(payload.get("created_at_ms") or now_ms()),
                    identity_hash,
                    raw_json,
                ),
            )
        return True

    def get_strategy_group_identity(
        self,
        group_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                "SELECT raw_json FROM strategy_group_identities WHERE group_id = ?",
                (str(group_id or "").strip(),),
            ).fetchone()
        return _json_object(row["raw_json"]) if row is not None else None

    def list_strategy_group_identities(
        self,
        *,
        account: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        where = "WHERE account = ?" if account else ""
        params = (str(account).strip().lower(),) if account else ()
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT raw_json FROM strategy_group_identities
                {where}
                ORDER BY account ASC, symbol ASC, group_id ASC
                """,
                params,
            ).fetchall()
        return [_json_object(row["raw_json"]) for row in rows]

    def upsert_combo_pair_inference(
        self,
        inference: dict[str, Any],
        *,
        reactivate_stale: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        payload = _normalize_combo_pair_inference_payload(inference)
        inference_id = str(payload["inference_id"])
        with self._optional_conn(conn, commit=True) as active_conn:
            existing_row = active_conn.execute(
                """
                SELECT raw_json, status, created_at_ms
                FROM combo_pair_inferences
                WHERE inference_id = ?
                """,
                (inference_id,),
            ).fetchone()
            if existing_row is not None:
                existing = _json_object(existing_row["raw_json"])
                _assert_same_combo_pair_inference_identity(existing, payload)
                existing_status = str(existing_row["status"] or "").strip().lower()
                reactivating = (
                    bool(reactivate_stale)
                    and existing_status == "expired_unresolved"
                    and str(existing.get("decision_reason") or "").strip()
                    == "facts_drifted_or_leg_claimed"
                )
                if (
                    existing_status not in {"proposal_ready", "ambiguous"}
                    and not reactivating
                ):
                    return False
                created_at_ms = int(existing_row["created_at_ms"])
            else:
                reactivating = False
                created_at_ms = int(payload.get("created_at_ms") or now_ms())
            updated_at_ms = int(now_ms())
            payload["created_at_ms"] = created_at_ms
            payload["updated_at_ms"] = updated_at_ms
            raw_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            values = _combo_pair_inference_sql_values(
                payload,
                raw_json=raw_json,
            )
            if existing_row is None:
                active_conn.execute(
                    """
                    INSERT INTO combo_pair_inferences (
                      inference_id, schema_version, algorithm_version,
                      account, symbol, market, market_date,
                      put_record_id, put_open_event_id,
                      call_record_id, call_open_event_id,
                      evidence_grade,
                      candidate_occurrence_ids_json,
                      candidate_exposure_ids_json,
                      input_snapshot_hash, status, proposal_expires_at_ms,
                      evidence_json, alternatives_json, strategy_group_id,
                      identity_hash, put_adoption_event_id, call_adoption_event_id,
                      put_void_event_id, call_void_event_id,
                      decision_at_ms, decision_by, decision_reason,
                      created_at_ms, updated_at_ms, raw_json
                    ) VALUES (
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    values,
                )
                return True
            active_conn.execute(
                """
                UPDATE combo_pair_inferences
                SET algorithm_version = ?, evidence_grade = ?,
                    candidate_occurrence_ids_json = ?,
                    candidate_exposure_ids_json = ?,
                    input_snapshot_hash = ?, status = ?,
                    proposal_expires_at_ms = ?, evidence_json = ?,
                    alternatives_json = ?, strategy_group_id = ?,
                    decision_at_ms = NULL, decision_by = NULL,
                    decision_reason = NULL,
                    updated_at_ms = ?, raw_json = ?
                WHERE inference_id = ?
                  AND (
                    status IN ('proposal_ready', 'ambiguous')
                    OR (
                      ? = 1
                      AND status = 'expired_unresolved'
                      AND decision_reason = 'facts_drifted_or_leg_claimed'
                    )
                  )
                """,
                (
                    str(payload["algorithm_version"]),
                    str(payload["evidence_grade"]),
                    _json_text(payload["candidate_occurrence_ids"]),
                    _json_text(payload["candidate_exposure_ids"]),
                    str(payload["input_snapshot_hash"]),
                    str(payload["status"]),
                    int(payload["proposal_expires_at_ms"]),
                    _json_text(payload["evidence"]),
                    _json_text(payload["alternative_inference_ids"]),
                    str(payload["strategy_group_id"]),
                    updated_at_ms,
                    raw_json,
                    inference_id,
                    int(reactivating),
                ),
            )
        return False

    def get_combo_pair_inference(
        self,
        inference_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT raw_json
                FROM combo_pair_inferences
                WHERE inference_id = ?
                """,
                (str(inference_id or "").strip(),),
            ).fetchone()
        return _json_object(row["raw_json"]) if row is not None else None

    def transition_combo_pair_inference(
        self,
        *,
        inference_id: str,
        expected_statuses: Sequence[str],
        new_status: str,
        expected_input_hash: str | None = None,
        decision_fields: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        inference_value = str(inference_id or "").strip()
        expected = sorted(
            {str(item or "").strip().lower() for item in expected_statuses}
            - {""}
        )
        status_value = str(new_status or "").strip().lower()
        allowed_statuses = {
            "proposal_ready",
            "ambiguous",
            "user_confirmed",
            "user_rejected",
            "expired_unresolved",
            "superseded",
        }
        if not inference_value or not expected or status_value not in allowed_statuses:
            raise ValueError("combo inference transition is incomplete")
        allowed_fields = {
            "decision_at_ms",
            "decision_by",
            "decision_reason",
            "strategy_group_id",
            "identity_hash",
            "put_adoption_event_id",
            "call_adoption_event_id",
            "put_void_event_id",
            "call_void_event_id",
        }
        updates = dict(decision_fields or {})
        invalid = sorted(set(updates) - allowed_fields)
        if invalid:
            raise ValueError(
                "unsupported combo inference decision fields: " + ",".join(invalid)
            )
        with self._optional_conn(conn, commit=True) as active_conn:
            row = active_conn.execute(
                "SELECT raw_json, status, input_snapshot_hash FROM combo_pair_inferences WHERE inference_id = ?",
                (inference_value,),
            ).fetchone()
            if row is None:
                raise ValueError(f"combo inference not found: {inference_value}")
            current_status = str(row["status"] or "").strip().lower()
            if current_status not in expected:
                raise ValueError(
                    f"combo inference status compare-and-set failed: {current_status}"
                )
            if (
                expected_input_hash is not None
                and str(row["input_snapshot_hash"] or "").strip()
                != str(expected_input_hash or "").strip()
            ):
                raise ValueError("combo inference input hash compare-and-set failed")
            payload = _json_object(row["raw_json"])
            payload.update(updates)
            payload["status"] = status_value
            updated_at_ms = int(updates.get("decision_at_ms") or now_ms())
            payload["updated_at_ms"] = updated_at_ms
            cursor = active_conn.execute(
                """
                UPDATE combo_pair_inferences
                SET status = ?, decision_at_ms = ?, decision_by = ?,
                    decision_reason = ?, strategy_group_id = ?, identity_hash = ?,
                    put_adoption_event_id = ?, call_adoption_event_id = ?,
                    put_void_event_id = ?, call_void_event_id = ?,
                    updated_at_ms = ?, raw_json = ?
                WHERE inference_id = ? AND status = ?
                """,
                (
                    status_value,
                    payload.get("decision_at_ms"),
                    payload.get("decision_by"),
                    payload.get("decision_reason"),
                    payload.get("strategy_group_id"),
                    payload.get("identity_hash"),
                    payload.get("put_adoption_event_id"),
                    payload.get("call_adoption_event_id"),
                    payload.get("put_void_event_id"),
                    payload.get("call_void_event_id"),
                    updated_at_ms,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    inference_value,
                    current_status,
                ),
            )
            if int(cursor.rowcount or 0) != 1:
                raise ValueError("combo inference status compare-and-set failed")
        return payload

    def list_combo_pair_inferences(
        self,
        *,
        account: str | None = None,
        status: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if account:
            clauses.append("account = ?")
            values.append(str(account).strip().lower())
        if status:
            clauses.append("status = ?")
            values.append(str(status).strip().lower())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT raw_json
                FROM combo_pair_inferences
                {where}
                ORDER BY account ASC, market_date DESC, symbol ASC,
                         updated_at_ms DESC, inference_id ASC
                """,
                values,
            ).fetchall()
        return [_json_object(row["raw_json"]) for row in rows]

    def expire_combo_pair_inferences(
        self,
        *,
        effective_now_ms: int,
        account: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        cutoff = int(effective_now_ms)
        if cutoff <= 0:
            raise ValueError("effective_now_ms must be > 0")
        clauses = [
            "status IN ('proposal_ready', 'ambiguous')",
            "proposal_expires_at_ms < ?",
        ]
        values: list[Any] = [cutoff]
        if account:
            clauses.append("account = ?")
            values.append(str(account).strip().lower())
        with self._optional_conn(conn, commit=True) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT inference_id, raw_json
                FROM combo_pair_inferences
                WHERE {' AND '.join(clauses)}
                ORDER BY inference_id ASC
                """,
                values,
            ).fetchall()
            updated = 0
            for row in rows:
                payload = _json_object(row["raw_json"])
                payload["status"] = "expired_unresolved"
                payload["updated_at_ms"] = cutoff
                payload["decision_at_ms"] = cutoff
                payload["decision_reason"] = "proposal_expired"
                active_conn.execute(
                    """
                    UPDATE combo_pair_inferences
                    SET status = 'expired_unresolved', decision_at_ms = ?,
                        decision_reason = 'proposal_expired', updated_at_ms = ?,
                        raw_json = ?
                    WHERE inference_id = ?
                      AND status IN ('proposal_ready', 'ambiguous')
                    """,
                    (
                        cutoff,
                        cutoff,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        str(row["inference_id"]),
                    ),
                )
                updated += 1
        return updated

    def expire_stale_combo_pair_inferences(
        self,
        *,
        account: str,
        active_inference_ids: Sequence[str],
        effective_now_ms: int,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        account_value = str(account or "").strip().lower()
        if not account_value:
            raise ValueError("account is required to expire stale combo inferences")
        changed_at_ms = int(effective_now_ms)
        if changed_at_ms <= 0:
            raise ValueError("effective_now_ms must be > 0")
        active_ids = {
            str(item).strip() for item in active_inference_ids if str(item).strip()
        }
        with self._optional_conn(conn, commit=True) as active_conn:
            rows = active_conn.execute(
                """
                SELECT inference_id, raw_json
                FROM combo_pair_inferences
                WHERE account = ?
                  AND status IN ('proposal_ready', 'ambiguous')
                ORDER BY inference_id ASC
                """,
                (account_value,),
            ).fetchall()
            updated = 0
            for row in rows:
                inference_id = str(row["inference_id"])
                if inference_id in active_ids:
                    continue
                payload = _json_object(row["raw_json"])
                payload["status"] = "expired_unresolved"
                payload["updated_at_ms"] = changed_at_ms
                payload["decision_at_ms"] = changed_at_ms
                payload["decision_reason"] = "facts_drifted_or_leg_claimed"
                cursor = active_conn.execute(
                    """
                    UPDATE combo_pair_inferences
                    SET status = 'expired_unresolved', decision_at_ms = ?,
                        decision_reason = 'facts_drifted_or_leg_claimed',
                        updated_at_ms = ?, raw_json = ?
                    WHERE inference_id = ?
                      AND status IN ('proposal_ready', 'ambiguous')
                    """,
                    (
                        changed_at_ms,
                        changed_at_ms,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        inference_id,
                    ),
                )
                updated += int(cursor.rowcount or 0)
        return updated

    def assert_foreign_keys_clean(self, *, conn: sqlite3.Connection | None = None) -> None:
        with self._optional_conn(conn) as active_conn:
            violations = active_conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"SQLite foreign key check failed: {len(violations)} violation(s)")

    def _read_account_decision_state_rows(
        self,
        *,
        account: str,
        conn: sqlite3.Connection,
        shared_trade_events: Sequence[dict[str, Any]] | None = None,
        shared_position_lots: Sequence[dict[str, Any]] | None = None,
        shared_assigned_stock_events: Sequence[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        account_value = str(account or "").strip().lower()
        if not account_value:
            raise ValueError("decision state snapshot requires account")
        events = (
            list(shared_trade_events)
            if shared_trade_events is not None
            else self.list_trade_events(conn=conn)
        )
        lots = (
            list(shared_position_lots)
            if shared_position_lots is not None
            else self.list_position_lots(conn=conn)
        )
        cases = [
            _json_object(row["raw_json"])
            for row in conn.execute(
                """
                SELECT raw_json
                FROM trade_lifecycle_cases
                WHERE account = ?
                ORDER BY updated_at_ms DESC, case_id DESC
                """,
                (account_value,),
            ).fetchall()
        ]
        evidence: list[dict[str, Any]] = []
        evidence_received_at_ms_by_id: dict[str, int] = {}
        for row in conn.execute(
            """
            SELECT lifecycle_evidence.raw_json,
                   lifecycle_evidence.created_at_ms
            FROM trade_lifecycle_evidence AS lifecycle_evidence
            JOIN trade_lifecycle_cases AS lifecycle_case
              ON lifecycle_case.case_id = lifecycle_evidence.case_id
            WHERE lifecycle_case.account = ?
            ORDER BY lifecycle_evidence.created_at_ms ASC,
                     lifecycle_evidence.evidence_id ASC
            """,
            (account_value,),
        ).fetchall():
            payload = _json_object(row["raw_json"])
            evidence.append(payload)
            evidence_id = str(payload.get("evidence_id") or "").strip()
            if evidence_id:
                evidence_received_at_ms_by_id[evidence_id] = int(
                    row["created_at_ms"]
                )
        allocations = [
            _json_object(row["raw_json"])
            for row in conn.execute(
                """
                SELECT allocation.raw_json
                FROM trade_lifecycle_allocations AS allocation
                JOIN trade_lifecycle_cases AS lifecycle_case
                  ON lifecycle_case.case_id = allocation.case_id
                WHERE lifecycle_case.account = ?
                ORDER BY allocation.created_at_ms ASC,
                         allocation.allocation_id ASC
                """,
                (account_value,),
            ).fetchall()
        ]
        source_claims = [
            _json_object(row["raw_json"])
            for row in conn.execute(
                """
                SELECT source_claim.raw_json
                FROM trade_lifecycle_source_consumptions AS source_claim
                JOIN trade_lifecycle_cases AS lifecycle_case
                  ON lifecycle_case.case_id = source_claim.case_id
                WHERE lifecycle_case.account = ?
                ORDER BY source_claim.created_at_ms ASC,
                         source_claim.source_key ASC
                """,
                (account_value,),
            ).fetchall()
        ]
        timing_policies = [
            _json_object(row["raw_json"])
            for row in conn.execute(
                """
                SELECT timing.raw_json
                FROM trade_lifecycle_timing_policies AS timing
                JOIN trade_lifecycle_cases AS lifecycle_case
                  ON lifecycle_case.case_id = timing.case_id
                WHERE lifecycle_case.account = ?
                ORDER BY timing.case_id ASC
                """,
                (account_value,),
            ).fetchall()
        ]
        assigned_stock_events = (
            list(shared_assigned_stock_events)
            if shared_assigned_stock_events is not None
            else [
                _json_object(row["event_json"])
                for row in conn.execute(
                    """
                    SELECT event_json
                    FROM assigned_stock_events
                    ORDER BY trade_time_ms ASC, stock_event_id ASC
                    """
                ).fetchall()
            ]
        )
        identities = self.list_strategy_group_identities(
            account=account_value,
            conn=conn,
        )
        return {
            "account": account_value,
            "trade_events": events,
            "stored_position_lots": lots,
            "account_position_lots": [
                row
                for row in lots
                if str(
                    (row.get("fields") or {}).get("account") or ""
                ).strip().lower()
                == account_value
            ],
            "account_lifecycle_cases": cases,
            "account_lifecycle_evidence": evidence,
            "account_lifecycle_evidence_received_at_ms_by_id": (
                evidence_received_at_ms_by_id
            ),
            "account_lifecycle_allocations": allocations,
            "account_lifecycle_source_consumptions": source_claims,
            "account_lifecycle_timing_policies": timing_policies,
            "account_assigned_stock_events": [
                row
                for row in assigned_stock_events
                if str(
                    row.get("account")
                    or (row.get("raw_payload") or {}).get("account")
                    or ""
                ).strip().lower()
                == account_value
            ],
            "account_combo_identities": identities,
        }

    def read_lifecycle_account_rows(
        self,
        *,
        account: str,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        account_value = str(account or "").strip().lower()
        if not account_value:
            raise ValueError("lifecycle account reader requires account")
        if conn is not None:
            return self._read_account_decision_state_rows(
                account=account_value,
                conn=conn,
            )
        active_conn = self._connect()
        try:
            active_conn.execute("BEGIN")
            rows = self._read_account_decision_state_rows(
                account=account_value,
                conn=active_conn,
            )
            active_conn.commit()
        except Exception:
            active_conn.rollback()
            raise
        finally:
            active_conn.close()
        return rows

    def read_lifecycle_case_rows(
        self,
        *,
        case_id: str,
    ) -> dict[str, Any]:
        case_value = str(case_id or "").strip()
        if not case_value:
            raise ValueError("lifecycle case reader requires case_id")
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            lifecycle_case = self.get_trade_lifecycle_case(
                case_value,
                conn=conn,
            )
            if lifecycle_case is None:
                raise ValueError(f"lifecycle case not found: {case_value}")
            rows = self._read_account_decision_state_rows(
                account=str(lifecycle_case.get("account") or ""),
                conn=conn,
            )
            rows["requested_lifecycle_case_id"] = case_value
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return rows

    def read_decision_state_rows(self, *, account: str) -> dict[str, Any]:
        return self.read_lifecycle_account_rows(account=account)

    def read_decision_state_rows_many(
        self,
        *,
        accounts: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        """Read multiple account decision states from one SQLite snapshot."""

        account_values = sorted(
            {
                str(account or "").strip().lower()
                for account in accounts
                if str(account or "").strip()
            }
        )
        if not account_values:
            raise ValueError("decision state batch requires accounts")
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            events = self.list_trade_events(conn=conn)
            lots = self.list_position_lots(conn=conn)
            assigned_stock_events = [
                _json_object(row["event_json"])
                for row in conn.execute(
                    """
                    SELECT event_json
                    FROM assigned_stock_events
                    ORDER BY trade_time_ms ASC, stock_event_id ASC
                    """
                ).fetchall()
            ]
            rows = {
                account: self._read_account_decision_state_rows(
                    account=account,
                    conn=conn,
                    shared_trade_events=events,
                    shared_position_lots=lots,
                    shared_assigned_stock_events=assigned_stock_events,
                )
                for account in account_values
            }
            conn.commit()
            return rows
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: Any) -> dict[str, Any]:
    payload = json.loads(str(value) or "{}")
    if not isinstance(payload, dict):
        raise ValueError("stored ledger JSON value must be an object")
    return dict(payload)


def _normalize_combo_pair_inference_payload(
    inference: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(inference or {})
    required = (
        "inference_id",
        "schema_version",
        "algorithm_version",
        "account",
        "symbol",
        "market",
        "market_date",
        "put_record_id",
        "put_open_event_id",
        "call_record_id",
        "call_open_event_id",
        "evidence_grade",
        "input_snapshot_hash",
        "status",
        "strategy_group_id",
    )
    missing = [
        field
        for field in required
        if not str(payload.get(field) or "").strip()
    ]
    if missing:
        raise ValueError(
            "combo pair inference missing fields: " + ",".join(missing)
        )
    status = str(payload["status"]).strip().lower()
    allowed_statuses = {
        "proposal_ready",
        "ambiguous",
        "user_confirmed",
        "user_rejected",
        "expired_unresolved",
        "superseded",
    }
    if status not in allowed_statuses:
        raise ValueError(f"unsupported combo pair inference status: {status}")
    try:
        expires_at_ms = int(payload.get("proposal_expires_at_ms") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "combo pair inference proposal_expires_at_ms must be numeric"
        ) from exc
    if expires_at_ms <= 0:
        raise ValueError(
            "combo pair inference proposal_expires_at_ms must be > 0"
        )
    payload.update(
        {
            "inference_id": str(payload["inference_id"]).strip(),
            "schema_version": str(payload["schema_version"]).strip(),
            "algorithm_version": str(payload["algorithm_version"]).strip(),
            "account": str(payload["account"]).strip().lower(),
            "symbol": str(payload["symbol"]).strip().upper(),
            "market": str(payload["market"]).strip().upper(),
            "market_date": str(payload["market_date"]).strip(),
            "put_record_id": str(payload["put_record_id"]).strip(),
            "put_open_event_id": str(payload["put_open_event_id"]).strip(),
            "call_record_id": str(payload["call_record_id"]).strip(),
            "call_open_event_id": str(payload["call_open_event_id"]).strip(),
            "evidence_grade": str(payload["evidence_grade"]).strip().lower(),
            "candidate_occurrence_ids": _canonical_text_values(
                payload.get("candidate_occurrence_ids")
            ),
            "candidate_exposure_ids": _canonical_text_values(
                payload.get("candidate_exposure_ids")
            ),
            "input_snapshot_hash": str(payload["input_snapshot_hash"]).strip(),
            "status": status,
            "proposal_expires_at_ms": expires_at_ms,
            "evidence": [
                dict(item)
                for item in (payload.get("evidence") or [])
                if isinstance(item, dict)
            ],
            "alternative_inference_ids": _canonical_text_values(
                payload.get("alternative_inference_ids")
            ),
            "strategy_group_id": str(payload["strategy_group_id"]).strip(),
        }
    )
    return payload


def _canonical_text_values(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError("combo pair inference ID collection must be a sequence")
    return sorted({str(item).strip() for item in value if str(item).strip()})


def _assert_same_combo_pair_inference_identity(
    existing: dict[str, Any],
    candidate: dict[str, Any],
) -> None:
    immutable_fields = (
        "inference_id",
        "schema_version",
        "account",
        "symbol",
        "market",
        "market_date",
        "put_record_id",
        "put_open_event_id",
        "call_record_id",
        "call_open_event_id",
    )
    conflicts = [
        field
        for field in immutable_fields
        if str(existing.get(field) or "").strip()
        != str(candidate.get(field) or "").strip()
    ]
    if conflicts:
        raise ValueError(
            "combo pair inference identity conflict: " + ",".join(conflicts)
        )


def _combo_pair_inference_sql_values(
    payload: dict[str, Any],
    *,
    raw_json: str,
) -> tuple[Any, ...]:
    return (
        str(payload["inference_id"]),
        str(payload["schema_version"]),
        str(payload["algorithm_version"]),
        str(payload["account"]),
        str(payload["symbol"]),
        str(payload["market"]),
        str(payload["market_date"]),
        str(payload["put_record_id"]),
        str(payload["put_open_event_id"]),
        str(payload["call_record_id"]),
        str(payload["call_open_event_id"]),
        str(payload["evidence_grade"]),
        _json_text(payload["candidate_occurrence_ids"]),
        _json_text(payload["candidate_exposure_ids"]),
        str(payload["input_snapshot_hash"]),
        str(payload["status"]),
        int(payload["proposal_expires_at_ms"]),
        _json_text(payload["evidence"]),
        _json_text(payload["alternative_inference_ids"]),
        str(payload["strategy_group_id"]),
        payload.get("identity_hash"),
        payload.get("put_adoption_event_id"),
        payload.get("call_adoption_event_id"),
        payload.get("put_void_event_id"),
        payload.get("call_void_event_id"),
        payload.get("decision_at_ms"),
        payload.get("decision_by"),
        payload.get("decision_reason"),
        int(payload["created_at_ms"]),
        int(payload["updated_at_ms"]),
        raw_json,
    )


def _notification_outbox_row(row: sqlite3.Row) -> dict[str, Any]:
    provider_receipt = (
        _json_object(row["provider_receipt_json"])
        if row["provider_receipt_json"]
        else None
    )
    return {
        "outbox_id": str(row["outbox_id"]),
        "case_id": str(row["case_id"]),
        "transition_type": str(row["transition_type"]),
        "resolution_revision": int(row["resolution_revision"]),
        "delivery_revision": int(row["delivery_revision"] or 0),
        "transition_key": str(row["transition_key"]),
        "state_fingerprint": str(row["state_fingerprint"]),
        "status": str(row["status"]),
        "delivery_batch_id": row["delivery_batch_id"],
        "payload": _json_object(row["payload_json"]),
        "payload_hash": str(row["payload_hash"]),
        "provider_message_id": row["provider_message_id"],
        "claim_id": row["claim_id"],
        "claimed_at_ms": row["claimed_at_ms"],
        "send_started_at_ms": row["send_started_at_ms"],
        "attempt_count": int(row["attempt_count"] or 0),
        "next_attempt_at_ms": row["next_attempt_at_ms"],
        "last_error": row["last_error"],
        "provider_receipt": provider_receipt,
        "created_at_ms": int(row["created_at_ms"]),
        "updated_at_ms": int(row["updated_at_ms"]),
        "confirmed_at_ms": row["confirmed_at_ms"],
    }


def _notification_delivery_batch_row(
    row: sqlite3.Row,
) -> dict[str, Any]:
    provider_receipt = (
        _json_object(row["provider_receipt_json"])
        if row["provider_receipt_json"]
        else None
    )
    return {
        "batch_id": str(row["batch_id"]),
        "route_fingerprint": str(row["route_fingerprint"]),
        "provider": str(row["provider"]),
        "channel": str(row["channel"]),
        "target_fingerprint": str(row["target_fingerprint"]),
        "renderer_version": str(row["renderer_version"]),
        "status": str(row["status"]),
        "payload": _json_object(row["payload_json"]),
        "payload_hash": str(row["payload_hash"]),
        "member_count": int(row["member_count"]),
        "first_intent_created_at_ms": int(
            row["first_intent_created_at_ms"]
        ),
        "last_intent_created_at_ms": int(
            row["last_intent_created_at_ms"]
        ),
        "provider_message_id": row["provider_message_id"],
        "claim_id": row["claim_id"],
        "claimed_at_ms": row["claimed_at_ms"],
        "send_started_at_ms": row["send_started_at_ms"],
        "attempt_count": int(row["attempt_count"] or 0),
        "next_attempt_at_ms": row["next_attempt_at_ms"],
        "last_error": row["last_error"],
        "provider_receipt": provider_receipt,
        "created_at_ms": int(row["created_at_ms"]),
        "updated_at_ms": int(row["updated_at_ms"]),
        "confirmed_at_ms": row["confirmed_at_ms"],
    }


def _lifecycle_case_immutable_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": payload.get("schema_version"),
        "case_id": str(payload.get("case_id") or "").strip(),
        "case_key": str(payload.get("case_key") or "").strip(),
        "account": str(payload.get("account") or "").strip().lower(),
        "broker": str(payload.get("broker") or "").strip().lower(),
        "futu_account_id": str(
            payload.get("futu_account_id") or ""
        ).strip(),
        "contract_key": payload.get("contract_key"),
        "position_side": str(payload.get("position_side") or "").strip().lower(),
        "expiration_ymd": str(payload.get("expiration_ymd") or "").strip(),
        "target_contracts_by_lot": dict(payload.get("target_contracts_by_lot") or {}),
        "observation_start_ms": payload.get("observation_start_ms"),
        "pending_until_ms": payload.get("pending_until_ms"),
    }


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
