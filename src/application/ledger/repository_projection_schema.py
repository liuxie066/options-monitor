from __future__ import annotations

from .repository_common import (
    POSITION_LOTS_COLUMN_CLASSIFICATION,
    POSITION_PROJECTION_SCHEMA,
    TRADE_EVENTS_COLUMN_CLASSIFICATION,
    _CURRENT_DECISION_GENERATION_COUNTERS,
    _add_column_if_missing,
    now_ms,
    sqlite3,
)

from .repository_trade_schema import (
    _create_index_if_table_empty,
    _ensure_trade_event_pagination_schema,
)

def _projection_schema_cookie(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA schema_version").fetchone()
    return int(row[0]) if row is not None else 0

def _position_projection_column_contract(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, tuple[str, ...]]]:
    out: dict[str, dict[str, tuple[str, ...]]] = {}
    for table, expected in (
        ("trade_events", TRADE_EVENTS_COLUMN_CLASSIFICATION),
        ("position_lots", POSITION_LOTS_COLUMN_CLASSIFICATION),
    ):
        actual = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        out[table] = {
            "missing": tuple(sorted(set(expected) - actual)),
            "unclassified": tuple(sorted(actual - set(expected))),
        }
    return out

def _position_projection_column_contract_is_closed(
    conn: sqlite3.Connection,
) -> bool:
    return all(
        not details["missing"] and not details["unclassified"]
        for details in _position_projection_column_contract(conn).values()
    )

def _ensure_lifecycle_evidence_count_triggers(conn: sqlite3.Connection) -> None:
    _add_column_if_missing(
        conn,
        "trade_lifecycle_evidence_revisions",
        "evidence_count",
        ("INTEGER CHECK(evidence_count IS NULL OR (typeof(evidence_count) = 'integer' AND evidence_count >= 0))"),
    )
    trigger_names = (
        "trg_trade_lifecycle_evidence_revision_insert",
        "trg_trade_lifecycle_evidence_revision_update_old",
        "trg_trade_lifecycle_evidence_revision_update_new",
        "trg_trade_lifecycle_evidence_revision_delete",
    )
    for trigger_name in trigger_names:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (trigger_name,),
        ).fetchone()
        if row is not None and "evidence_count" not in str(row["sql"] or ""):
            conn.execute(f"DROP TRIGGER {trigger_name}")

    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS
        trg_trade_lifecycle_evidence_revision_insert
        AFTER INSERT ON trade_lifecycle_evidence
        WHEN NEW.case_id IS NOT NULL AND NEW.case_id != ''
        BEGIN
          INSERT INTO trade_lifecycle_evidence_revisions (
            case_id, revision, evidence_count
          ) VALUES (NEW.case_id, 1, 1)
          ON CONFLICT(case_id) DO UPDATE SET
            revision = revision + 1,
            evidence_count = CASE
              WHEN evidence_count IS NULL THEN NULL
              ELSE evidence_count + 1
            END;
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
            case_id, revision, evidence_count
          ) VALUES (OLD.case_id, 1, NULL)
          ON CONFLICT(case_id) DO UPDATE SET
            revision = revision + 1,
            evidence_count = CASE
              WHEN evidence_count IS NULL THEN NULL
              WHEN evidence_count > 0 THEN evidence_count - 1
              ELSE RAISE(ABORT, 'lifecycle evidence count underflow')
            END;
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
            case_id, revision, evidence_count
          ) VALUES (NEW.case_id, 1, 1)
          ON CONFLICT(case_id) DO UPDATE SET
            revision = revision + 1,
            evidence_count = CASE
              WHEN evidence_count IS NULL THEN NULL
              ELSE evidence_count + 1
            END;
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
            case_id, revision, evidence_count
          ) VALUES (OLD.case_id, 1, NULL)
          ON CONFLICT(case_id) DO UPDATE SET
            revision = revision + 1,
            evidence_count = CASE
              WHEN evidence_count IS NULL THEN NULL
              WHEN evidence_count > 0 THEN evidence_count - 1
              ELSE RAISE(ABORT, 'lifecycle evidence count underflow')
            END;
        END
        """
    )

def _current_decision_generation_statement(
    *,
    account_sql: str,
    counter: str,
    updated_at_sql: str,
    where_sql: str = "1",
) -> str:
    if counter not in _CURRENT_DECISION_GENERATION_COUNTERS:
        raise ValueError(f"unsupported current-decision counter: {counter}")
    account = f"trim(CAST(({account_sql}) AS TEXT))"
    counter_values = ", ".join("1" if name == counter else "0" for name in _CURRENT_DECISION_GENERATION_COUNTERS)
    counter_columns = ", ".join(_CURRENT_DECISION_GENERATION_COUNTERS)
    return f"""
      INSERT INTO current_decision_input_generations (
        account, generation, {counter_columns}, updated_at_ms
      )
      SELECT {account}, 1, {counter_values}, CAST({updated_at_sql} AS INTEGER)
      WHERE ({where_sql})
        AND {account} != ''
        AND {account} = lower({account})
      ON CONFLICT(account) DO UPDATE SET
        generation = generation + 1,
        {counter} = {counter} + 1,
        updated_at_ms = excluded.updated_at_ms;
    """

def _create_current_decision_generation_triggers(
    conn: sqlite3.Connection,
    *,
    label: str,
    table: str,
    counter: str,
    new_account_sql: str,
    old_account_sql: str,
    insert_time_sql: str,
    update_time_sql: str,
    delete_time_sql: str,
    insert_when: str = "1",
    update_when: str = "1",
    delete_when: str = "1",
) -> None:
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_current_decision_{label}_insert
        AFTER INSERT ON {table}
        WHEN {insert_when}
        BEGIN
          {
            _current_decision_generation_statement(
                account_sql=new_account_sql,
                counter=counter,
                updated_at_sql=insert_time_sql,
            )
        }
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_current_decision_{label}_update
        AFTER UPDATE ON {table}
        WHEN {update_when}
        BEGIN
          {
            _current_decision_generation_statement(
                account_sql=old_account_sql,
                counter=counter,
                updated_at_sql=update_time_sql,
            )
        }
          {
            _current_decision_generation_statement(
                account_sql=new_account_sql,
                counter=counter,
                updated_at_sql=update_time_sql,
                where_sql=f"({new_account_sql}) IS NOT ({old_account_sql})",
            )
        }
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_current_decision_{label}_delete
        AFTER DELETE ON {table}
        WHEN {delete_when}
        BEGIN
          {
            _current_decision_generation_statement(
                account_sql=old_account_sql,
                counter=counter,
                updated_at_sql=delete_time_sql,
            )
        }
        END
        """
    )

