from __future__ import annotations

import logging
from threading import Lock

from domain.domain.trade_execution import execution_identity_from_input
from .repository_common import (
    Any,
    Mapping,
    _add_column_if_missing,
    encode_trade_event_for_storage,
    json,
    sqlite3,
    stored_trade_event_to_ledger_event,
    symbol_market,
    trade_event_position_effect,
    valid_void_target_event_id,
)

EXECUTION_IDENTITY_INDEXES = {
    "trade_events": ("idx_trade_events_execution_identity_v1", "$.raw_payload.execution_id"),
    "assigned_stock_events": ("idx_assigned_stock_execution_identity_v1", "$.execution_id"),
}

_logger = logging.getLogger(__name__)
_execution_identity_index_warning_lock = Lock()
_warned_execution_identity_index_gaps: set[tuple[str | None, str, str]] = set()


def validated_execution_identity_metadata(raw: Mapping[str, Any]) -> str:
    """A declared lookup key must describe the persisted execution input."""
    identity = execution_identity_from_input(raw.get("execution_input"))
    declared = raw.get("execution_id")
    if declared not in (None, "") and (
        not isinstance(declared, str) or declared != identity
    ):
        raise ValueError("trade_execution_identity_metadata_mismatch")
    return identity


def _execution_identity_index_sql(table: str) -> str:
    name, path = EXECUTION_IDENTITY_INDEXES[table]
    return f"CREATE INDEX {name} ON {table}(json_extract(event_json, '{path}'))"


def _execution_identity_index_cause(conn: sqlite3.Connection, table: str) -> str | None:
    name, _path = EXECUTION_IDENTITY_INDEXES[table]
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=? AND tbl_name=?",
        (name, table),
    ).fetchone()
    if row is None:
        return "missing"
    return None if row[0] == _execution_identity_index_sql(table) else "definition_mismatch"


def _execution_identity_index_ready(conn: sqlite3.Connection, table: str) -> bool:
    return _execution_identity_index_cause(conn, table) is None


def _execution_identity_index_gap(
    conn: sqlite3.Connection, table: str,
) -> dict[str, str | int] | None:
    cause = _execution_identity_index_cause(conn, table)
    if cause is None:
        return None
    return {
        "table": table,
        "cause": cause,
        "rows": int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]),
    }


def _execution_candidate_rows(
    conn: sqlite3.Connection, table: str, execution_id: str, *, store_key: str | None = None,
) -> list[sqlite3.Row] | None:
    cause = _execution_identity_index_cause(conn, table)
    if cause is not None:
        # ponytail: one global warning lock; split it if fallback contention matters.
        with _execution_identity_index_warning_lock:
            key = (store_key, table, cause)
            if key not in _warned_execution_identity_index_gaps:
                rows = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                _logger.warning(
                    "execution_identity_index_fallback store_key=%s table=%s cause=%s rows=%s",
                    store_key, table, cause, rows,
                )
                _warned_execution_identity_index_gaps.add(key)
        return None
    _name, path = EXECUTION_IDENTITY_INDEXES[table]
    identity = f"json_extract(event_json, '{path}')"
    key = "event_id" if table == "trade_events" else "stock_event_id"
    # ponytail: scan legacy candidates; migrate their identities only with verified evidence.
    return conn.execute(
        f"SELECT event_json FROM {table} WHERE {identity}=? OR {identity} IS NULL OR {identity}='' "
        f"ORDER BY trade_time_ms ASC, {key} ASC",
        (execution_id,),
    ).fetchall()


def _validate_execution_identity_rows(conn: sqlite3.Connection, table: str) -> None:
    if table not in EXECUTION_IDENTITY_INDEXES:
        raise ValueError("unsupported execution table")
    for row in conn.execute(f"SELECT event_json FROM {table}"):
        event = json.loads(row[0])
        if not isinstance(event, dict):
            raise ValueError("execution event must be a JSON object")
        raw = event if table == "assigned_stock_events" else event.get("raw_payload") or {}
        if not isinstance(raw, dict):
            raise ValueError("execution payload must be a JSON object")
        validated_execution_identity_metadata(raw)


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

