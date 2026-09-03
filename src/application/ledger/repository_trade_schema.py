from __future__ import annotations

from .repository_common import (
    Any,
    _add_column_if_missing,
    encode_trade_event_for_storage,
    json,
    sqlite3,
    stored_trade_event_to_ledger_event,
    symbol_market,
    trade_event_position_effect,
    valid_void_target_event_id,
)

TRADE_EVENT_PAGINATION_INDEXES = (
    "idx_trade_events_pagination_missing",
    "idx_trade_events_ingest_seq",
    "idx_trade_events_market_keyset",
    "idx_trade_events_market_effect_keyset",
    "idx_trade_events_account_market_keyset",
    "idx_trade_events_account_market_effect_keyset",
)

TRADE_EVENT_PAGINATION_TRIGGERS = (
    "trg_trade_events_ingest_seq_immutable",
    "trg_trade_events_query_projection_immutable",
    "trg_trade_events_pagination_projection_insert_guard",
    "trg_trade_events_pagination_projection_update_guard",
    "trg_trade_events_delete_immutable",
)

_OPEND_TRADE_TIME_CORRECTION_SCHEMA = "opend_trade_time_correction.v1"

_TRADE_EVENT_PAGINATION_MISSING = """
    ingest_seq IS NULL
    OR typeof(ingest_seq) != 'integer' OR ingest_seq < 1
    OR typeof(trade_time_ms) != 'integer'
    OR market IS NULL OR market NOT IN ('US', 'HK')
    OR position_effect IS NULL OR trim(position_effect) = ''
       OR position_effect != lower(trim(position_effect))
    OR account IS NULL OR trim(account) = ''
       OR account != trim(account) OR account != lower(account)
"""

class TradeEventPaginationUnavailable(RuntimeError):
    """The controlled legacy projection migration has not completed."""

