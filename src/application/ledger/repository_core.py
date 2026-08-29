from __future__ import annotations

from .repository_schema import (
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
    connect_private_sqlite,
    contextmanager,
    exclusive_private_file_lock,
    initialize_ledger_connection,
    private_path,
    secure_sqlite_artifacts,
    sqlite3,
)

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
        with self._writer_connection() as conn:
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wheel_events (
                  event_id TEXT PRIMARY KEY,
                  account TEXT NOT NULL CHECK(
                    typeof(account) = 'text'
                    AND account != ''
                    AND account = lower(account)
                  ),
                  stock_lot_id TEXT NOT NULL CHECK(stock_lot_id != ''),
                  event_type TEXT NOT NULL CHECK(event_type IN (
                    'wheel_started',
                    'wheel_manual_ended',
                    'wheel_called_away',
                    'wheel_call_intent_created',
                    'wheel_call_intent_cancelled',
                    'wheel_call_intent_consumed',
                    'wheel_call_linkage_rejected',
                    'wheel_event_voided'
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
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_wheel_events_account_lot
                ON wheel_events(account, stock_lot_id, occurred_at_ms, event_id)
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
