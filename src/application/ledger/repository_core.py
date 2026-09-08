from __future__ import annotations

from .repository_trade_schema import EXECUTION_IDENTITY_INDEXES, _execution_identity_index_sql
from .repository_schema import (
    Any,
    Path,
    _add_column_if_missing,
    _create_index_if_table_empty,
    _ensure_current_decision_projection_schema,
    _ensure_lifecycle_attempt_audit_schema,
    _ensure_lifecycle_delivery_status_revision_v1,
    _ensure_lifecycle_evidence_count_triggers,
    _ensure_notification_delivery_batches_v1,
    _ensure_notification_outbox_v2,
    _ensure_position_projection_schema,
    _json_object,
    connect_private_sqlite,
    contextmanager,
    exclusive_private_file_lock,
    initialize_ledger_connection,
    normalize_wheel_event,
    private_path,
    secure_sqlite_artifacts,
    sqlite3,
)


_WHEEL_EVENT_TYPES_V2 = (
    "wheel_started",
    "wheel_manual_ended",
    "wheel_called_away",
    "wheel_call_intent_created",
    "wheel_call_intent_cancelled",
    "wheel_call_intent_consumed",
    "wheel_call_linkage_rejected",
    "wheel_event_voided",
    "wheel_branch_created",
    "wheel_branch_decided",
    "wheel_put_intent_created",
    "wheel_put_intent_cancelled",
    "wheel_put_intent_consumed",
    "wheel_put_linkage_rejected",
)


def _create_wheel_events_v2_table(conn: sqlite3.Connection, table: str) -> None:
    event_types = ",\n                    ".join(
        f"'{event_type}'" for event_type in _WHEEL_EVENT_TYPES_V2
    )
    conn.execute(
        f"""
        CREATE TABLE {table} (
          event_id TEXT PRIMARY KEY,
          event_schema_version TEXT NOT NULL CHECK(
            event_schema_version IN ('wheel_event.v1', 'wheel_event.v2')
          ),
          account TEXT NOT NULL CHECK(
            typeof(account) = 'text'
            AND account != ''
            AND account = lower(account)
          ),
          wheel_branch_id TEXT NOT NULL CHECK(wheel_branch_id != ''),
          stock_lot_id TEXT CHECK(stock_lot_id IS NULL OR stock_lot_id != ''),
          event_type TEXT NOT NULL CHECK(event_type IN (
            {event_types}
          )),
          occurred_at_ms INTEGER NOT NULL CHECK(occurred_at_ms > 0),
          recorded_at_ms INTEGER NOT NULL CHECK(recorded_at_ms > 0),
          intent_id TEXT,
          source_trade_event_id TEXT,
          payload_json TEXT NOT NULL CHECK(
            json_valid(payload_json)
            AND json_type(payload_json) = 'object'
          ),
          payload_hash TEXT NOT NULL CHECK(
            length(payload_hash) = 64
            AND payload_hash NOT GLOB '*[^0-9a-f]*'
          ),
          FOREIGN KEY(source_trade_event_id) REFERENCES trade_events(event_id)
        )
        """
    )


