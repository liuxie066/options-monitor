from __future__ import annotations

from .repository_common import (
    _add_column_if_missing,
    sqlite3,
)

from .repository_projection_schema import (
    _create_current_decision_case_scope_guards,
    _create_current_decision_generation_triggers,
)

from .repository_trade_schema import (
    _create_index_if_table_empty,
)

def _ensure_current_decision_projection_schema(conn: sqlite3.Connection) -> None:
    _add_column_if_missing(
        conn,
        "assigned_stock_events",
        "account",
        ("TEXT CHECK(account IS NULL OR (typeof(account) = 'text' AND account != '' AND account = lower(account)))"),
    )
    _add_column_if_missing(
        conn,
        "trade_lifecycle_cases",
        "decision_fact_json",
        (
            "TEXT CHECK(decision_fact_json IS NULL OR "
            "(typeof(decision_fact_json) = 'text' AND json_valid(decision_fact_json)))"
        ),
    )
    _add_column_if_missing(
        conn,
        "trade_lifecycle_cases",
        "decision_fact_sha256",
        (
            "TEXT CHECK(decision_fact_sha256 IS NULL OR "
            "(typeof(decision_fact_sha256) = 'text' "
            "AND length(decision_fact_sha256) = 64 "
            "AND decision_fact_sha256 NOT GLOB '*[^0-9a-f]*'))"
        ),
    )
    _create_index_if_table_empty(
        conn,
        index_name="idx_assigned_stock_events_account_time",
        table="assigned_stock_events",
        create_sql=(
            "CREATE INDEX idx_assigned_stock_events_account_time "
            "ON assigned_stock_events(account, trade_time_ms, stock_event_id)"
        ),
    )
    _create_index_if_table_empty(
        conn,
        index_name="idx_trade_lifecycle_cases_account_status",
        table="trade_lifecycle_cases",
        create_sql=(
            "CREATE INDEX idx_trade_lifecycle_cases_account_status "
            "ON trade_lifecycle_cases(account, status, updated_at_ms DESC, case_id DESC) "
            "WHERE status IN ("
            "'pending', 'waiting_settlement_evidence', 'needs_review', "
            "'partially_resolved', 'conflict'"
            ")"
        ),
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS current_decision_input_generations (
          account TEXT PRIMARY KEY,
          generation INTEGER NOT NULL
            CHECK(typeof(generation) = 'integer' AND generation >= 0),
          case_generation INTEGER NOT NULL
            CHECK(typeof(case_generation) = 'integer' AND case_generation >= 0),
          evidence_generation INTEGER NOT NULL
            CHECK(typeof(evidence_generation) = 'integer' AND evidence_generation >= 0),
          allocation_generation INTEGER NOT NULL
            CHECK(typeof(allocation_generation) = 'integer' AND allocation_generation >= 0),
          source_consumption_generation INTEGER NOT NULL
            CHECK(
              typeof(source_consumption_generation) = 'integer'
              AND source_consumption_generation >= 0
            ),
          timing_generation INTEGER NOT NULL
            CHECK(typeof(timing_generation) = 'integer' AND timing_generation >= 0),
          combo_identity_generation INTEGER NOT NULL
            CHECK(
              typeof(combo_identity_generation) = 'integer'
              AND combo_identity_generation >= 0
            ),
          assigned_stock_generation INTEGER NOT NULL
            CHECK(
              typeof(assigned_stock_generation) = 'integer'
              AND assigned_stock_generation >= 0
            ),
          updated_at_ms INTEGER NOT NULL
            CHECK(typeof(updated_at_ms) = 'integer' AND updated_at_ms > 0),
          CHECK(typeof(account) = 'text' AND account != '' AND account = lower(account))
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS current_decision_projections (
          account TEXT PRIMARY KEY,
          projection_schema TEXT NOT NULL,
          projector_implementation_fingerprint TEXT NOT NULL,
          built_position_source_generation INTEGER NOT NULL,
          built_position_lots_generation INTEGER NOT NULL,
          position_lots_fingerprint TEXT NOT NULL,
          built_decision_input_generation INTEGER NOT NULL,
          built_case_generation INTEGER NOT NULL,
          built_evidence_generation INTEGER NOT NULL,
          built_allocation_generation INTEGER NOT NULL,
          built_source_consumption_generation INTEGER NOT NULL,
          built_timing_generation INTEGER NOT NULL,
          built_combo_identity_generation INTEGER NOT NULL,
          built_assigned_stock_generation INTEGER NOT NULL,
          decision_state_fingerprint TEXT NOT NULL,
          payload_sha256 TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          updated_at_ms INTEGER NOT NULL,
          CHECK(typeof(account) = 'text' AND account != '' AND account = lower(account)),
          CHECK(typeof(projection_schema) = 'text' AND projection_schema != ''),
          CHECK(
            typeof(projector_implementation_fingerprint) = 'text'
            AND length(projector_implementation_fingerprint) = 64
            AND projector_implementation_fingerprint NOT GLOB '*[^0-9a-f]*'
          ),
          CHECK(
            typeof(built_position_source_generation) = 'integer'
            AND built_position_source_generation >= 0
          ),
          CHECK(
            typeof(built_position_lots_generation) = 'integer'
            AND built_position_lots_generation >= 0
          ),
          CHECK(
            typeof(position_lots_fingerprint) = 'text'
            AND length(position_lots_fingerprint) = 64
            AND position_lots_fingerprint NOT GLOB '*[^0-9a-f]*'
          ),
          CHECK(
            typeof(built_decision_input_generation) = 'integer'
            AND built_decision_input_generation >= 0
          ),
          CHECK(
            typeof(built_case_generation) = 'integer'
            AND built_case_generation >= 0
          ),
          CHECK(
            typeof(built_evidence_generation) = 'integer'
            AND built_evidence_generation >= 0
          ),
          CHECK(
            typeof(built_allocation_generation) = 'integer'
            AND built_allocation_generation >= 0
          ),
          CHECK(
            typeof(built_source_consumption_generation) = 'integer'
            AND built_source_consumption_generation >= 0
          ),
          CHECK(
            typeof(built_timing_generation) = 'integer'
            AND built_timing_generation >= 0
          ),
          CHECK(
            typeof(built_combo_identity_generation) = 'integer'
            AND built_combo_identity_generation >= 0
          ),
          CHECK(
            typeof(built_assigned_stock_generation) = 'integer'
            AND built_assigned_stock_generation >= 0
          ),
          CHECK(
            typeof(decision_state_fingerprint) = 'text'
            AND length(decision_state_fingerprint) = 64
            AND decision_state_fingerprint NOT GLOB '*[^0-9a-f]*'
          ),
          CHECK(
            typeof(payload_sha256) = 'text'
            AND length(payload_sha256) = 64
            AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
          ),
          CHECK(typeof(payload_json) = 'text' AND json_valid(payload_json)),
          CHECK(typeof(updated_at_ms) = 'integer' AND updated_at_ms > 0)
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_lifecycle_case_targets (
          case_id TEXT NOT NULL,
          account TEXT NOT NULL,
          target_lot_id TEXT NOT NULL,
          target_contracts INTEGER,
          PRIMARY KEY(case_id, target_lot_id),
          CHECK(typeof(case_id) = 'text' AND case_id != ''),
          CHECK(typeof(account) = 'text' AND account != '' AND account = lower(account)),
          CHECK(typeof(target_lot_id) = 'text' AND target_lot_id != ''),
          CHECK(
            target_contracts IS NULL OR (
              typeof(target_contracts) = 'integer' AND target_contracts > 0
            )
          ),
          FOREIGN KEY(case_id) REFERENCES trade_lifecycle_cases(case_id)
            ON DELETE CASCADE
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trade_lifecycle_case_targets_account_lot
        ON trade_lifecycle_case_targets(account, target_lot_id, case_id)
        """
    )

    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS
        trg_current_decision_lifecycle_case_account_insert_guard
        BEFORE INSERT ON trade_lifecycle_cases
        BEGIN
          SELECT CASE
            WHEN json_valid(NEW.raw_json) = 0
              THEN RAISE(ABORT, 'lifecycle case JSON is invalid')
            WHEN NEW.account = '' OR NEW.account != lower(NEW.account)
              THEN RAISE(ABORT, 'lifecycle case account must be lowercase')
            WHEN trim(CAST(json_extract(NEW.raw_json, '$.account') AS TEXT)) IS NOT NEW.account
              THEN RAISE(ABORT, 'lifecycle case account conflicts with JSON')
          END;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS
        trg_current_decision_lifecycle_case_account_update_guard
        BEFORE UPDATE OF account, raw_json ON trade_lifecycle_cases
        BEGIN
          SELECT CASE
            WHEN OLD.account = '' OR OLD.account != lower(OLD.account)
              THEN RAISE(ABORT, 'lifecycle case account must be lowercase')
            WHEN json_valid(NEW.raw_json) = 0
              THEN RAISE(ABORT, 'lifecycle case JSON is invalid')
            WHEN NEW.account = '' OR NEW.account != lower(NEW.account)
              THEN RAISE(ABORT, 'lifecycle case account must be lowercase')
            WHEN trim(CAST(json_extract(NEW.raw_json, '$.account') AS TEXT)) IS NOT NEW.account
              THEN RAISE(ABORT, 'lifecycle case account conflicts with JSON')
          END;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS
        trg_current_decision_lifecycle_case_account_delete_guard
        BEFORE DELETE ON trade_lifecycle_cases
        BEGIN
          SELECT CASE
            WHEN OLD.account = '' OR OLD.account != lower(OLD.account)
              THEN RAISE(ABORT, 'lifecycle case account must be lowercase')
          END;
        END
        """
    )
    for operation in ("INSERT", "UPDATE OF decision_fact_json, decision_fact_sha256"):
        suffix = "insert" if operation == "INSERT" else "update"
        conn.execute(
            f"""
        CREATE TRIGGER IF NOT EXISTS
        trg_current_decision_lifecycle_case_fact_{suffix}_guard
        BEFORE {operation} ON trade_lifecycle_cases
        BEGIN
          SELECT CASE
            WHEN (NEW.decision_fact_json IS NULL) !=
                 (NEW.decision_fact_sha256 IS NULL)
              THEN RAISE(ABORT, 'lifecycle case decision fact is incomplete')
            WHEN NEW.decision_fact_json IS NOT NULL
              AND (
                json_valid(NEW.decision_fact_json) = 0
                OR json_extract(NEW.decision_fact_json, '$.schema_version')
                   != 'lifecycle_case_decision_fact.v1'
              )
              THEN RAISE(ABORT, 'lifecycle case decision fact is invalid')
          END;
        END
        """
        )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS
        trg_current_decision_case_target_guard
        BEFORE INSERT ON trade_lifecycle_case_targets
        BEGIN
          SELECT CASE WHEN NOT EXISTS (
            SELECT 1
            FROM trade_lifecycle_cases
            WHERE case_id = NEW.case_id AND account = NEW.account
          ) THEN RAISE(ABORT, 'lifecycle case target account mismatch') END;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS
        trg_current_decision_case_target_update_guard
        BEFORE UPDATE ON trade_lifecycle_case_targets
        BEGIN
          SELECT CASE WHEN NOT EXISTS (
            SELECT 1
            FROM trade_lifecycle_cases
            WHERE case_id = OLD.case_id AND account = OLD.account
          ) OR NOT EXISTS (
            SELECT 1
            FROM trade_lifecycle_cases
            WHERE case_id = NEW.case_id AND account = NEW.account
          ) THEN RAISE(ABORT, 'lifecycle case target account mismatch') END;
        END
        """
    )
    assigned_account = "trim(CAST(json_extract(NEW.event_json, '$.account') AS TEXT))"
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS
        trg_current_decision_assigned_stock_account_insert_guard
        BEFORE INSERT ON assigned_stock_events
        BEGIN
          SELECT CASE
            WHEN json_valid(NEW.event_json) = 0
              THEN RAISE(ABORT, 'assigned stock event JSON is invalid')
            WHEN NEW.account IS NULL OR NEW.account = '' OR NEW.account != lower(NEW.account)
              THEN RAISE(ABORT, 'assigned stock account is required')
            WHEN NEW.account IS NOT {assigned_account}
              THEN RAISE(ABORT, 'assigned stock account conflicts with JSON')
          END;
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS
        trg_current_decision_assigned_stock_account_update_guard
        BEFORE UPDATE OF account, event_json ON assigned_stock_events
        BEGIN
          SELECT CASE
            WHEN OLD.account IS NULL OR OLD.account = '' OR OLD.account != lower(OLD.account)
              THEN RAISE(ABORT, 'assigned stock account is required')
            WHEN json_valid(NEW.event_json) = 0
              THEN RAISE(ABORT, 'assigned stock event JSON is invalid')
            WHEN NEW.account IS NULL OR NEW.account = '' OR NEW.account != lower(NEW.account)
              THEN RAISE(ABORT, 'assigned stock account is required')
            WHEN NEW.account IS NOT {assigned_account}
              THEN RAISE(ABORT, 'assigned stock account conflicts with JSON')
          END;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS
        trg_current_decision_assigned_stock_account_delete_guard
        BEFORE DELETE ON assigned_stock_events
        BEGIN
          SELECT CASE
            WHEN OLD.account IS NULL OR OLD.account = '' OR OLD.account != lower(OLD.account)
              THEN RAISE(ABORT, 'assigned stock account is required')
          END;
        END
        """
    )

    identity_account = "trim(CAST(json_extract(NEW.raw_json, '$.account') AS TEXT))"
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS
        trg_current_decision_combo_identity_account_insert_guard
        BEFORE INSERT ON strategy_group_identities
        BEGIN
          SELECT CASE
            WHEN json_valid(NEW.raw_json) = 0
              THEN RAISE(ABORT, 'strategy identity JSON is invalid')
            WHEN NEW.account = '' OR NEW.account != lower(NEW.account)
              THEN RAISE(ABORT, 'strategy identity account must be lowercase')
            WHEN NEW.account IS NOT {identity_account}
              THEN RAISE(ABORT, 'strategy identity account conflicts with JSON')
          END;
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS
        trg_current_decision_combo_identity_account_update_guard
        BEFORE UPDATE OF account, raw_json ON strategy_group_identities
        BEGIN
          SELECT CASE
            WHEN OLD.account = '' OR OLD.account != lower(OLD.account)
              THEN RAISE(ABORT, 'strategy identity account must be lowercase')
            WHEN json_valid(NEW.raw_json) = 0
              THEN RAISE(ABORT, 'strategy identity JSON is invalid')
            WHEN NEW.account = '' OR NEW.account != lower(NEW.account)
              THEN RAISE(ABORT, 'strategy identity account must be lowercase')
            WHEN NEW.account IS NOT {identity_account}
              THEN RAISE(ABORT, 'strategy identity account conflicts with JSON')
          END;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS
        trg_current_decision_combo_identity_account_delete_guard
        BEFORE DELETE ON strategy_group_identities
        BEGIN
          SELECT CASE
            WHEN OLD.account = '' OR OLD.account != lower(OLD.account)
              THEN RAISE(ABORT, 'strategy identity account must be lowercase')
          END;
        END
        """
    )

    for label, table, nullable in (
        ("lifecycle_evidence", "trade_lifecycle_evidence", True),
        ("lifecycle_allocation", "trade_lifecycle_allocations", False),
        ("lifecycle_source_consumption", "trade_lifecycle_source_consumptions", False),
        ("lifecycle_timing", "trade_lifecycle_timing_policies", False),
    ):
        _create_current_decision_case_scope_guards(
            conn,
            label=label,
            table=table,
            nullable=nullable,
        )

    case_update_when = " OR ".join(
        f"OLD.{column} IS NOT NEW.{column}"
        for column in (
            "case_id",
            "case_key",
            "account",
            "broker",
            "symbol",
            "option_type",
            "position_side",
            "strike",
            "expiration_ymd",
            "contract_key",
            "status",
            "decision_type",
            "target_lot_ids_json",
            "target_contracts_by_lot_json",
            "observation_start_ms",
            "pending_until_ms",
            "decision_fact_json",
            "decision_fact_sha256",
            "raw_json",
        )
    )
    _create_current_decision_generation_triggers(
        conn,
        label="lifecycle_case",
        table="trade_lifecycle_cases",
        counter="case_generation",
        new_account_sql="NEW.account",
        old_account_sql="OLD.account",
        insert_time_sql="NEW.updated_at_ms",
        update_time_sql="NEW.updated_at_ms",
        delete_time_sql="OLD.updated_at_ms",
        update_when=case_update_when,
    )
    evidence_account_new = "(SELECT account FROM trade_lifecycle_cases WHERE case_id = NEW.case_id)"
    evidence_account_old = "(SELECT account FROM trade_lifecycle_cases WHERE case_id = OLD.case_id)"
    _create_current_decision_generation_triggers(
        conn,
        label="lifecycle_evidence",
        table="trade_lifecycle_evidence",
        counter="evidence_generation",
        new_account_sql=evidence_account_new,
        old_account_sql=evidence_account_old,
        insert_time_sql="NEW.created_at_ms",
        update_time_sql="NEW.created_at_ms",
        delete_time_sql="OLD.created_at_ms",
        insert_when="NEW.case_id IS NOT NULL AND NEW.case_id != ''",
        update_when="OLD.case_id IS NOT NEW.case_id",
        delete_when="OLD.case_id IS NOT NULL AND OLD.case_id != ''",
    )
    for label, table, counter, timestamp_column in (
        (
            "lifecycle_allocation",
            "trade_lifecycle_allocations",
            "allocation_generation",
            "created_at_ms",
        ),
        (
            "lifecycle_source_consumption",
            "trade_lifecycle_source_consumptions",
            "source_consumption_generation",
            "created_at_ms",
        ),
        (
            "lifecycle_timing",
            "trade_lifecycle_timing_policies",
            "timing_generation",
            "created_at_ms",
        ),
    ):
        _create_current_decision_generation_triggers(
            conn,
            label=label,
            table=table,
            counter=counter,
            new_account_sql=("(SELECT account FROM trade_lifecycle_cases WHERE case_id = NEW.case_id)"),
            old_account_sql=("(SELECT account FROM trade_lifecycle_cases WHERE case_id = OLD.case_id)"),
            insert_time_sql=f"NEW.{timestamp_column}",
            update_time_sql=f"NEW.{timestamp_column}",
            delete_time_sql=f"OLD.{timestamp_column}",
        )
    _create_current_decision_generation_triggers(
        conn,
        label="combo_identity",
        table="strategy_group_identities",
        counter="combo_identity_generation",
        new_account_sql="NEW.account",
        old_account_sql="OLD.account",
        insert_time_sql="NEW.created_at_ms",
        update_time_sql="NEW.created_at_ms",
        delete_time_sql="OLD.created_at_ms",
    )
    _create_current_decision_generation_triggers(
        conn,
        label="assigned_stock",
        table="assigned_stock_events",
        counter="assigned_stock_generation",
        new_account_sql="NEW.account",
        old_account_sql="OLD.account",
        insert_time_sql="NEW.updated_at_ms",
        update_time_sql="NEW.updated_at_ms",
        delete_time_sql="OLD.updated_at_ms",
        update_when=(
            "OLD.stock_event_id IS NOT NEW.stock_event_id "
            "OR OLD.account IS NOT NEW.account "
            "OR OLD.event_json IS NOT NEW.event_json "
            "OR OLD.trade_time_ms IS NOT NEW.trade_time_ms"
        ),
    )