def _trade_event_pagination_projections(
    event_json: Any,
    *,
    voided_event_ids: frozenset[str] = frozenset(),
) -> tuple[str, int, str, str, str]:
    try:
        payload = json.loads(str(event_json or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError("trade event JSON is invalid during pagination migration") from exc
    try:
        encoded = encode_trade_event_for_storage(payload)
        event = encoded.event
    except ValueError:
        event, diagnostics = stored_trade_event_to_ledger_event(payload)
        error_codes = {
            item.code for item in diagnostics if item.severity == "error"
        }
        if (
            event is None
            or error_codes != {"event_time_must_be_positive"}
            or event.event_id not in voided_event_ids
        ):
            raise
    if event is None:  # pragma: no cover - the encoder contract owns this guard
        raise ValueError("trade event cannot be projected")
    account = str(event.contract_key.account or "").strip()
    if not account or account != account.lower():
        raise ValueError(
            f"trade event account cannot be projected: event_id={event.event_id}"
        )
    market = symbol_market(event.contract_key.underlying_symbol)
    if market not in {"US", "HK"}:
        raise ValueError(
            f"trade event market cannot be derived: event_id={event.event_id}"
        )
    position_effect = trade_event_position_effect(event.event_type).strip().lower()
    if not position_effect:
        raise ValueError(
            f"trade event position effect cannot be derived: event_id={event.event_id}"
        )
    return (
        str(event.event_id),
        int(event.event_time_ms),
        account,
        market,
        position_effect,
    )

def _valid_voided_trade_event_ids(conn: sqlite3.Connection) -> frozenset[str]:
    targets: set[str] = set()
    for row in conn.execute("SELECT event_json FROM trade_events"):
        try:
            payload = json.loads(str(row["event_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        target = valid_void_target_event_id(payload)
        if target:
            targets.add(target)
    return frozenset(targets)

def _trade_event_query_projections(event_json: Any) -> tuple[str, str, str]:
    _event_id, _trade_time_ms, account, market, position_effect = (
        _trade_event_pagination_projections(event_json)
    )
    return account, market, position_effect

def _trade_event_pagination_missing_row(
    conn: sqlite3.Connection,
) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT event_id FROM trade_events WHERE {_TRADE_EVENT_PAGINATION_MISSING} LIMIT 1"
    ).fetchone()

def _trade_event_pagination_schema_ready(conn: sqlite3.Connection) -> bool:
    rows = conn.execute(
        """
        SELECT type, name
        FROM sqlite_master
        WHERE name IN ({})
        """.format(
            ", ".join(
                "?"
                for _ in (
                    *TRADE_EVENT_PAGINATION_INDEXES,
                    *TRADE_EVENT_PAGINATION_TRIGGERS,
                )
            )
        ),
        (*TRADE_EVENT_PAGINATION_INDEXES, *TRADE_EVENT_PAGINATION_TRIGGERS),
    ).fetchall()
    present = {(str(row["type"]), str(row["name"])) for row in rows}
    required = {
        *(('index', name) for name in TRADE_EVENT_PAGINATION_INDEXES),
        *(('trigger', name) for name in TRADE_EVENT_PAGINATION_TRIGGERS),
    }
    return required.issubset(present) and _trade_event_pagination_missing_row(conn) is None


def _publish_trade_event_query_projection_immutable_trigger(
    conn: sqlite3.Connection,
) -> None:
    conn.execute("DROP TRIGGER IF EXISTS trg_trade_events_query_projection_immutable")
    conn.execute(
        f"""
        CREATE TRIGGER trg_trade_events_query_projection_immutable
        BEFORE UPDATE OF event_id, account, event_json, trade_time_ms,
          market, position_effect ON trade_events
        WHEN OLD.ingest_seq IS NOT NULL AND (
          NEW.event_id IS NOT OLD.event_id
          OR NEW.account IS NOT OLD.account
          OR NEW.market IS NOT OLD.market
          OR NEW.position_effect IS NOT OLD.position_effect
          OR json_extract(NEW.event_json, '$.event_id')
             IS NOT json_extract(OLD.event_json, '$.event_id')
          OR json_extract(NEW.event_json, '$.event_type')
             IS NOT json_extract(OLD.event_json, '$.event_type')
          OR json_extract(NEW.event_json, '$.contract_key.account')
             IS NOT json_extract(OLD.event_json, '$.contract_key.account')
          OR json_extract(NEW.event_json, '$.contract_key.broker')
             IS NOT json_extract(OLD.event_json, '$.contract_key.broker')
          OR json_extract(NEW.event_json, '$.contract_key.underlying_symbol')
             IS NOT json_extract(OLD.event_json, '$.contract_key.underlying_symbol')
          OR json_extract(NEW.event_json, '$.contract_key.option_type')
             IS NOT json_extract(OLD.event_json, '$.contract_key.option_type')
          OR json_extract(NEW.event_json, '$.contract_key.strike')
             IS NOT json_extract(OLD.event_json, '$.contract_key.strike')
          OR json_extract(NEW.event_json, '$.contract_key.expiration_ymd')
             IS NOT json_extract(OLD.event_json, '$.contract_key.expiration_ymd')
          OR (
            (
              NEW.trade_time_ms IS NOT OLD.trade_time_ms
              OR json_extract(NEW.event_json, '$.event_time_ms')
                 IS NOT json_extract(OLD.event_json, '$.event_time_ms')
            )
            AND NOT (
              NEW.trade_time_ms IS NOT OLD.trade_time_ms
              AND json_extract(NEW.event_json, '$.event_time_ms')
                  IS NOT json_extract(OLD.event_json, '$.event_time_ms')
              AND json_type(
                NEW.event_json,
                '$.raw_payload.trade_time_correction_provenance'
              ) IS 'object'
              AND json_extract(
                NEW.event_json,
                '$.raw_payload.trade_time_correction_provenance.schema_version'
              ) = '{_OPEND_TRADE_TIME_CORRECTION_SCHEMA}'
              AND json_extract(
                NEW.event_json,
                '$.raw_payload.trade_time_correction_provenance.provider'
              ) = 'opend'
              AND json_extract(
                NEW.event_json,
                '$.raw_payload.trade_time_correction_provenance.source'
              ) = 'manual_trade_event_repair'
              AND json_type(
                NEW.event_json,
                '$.raw_payload.trade_time_correction_provenance.before_trade_time_ms'
              ) IS 'integer'
              AND CAST(json_extract(
                NEW.event_json,
                '$.raw_payload.trade_time_correction_provenance.before_trade_time_ms'
              ) AS INTEGER) IS OLD.trade_time_ms
              AND json_type(
                NEW.event_json,
                '$.raw_payload.trade_time_correction_provenance.after_trade_time_ms'
              ) IS 'integer'
              AND CAST(json_extract(
                NEW.event_json,
                '$.raw_payload.trade_time_correction_provenance.after_trade_time_ms'
              ) AS INTEGER) IS NEW.trade_time_ms
              AND json_extract(
                NEW.event_json,
                '$.raw_payload.opend_order_evidence.provider'
              ) = 'opend'
              AND json_extract(
                NEW.event_json,
                '$.raw_payload.opend_order_evidence.schema_version'
              ) = 'opend_order_evidence.v1'
              AND json_array_length(
                NEW.event_json,
                '$.raw_payload.opend_order_evidence.orders'
              ) > 0
              AND NEW.trade_time_ms = (
                SELECT MIN(CAST(json_extract(value, '$.trade_time_ms') AS INTEGER))
                FROM json_each(
                  NEW.event_json,
                  '$.raw_payload.opend_order_evidence.orders'
                )
              )
            )
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'trade event query projection is immutable');
        END
        """
    )


def _ensure_opend_trade_time_correction_guard(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'trigger' AND name = 'trg_trade_events_query_projection_immutable'
        """
    ).fetchone()
    if row is None or _OPEND_TRADE_TIME_CORRECTION_SCHEMA not in str(row["sql"] or ""):
        _publish_trade_event_query_projection_immutable_trigger(conn)

def _publish_trade_event_pagination_schema(conn: sqlite3.Connection) -> None:
    missing = _trade_event_pagination_missing_row(conn)
    if missing is not None:
        raise ValueError(
            "trade event pagination migration is incomplete: "
            f"event_id={missing['event_id']}"
        )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_trade_events_pagination_missing
        ON trade_events(event_id)
        WHERE {_TRADE_EVENT_PAGINATION_MISSING}
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_events_ingest_seq "
        "ON trade_events(ingest_seq)"
    )
    for index_name, columns in (
        (
            "idx_trade_events_market_keyset",
            "market, trade_time_ms DESC, event_id DESC, ingest_seq",
        ),
        (
            "idx_trade_events_market_effect_keyset",
            "market, position_effect, trade_time_ms DESC, event_id DESC, ingest_seq",
        ),
        (
            "idx_trade_events_account_market_keyset",
            "account, market, trade_time_ms DESC, event_id DESC, ingest_seq",
        ),
        (
            "idx_trade_events_account_market_effect_keyset",
            "account, market, position_effect, trade_time_ms DESC, event_id DESC, ingest_seq",
        ),
    ):
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {index_name} ON trade_events({columns})"
        )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_trade_events_ingest_seq_immutable
        BEFORE UPDATE OF ingest_seq ON trade_events
        WHEN NEW.ingest_seq IS NOT OLD.ingest_seq
        BEGIN
          SELECT RAISE(ABORT, 'trade event ingest_seq is immutable');
        END
        """
    )
    _publish_trade_event_query_projection_immutable_trigger(conn)
    projection_guard = """
      SELECT CASE
        WHEN json_valid(NEW.event_json) != 1
          OR json_type(NEW.event_json) IS NOT 'object'
          THEN RAISE(ABORT, 'trade event JSON is invalid')
        WHEN NEW.ingest_seq IS NULL OR typeof(NEW.ingest_seq) != 'integer'
          OR NEW.ingest_seq < 1
          OR typeof(NEW.trade_time_ms) != 'integer'
          OR typeof(NEW.account) != 'text'
          OR NEW.account = '' OR NEW.account != lower(trim(NEW.account))
          OR typeof(NEW.market) != 'text' OR NEW.market NOT IN ('US', 'HK')
          OR typeof(NEW.position_effect) != 'text'
          OR NEW.position_effect = ''
          OR NEW.position_effect != lower(trim(NEW.position_effect))
          THEN RAISE(ABORT, 'trade event pagination projection is incomplete')
        WHEN json_type(NEW.event_json, '$.event_id') IS NOT 'text'
          OR json_type(NEW.event_json, '$.event_time_ms') IS NOT 'integer'
          OR json_type(NEW.event_json, '$.event_type') IS NOT 'text'
          OR json_type(NEW.event_json, '$.contract_key') IS NOT 'object'
          OR json_type(NEW.event_json, '$.contract_key.account') IS NOT 'text'
          OR json_type(NEW.event_json, '$.contract_key.broker') IS NOT 'text'
          OR json_type(
            NEW.event_json, '$.contract_key.underlying_symbol'
          ) IS NOT 'text'
          OR json_type(NEW.event_json, '$.contract_key.option_type') IS NOT 'text'
          OR json_type(NEW.event_json, '$.contract_key.strike')
             NOT IN ('integer', 'real')
          OR json_type(
            NEW.event_json, '$.contract_key.expiration_ymd'
          ) IS NOT 'text'
          THEN RAISE(ABORT, 'trade event pagination query fields are incomplete')
        WHEN CAST(json_extract(NEW.event_json, '$.event_id') AS TEXT)
          IS NOT NEW.event_id
          OR CAST(json_extract(NEW.event_json, '$.event_time_ms') AS INTEGER)
             IS NOT NEW.trade_time_ms
          THEN RAISE(ABORT, 'trade event identity projection conflicts')
        WHEN CAST(json_extract(
          NEW.event_json, '$.contract_key.account'
        ) AS TEXT) IS NOT NEW.account
          THEN RAISE(ABORT, 'trade event account projection conflicts')
        WHEN NEW.market != CASE
          WHEN upper(trim(CAST(json_extract(
            NEW.event_json, '$.contract_key.underlying_symbol'
          ) AS TEXT))) LIKE '%.HK' THEN 'HK'
          ELSE 'US'
        END
          THEN RAISE(ABORT, 'trade event market projection conflicts')
        WHEN NEW.position_effect != CASE
          WHEN lower(trim(CAST(json_extract(
            NEW.event_json, '$.event_type'
          ) AS TEXT))) IN ('close', 'expire_close', 'assignment', 'exercise')
            THEN 'close'
          ELSE lower(trim(CAST(json_extract(
            NEW.event_json, '$.event_type'
          ) AS TEXT)))
          END
          THEN RAISE(ABORT, 'trade event position-effect projection conflicts')
      END;
    """
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_trade_events_pagination_projection_insert_guard
        BEFORE INSERT ON trade_events
        BEGIN
          {projection_guard}
          SELECT CASE
            WHEN EXISTS (
              SELECT 1 FROM trade_events WHERE event_id = NEW.event_id
            )
              THEN RAISE(ABORT, 'trade event replacement is not allowed')
            WHEN NEW.ingest_seq IS NOT (
              SELECT last_value
              FROM trade_event_ingest_sequence
              WHERE singleton_id = 1
            )
              THEN RAISE(ABORT, 'trade event ingest_seq was not allocated')
          END;
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_trade_events_pagination_projection_update_guard
        BEFORE UPDATE OF event_id, event_json, trade_time_ms, ingest_seq,
          account, market, position_effect ON trade_events
        BEGIN
          {projection_guard}
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_trade_events_delete_immutable
        BEFORE DELETE ON trade_events
        BEGIN
          SELECT RAISE(ABORT, 'trade event membership is immutable');
        END
        """
    )

def _backfill_trade_event_pagination_schema(conn: sqlite3.Connection) -> int:
    invalid_created_at = conn.execute(
        """
        SELECT event_id
        FROM trade_events
        WHERE typeof(created_at_ms) != 'integer'
        LIMIT 1
        """
    ).fetchone()
    if invalid_created_at is not None:
        raise ValueError(
            "trade event created_at_ms must be an integer for pagination migration: "
            f"event_id={invalid_created_at['event_id']}"
        )
    invalid_trade_time = conn.execute(
        """
        SELECT event_id
        FROM trade_events
        WHERE typeof(trade_time_ms) != 'integer'
        LIMIT 1
        """
    ).fetchone()
    if invalid_trade_time is not None:
        raise ValueError(
            "trade event trade_time_ms must be an integer for pagination migration: "
            f"event_id={invalid_trade_time['event_id']}"
        )
    invalid_sequence = conn.execute(
        """
        SELECT event_id
        FROM trade_events
        WHERE ingest_seq IS NOT NULL
          AND (typeof(ingest_seq) != 'integer' OR ingest_seq < 1)
        LIMIT 1
        """
    ).fetchone()
    if invalid_sequence is not None:
        raise ValueError(
            "trade event ingest sequence is invalid: "
            f"event_id={invalid_sequence['event_id']}"
        )
    duplicate_sequence = conn.execute(
        """
        SELECT ingest_seq
        FROM trade_events
        WHERE ingest_seq IS NOT NULL
        GROUP BY ingest_seq
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()
    if duplicate_sequence is not None:
        raise ValueError(
            "trade event ingest sequence is not unique: "
            f"ingest_seq={duplicate_sequence['ingest_seq']}"
        )

    max_row = conn.execute(
        "SELECT COALESCE(MAX(ingest_seq), 0) AS max_seq FROM trade_events"
    ).fetchone()
    counter_row = conn.execute(
        "SELECT last_value FROM trade_event_ingest_sequence WHERE singleton_id = 1"
    ).fetchone()
    next_sequence = max(
        int(max_row["max_seq"] if max_row is not None else 0),
        int(counter_row["last_value"] if counter_row is not None else 0),
    )
    voided_event_ids = _valid_voided_trade_event_ids(conn)
    for trigger_name in (
        "trg_trade_events_ingest_seq_immutable",
        "trg_trade_events_query_projection_immutable",
        "trg_trade_events_pagination_projection_insert_guard",
        "trg_trade_events_pagination_projection_update_guard",
        "trg_trade_events_delete_immutable",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trade_events_pagination_backfill "
        "ON trade_events(created_at_ms, event_id)"
    )
    updated = 0
    last_created_at_ms: int | None = None
    last_event_id: str | None = None
    try:
        while True:
            if last_created_at_ms is None:
                rows = conn.execute(
                    """
                    SELECT event_id, account, event_json, trade_time_ms,
                           created_at_ms, ingest_seq, market, position_effect
                    FROM trade_events
                    ORDER BY created_at_ms ASC, event_id ASC
                    LIMIT 1000
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT event_id, account, event_json, trade_time_ms,
                           created_at_ms, ingest_seq, market, position_effect
                    FROM trade_events
                    WHERE (created_at_ms, event_id) > (?, ?)
                    ORDER BY created_at_ms ASC, event_id ASC
                    LIMIT 1000
                    """,
                    (last_created_at_ms, last_event_id),
                ).fetchall()
            if not rows:
                break
            values: list[tuple[str, int, str, str, str]] = []
            for row in rows:
                (
                    canonical_event_id,
                    canonical_trade_time_ms,
                    account,
                    market,
                    position_effect,
                ) = _trade_event_pagination_projections(
                    row["event_json"],
                    voided_event_ids=voided_event_ids,
                )
                event_id = str(row["event_id"])
                if canonical_event_id != event_id:
                    raise ValueError(
                        f"trade event id conflicts with JSON: event_id={event_id}"
                    )
                if canonical_trade_time_ms != row["trade_time_ms"]:
                    raise ValueError(
                        f"trade event time conflicts with JSON: event_id={event_id}"
                    )
                raw_account = str(row["account"] or "")
                stored_account = raw_account.strip()
                if stored_account and stored_account != account:
                    raise ValueError(
                        f"trade event account conflicts with JSON: event_id={event_id}"
                    )
                raw_market = str(row["market"] or "")
                stored_market = raw_market.strip().upper()
                if stored_market and stored_market != market:
                    raise ValueError(
                        f"trade event market projection conflicts: event_id={event_id}"
                    )
                raw_effect = str(row["position_effect"] or "")
                stored_effect = raw_effect.strip().lower()
                if stored_effect and stored_effect != position_effect:
                    raise ValueError(
                        "trade event position-effect projection conflicts: "
                        f"event_id={event_id}"
                    )
                ingest_seq = row["ingest_seq"]
                row_is_missing = (
                    ingest_seq is None
                    or raw_account != account
                    or raw_market != market
                    or raw_effect != position_effect
                )
                if ingest_seq is None:
                    next_sequence += 1
                    ingest_seq = next_sequence
                if row_is_missing:
                    values.append(
                        (account, int(ingest_seq), market, position_effect, event_id)
                    )
                last_created_at_ms = int(row["created_at_ms"])
                last_event_id = event_id
            if values:
                conn.executemany(
                    """
                    UPDATE trade_events
                    SET account = ?, ingest_seq = ?, market = ?, position_effect = ?
                    WHERE event_id = ?
                    """,
                    values,
                )
                updated += len(values)
    finally:
        conn.execute("DROP INDEX IF EXISTS idx_trade_events_pagination_backfill")

    conn.execute(
        """
        INSERT INTO trade_event_ingest_sequence (singleton_id, last_value)
        VALUES (1, ?)
        ON CONFLICT(singleton_id) DO UPDATE SET
          last_value = MAX(last_value, excluded.last_value)
        """,
        (next_sequence,),
    )
    _publish_trade_event_pagination_schema(conn)
    return updated

def _ensure_trade_event_pagination_schema(conn: sqlite3.Connection) -> None:
    """Declare pagination schema; non-empty legacy stores require controlled migration."""

    _add_column_if_missing(conn, "trade_events", "ingest_seq", "INTEGER")
    _add_column_if_missing(conn, "trade_events", "market", "TEXT")
    _add_column_if_missing(conn, "trade_events", "position_effect", "TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_event_ingest_sequence (
          singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
          last_value INTEGER NOT NULL CHECK(last_value >= 0)
        )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO trade_event_ingest_sequence (singleton_id, last_value)
        VALUES (1, 0)
        """
    )
    if _trade_event_pagination_schema_ready(conn):
        return
    has_rows = conn.execute("SELECT 1 FROM trade_events LIMIT 1").fetchone()
    if has_rows is None:
        _publish_trade_event_pagination_schema(conn)

def _create_index_if_table_empty(
    conn: sqlite3.Connection,
    *,
    index_name: str,
    table: str,
    create_sql: str,
) -> bool:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
        (index_name,),
    ).fetchone()
    if exists is not None:
        return True
    populated = conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
    if populated is not None:
        return False
    conn.execute(create_sql)
    return True