def _create_wheel_events_v2_guards(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_wheel_events_account_branch
        ON wheel_events(account, wheel_branch_id, occurred_at_ms, event_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_wheel_events_account_lot
        ON wheel_events(account, stock_lot_id, occurred_at_ms, event_id)
        WHERE stock_lot_id IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_wheel_events_append_only_update
        BEFORE UPDATE ON wheel_events
        BEGIN
          SELECT RAISE(ABORT, 'wheel_events is append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_wheel_events_append_only_delete
        BEFORE DELETE ON wheel_events
        BEGIN
          SELECT RAISE(ABORT, 'wheel_events is append-only');
        END
        """
    )


def _wheel_events_schema_is_v2(conn: sqlite3.Connection) -> bool:
    columns = {
        str(row["name"]): row
        for row in conn.execute("PRAGMA table_info(wheel_events)").fetchall()
    }
    if "event_schema_version" not in columns or "wheel_branch_id" not in columns:
        return False
    if int(columns["wheel_branch_id"]["notnull"] or 0) != 1:
        return False
    if int(columns["stock_lot_id"]["notnull"] or 0) != 0:
        return False
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'wheel_events'"
    ).fetchone()
    sql = str(row["sql"] or "") if row is not None else ""
    required_tokens = ("wheel_event.v1", "wheel_event.v2", *_WHEEL_EVENT_TYPES_V2)
    return all(token in sql for token in required_tokens)


def _normalized_wheel_row(
    row: sqlite3.Row,
    *,
    columns: set[str],
) -> dict[str, Any]:
    stock_lot_id = row["stock_lot_id"]
    event_schema_version = (
        str(row["event_schema_version"] or "").strip()
        if "event_schema_version" in columns
        else "wheel_event.v1"
    )
    wheel_branch_id = (
        str(row["wheel_branch_id"] or "").strip()
        if "wheel_branch_id" in columns
        else str(stock_lot_id or "").strip()
    )
    stored = {
        "event_id": row["event_id"],
        "event_schema_version": event_schema_version,
        "account": row["account"],
        "wheel_branch_id": wheel_branch_id,
        "stock_lot_id": stock_lot_id,
        "event_type": row["event_type"],
        "occurred_at_ms": row["occurred_at_ms"],
        "recorded_at_ms": row["recorded_at_ms"],
        "intent_id": row["intent_id"],
        "source_trade_event_id": row["source_trade_event_id"],
        "payload": _json_object(row["payload_json"]),
        "payload_hash": row["payload_hash"],
    }
    normalized = normalize_wheel_event(stored)
    preserved_fields = (
        "event_id",
        "event_schema_version",
        "account",
        "wheel_branch_id",
        "stock_lot_id",
        "event_type",
        "occurred_at_ms",
        "recorded_at_ms",
        "intent_id",
        "source_trade_event_id",
        "payload",
        "payload_hash",
    )
    if any(normalized.get(field) != stored[field] for field in preserved_fields):
        raise RuntimeError(
            f"wheel event migration validation failed for event_id={stored['event_id']}"
        )
    return normalized


def _ensure_wheel_events_v2(conn: sqlite3.Connection) -> None:
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'wheel_events'"
    ).fetchone()
    if table is None:
        _create_wheel_events_v2_table(conn, "wheel_events")
        _create_wheel_events_v2_guards(conn)
        return
    if _wheel_events_schema_is_v2(conn):
        _create_wheel_events_v2_guards(conn)
        return

    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(wheel_events)").fetchall()
    }
    rows = conn.execute("SELECT * FROM wheel_events ORDER BY event_id ASC").fetchall()
    normalized_rows = [_normalized_wheel_row(row, columns=columns) for row in rows]
    replacement = "wheel_events_v2_migration"
    conn.execute(f"DROP TABLE IF EXISTS {replacement}")
    _create_wheel_events_v2_table(conn, replacement)
    for source_row, normalized in zip(rows, normalized_rows, strict=True):
        conn.execute(
            f"""
            INSERT INTO {replacement} (
              event_id, event_schema_version, account, wheel_branch_id,
              stock_lot_id, event_type, occurred_at_ms, recorded_at_ms,
              intent_id, source_trade_event_id, payload_json, payload_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized["event_id"],
                normalized["event_schema_version"],
                normalized["account"],
                normalized["wheel_branch_id"],
                normalized["stock_lot_id"],
                normalized["event_type"],
                normalized["occurred_at_ms"],
                normalized["recorded_at_ms"],
                normalized["intent_id"],
                normalized["source_trade_event_id"],
                source_row["payload_json"],
                normalized["payload_hash"],
            ),
        )
    migrated_rows = conn.execute(
        f"SELECT * FROM {replacement} ORDER BY event_id ASC"
    ).fetchall()
    if len(migrated_rows) != len(rows):
        raise RuntimeError("wheel event migration row count mismatch")
    for source_row, migrated_row in zip(rows, migrated_rows, strict=True):
        source_event = _normalized_wheel_row(source_row, columns=columns)
        migrated_event = _normalized_wheel_row(
            migrated_row,
            columns=set(migrated_row.keys()),
        )
        if source_event != migrated_event:
            raise RuntimeError(
                f"wheel event migration readback mismatch for event_id={source_event['event_id']}"
            )
    if conn.execute(f"PRAGMA foreign_key_check({replacement})").fetchall():
        raise RuntimeError("wheel event migration foreign key check failed")
    conn.execute("DROP TABLE wheel_events")
    conn.execute(f"ALTER TABLE {replacement} RENAME TO wheel_events")
    _create_wheel_events_v2_guards(conn)


def _ensure_wheel_activation_windows(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wheel_activation_windows (
          market TEXT NOT NULL CHECK(market IN ('us', 'hk')),
          account TEXT NOT NULL CHECK(
            typeof(account) = 'text'
            AND account != ''
            AND account = lower(account)
          ),
          generation INTEGER NOT NULL CHECK(generation > 0),
          activated_at_ms INTEGER NOT NULL CHECK(activated_at_ms > 0),
          deactivated_at_ms INTEGER CHECK(
            deactivated_at_ms IS NULL OR deactivated_at_ms > activated_at_ms
          ),
          policy_hash TEXT NOT NULL CHECK(
            length(policy_hash) = 64
            AND policy_hash NOT GLOB '*[^0-9a-f]*'
          ),
          activation_request_id TEXT NOT NULL CHECK(activation_request_id != ''),
          activation_request_hash TEXT NOT NULL CHECK(
            length(activation_request_hash) = 64
            AND activation_request_hash NOT GLOB '*[^0-9a-f]*'
          ),
          deactivation_request_id TEXT,
          deactivation_request_hash TEXT CHECK(
            deactivation_request_hash IS NULL OR (
              length(deactivation_request_hash) = 64
              AND deactivation_request_hash NOT GLOB '*[^0-9a-f]*'
            )
          ),
          PRIMARY KEY(market, account, generation),
          CHECK(
            (deactivated_at_ms IS NULL AND deactivation_request_id IS NULL AND deactivation_request_hash IS NULL)
            OR
            (deactivated_at_ms IS NOT NULL AND deactivation_request_id != '' AND deactivation_request_hash IS NOT NULL)
          )
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_wheel_activation_windows_open
        ON wheel_activation_windows(market, account)
        WHERE deactivated_at_ms IS NULL
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_wheel_activation_request
        ON wheel_activation_windows(market, account, activation_request_id)
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_wheel_deactivation_request
        ON wheel_activation_windows(market, account, deactivation_request_id)
        WHERE deactivation_request_id IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_wheel_activation_windows_insert_guard
        BEFORE INSERT ON wheel_activation_windows
        BEGIN
          SELECT CASE WHEN NEW.generation != COALESCE((
            SELECT MAX(generation) + 1
            FROM wheel_activation_windows
            WHERE market = NEW.market AND account = NEW.account
          ), 1) THEN RAISE(ABORT, 'wheel activation generation must increase by one') END;
          SELECT CASE WHEN EXISTS (
            SELECT 1
            FROM wheel_activation_windows
            WHERE market = NEW.market
              AND account = NEW.account
              AND (
                deactivated_at_ms IS NULL
                OR NEW.activated_at_ms <= activated_at_ms
                OR NEW.activated_at_ms < deactivated_at_ms
              )
          ) THEN RAISE(ABORT, 'wheel activation windows must not overlap') END;
          SELECT CASE WHEN EXISTS (
            SELECT 1
            FROM wheel_activation_windows
            WHERE market = NEW.market
              AND account = NEW.account
              AND deactivation_request_id = NEW.activation_request_id
          ) THEN RAISE(ABORT, 'wheel activation request identity must be unique') END;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_wheel_activation_windows_update_guard
        BEFORE UPDATE ON wheel_activation_windows
        WHEN NOT (
          OLD.deactivated_at_ms IS NULL
          AND NEW.deactivated_at_ms IS NOT NULL
          AND NEW.deactivated_at_ms > OLD.activated_at_ms
          AND OLD.market IS NEW.market
          AND OLD.account IS NEW.account
          AND OLD.generation IS NEW.generation
          AND OLD.activated_at_ms IS NEW.activated_at_ms
          AND OLD.policy_hash IS NEW.policy_hash
          AND OLD.activation_request_id IS NEW.activation_request_id
          AND OLD.activation_request_hash IS NEW.activation_request_hash
          AND OLD.deactivation_request_id IS NULL
          AND NEW.deactivation_request_id IS NOT NULL
          AND NEW.deactivation_request_id != ''
          AND NEW.deactivation_request_id != OLD.activation_request_id
          AND NOT EXISTS (
            SELECT 1
            FROM wheel_activation_windows AS existing
            WHERE existing.market = OLD.market
              AND existing.account = OLD.account
              AND (
                existing.activation_request_id = NEW.deactivation_request_id
                OR existing.deactivation_request_id = NEW.deactivation_request_id
              )
          )
          AND OLD.deactivation_request_hash IS NULL
          AND NEW.deactivation_request_hash IS NOT NULL
        )
        BEGIN
          SELECT RAISE(ABORT, 'wheel activation window boundaries are immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_wheel_activation_windows_delete_guard
        BEFORE DELETE ON wheel_activation_windows
        BEGIN
          SELECT RAISE(ABORT, 'wheel activation windows are append-only');
        END
        """
    )


@contextmanager
def with_sqlite_repo_writer_lock(repo: Any):
    """Serialize compound intake work without opening a Ledger transaction."""
    candidate = getattr(repo, "primary_repo", repo)
    if isinstance(candidate, RepositoryCoreMixin):
        with candidate._writer_lock():
            yield candidate
        return
    # In-memory protocol repositories have no durable cross-process state.
    yield candidate


class RepositoryCoreMixin:
    def __init__(self, db_path: Path):
        self.db_path = private_path(db_path)
        self.data_config_path: Path | None = None
        self.bootstrap_status = "not_started"
        self.bootstrap_message: str | None = None
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = connect_private_sqlite(self.db_path)
        try:
            initialize_ledger_connection(conn)
            conn.execute("PRAGMA busy_timeout=5000")
            with self._writer_lock():
                row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
                if row is None or str(row[0]).lower() != "wal":
                    raise RuntimeError("SQLite WAL mode is required for the option ledger")
                conn.execute("PRAGMA synchronous=NORMAL")
            secure_sqlite_artifacts(self.db_path)
            return conn
        except BaseException:
            try:
                conn.close()
            except Exception:
                pass
            try:
                secure_sqlite_artifacts(self.db_path)
            except Exception:
                pass
            raise

    @contextmanager
    def _writer_lock(self):
        with exclusive_private_file_lock(Path(f"{self.db_path}.writer.lock")):
            yield

    @contextmanager
    def _writer_connection(self, *, begin_immediate: bool = False):
        with self._writer_lock():
            conn = self._connect()
            try:
                if begin_immediate:
                    conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            finally:
                try:
                    conn.close()
                finally:
                    secure_sqlite_artifacts(self.db_path)

    @contextmanager
    def _optional_conn(self, conn: sqlite3.Connection | None, *, commit: bool = False):
        if conn is not None:
            initialize_ledger_connection(conn)
            yield conn
            return
        if commit:
            with self._writer_connection() as active_conn:
                yield active_conn
            return
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()
            secure_sqlite_artifacts(self.db_path)

    def _table_exists(self, name: str, *, conn: sqlite3.Connection | None = None) -> bool:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
                (str(name),),
            ).fetchone()
        return row is not None

    def _init_db(self) -> None:
        with self._writer_connection(begin_immediate=True) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_events (
                  event_id TEXT PRIMARY KEY,
                  account TEXT,
                  event_json TEXT NOT NULL,
                  trade_time_ms INTEGER NOT NULL,
                  created_at_ms INTEGER NOT NULL,
                  updated_at_ms INTEGER NOT NULL,
                  ingest_seq INTEGER,
                  market TEXT,
                  position_effect TEXT
                )
                """
            )
            _create_index_if_table_empty(
                conn,
                index_name="idx_trade_events_trade_time",
                table="trade_events",
                create_sql=("CREATE INDEX idx_trade_events_trade_time ON trade_events(trade_time_ms, event_id)"),
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS position_lots (
                  record_id TEXT PRIMARY KEY,
                  account TEXT,
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
            _create_index_if_table_empty(
                conn,
                index_name="idx_position_lots_expiration",
                table="position_lots",
                create_sql=("CREATE INDEX idx_position_lots_expiration ON position_lots(expiration, record_id)"),
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assigned_stock_events (
                  stock_event_id TEXT PRIMARY KEY,
                  account TEXT CHECK(
                    account IS NULL OR (
                      typeof(account) = 'text'
                      AND account != ''
                      AND account = lower(account)
                    )
                  ),
                  event_json TEXT NOT NULL,
                  trade_time_ms INTEGER NOT NULL,
                  created_at_ms INTEGER NOT NULL,
                  updated_at_ms INTEGER NOT NULL
                )
                """
            )
            for table, (index_name, _path) in EXECUTION_IDENTITY_INDEXES.items():
                _create_index_if_table_empty(
                    conn, index_name=index_name, table=table,
                    create_sql=_execution_identity_index_sql(table),
                )
            _ensure_wheel_events_v2(conn)
            _ensure_wheel_activation_windows(conn)
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
                  decision_fact_json TEXT CHECK(
                    decision_fact_json IS NULL OR (
                      typeof(decision_fact_json) = 'text'
                      AND json_valid(decision_fact_json)
                    )
                  ),
                  decision_fact_sha256 TEXT CHECK(
                    decision_fact_sha256 IS NULL OR (
                      typeof(decision_fact_sha256) = 'text'
                      AND length(decision_fact_sha256) = 64
                      AND decision_fact_sha256 NOT GLOB '*[^0-9a-f]*'
                    )
                  ),
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
                  revision INTEGER NOT NULL
                    CHECK(typeof(revision) = 'integer' AND revision >= 0),
                  evidence_count INTEGER
                    CHECK(
                      evidence_count IS NULL OR (
                        typeof(evidence_count) = 'integer'
                        AND evidence_count >= 0
                      )
                    )
                )
                """
            )
            _ensure_lifecycle_evidence_count_triggers(conn)
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
            _ensure_lifecycle_attempt_audit_schema(conn)
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
            _ensure_position_projection_schema(conn)
            _ensure_current_decision_projection_schema(conn)
            conn.commit()