def _create_current_decision_case_scope_guards(
    conn: sqlite3.Connection,
    *,
    label: str,
    table: str,
    nullable: bool = False,
) -> None:
    def invalid_case(row: str) -> str:
        case_id = f"trim(CAST({row}.case_id AS TEXT))"
        missing = (
            f"NOT EXISTS (SELECT 1 FROM trade_lifecycle_cases "
            f"WHERE case_id = {row}.case_id AND account != '' "
            f"AND account = lower(account))"
        )
        if nullable:
            return f"({case_id} != '' AND {missing})"
        return f"({case_id} = '' OR {missing})"

    message = "current decision case account is missing"
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS
        trg_current_decision_{label}_account_insert_guard
        BEFORE INSERT ON {table}
        BEGIN
          SELECT CASE WHEN {invalid_case("NEW")}
            THEN RAISE(ABORT, '{message}') END;
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS
        trg_current_decision_{label}_account_update_guard
        BEFORE UPDATE ON {table}
        BEGIN
          SELECT CASE WHEN {invalid_case("OLD")}
            THEN RAISE(ABORT, '{message}') END;
          SELECT CASE WHEN {invalid_case("NEW")}
            THEN RAISE(ABORT, '{message}') END;
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS
        trg_current_decision_{label}_account_delete_guard
        BEFORE DELETE ON {table}
        BEGIN
          SELECT CASE WHEN {invalid_case("OLD")}
            THEN RAISE(ABORT, '{message}') END;
        END
        """
    )

def _ensure_position_projection_schema(conn: sqlite3.Connection) -> None:
    _add_column_if_missing(conn, "trade_events", "account", "TEXT")
    _add_column_if_missing(conn, "position_lots", "account", "TEXT")
    _ensure_trade_event_pagination_schema(conn)

    _create_index_if_table_empty(
        conn,
        index_name="idx_trade_events_account_time",
        table="trade_events",
        create_sql=("CREATE INDEX idx_trade_events_account_time ON trade_events(account, trade_time_ms, event_id)"),
    )
    _create_index_if_table_empty(
        conn,
        index_name="idx_position_lots_account_expiration",
        table="position_lots",
        create_sql=(
            "CREATE INDEX idx_position_lots_account_expiration ON position_lots(account, expiration, record_id)"
        ),
    )
    _create_index_if_table_empty(
        conn,
        index_name="idx_position_lots_account_record",
        table="position_lots",
        create_sql=("CREATE INDEX idx_position_lots_account_record ON position_lots(account, record_id)"),
    )

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS position_projection_source_state (
          singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
          source_generation INTEGER NOT NULL DEFAULT 0,
          projector_schema TEXT NOT NULL DEFAULT '{POSITION_PROJECTION_SCHEMA}',
          projector_implementation_fingerprint TEXT,
          sqlite_schema_cookie INTEGER,
          checkpoint_mode TEXT NOT NULL DEFAULT 'disabled'
            CHECK(checkpoint_mode IN ('disabled', 'enabled', 'untrusted')),
          last_full_verified_source_generation INTEGER,
          updated_at_ms INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS position_projection_heads (
          account TEXT PRIMARY KEY
            CHECK(account != '' AND account = lower(account)),
          lots_generation INTEGER NOT NULL DEFAULT 0,
          built_source_generation INTEGER,
          built_lots_generation INTEGER,
          projection_fingerprint TEXT,
          lot_count INTEGER NOT NULL DEFAULT 0 CHECK(lot_count >= 0),
          projector_schema TEXT NOT NULL DEFAULT '{POSITION_PROJECTION_SCHEMA}',
          projector_implementation_fingerprint TEXT,
          status TEXT NOT NULL DEFAULT 'uninitialized'
            CHECK(status IN ('uninitialized', 'trusted', 'untrusted')),
          updated_at_ms INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS position_projection_checkpoints (
          checkpoint_id TEXT PRIMARY KEY,
          projector_schema TEXT NOT NULL,
          projector_implementation_fingerprint TEXT NOT NULL,
          prefix_event_count INTEGER NOT NULL CHECK(prefix_event_count >= 0),
          prefix_end_trade_time_ms INTEGER NOT NULL CHECK(prefix_end_trade_time_ms >= 0),
          prefix_end_event_id TEXT NOT NULL,
          prefix_chain_sha256 TEXT NOT NULL,
          source_generation INTEGER NOT NULL CHECK(source_generation >= 0),
          sqlite_schema_cookie INTEGER NOT NULL CHECK(sqlite_schema_cookie >= 0),
          accumulator_json BLOB NOT NULL,
          accumulator_sha256 TEXT NOT NULL,
          diagnostic_count INTEGER NOT NULL CHECK(diagnostic_count = 0),
          diagnostic_sha256 TEXT NOT NULL,
          state_bytes INTEGER NOT NULL CHECK(state_bytes > 0),
          trust_status TEXT NOT NULL CHECK(trust_status IN ('trusted', 'invalid')),
          verification_kind TEXT NOT NULL
            CHECK(verification_kind IN ('full_oracle', 'derived')),
          parent_checkpoint_id TEXT,
          created_at_ms INTEGER NOT NULL,
          verified_at_ms INTEGER NOT NULL,
          invalidated_at_ms INTEGER,
          invalidation_reason TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_position_projection_checkpoints_selection
        ON position_projection_checkpoints(
          trust_status, prefix_event_count DESC, created_at_ms DESC,
          checkpoint_id DESC
        )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO position_projection_source_state (
          singleton_id, source_generation, projector_schema,
          projector_implementation_fingerprint, sqlite_schema_cookie,
          checkpoint_mode, last_full_verified_source_generation, updated_at_ms
        ) VALUES (1, 0, ?, NULL, ?, 'disabled', NULL, ?)
        """,
        (POSITION_PROJECTION_SCHEMA, _projection_schema_cookie(conn), int(now_ms())),
    )

    event_new_account = (
        "coalesce(nullif(trim(CAST(json_extract(NEW.event_json, "
        "'$.contract_key.account') AS TEXT)), ''), "
        "trim(CAST(json_extract(NEW.event_json, '$.account') AS TEXT)), '')"
    )
    event_old_account = (
        "coalesce(nullif(trim(CAST(json_extract(OLD.event_json, "
        "'$.contract_key.account') AS TEXT)), ''), "
        "trim(CAST(json_extract(OLD.event_json, '$.account') AS TEXT)), '')"
    )
    event_new_type = (
        "coalesce(lower(trim(CAST(json_extract(NEW.event_json, "
        "'$.event_type') AS TEXT))), '')"
    )
    lot_new_account = "coalesce(trim(CAST(json_extract(NEW.fields_json, '$.account') AS TEXT)), '')"
    lot_old_account = "coalesce(trim(CAST(json_extract(OLD.fields_json, '$.account') AS TEXT)), '')"
    effective_new_lot_account = f"coalesce(NEW.account, {lot_new_account})"
    effective_old_lot_account = f"coalesce(OLD.account, {lot_old_account})"

    # S3 changes source triggers from generation-only to generation plus bounded
    # checkpoint invalidation. Replace an S1 body once; reopening an S3 database
    # must not churn SQLite's schema cookie.
    for trigger_name in (
        "trg_trade_events_source_insert",
        "trg_trade_events_source_update",
        "trg_trade_events_source_delete",
    ):
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (trigger_name,),
        ).fetchone()
        if row is not None and "position_projection_checkpoints" not in str(
            row["sql"] or ""
        ):
            conn.execute(f"DROP TRIGGER {trigger_name}")

    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_trade_events_account_insert_guard
        BEFORE INSERT ON trade_events
        BEGIN
          SELECT CASE
            WHEN json_valid(NEW.event_json) = 0 THEN RAISE(ABORT, 'invalid trade event JSON')
            WHEN {event_new_account} != '' AND {event_new_account} != lower({event_new_account})
              THEN RAISE(ABORT, 'trade event account must be lowercase')
            WHEN NEW.account IS NOT NULL
              AND (
                NEW.account = ''
                OR NEW.account != lower(NEW.account)
                OR {event_new_account} = ''
                OR NEW.account != {event_new_account}
              )
              THEN RAISE(ABORT, 'trade event account conflicts with event JSON')
          END;
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_trade_events_account_update_guard
        BEFORE UPDATE OF account, event_json ON trade_events
        BEGIN
          SELECT CASE
            WHEN json_valid(NEW.event_json) = 0 THEN RAISE(ABORT, 'invalid trade event JSON')
            WHEN {event_new_account} != '' AND {event_new_account} != lower({event_new_account})
              THEN RAISE(ABORT, 'trade event account must be lowercase')
            WHEN NEW.account IS NOT NULL
              AND (
                NEW.account = ''
                OR NEW.account != lower(NEW.account)
                OR {event_new_account} = ''
                OR NEW.account != {event_new_account}
              )
              THEN RAISE(ABORT, 'trade event account conflicts with event JSON')
          END;
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_trade_events_source_insert
        AFTER INSERT ON trade_events
        BEGIN
          UPDATE position_projection_source_state
          SET source_generation = source_generation + 1,
              updated_at_ms = NEW.updated_at_ms
          WHERE singleton_id = 1;
          INSERT INTO position_projection_heads (
            account, lots_generation, projector_schema, status, updated_at_ms
          )
          SELECT {event_new_account}, 0, '{POSITION_PROJECTION_SCHEMA}',
                 'uninitialized', NEW.updated_at_ms
          WHERE {event_new_account} != ''
            AND {event_new_account} = lower({event_new_account})
          ON CONFLICT(account) DO NOTHING;
          UPDATE position_projection_checkpoints
          SET trust_status = 'invalid',
              invalidated_at_ms = NEW.updated_at_ms,
              invalidation_reason = CASE
                WHEN {event_new_type} IN ('void', 'repair')
                  THEN 'control_event_insert'
                WHEN {event_new_type} NOT IN (
                  'open', 'close', 'expire_close', 'assignment', 'exercise',
                  'adjust', 'verification'
                )
                  THEN 'unclassified_event_insert'
                ELSE 'prefix_intersection_insert'
              END
          WHERE trust_status = 'trusted'
            AND (
              {event_new_type} NOT IN (
                'open', 'close', 'expire_close', 'assignment', 'exercise',
                'adjust', 'verification'
              )
              OR prefix_end_trade_time_ms > NEW.trade_time_ms
              OR (
                prefix_end_trade_time_ms = NEW.trade_time_ms
                AND prefix_end_event_id >= NEW.event_id
              )
            );
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_trade_events_source_update
        AFTER UPDATE OF event_id, account, event_json, trade_time_ms ON trade_events
        WHEN OLD.event_id IS NOT NEW.event_id
          OR OLD.account IS NOT NEW.account
          OR OLD.event_json IS NOT NEW.event_json
          OR OLD.trade_time_ms IS NOT NEW.trade_time_ms
        BEGIN
          UPDATE position_projection_source_state
          SET source_generation = source_generation + 1,
              updated_at_ms = NEW.updated_at_ms
          WHERE singleton_id = 1;
          INSERT INTO position_projection_heads (
            account, lots_generation, projector_schema, status, updated_at_ms
          )
          SELECT {event_old_account}, 0, '{POSITION_PROJECTION_SCHEMA}',
                 'uninitialized', NEW.updated_at_ms
          WHERE {event_old_account} != ''
            AND {event_old_account} = lower({event_old_account})
          ON CONFLICT(account) DO NOTHING;
          INSERT INTO position_projection_heads (
            account, lots_generation, projector_schema, status, updated_at_ms
          )
          SELECT {event_new_account}, 0, '{POSITION_PROJECTION_SCHEMA}',
                 'uninitialized', NEW.updated_at_ms
          WHERE {event_new_account} != ''
            AND {event_new_account} = lower({event_new_account})
          ON CONFLICT(account) DO NOTHING;
          UPDATE position_projection_checkpoints
          SET trust_status = 'invalid',
              invalidated_at_ms = NEW.updated_at_ms,
              invalidation_reason = 'event_update'
          WHERE trust_status = 'trusted';
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_trade_events_source_delete
        AFTER DELETE ON trade_events
        BEGIN
          UPDATE position_projection_source_state
          SET source_generation = source_generation + 1,
              updated_at_ms = OLD.updated_at_ms
          WHERE singleton_id = 1;
          UPDATE position_projection_checkpoints
          SET trust_status = 'invalid',
              invalidated_at_ms = OLD.updated_at_ms,
              invalidation_reason = 'event_delete'
          WHERE trust_status = 'trusted';
        END
        """
    )

    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_position_lots_account_insert_guard
        BEFORE INSERT ON position_lots
        BEGIN
          SELECT CASE
            WHEN json_valid(NEW.fields_json) = 0 THEN RAISE(ABORT, 'invalid position lot JSON')
            WHEN {lot_new_account} = '' THEN RAISE(ABORT, 'position lot account is required')
            WHEN {lot_new_account} != lower({lot_new_account})
              THEN RAISE(ABORT, 'position lot account must be lowercase')
            WHEN NEW.account IS NOT NULL AND NEW.account != {lot_new_account}
              THEN RAISE(ABORT, 'position lot account conflicts with fields JSON')
          END;
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_position_lots_account_update_guard
        BEFORE UPDATE OF account, fields_json ON position_lots
        BEGIN
          SELECT CASE
            WHEN json_valid(NEW.fields_json) = 0 THEN RAISE(ABORT, 'invalid position lot JSON')
            WHEN {lot_new_account} = '' THEN RAISE(ABORT, 'position lot account is required')
            WHEN {lot_new_account} != lower({lot_new_account})
              THEN RAISE(ABORT, 'position lot account must be lowercase')
            WHEN NEW.account IS NOT NULL AND NEW.account != {lot_new_account}
              THEN RAISE(ABORT, 'position lot account conflicts with fields JSON')
          END;
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_position_lots_generation_insert
        AFTER INSERT ON position_lots
        BEGIN
          INSERT INTO position_projection_heads (
            account, lots_generation, projector_schema, status, updated_at_ms
          ) VALUES (
            {effective_new_lot_account}, 1, '{POSITION_PROJECTION_SCHEMA}',
            'uninitialized', NEW.updated_at_ms
          )
          ON CONFLICT(account) DO UPDATE SET
            lots_generation = lots_generation + 1,
            updated_at_ms = excluded.updated_at_ms;
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_position_lots_generation_delete
        AFTER DELETE ON position_lots
        WHEN {effective_old_lot_account} != ''
          AND {effective_old_lot_account} = lower({effective_old_lot_account})
        BEGIN
          INSERT INTO position_projection_heads (
            account, lots_generation, projector_schema, status, updated_at_ms
          ) VALUES (
            {effective_old_lot_account}, 1, '{POSITION_PROJECTION_SCHEMA}',
            'uninitialized', OLD.updated_at_ms
          )
          ON CONFLICT(account) DO UPDATE SET
            lots_generation = lots_generation + 1,
            updated_at_ms = excluded.updated_at_ms;
        END
        """
    )

    lot_changed = " OR ".join(
        (
            "OLD.record_id IS NOT NEW.record_id",
            "OLD.account IS NOT NEW.account",
            "OLD.fields_json IS NOT NEW.fields_json",
            "OLD.source_event_id IS NOT NEW.source_event_id",
            "OLD.expiration IS NOT NEW.expiration",
            "OLD.strike IS NOT NEW.strike",
            "OLD.multiplier IS NOT NEW.multiplier",
        )
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_position_lots_generation_update_same
        AFTER UPDATE OF record_id, account, fields_json, source_event_id,
          expiration, strike, multiplier ON position_lots
        WHEN ({lot_changed})
          AND {effective_old_lot_account} = {effective_new_lot_account}
        BEGIN
          INSERT INTO position_projection_heads (
            account, lots_generation, projector_schema, status, updated_at_ms
          ) VALUES (
            {effective_new_lot_account}, 1, '{POSITION_PROJECTION_SCHEMA}',
            'uninitialized', NEW.updated_at_ms
          )
          ON CONFLICT(account) DO UPDATE SET
            lots_generation = lots_generation + 1,
            updated_at_ms = excluded.updated_at_ms;
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_position_lots_generation_update_old
        AFTER UPDATE OF record_id, account, fields_json, source_event_id,
          expiration, strike, multiplier ON position_lots
        WHEN ({lot_changed})
          AND {effective_old_lot_account} != {effective_new_lot_account}
          AND {effective_old_lot_account} != ''
          AND {effective_old_lot_account} = lower({effective_old_lot_account})
        BEGIN
          INSERT INTO position_projection_heads (
            account, lots_generation, projector_schema, status, updated_at_ms
          ) VALUES (
            {effective_old_lot_account}, 1, '{POSITION_PROJECTION_SCHEMA}',
            'uninitialized', NEW.updated_at_ms
          )
          ON CONFLICT(account) DO UPDATE SET
            lots_generation = lots_generation + 1,
            updated_at_ms = excluded.updated_at_ms;
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_position_lots_generation_update_new
        AFTER UPDATE OF record_id, account, fields_json, source_event_id,
          expiration, strike, multiplier ON position_lots
        WHEN ({lot_changed})
          AND {effective_old_lot_account} != {effective_new_lot_account}
        BEGIN
          INSERT INTO position_projection_heads (
            account, lots_generation, projector_schema, status, updated_at_ms
          ) VALUES (
            {effective_new_lot_account}, 1, '{POSITION_PROJECTION_SCHEMA}',
            'uninitialized', NEW.updated_at_ms
          )
          ON CONFLICT(account) DO UPDATE SET
            lots_generation = lots_generation + 1,
            updated_at_ms = excluded.updated_at_ms;
        END
        """
    )
    conn.execute(
        """
        UPDATE position_projection_source_state
        SET sqlite_schema_cookie = ?, updated_at_ms = ?
        WHERE singleton_id = 1
          AND projector_implementation_fingerprint IS NULL
        """,
        (_projection_schema_cookie(conn), int(now_ms())),
    )