# Marker embedded in the pagination projection guards. Names alone cannot tell a
# current definition from a stale one. A store created before the §7.4 change used
# to keep the old ``typeof(strike)`` whitelist for two reasons at once: the guards
# were published with ``CREATE TRIGGER IF NOT EXISTS``, a no-op once the name
# exists, and the open path returned on readiness alone without reaching the
# publish. Both are fixed — the publish now drops and recreates, and readiness no
# longer decides on its own — but a store whose rows still lack the pagination
# fields is handed to the migration path with its stale guards untouched, so this
# marker is what separates a stale definition from a current one. Bump it whenever
# the guard definition changes;
# ``_trade_event_pagination_guard_definitions_current`` compares it against
# ``sqlite_master.sql``.
_TRADE_EVENT_PAGINATION_GUARD_SCHEMA = "trade_event_pagination_guard.v2"

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


def _trade_event_ingest_seq_is_duplicated(conn: sqlite3.Connection) -> bool:
    """Do two stored rows share an ``ingest_seq``?

    ``idx_trade_events_ingest_seq`` is unique, so a store holding duplicates can
    only have been left without that index — and creating it over those rows
    would abort. Callers reach this only once
    ``_trade_event_pagination_missing_row`` has reported no offending row, so
    every ``ingest_seq`` visible here is a whole number and none is ``NULL``. The
    SQL alone does not give that: ``GROUP BY`` treats ``NULL`` as equal, so two
    ``NULL`` rows would group and be reported as duplicates. The precondition
    above is what keeps them out.
    """

    return (
        conn.execute(
            """
            SELECT 1
            FROM trade_events
            GROUP BY ingest_seq
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        ).fetchone()
        is not None
    )


def _trade_event_pagination_guard_definitions_current(conn: sqlite3.Connection) -> bool:
    """Do the stored pagination guards carry the current definition?

    ``_trade_event_pagination_schema_ready`` checks that the guards exist by name
    and type and reads the stored rows, but it never looks at *how* they are defined.
    A store created before the §7.4 change therefore keeps the old
    ``typeof(strike) NOT IN ('integer', 'real')`` whitelist until something
    republishes the definition, and the first write of a decimal-text strike is
    aborted with a misleading ``trade event pagination query fields are incomplete``.
    Opening such a store is what republishes it; a store whose rows still lack the
    pagination fields is left to the migration path instead, so its guards keep the
    old whitelist until that runs. Compare a marker
    embedded in the definition instead, mirroring
    ``_ensure_opend_trade_time_correction_guard``.
    """

    rows = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'trigger' AND name IN (?, ?)
        """,
        (
            "trg_trade_events_pagination_projection_insert_guard",
            "trg_trade_events_pagination_projection_update_guard",
        ),
    ).fetchall()
    if len(rows) != 2:
        return False
    return all(
        _TRADE_EVENT_PAGINATION_GUARD_SCHEMA in str(row["sql"] or "") for row in rows
    )


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

def _publish_trade_event_pagination_schema(
    conn: sqlite3.Connection, *, with_unique_ingest_seq_index: bool = True
) -> None:
    """Write the pagination definitions, including the unique ``ingest_seq`` index.

    ``with_unique_ingest_seq_index`` is false only for a store whose rows already
    share an ``ingest_seq``: that index cannot be built over them, but everything
    else here — the keyset indexes and the projection guards — can, and leaving
    the guards stale is what turns a duplicate-bearing store into one where every
    write aborts with a message about query fields. Readiness still reports the
    gap because ``idx_trade_events_ingest_seq`` stays absent.
    """

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
    if with_unique_ingest_seq_index:
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
    projection_guard = f"""
      -- {_TRADE_EVENT_PAGINATION_GUARD_SCHEMA}
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
             NOT IN ('integer', 'real', 'text')
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
    # The guards must be replaced rather than skipped when their definition
    # changed: ``CREATE TRIGGER IF NOT EXISTS`` would keep a stale definition
    # (e.g. the pre-§7.4 ``typeof(strike)`` whitelist) that rejects every write.
    # Mirrors ``_publish_trade_event_query_projection_immutable_trigger``.
    for trigger_name in (
        "trg_trade_events_pagination_projection_insert_guard",
        "trg_trade_events_pagination_projection_update_guard",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
    conn.execute(
        f"""
        CREATE TRIGGER trg_trade_events_pagination_projection_insert_guard
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
        CREATE TRIGGER trg_trade_events_pagination_projection_update_guard
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


def _try_publish_trade_event_pagination_schema(
    conn: sqlite3.Connection, *, with_unique_ingest_seq_index: bool = True
) -> bool:
    """Publish the pagination schema, or leave the store exactly as it was found.

    Returns whether the publish was applied. A failure that was undone to the
    savepoint returns ``False``; anything the undo cannot answer for propagates
    instead, because the store is then not guaranteed to be back the way it was
    found and the caller has to abort. Two such failures reach the caller. One has
    already destroyed the caller's transaction, and the savepoint went with it, so
    no undo runs at all: the original failure arrives with ``__cause__`` ``None``,
    and with ``__context__`` ``None`` as well unless the caller is already handling
    an exception when it gets here, in which case that one becomes the context. The
    other has an undo that cannot complete, and
    there the original failure is re-raised as the message, with the undo's own
    error chained behind it as ``__context__`` rather than raised in its place. That
    second arm also covers an undo that ran but could not release — the store *is*
    back the way it was found and only the savepoint is left behind — and since
    which of the two happened is not read back, both propagate the same way.

    Statements in ``_publish_trade_event_pagination_schema`` can fail for reasons
    no precondition at the call site models, and guessing at them one by one is
    what this replaces. The one an open path actually meets is a table or view
    already owning an index name: SQLite treats ``CREATE INDEX IF NOT EXISTS`` as
    a no-op only when the name belongs to an *index*, so a shadowed name raises
    instead of skipping. Letting that escape is worse than the gap it reports —
    ``_ensure_trade_event_pagination_schema`` runs from every ``__init__``, so the
    exception takes the whole ledger with it and a store that opened before this
    branch existed never opens again.

    Undoing to the savepoint restores the store's contents, and readiness is read
    from the store rather than remembered — it needs the object names and types
    *and* stored rows whose pagination projection is complete, all of which the undo
    puts back — so it reports what it reported before the attempt. On the shadowed
    store this branch exists for the index name is absent, so that is ``False`` and
    the pagination entry points report the gap through
    ``TradeEventPaginationUnavailable``. That message says a migration is required;
    it does not name the missing object. A store that already carried every name and
    only needed stale definitions refreshed keeps readiness true, and whatever its
    stale definition refuses stays refused until those definitions are published.
    """

    conn.execute("SAVEPOINT trade_event_pagination_publish")
    try:
        _publish_trade_event_pagination_schema(
            conn, with_unique_ingest_seq_index=with_unique_ingest_seq_index
        )
    except sqlite3.Error as failure:
        if not conn.in_transaction:
            # The failure already took the whole transaction with it, and the
            # savepoint went with it. There is nothing left to undo to, and
            # swallowing here is worse than the gap it would report: the caller gets
            # back a connection with no transaction at all, so ``_init_db`` keeps
            # issuing DDL in autocommit and commits a partial schema one statement
            # at a time, then dies on the first statement that references a
            # rolled-back object -- naming that missing object instead of the
            # failure that actually happened. Re-raise so the caller aborts and its
            # own rollback path runs, as it did before this helper existed.
            raise
        try:
            conn.execute("ROLLBACK TO trade_event_pagination_publish")
            conn.execute("RELEASE trade_event_pagination_publish")
        except sqlite3.Error:
            # The undo did not complete. Usually the savepoint is gone while the
            # transaction is not, so there was nothing to roll back to; it also
            # covers a rollback that ran but could not release, where the store is
            # back the way it was found and only the savepoint is left for the
            # caller's own rollback to discard. Which of the two happened is not
            # read back: either way the store is not *guaranteed* to be as it was
            # found, so the original failure propagates and the caller aborts.
            # Letting the undo's own ``no such savepoint`` escape instead would hand
            # the caller a cause that says nothing about what actually went wrong --
            # the same misleading-cause failure the branch above exists to avoid.
            raise failure
        return False
    conn.execute("RELEASE trade_event_pagination_publish")
    return True


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
    """Declare pagination schema; non-empty stores with unmigrated rows require controlled migration.

    Existing guards are refreshed in place when their definitions are stale — they
    are pure query-shape guards, so republishing rewrites no rows and is safe once
    the stored rows already satisfy the pagination projection. A store holding
    duplicate ``ingest_seq`` values receives everything but the unique index over
    them, and a publish that fails while the caller's transaction survives is undone
    to its savepoint rather than allowed to abort the open, so the store opens
    carrying the gap instead of going unopenable. (A publish that destroys the
    transaction does abort the open, exactly as it did before this helper existed.)
    What readiness reports afterwards is what it reported before the undo — it is
    read from the store's object names and types and from the stored rows, both of
    which the undo restores — so the shadowed store this exists for stays ``False``
    and its gap is still reported by the pagination entry points, while a store that
    already carried every name and only needed stale definitions refreshed keeps
    readiness true and receives those definitions refreshed in place. A stale
    definition therefore only keeps refusing if the publish replacing it is undone —
    or on the one store that reaches no publish at all, the store whose rows
    still lack the pagination fields, which is left to the explicit migration path
    with its stale guards in place. Anything the undo cannot answer for propagates,
    since the store is then not guaranteed to be back the way it was found.
    """

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
    # Seed from the stored rows rather than a literal: a store that lost this row
    # (a partial restore, say) must not restart at 1 and re-issue values the rows
    # already carry, which is what leaves duplicate ``ingest_seq`` behind once no
    # unique index is in place to refuse them. Only whole, non-negative values may
    # seed it — the readiness check below has not run yet, so a ``TEXT`` or
    # negative ``ingest_seq`` is still visible here, and either would violate the
    # ``last_value`` constraint (or poison it with a non-integer).
    conn.execute(
        """
        INSERT INTO trade_event_ingest_sequence (singleton_id, last_value)
        VALUES (
          1,
          MAX(
            0,
            COALESCE(
              (
                SELECT MAX(ingest_seq)
                FROM trade_events
                WHERE typeof(ingest_seq) = 'integer'
              ),
              0
            )
          )
        )
        ON CONFLICT(singleton_id) DO UPDATE SET
          last_value = MAX(last_value, excluded.last_value)
        """
    )
    if _trade_event_pagination_schema_ready(conn) and (
        _trade_event_pagination_guard_definitions_current(conn)
    ):
        return
    if _trade_event_pagination_missing_row(conn) is None:
        # The stored rows already satisfy the pagination projection and only the
        # definitions are stale (e.g. a guard written before the §7.4 decimal-text
        # strike), so refreshing them is safe and rewrites no rows. This is the
        # same precondition ``_publish_trade_event_pagination_schema`` asserts.
        # Without it an upgraded store keeps a guard that aborts every write.
        #
        # ``CREATE UNIQUE INDEX`` cannot succeed while two rows share an
        # ``ingest_seq``, so that one index is skipped rather than the whole
        # publish: a stale guard would otherwise abort every write on a store that
        # stays readable, reporting query fields rather than the real cause. The
        # gap is still announced — readiness needs that index, so it stays false
        # and ``TradeEventPaginationUnavailable`` reports it. That message says a
        # migration is required; it does not name the missing object.
        _try_publish_trade_event_pagination_schema(
            conn,
            with_unique_ingest_seq_index=not _trade_event_ingest_seq_is_duplicated(conn),
        )
        return
    has_rows = conn.execute("SELECT 1 FROM trade_events LIMIT 1").fetchone()
    if has_rows is None:
        _try_publish_trade_event_pagination_schema(conn)

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
