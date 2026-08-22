from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, cast

from domain.domain.ledger.position_fields import effective_expiration, now_ms
from domain.domain.ledger.position_fingerprint import (
    ordered_position_lots_fingerprint,
)
from domain.domain.symbol_identity import symbol_market
from domain.domain.wheel import normalize_wheel_event
from src.application.ledger.event_codec import encode_trade_event_for_storage, trade_event_application_payload
from src.application.ledger.lifecycle_attempt_audit import (
    LIFECYCLE_ATTEMPT_CHAIN_GENESIS,
    LIFECYCLE_RECEIPT_CODEC,
    LIFECYCLE_RECEIPT_CODEC_VERSION,
    LifecycleAttemptAuditEnvelope,
    canonical_lifecycle_observation_bytes,
    compute_lifecycle_attempt_chain_sha256,
    lifecycle_invocation_id_bytes,
    lifecycle_receipt_sha256,
    lifecycle_sha256_bytes,
    validate_lifecycle_attempt_audit_envelope,
    verify_lifecycle_attempt_audit_chain,
)
from src.application.ledger.lifecycle_settlement_semantics import (
    settlement_semantic_from_evidence,
)
from src.application.ledger.position_records import PositionLotRecord
from src.application.ledger.sqlite_row_codec import (
    position_lot_row_to_record,
    read_current_decision_projection_inputs_from_conn,
)
from src.application.ledger.store_resolution import resolve_ledger_store
from src.infrastructure.feishu_bitable import parse_note_kv, safe_float
from src.infrastructure.private_storage import (
    connect_private_sqlite,
    private_path,
    secure_sqlite_artifacts,
)


POSITION_PROJECTION_SCHEMA = "position_projection.v1"

_CURRENT_DECISION_GENERATION_COUNTERS = (
    "case_generation",
    "evidence_generation",
    "allocation_generation",
    "source_consumption_generation",
    "timing_generation",
    "combo_identity_generation",
    "assigned_stock_generation",
)

TRADE_EVENTS_COLUMN_CLASSIFICATION = {
    "event_id": "integrity/identity",
    "account": "projection-affecting",
    "event_json": "projection-affecting",
    "trade_time_ms": "projection-affecting",
    "ingest_seq": "integrity/snapshot",
    "market": "projection-affecting",
    "position_effect": "projection-affecting",
    "created_at_ms": "metadata-only",
    "updated_at_ms": "metadata-only",
}

POSITION_LOTS_COLUMN_CLASSIFICATION = {
    "record_id": "integrity/identity",
    "account": "projection-affecting",
    "fields_json": "projection-affecting",
    "source_event_id": "projection-affecting",
    "expiration": "projection-affecting",
    "strike": "projection-affecting",
    "multiplier": "projection-affecting",
    "updated_at_ms": "metadata-only",
}


@dataclass(frozen=True)
class PositionLotDiff:
    added: int
    changed: int
    removed: int
    unchanged: int
    accounts: tuple[str, ...]
    touched_accounts: tuple[str, ...]

    @property
    def lot_count(self) -> int:
        return self.added + self.changed + self.unchanged


@dataclass(frozen=True)
class PositionProjectionAccountSnapshot:
    account: str
    fingerprint: str
    lot_count: int
    records: tuple[dict[str, Any], ...] = ()


class OptionPositionsReadRepo(Protocol):
    def list_position_lots(self, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]: ...


class OptionPositionsEventReadRepo(OptionPositionsReadRepo, Protocol):
    def list_trade_events(self, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]: ...
    def list_trade_events_page(self, **kwargs: Any) -> dict[str, Any]: ...


class OptionPositionsEventWriteRepo(OptionPositionsEventReadRepo, Protocol):
    def upsert_trade_event(self, event: Any, *, conn: sqlite3.Connection | None = None) -> bool: ...
    def replace_position_lots(
        self,
        records: Sequence[PositionLotRecord],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int: ...


class PositionProjectionPublicationRepo(OptionPositionsEventWriteRepo, Protocol):
    def apply_position_lot_diff(
        self,
        records: Sequence[PositionLotRecord],
        *,
        remove_missing: bool = True,
        conn: sqlite3.Connection | None = None,
    ) -> PositionLotDiff: ...
    def publish_full_position_projection_heads(
        self,
        *,
        implementation_fingerprint: str,
        known_accounts: Sequence[str],
        changed_accounts: Sequence[str],
        full_verified: bool = True,
        publish_source_implementation: bool = True,
        readiness_prevalidated: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[int, bool, str | None]: ...


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


def _position_lot_storage_values(
    record: PositionLotRecord,
) -> tuple[str, str, str, str | None, int | None, float | None, float | None]:
    if not isinstance(record, PositionLotRecord):
        raise TypeError("replace_position_lots requires PositionLotRecord records")
    record_id = record.record_id
    fields = record.fields
    _validate_position_lot_fields(record_id=record_id, fields=fields)
    account = str(fields.get("account") or "").strip()
    if not account:
        raise ValueError(f"position lot account is required: record_id={record_id}")
    if account != account.lower():
        raise ValueError(f"position lot account must be lowercase: record_id={record_id}")
    fields_json = json.dumps(
        fields,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )
    expiration_ms, strike, multiplier = _position_lot_contract_scalars(fields)
    source_event_id = str(fields.get("source_event_id")) if fields.get("source_event_id") else None
    return (
        record_id,
        account,
        fields_json,
        source_event_id,
        int(expiration_ms) if expiration_ms is not None else None,
        float(strike) if strike is not None else None,
        float(multiplier) if multiplier is not None else None,
    )


def _canonical_existing_fields_json(raw: Any) -> str | None:
    try:
        fields = json.loads(str(raw or "{}"))
        if not isinstance(fields, dict):
            return None
        return json.dumps(
            fields,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _storage_scalar_matches(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return abs(float(left) - float(right)) < 1e-9


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


def _normalized_lifecycle_case_targets(
    payload: dict[str, Any],
    *,
    case_id: str,
    account: str,
) -> tuple[tuple[str, ...], dict[str, int], tuple[tuple[str, str, str, int | None], ...]]:
    target_lot_ids_raw = payload.get("target_lot_ids") or []
    if not isinstance(target_lot_ids_raw, (list, tuple)):
        raise ValueError("trade lifecycle case target_lot_ids must be a list")
    target_lot_ids = tuple(str(value or "").strip() for value in target_lot_ids_raw)
    if any(not value for value in target_lot_ids) or len(set(target_lot_ids)) != len(target_lot_ids):
        raise ValueError("trade lifecycle case target_lot_ids are invalid")
    target_contracts_raw = payload.get("target_contracts_by_lot") or {}
    if not isinstance(target_contracts_raw, dict):
        raise ValueError("trade lifecycle case target_contracts_by_lot must be an object")
    target_contracts: dict[str, int] = {}
    for key, value in target_contracts_raw.items():
        lot_id = str(key or "").strip()
        if not lot_id or type(value) is not int or value <= 0:
            raise ValueError("trade lifecycle case target contract count is invalid")
        target_contracts[lot_id] = value
    all_lot_ids = tuple(sorted(set(target_lot_ids) | set(target_contracts)))
    return (
        target_lot_ids,
        target_contracts,
        tuple((case_id, account, lot_id, target_contracts.get(lot_id)) for lot_id in all_lot_ids),
    )


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


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
) -> tuple[str, int, str, str, str]:
    try:
        payload = json.loads(str(event_json or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError("trade event JSON is invalid during pagination migration") from exc
    encoded = encode_trade_event_for_storage(payload)
    if encoded.event is None:
        raise ValueError(
            f"trade event cannot be projected: event_id={encoded.event_id}"
        )
    account = str(encoded.event.contract_key.account or "").strip()
    if not account or account != account.lower():
        raise ValueError(
            f"trade event account cannot be projected: event_id={encoded.event_id}"
        )
    market = symbol_market(encoded.event.contract_key.underlying_symbol)
    if market not in {"US", "HK"}:
        raise ValueError(
            f"trade event market cannot be derived: event_id={encoded.event_id}"
        )
    application_payload = trade_event_application_payload(encoded.payload)
    position_effect = str(application_payload.get("position_effect") or "").strip().lower()
    if not position_effect:
        raise ValueError(
            f"trade event position effect cannot be derived: event_id={encoded.event_id}"
        )
    return (
        str(encoded.event_id),
        int(encoded.event_time_ms),
        account,
        market,
        position_effect,
    )


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
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_trade_events_query_projection_immutable
        BEFORE UPDATE OF event_id, account, event_json, trade_time_ms,
          market, position_effect ON trade_events
        WHEN OLD.ingest_seq IS NOT NULL AND (
          NEW.event_id IS NOT OLD.event_id
          OR NEW.account IS NOT OLD.account
          OR NEW.trade_time_ms IS NOT OLD.trade_time_ms
          OR NEW.market IS NOT OLD.market
          OR NEW.position_effect IS NOT OLD.position_effect
          OR json_extract(NEW.event_json, '$.event_id')
             IS NOT json_extract(OLD.event_json, '$.event_id')
          OR json_extract(NEW.event_json, '$.event_time_ms')
             IS NOT json_extract(OLD.event_json, '$.event_time_ms')
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
        )
        BEGIN
          SELECT RAISE(ABORT, 'trade event query projection is immutable');
        END
        """
    )
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
                ) = _trade_event_pagination_projections(row["event_json"])
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


def _ensure_lifecycle_attempt_audit_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_lifecycle_attempt_audit_heads (
          audit_case_key INTEGER PRIMARY KEY CHECK(typeof(audit_case_key) = 'integer'),
          case_id TEXT NOT NULL UNIQUE,
          last_ordinal INTEGER NOT NULL
            CHECK(typeof(last_ordinal) = 'integer' AND last_ordinal >= 0),
          chain_sha256 BLOB NOT NULL
            CHECK(typeof(chain_sha256) = 'blob' AND length(chain_sha256) = 32),
          current_span_ordinal INTEGER
            CHECK(
              current_span_ordinal IS NULL OR (
                typeof(current_span_ordinal) = 'integer'
                AND current_span_ordinal >= 1
              )
            ),
          last_invocation_id BLOB
            CHECK(
              last_invocation_id IS NULL OR (
                typeof(last_invocation_id) = 'blob'
                AND length(last_invocation_id) = 16
              )
            ),
          updated_at_ms INTEGER NOT NULL
            CHECK(typeof(updated_at_ms) = 'integer' AND updated_at_ms >= 1),
          FOREIGN KEY(case_id) REFERENCES trade_lifecycle_cases(case_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_lifecycle_attempt_audits (
          audit_case_key INTEGER NOT NULL CHECK(typeof(audit_case_key) = 'integer'),
          ordinal INTEGER NOT NULL
            CHECK(typeof(ordinal) = 'integer' AND ordinal >= 1),
          invocation_id BLOB NOT NULL
            CHECK(typeof(invocation_id) = 'blob' AND length(invocation_id) = 16),
          attempted_at_ms INTEGER NOT NULL
            CHECK(typeof(attempted_at_ms) = 'integer' AND attempted_at_ms >= 1),
          outcome_code INTEGER NOT NULL
            CHECK(typeof(outcome_code) = 'integer' AND outcome_code BETWEEN 1 AND 8),
          semantic_fingerprint BLOB
            CHECK(
              semantic_fingerprint IS NULL OR (
                typeof(semantic_fingerprint) = 'blob'
                AND length(semantic_fingerprint) = 32
              )
            ),
          receipt_sha256 BLOB
            CHECK(
              receipt_sha256 IS NULL OR (
                typeof(receipt_sha256) = 'blob'
                AND length(receipt_sha256) = 32
              )
            ),
          diagnostic_sha256 BLOB
            CHECK(
              diagnostic_sha256 IS NULL OR (
                typeof(diagnostic_sha256) = 'blob'
                AND length(diagnostic_sha256) = 32
              )
            ),
          span_ordinal INTEGER
            CHECK(
              span_ordinal IS NULL OR (
                typeof(span_ordinal) = 'integer' AND span_ordinal >= 1
              )
            ),
          PRIMARY KEY(audit_case_key, ordinal),
          UNIQUE(audit_case_key, invocation_id),
          FOREIGN KEY(audit_case_key)
            REFERENCES trade_lifecycle_attempt_audit_heads(audit_case_key),
          CHECK(
            (
              outcome_code IN (1, 2)
              AND semantic_fingerprint IS NOT NULL
              AND receipt_sha256 IS NOT NULL
              AND diagnostic_sha256 IS NULL
              AND span_ordinal IS NOT NULL
            ) OR (
              outcome_code IN (3, 4, 5, 6, 7, 8)
              AND semantic_fingerprint IS NULL
              AND receipt_sha256 IS NULL
              AND diagnostic_sha256 IS NOT NULL
              AND span_ordinal IS NULL
            )
          )
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_lifecycle_receipt_blobs (
          receipt_sha256 BLOB PRIMARY KEY
            CHECK(typeof(receipt_sha256) = 'blob' AND length(receipt_sha256) = 32),
          codec TEXT NOT NULL CHECK(codec = 'zlib'),
          codec_version INTEGER NOT NULL
            CHECK(typeof(codec_version) = 'integer' AND codec_version = 1),
          uncompressed_bytes INTEGER NOT NULL
            CHECK(typeof(uncompressed_bytes) = 'integer' AND uncompressed_bytes >= 0),
          compressed_bytes INTEGER NOT NULL
            CHECK(typeof(compressed_bytes) = 'integer' AND compressed_bytes >= 1),
          compressed_payload BLOB NOT NULL CHECK(typeof(compressed_payload) = 'blob'),
          created_at_ms INTEGER NOT NULL
            CHECK(typeof(created_at_ms) = 'integer' AND created_at_ms >= 1),
          CHECK(compressed_bytes = length(compressed_payload))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_lifecycle_observation_spans (
          audit_case_key INTEGER NOT NULL CHECK(typeof(audit_case_key) = 'integer'),
          span_ordinal INTEGER NOT NULL
            CHECK(typeof(span_ordinal) = 'integer' AND span_ordinal >= 1),
          semantic_schema TEXT NOT NULL CHECK(semantic_schema != ''),
          semantic_fingerprint BLOB NOT NULL
            CHECK(
              typeof(semantic_fingerprint) = 'blob'
              AND length(semantic_fingerprint) = 32
            ),
          first_evidence_id TEXT NOT NULL,
          first_evidence_receipt_sha256 BLOB NOT NULL
            CHECK(
              typeof(first_evidence_receipt_sha256) = 'blob'
              AND length(first_evidence_receipt_sha256) = 32
            ),
          first_success_ordinal INTEGER NOT NULL
            CHECK(typeof(first_success_ordinal) = 'integer' AND first_success_ordinal >= 1),
          first_success_at_ms INTEGER NOT NULL
            CHECK(typeof(first_success_at_ms) = 'integer' AND first_success_at_ms >= 1),
          last_success_ordinal INTEGER NOT NULL
            CHECK(
              typeof(last_success_ordinal) = 'integer'
              AND last_success_ordinal >= first_success_ordinal
            ),
          last_success_at_ms INTEGER NOT NULL
            CHECK(
              typeof(last_success_at_ms) = 'integer'
              AND last_success_at_ms >= first_success_at_ms
            ),
          successful_observation_count INTEGER NOT NULL
            CHECK(
              typeof(successful_observation_count) = 'integer'
              AND successful_observation_count >= 1
            ),
          intervening_failed_attempt_count INTEGER NOT NULL
            CHECK(
              typeof(intervening_failed_attempt_count) = 'integer'
              AND intervening_failed_attempt_count >= 0
            ),
          closed_chain_sha256 BLOB
            CHECK(
              closed_chain_sha256 IS NULL OR (
                typeof(closed_chain_sha256) = 'blob'
                AND length(closed_chain_sha256) = 32
              )
            ),
          last_receipt_sha256 BLOB
            CHECK(
              last_receipt_sha256 IS NULL OR (
                typeof(last_receipt_sha256) = 'blob'
                AND length(last_receipt_sha256) = 32
              )
            ),
          closed_at_ms INTEGER
            CHECK(
              closed_at_ms IS NULL OR (
                typeof(closed_at_ms) = 'integer'
                AND closed_at_ms >= last_success_at_ms
              )
            ),
          PRIMARY KEY(audit_case_key, span_ordinal),
          FOREIGN KEY(audit_case_key)
            REFERENCES trade_lifecycle_attempt_audit_heads(audit_case_key),
          FOREIGN KEY(first_evidence_id)
            REFERENCES trade_lifecycle_evidence(evidence_id),
          FOREIGN KEY(last_receipt_sha256)
            REFERENCES trade_lifecycle_receipt_blobs(receipt_sha256),
          CHECK(
            (closed_chain_sha256 IS NULL AND closed_at_ms IS NULL)
            OR (closed_chain_sha256 IS NOT NULL AND closed_at_ms IS NOT NULL)
          )
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trade_lifecycle_observation_spans_last_receipt
        ON trade_lifecycle_observation_spans(last_receipt_sha256)
        """
    )
    for operation in ("INSERT", "UPDATE OF audit_case_key, first_evidence_id"):
        suffix = "insert" if operation == "INSERT" else "update"
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS
            trg_trade_lifecycle_observation_spans_evidence_case_{suffix}
            BEFORE {operation} ON trade_lifecycle_observation_spans
            WHEN NOT EXISTS (
              SELECT 1
              FROM trade_lifecycle_attempt_audit_heads AS audit_head
              JOIN trade_lifecycle_evidence AS evidence
                ON evidence.evidence_id = NEW.first_evidence_id
              WHERE audit_head.audit_case_key = NEW.audit_case_key
                AND evidence.case_id = audit_head.case_id
            )
            BEGIN
              SELECT RAISE(ABORT, 'lifecycle observation span evidence case mismatch');
            END
            """
        )


def initialize_ledger_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA recursive_triggers=ON")
    row = conn.execute("PRAGMA foreign_keys").fetchone()
    enabled = int(row[0]) if row is not None else 0
    if enabled != 1:
        raise RuntimeError("SQLite foreign key enforcement is required for the option ledger")
    return conn


class SQLiteOptionPositionsRepository:
    def __init__(self, db_path: Path):
        self.db_path = private_path(db_path)
        self.data_config_path: Path | None = None
        self.bootstrap_status = "not_started"
        self.bootstrap_message: str | None = None
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = connect_private_sqlite(self.db_path)
        initialize_ledger_connection(conn)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        secure_sqlite_artifacts(self.db_path)
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
                secure_sqlite_artifacts(self.db_path)

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

    def backfill_position_lot_contract_columns(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        updated = 0
        with self._optional_conn(conn, commit=True) as active_conn:
            updated = self._backfill_position_lot_contract_columns(active_conn)
        return updated

    def _backfill_position_lot_contract_columns(self, conn: sqlite3.Connection) -> int:
        updated = 0
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
            updated += 1
        return updated

    def backfill_position_projection_accounts(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, int]:
        """Explicitly backfill normalized accounts after validating every row."""

        with self._optional_conn(conn, commit=True) as active_conn:
            event_updates: list[tuple[str, str]] = []
            for row in active_conn.execute("SELECT event_id, account, event_json FROM trade_events ORDER BY event_id"):
                try:
                    payload = json.loads(str(row["event_json"] or "{}"))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"trade event JSON is invalid: event_id={row['event_id']}") from exc
                contract_key = payload.get("contract_key") if isinstance(payload, dict) else None
                account = str(
                    (contract_key.get("account") if isinstance(contract_key, dict) else None)
                    or (payload.get("account") if isinstance(payload, dict) else None)
                    or ""
                ).strip()
                if not account or account != account.lower():
                    raise ValueError(f"trade event account cannot be normalized: event_id={row['event_id']}")
                stored = str(row["account"] or "").strip()
                if stored and stored != account:
                    raise ValueError(f"trade event account conflicts with JSON: event_id={row['event_id']}")
                if not stored:
                    event_updates.append((account, str(row["event_id"])))

            lot_updates: list[tuple[str, str]] = []
            for row in active_conn.execute(
                "SELECT record_id, account, fields_json FROM position_lots ORDER BY record_id"
            ):
                try:
                    fields = json.loads(str(row["fields_json"] or "{}"))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"position lot JSON is invalid: record_id={row['record_id']}") from exc
                account = str(fields.get("account") if isinstance(fields, dict) else "").strip()
                if not account or account != account.lower():
                    raise ValueError(f"position lot account cannot be normalized: record_id={row['record_id']}")
                stored = str(row["account"] or "").strip()
                if stored and stored != account:
                    raise ValueError(f"position lot account conflicts with JSON: record_id={row['record_id']}")
                if not stored:
                    lot_updates.append((account, str(row["record_id"])))

            active_conn.executemany(
                "UPDATE trade_events SET account = ? WHERE event_id = ?",
                event_updates,
            )
            active_conn.executemany(
                "UPDATE position_lots SET account = ? WHERE record_id = ?",
                lot_updates,
            )
        return {
            "trade_events_updated": len(event_updates),
            "position_lots_updated": len(lot_updates),
        }

    def backfill_trade_event_pagination(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Run the controlled bounded-memory pagination backfill."""

        with self._optional_conn(conn, commit=True) as active_conn:
            return _backfill_trade_event_pagination_schema(active_conn)

    def build_position_projection_indexes(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[str, ...]:
        """Explicitly build normalized indexes for an already populated store."""

        definitions = (
            (
                "idx_trade_events_trade_time",
                "CREATE INDEX IF NOT EXISTS idx_trade_events_trade_time "
                "ON trade_events(trade_time_ms, event_id)",
            ),
            (
                "idx_trade_events_account_time",
                "CREATE INDEX IF NOT EXISTS idx_trade_events_account_time "
                "ON trade_events(account, trade_time_ms, event_id)",
            ),
            (
                "idx_position_lots_account_expiration",
                "CREATE INDEX IF NOT EXISTS idx_position_lots_account_expiration "
                "ON position_lots(account, expiration, record_id)",
            ),
            (
                "idx_position_lots_account_record",
                "CREATE INDEX IF NOT EXISTS idx_position_lots_account_record ON position_lots(account, record_id)",
            ),
        )
        with self._optional_conn(conn, commit=True) as active_conn:
            before = {
                str(row["name"]) for row in active_conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
            }
            for _name, create_sql in definitions:
                active_conn.execute(create_sql)
        return tuple(name for name, _sql in definitions if name not in before)

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
        account, market, position_effect = _trade_event_query_projections(
            encoded.event_json
        )
        ts = int(now_ms())
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                """
                SELECT account, event_json, ingest_seq, market, position_effect
                FROM trade_events
                WHERE event_id = ?
                """,
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
                if (
                    str(existing["account"] or "").strip() != account
                    or existing["ingest_seq"] is None
                    or str(existing["market"] or "").strip().upper() != market
                    or str(existing["position_effect"] or "").strip().lower()
                    != position_effect
                ):
                    raise ValueError(
                        "existing trade event pagination projection is incomplete: "
                        f"event_id={encoded.event_id}"
                    )
                return False
            active_conn.execute(
                """
                UPDATE trade_event_ingest_sequence
                SET last_value = last_value + 1
                WHERE singleton_id = 1
                """
            )
            sequence_row = active_conn.execute(
                """
                SELECT last_value
                FROM trade_event_ingest_sequence
                WHERE singleton_id = 1
                """
            ).fetchone()
            if sequence_row is None:
                raise RuntimeError("trade event ingest sequence allocator is unavailable")
            active_conn.execute(
                """
                INSERT INTO trade_events (
                  event_id, account, event_json, trade_time_ms,
                  created_at_ms, updated_at_ms, ingest_seq, market,
                  position_effect
                ) VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    encoded.event_id,
                    account,
                    encoded.event_json,
                    encoded.event_time_ms,
                    ts,
                    ts,
                    int(sequence_row["last_value"]),
                    market,
                    position_effect,
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

    def list_trade_events_page(
        self,
        *,
        limit: int = 10,
        snapshot_max_ingest_seq: int | None = None,
        last_trade_time_ms: int | None = None,
        last_event_id: str | None = None,
        account: str | None = None,
        broker: str | None = None,
        symbol: str | None = None,
        option_type: str | None = None,
        strike: float | None = None,
        expiration_ymd: str | None = None,
        market: str | None = None,
        position_effect: str | None = None,
        authorized_accounts: Sequence[str] = (),
        include_total: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        page_limit = int(limit)
        if not 1 <= page_limit <= 20:
            raise ValueError("events limit must be between 1 and 20")
        normalized_market = str(market or "").strip().upper()
        if normalized_market not in {"US", "HK"}:
            raise ValueError("events market must be US or HK")
        if (last_trade_time_ms is None) != (last_event_id is None):
            raise ValueError("events keyset boundary must be complete")
        if snapshot_max_ingest_seq is not None and int(snapshot_max_ingest_seq) < 0:
            raise ValueError("events snapshot boundary must be non-negative")
        authority_accounts = tuple(
            sorted(
                {
                    str(item or "").strip().lower()
                    for item in authorized_accounts
                    if str(item or "").strip()
                }
            )
        )
        if not authority_accounts:
            raise ValueError("events authority account scope is required")
        normalized_account = str(account or "").strip().lower() or None
        if normalized_account and normalized_account not in authority_accounts:
            raise ValueError("events account is outside the authority scope")

        owned = conn is None
        with self._optional_conn(conn) as active_conn:
            if owned:
                active_conn.execute("BEGIN DEFERRED")
            elif not active_conn.in_transaction:
                raise ValueError(
                    "events page connection must already own a transaction"
                )
            if not _trade_event_pagination_schema_ready(active_conn):
                raise TradeEventPaginationUnavailable(
                    "trade event pagination migration is required"
                )
            fence = snapshot_max_ingest_seq
            if fence is None:
                row = active_conn.execute(
                    "SELECT COALESCE(MAX(ingest_seq), 0) AS max_seq FROM trade_events"
                ).fetchone()
                fence = int(row["max_seq"] if row is not None else 0)

            base_clauses = ["ingest_seq <= ?", "market = ?"]
            base_params: list[Any] = [int(fence), normalized_market]
            if normalized_account:
                base_clauses.append("account = ?")
                base_params.append(normalized_account)
            else:
                placeholders = ", ".join("?" for _ in authority_accounts)
                base_clauses.append(f"account IN ({placeholders})")
                base_params.extend(authority_accounts)
            if position_effect:
                base_clauses.append("position_effect = ?")
                base_params.append(str(position_effect).strip().lower())
            if broker:
                base_clauses.append(
                    "json_extract(event_json, '$.contract_key.broker') = ?"
                )
                base_params.append(str(broker))
            if symbol:
                base_clauses.append(
                    "json_extract(event_json, '$.contract_key.underlying_symbol') = ?"
                )
                base_params.append(str(symbol).strip().upper())
            if option_type:
                base_clauses.append(
                    "json_extract(event_json, '$.contract_key.option_type') = ?"
                )
                base_params.append(str(option_type).strip().lower())
            if expiration_ymd:
                base_clauses.append(
                    "json_extract(event_json, '$.contract_key.expiration_ymd') = ?"
                )
                base_params.append(str(expiration_ymd).strip())
            if strike is not None:
                base_clauses.append(
                    "ABS(CAST(json_extract(event_json, '$.contract_key.strike') AS REAL) - ?) < 1e-9"
                )
                base_params.append(float(strike))

            page_clauses = list(base_clauses)
            page_params = list(base_params)
            if last_trade_time_ms is not None and last_event_id is not None:
                page_clauses.append(
                    "(trade_time_ms, event_id) < (?, ?)"
                )
                page_params.extend(
                    [int(last_trade_time_ms), str(last_event_id)]
                )
            page_where = " AND ".join(page_clauses)
            rows = active_conn.execute(
                f"""
                SELECT event_id, event_json, trade_time_ms
                FROM trade_events
                WHERE {page_where}
                ORDER BY trade_time_ms DESC, event_id DESC
                LIMIT ?
                """,
                (*page_params, page_limit + 1),
            ).fetchall()

            total = None
            if include_total:
                base_where = " AND ".join(base_clauses)
                count_row = active_conn.execute(
                    f"SELECT COUNT(*) AS row_count FROM trade_events WHERE {base_where}",
                    base_params,
                ).fetchone()
                total = int(count_row["row_count"] if count_row is not None else 0)

        page = rows[:page_limit]
        return {
            "rows": [
                trade_event_application_payload(json.loads(str(row["event_json"])))
                for row in page
            ],
            "snapshot_max_ingest_seq": int(fence),
            "has_more": len(rows) > page_limit,
            "total_count": total,
            "last_trade_time_ms": (
                int(page[-1]["trade_time_ms"]) if page else None
            ),
            "last_event_id": str(page[-1]["event_id"]) if page else None,
        }

    def list_position_projection_event_rows(
        self,
        *,
        after: tuple[int, str] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        with self._optional_conn(conn) as active_conn:
            if after is None:
                rows = active_conn.execute(
                    """
                    SELECT event_id, account, event_json, trade_time_ms
                    FROM trade_events
                    ORDER BY trade_time_ms ASC, event_id ASC
                    """
                ).fetchall()
            else:
                rows = active_conn.execute(
                    """
                    SELECT event_id, account, event_json, trade_time_ms
                    FROM trade_events
                    WHERE trade_time_ms > ?
                       OR (trade_time_ms = ? AND event_id > ?)
                    ORDER BY trade_time_ms ASC, event_id ASC
                    """,
                    (int(after[0]), int(after[0]), str(after[1])),
                ).fetchall()
        return [
            {
                "event_id": str(row["event_id"]),
                "account": row["account"],
                "event_json": str(row["event_json"]),
                "trade_time_ms": int(row["trade_time_ms"]),
            }
            for row in rows
        ]

    def get_trade_events_by_ids(
        self,
        event_ids: Sequence[str],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        normalized = tuple(dict.fromkeys(str(item or "").strip() for item in event_ids))
        if not normalized or any(not item for item in normalized):
            return []
        placeholders = ",".join("?" for _item in normalized)
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT event_json
                FROM trade_events
                WHERE event_id IN ({placeholders})
                ORDER BY trade_time_ms ASC, event_id ASC
                """,
                normalized,
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
        account = str(event.get("account") or "").strip()
        if not account or account != account.lower():
            raise ValueError("assigned stock event requires lowercase account")
        try:
            trade_time_ms = int(event.get("trade_time_ms") or event.get("event_time_ms") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("assigned stock event requires numeric trade_time_ms") from exc
        if trade_time_ms <= 0:
            raise ValueError("assigned stock event requires trade_time_ms > 0")
        payload = dict(event)
        payload["stock_event_id"] = stock_event_id
        payload["account"] = account
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
                  stock_event_id, account, event_json, trade_time_ms,
                  created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (stock_event_id, account, event_json, trade_time_ms, ts, ts),
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

    def list_assigned_stock_events_for_account(
        self,
        account: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        account_value = str(account or "").strip()
        if not account_value or account_value != account_value.lower():
            raise ValueError("assigned stock account must be lowercase")
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT event_json
                FROM assigned_stock_events
                WHERE account = ?
                ORDER BY trade_time_ms ASC, stock_event_id ASC
                """,
                (account_value,),
            ).fetchall()
        return [_json_object(row["event_json"]) for row in rows]

    def append_wheel_event_once(
        self,
        event: Mapping[str, Any],
        *,
        conn: sqlite3.Connection,
    ) -> bool:
        if conn is None or not conn.in_transaction:
            raise ValueError("wheel event append requires an active transaction")
        payload = normalize_wheel_event(event)
        existing = conn.execute(
            "SELECT payload_hash FROM wheel_events WHERE event_id = ?",
            (payload["event_id"],),
        ).fetchone()
        if existing is not None:
            if str(existing["payload_hash"] or "") != payload["payload_hash"]:
                raise ValueError(
                    f"wheel event conflict for event_id={payload['event_id']}"
                )
            return False
        conn.execute(
            """
            INSERT INTO wheel_events (
              event_id, account, stock_lot_id, event_type,
              occurred_at_ms, recorded_at_ms, intent_id,
              source_trade_event_id, payload_json, payload_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["event_id"],
                payload["account"],
                payload["stock_lot_id"],
                payload["event_type"],
                payload["occurred_at_ms"],
                payload["recorded_at_ms"],
                payload["intent_id"],
                payload["source_trade_event_id"],
                _json_text(payload["payload"]),
                payload["payload_hash"],
            ),
        )
        return True

    def list_wheel_events(
        self,
        *,
        account: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        account_value = str(account or "").strip().lower()
        with self._optional_conn(conn) as active_conn:
            if account_value:
                rows = active_conn.execute(
                    """
                    SELECT * FROM wheel_events
                    WHERE account = ?
                    ORDER BY occurred_at_ms ASC, event_id ASC
                    """,
                    (account_value,),
                ).fetchall()
            else:
                rows = active_conn.execute(
                    """
                    SELECT * FROM wheel_events
                    ORDER BY occurred_at_ms ASC, event_id ASC
                    """
                ).fetchall()
        return [
            normalize_wheel_event(
                {
                    "event_id": row["event_id"],
                    "account": row["account"],
                    "stock_lot_id": row["stock_lot_id"],
                    "event_type": row["event_type"],
                    "occurred_at_ms": row["occurred_at_ms"],
                    "recorded_at_ms": row["recorded_at_ms"],
                    "intent_id": row["intent_id"],
                    "source_trade_event_id": row["source_trade_event_id"],
                    "payload": _json_object(row["payload_json"]),
                    "payload_hash": row["payload_hash"],
                }
            )
            for row in rows
        ]

    def replace_position_lots(
        self,
        records: Sequence[PositionLotRecord],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        return self.apply_position_lot_diff(records, conn=conn).lot_count

    def apply_position_lot_diff(
        self,
        records: Sequence[PositionLotRecord],
        *,
        remove_missing: bool = True,
        conn: sqlite3.Connection | None = None,
    ) -> PositionLotDiff:
        desired: dict[
            str,
            tuple[str, str, str, str | None, int | None, float | None, float | None],
        ] = {}
        for record in records:
            values = _position_lot_storage_values(record)
            record_id = values[0]
            if record_id in desired:
                raise ValueError(f"duplicate position lot record_id: {record_id}")
            desired[record_id] = values

        added = 0
        changed = 0
        removed = 0
        unchanged = 0
        all_accounts = {values[1] for values in desired.values()}
        touched_accounts: set[str] = set()
        ts = int(now_ms())
        with self._optional_conn(conn, commit=True) as active_conn:
            if remove_missing:
                current_rows = active_conn.execute(
                    """
                    SELECT record_id, account, fields_json, source_event_id,
                           expiration, strike, multiplier
                    FROM position_lots
                    ORDER BY record_id ASC
                    """
                ).fetchall()
                prior_lot_count = len(current_rows)
            else:
                record_ids = tuple(desired)
                if record_ids:
                    placeholders = ",".join("?" for _item in record_ids)
                    current_rows = active_conn.execute(
                        f"""
                        SELECT record_id, account, fields_json, source_event_id,
                               expiration, strike, multiplier
                        FROM position_lots
                        WHERE record_id IN ({placeholders})
                        ORDER BY record_id ASC
                        """,
                        record_ids,
                    ).fetchall()
                else:
                    current_rows = []
                head_rows = active_conn.execute(
                    """
                    SELECT account, lot_count
                    FROM position_projection_heads
                    ORDER BY account ASC
                    """
                ).fetchall()
                all_accounts.update(str(row["account"]) for row in head_rows)
                prior_lot_count = sum(int(row["lot_count"] or 0) for row in head_rows)
            current_by_id = {str(row["record_id"]): row for row in current_rows}

            for record_id, row in current_by_id.items():
                old_account = str(row["account"] or "").strip()
                if not old_account:
                    raw_fields = json.loads(str(row["fields_json"]) or "{}")
                    old_account = str(raw_fields.get("account") if isinstance(raw_fields, dict) else "").strip()
                if old_account:
                    all_accounts.add(old_account)
                if record_id in desired or not remove_missing:
                    continue
                active_conn.execute(
                    "DELETE FROM position_lots WHERE record_id = ?",
                    (record_id,),
                )
                removed += 1
                if old_account:
                    touched_accounts.add(old_account)

            for record_id, values in desired.items():
                (
                    _record_id,
                    account,
                    fields_json,
                    source_event_id,
                    expiration_ms,
                    strike,
                    multiplier,
                ) = values
                current = current_by_id.get(record_id)
                if current is None:
                    active_conn.execute(
                        """
                        INSERT INTO position_lots (
                          record_id, account, fields_json, source_event_id,
                          expiration, strike, multiplier, updated_at_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (*values, ts),
                    )
                    added += 1
                    touched_accounts.add(account)
                    continue

                raw_current_fields_json = str(current["fields_json"] or "{}")
                current_fields_json = (
                    raw_current_fields_json
                    if raw_current_fields_json == fields_json
                    else _canonical_existing_fields_json(raw_current_fields_json)
                )
                public_changed = current_fields_json != fields_json or current["source_event_id"] != source_event_id
                scalar_conflict = any(
                    current[column] is not None and not _storage_scalar_matches(current[column], desired_value)
                    for column, desired_value in (
                        ("expiration", expiration_ms),
                        ("strike", strike),
                        ("multiplier", multiplier),
                    )
                )
                if not public_changed and not scalar_conflict:
                    # Explicit migration owns historical sidecar backfill. Existing
                    # public bytes remain unchanged even if a legacy scalar is null.
                    unchanged += 1
                    continue

                old_fields = json.loads(str(current["fields_json"]) or "{}")
                old_account = str(
                    current["account"] or (old_fields.get("account") if isinstance(old_fields, dict) else "") or ""
                ).strip()
                active_conn.execute(
                    """
                    UPDATE position_lots
                    SET account = ?, fields_json = ?, source_event_id = ?,
                        expiration = ?, strike = ?, multiplier = ?, updated_at_ms = ?
                    WHERE record_id = ?
                    """,
                    (
                        account,
                        fields_json,
                        source_event_id,
                        expiration_ms,
                        strike,
                        multiplier,
                        ts,
                        record_id,
                    ),
                )
                changed += 1
                touched_accounts.add(account)
                if old_account:
                    touched_accounts.add(old_account)

        final_lot_count = len(desired) if remove_missing else prior_lot_count + added
        unchanged_count = (
            unchanged
            if remove_missing
            else max(0, final_lot_count - added - changed)
        )
        return PositionLotDiff(
            added=added,
            changed=changed,
            removed=removed,
            unchanged=unchanged_count,
            accounts=tuple(sorted(all_accounts)),
            touched_accounts=tuple(sorted(touched_accounts)),
        )

    def position_projection_column_contract(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, dict[str, tuple[str, ...]]]:
        with self._optional_conn(conn) as active_conn:
            return _position_projection_column_contract(active_conn)

    def position_projection_schema_cookie(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        with self._optional_conn(conn) as active_conn:
            return _projection_schema_cookie(active_conn)

    def position_projection_indexes_ready(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        required = {
            "idx_trade_events_trade_time",
            "idx_trade_events_account_time",
            "idx_position_lots_account_expiration",
            "idx_position_lots_account_record",
        }
        with self._optional_conn(conn) as active_conn:
            present = {
                str(row["name"])
                for row in active_conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
            }
        return required.issubset(present)

    def position_projection_normalized_columns_ready(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        with self._optional_conn(conn) as active_conn:
            event_problem = active_conn.execute(
                """
                SELECT 1
                FROM trade_events
                WHERE account IS NULL
                   OR account = ''
                   OR account != lower(account)
                   OR account != coalesce(
                        nullif(trim(CAST(json_extract(
                          event_json, '$.contract_key.account'
                        ) AS TEXT)), ''),
                        trim(CAST(json_extract(event_json, '$.account') AS TEXT))
                      )
                LIMIT 1
                """
            ).fetchone()
            lot_problem = active_conn.execute(
                """
                SELECT 1
                FROM position_lots
                WHERE account IS NULL
                   OR account = ''
                   OR account != lower(account)
                   OR account != trim(CAST(
                        json_extract(fields_json, '$.account') AS TEXT
                      ))
                   OR (
                        json_extract(fields_json, '$.option_type') IN ('put', 'call')
                        AND (expiration IS NULL OR strike IS NULL OR multiplier IS NULL)
                   )
                LIMIT 1
                """
            ).fetchone()
        return event_problem is None and lot_problem is None

    def list_position_projection_accounts(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[str, ...]:
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT account FROM position_projection_heads
                ORDER BY account ASC
                """
            ).fetchall()
        return tuple(str(row["account"]) for row in rows if str(row["account"] or "").strip())

    def position_projection_account_snapshot(
        self,
        account: str,
        *,
        include_records: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> PositionProjectionAccountSnapshot:
        account_value = str(account or "").strip()
        if not account_value or account_value != account_value.lower():
            raise ValueError("position projection account must be lowercase")
        with self._optional_conn(conn) as active_conn:
            cursor = active_conn.execute(
                """
                SELECT record_id, fields_json, expiration, strike, multiplier
                FROM position_lots
                WHERE account = ?
                ORDER BY record_id ASC
                """,
                (account_value,),
            )
            retained: list[dict[str, Any]] = []
            lot_count = 0

            def _ordered_rows():
                nonlocal lot_count
                for row in cursor:
                    record = position_lot_row_to_record(row)
                    lot_count += 1
                    if include_records:
                        retained.append(record)
                    yield record

            fingerprint = ordered_position_lots_fingerprint(_ordered_rows())
        return PositionProjectionAccountSnapshot(
            account=account_value,
            fingerprint=fingerprint,
            lot_count=lot_count,
            records=tuple(retained),
        )

    def publish_full_position_projection_heads(
        self,
        *,
        implementation_fingerprint: str,
        known_accounts: Sequence[str],
        changed_accounts: Sequence[str],
        full_verified: bool = True,
        publish_source_implementation: bool = True,
        readiness_prevalidated: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[int, bool, str | None]:
        fingerprint = str(implementation_fingerprint or "").strip()
        if len(fingerprint) != 64:
            raise ValueError("projector implementation fingerprint is required")
        with self._optional_conn(conn, commit=True) as active_conn:
            source = active_conn.execute(
                """
                SELECT source_generation, sqlite_schema_cookie
                FROM position_projection_source_state
                WHERE singleton_id = 1
                """
            ).fetchone()
            if source is None:
                raise RuntimeError("position projection source state is missing")
            source_generation = int(source["source_generation"])
            schema_cookie = _projection_schema_cookie(active_conn)
            ready = bool(readiness_prevalidated) or _position_projection_column_contract_is_closed(
                active_conn
            )
            reason: str | None = None
            if readiness_prevalidated:
                pass
            elif not ready:
                reason = "column_contract_open"
            elif not self.position_projection_indexes_ready(conn=active_conn):
                ready = False
                reason = "normalized_indexes_missing"
            elif not self.position_projection_normalized_columns_ready(conn=active_conn):
                ready = False
                reason = "normalized_columns_incomplete"

            accounts = set(self.list_position_projection_accounts(conn=active_conn))
            accounts.update(str(item or "").strip() for item in known_accounts)
            accounts.update(str(item or "").strip() for item in changed_accounts)
            accounts = {account for account in accounts if account and account == account.lower()}
            ts = int(now_ms())
            total = 0
            changed = {str(item or "").strip() for item in changed_accounts}
            for account in sorted(accounts):
                head = active_conn.execute(
                    """
                    SELECT lots_generation, built_lots_generation,
                           projection_fingerprint, lot_count, status,
                           projector_schema, projector_implementation_fingerprint
                    FROM position_projection_heads
                    WHERE account = ?
                    """,
                    (account,),
                ).fetchone()
                can_reuse = (
                    account not in changed
                    and head is not None
                    and str(head["status"] or "") == "trusted"
                    and str(head["projector_schema"] or "") == POSITION_PROJECTION_SCHEMA
                    and str(head["projector_implementation_fingerprint"] or "") == fingerprint
                    and head["built_lots_generation"] is not None
                    and int(head["lots_generation"]) == int(head["built_lots_generation"])
                    and bool(str(head["projection_fingerprint"] or ""))
                )
                if can_reuse:
                    account_fingerprint = str(head["projection_fingerprint"])
                    lot_count = int(head["lot_count"])
                else:
                    snapshot = self.position_projection_account_snapshot(
                        account,
                        conn=active_conn,
                    )
                    account_fingerprint = snapshot.fingerprint
                    lot_count = snapshot.lot_count
                total += lot_count
                lots_generation = int(head["lots_generation"] or 0) if head else 0
                active_conn.execute(
                    """
                    INSERT INTO position_projection_heads (
                      account, lots_generation, built_source_generation,
                      built_lots_generation, projection_fingerprint, lot_count,
                      projector_schema, projector_implementation_fingerprint,
                      status, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account) DO UPDATE SET
                      built_source_generation = excluded.built_source_generation,
                      built_lots_generation = excluded.built_lots_generation,
                      projection_fingerprint = excluded.projection_fingerprint,
                      lot_count = excluded.lot_count,
                      projector_schema = excluded.projector_schema,
                      projector_implementation_fingerprint =
                        excluded.projector_implementation_fingerprint,
                      status = excluded.status,
                      updated_at_ms = excluded.updated_at_ms
                    """,
                    (
                        account,
                        lots_generation,
                        source_generation,
                        lots_generation,
                        account_fingerprint,
                        lot_count,
                        POSITION_PROJECTION_SCHEMA,
                        fingerprint,
                        "trusted" if ready else "untrusted",
                        ts,
                    ),
                )
            active_conn.execute(
                """
                UPDATE position_projection_source_state
                SET projector_schema = ?,
                    projector_implementation_fingerprint = CASE
                      WHEN ? THEN ?
                      ELSE projector_implementation_fingerprint
                    END,
                    sqlite_schema_cookie = CASE
                      WHEN ? THEN ?
                      ELSE sqlite_schema_cookie
                    END,
                    last_full_verified_source_generation = CASE
                      WHEN ? THEN ?
                      ELSE last_full_verified_source_generation
                    END,
                    updated_at_ms = ?
                WHERE singleton_id = 1
                """,
                (
                    POSITION_PROJECTION_SCHEMA,
                    1 if publish_source_implementation else 0,
                    fingerprint,
                    1 if publish_source_implementation else 0,
                    schema_cookie,
                    1 if full_verified else 0,
                    source_generation,
                    ts,
                ),
            )
        return total, ready, reason

    def read_position_projection_account_metadata(
        self,
        account: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        account_value = str(account or "").strip()
        if not account_value or account_value != account_value.lower():
            raise ValueError("position projection account must be lowercase")
        with self._optional_conn(conn) as active_conn:
            source = active_conn.execute(
                "SELECT * FROM position_projection_source_state WHERE singleton_id = 1"
            ).fetchone()
            head = active_conn.execute(
                "SELECT * FROM position_projection_heads WHERE account = ?",
                (account_value,),
            ).fetchone()
            cookie = _projection_schema_cookie(active_conn)
        return {
            "source": dict(source) if source is not None else None,
            "head": dict(head) if head is not None else None,
            "schema_cookie": cookie,
        }

    def read_position_projection_source_state(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                "SELECT * FROM position_projection_source_state WHERE singleton_id = 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("position projection source state is missing")
        return dict(row)

    def set_position_projection_checkpoint_mode(
        self,
        mode: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        value = str(mode or "").strip().lower()
        if value not in {"disabled", "enabled", "untrusted"}:
            raise ValueError("checkpoint mode must be disabled, enabled, or untrusted")
        with self._optional_conn(conn, commit=True) as active_conn:
            active_conn.execute(
                """
                UPDATE position_projection_source_state
                SET checkpoint_mode = ?, updated_at_ms = ?
                WHERE singleton_id = 1
                """,
                (value, int(now_ms())),
            )

    def list_position_projection_checkpoints(
        self,
        *,
        trusted_only: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        where = "WHERE trust_status = 'trusted'" if trusted_only else ""
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT * FROM position_projection_checkpoints
                {where}
                ORDER BY prefix_event_count DESC, created_at_ms DESC,
                         checkpoint_id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def read_newest_trusted_position_projection_checkpoint(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT * FROM position_projection_checkpoints
                WHERE trust_status = 'trusted'
                ORDER BY prefix_event_count DESC, created_at_ms DESC,
                         checkpoint_id DESC
                LIMIT 1
                """
            ).fetchone()
        return dict(row) if row is not None else None

    def insert_position_projection_checkpoint(
        self,
        checkpoint: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        columns = (
            "checkpoint_id",
            "projector_schema",
            "projector_implementation_fingerprint",
            "prefix_event_count",
            "prefix_end_trade_time_ms",
            "prefix_end_event_id",
            "prefix_chain_sha256",
            "source_generation",
            "sqlite_schema_cookie",
            "accumulator_json",
            "accumulator_sha256",
            "diagnostic_count",
            "diagnostic_sha256",
            "state_bytes",
            "trust_status",
            "verification_kind",
            "parent_checkpoint_id",
            "created_at_ms",
            "verified_at_ms",
            "invalidated_at_ms",
            "invalidation_reason",
        )
        if set(checkpoint) != set(columns):
            raise ValueError("position projection checkpoint fields differ from v1 schema")
        placeholders = ",".join("?" for _item in columns)
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                "SELECT * FROM position_projection_checkpoints WHERE checkpoint_id = ?",
                (checkpoint["checkpoint_id"],),
            ).fetchone()
            if existing is not None:
                if checkpoint["verification_kind"] == "full_oracle":
                    mutable_columns = tuple(
                        column for column in columns if column != "checkpoint_id"
                    )
                    active_conn.execute(
                        f"""
                        UPDATE position_projection_checkpoints
                        SET {','.join(f'{column} = ?' for column in mutable_columns)}
                        WHERE checkpoint_id = ?
                        """,
                        (
                            *(checkpoint[column] for column in mutable_columns),
                            checkpoint["checkpoint_id"],
                        ),
                    )
                    return
                immutable = set(columns) - {
                    "verification_kind",
                    "parent_checkpoint_id",
                    "created_at_ms",
                    "verified_at_ms",
                    "trust_status",
                    "invalidated_at_ms",
                    "invalidation_reason",
                }
                if any(existing[column] != checkpoint[column] for column in immutable):
                    raise ValueError("checkpoint id conflicts with immutable payload")
                return
            active_conn.execute(
                f"""
                INSERT INTO position_projection_checkpoints ({','.join(columns)})
                VALUES ({placeholders})
                """,
                tuple(checkpoint[column] for column in columns),
            )

    def invalidate_position_projection_checkpoints(
        self,
        *,
        reason: str,
        checkpoint_ids: Sequence[str] = (),
        mark_mode_untrusted: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        reason_value = str(reason or "").strip()
        if not reason_value:
            raise ValueError("checkpoint invalidation reason is required")
        normalized = tuple(
            dict.fromkeys(str(item or "").strip() for item in checkpoint_ids)
        )
        with self._optional_conn(conn, commit=True) as active_conn:
            ts = int(now_ms())
            if normalized:
                placeholders = ",".join("?" for _item in normalized)
                cursor = active_conn.execute(
                    f"""
                    UPDATE position_projection_checkpoints
                    SET trust_status = 'invalid', invalidated_at_ms = ?,
                        invalidation_reason = ?
                    WHERE trust_status = 'trusted'
                      AND checkpoint_id IN ({placeholders})
                    """,
                    (ts, reason_value, *normalized),
                )
            else:
                cursor = active_conn.execute(
                    """
                    UPDATE position_projection_checkpoints
                    SET trust_status = 'invalid', invalidated_at_ms = ?,
                        invalidation_reason = ?
                    WHERE trust_status = 'trusted'
                    """,
                    (ts, reason_value),
                )
            if mark_mode_untrusted:
                active_conn.execute(
                    """
                    UPDATE position_projection_source_state
                    SET checkpoint_mode = 'untrusted', updated_at_ms = ?
                    WHERE singleton_id = 1
                    """,
                    (ts,),
                )
        return int(cursor.rowcount)

    def prune_position_projection_checkpoints(
        self,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[str, ...]:
        with self._optional_conn(conn, commit=True) as active_conn:
            invalid = active_conn.execute(
                "SELECT checkpoint_id FROM position_projection_checkpoints WHERE trust_status = 'invalid'"
            ).fetchall()
            trusted = active_conn.execute(
                """
                SELECT checkpoint_id, verification_kind
                FROM position_projection_checkpoints
                WHERE trust_status = 'trusted'
                ORDER BY prefix_event_count DESC, created_at_ms DESC,
                         checkpoint_id DESC
                """
            ).fetchall()
            keep = {str(row["checkpoint_id"]) for row in trusted[:2]}
            full = next(
                (
                    str(row["checkpoint_id"])
                    for row in trusted
                    if str(row["verification_kind"]) == "full_oracle"
                ),
                None,
            )
            if full is not None:
                keep.add(full)
            removable = [*invalid, *trusted]
            removed = tuple(
                sorted(
                    str(row["checkpoint_id"])
                    for row in removable
                    if str(row["checkpoint_id"]) not in keep
                )
            )
            if removed:
                placeholders = ",".join("?" for _item in removed)
                active_conn.execute(
                    f"DELETE FROM position_projection_checkpoints WHERE checkpoint_id IN ({placeholders})",
                    removed,
                )
        return removed

    def list_active_position_lots(
        self,
        *,
        account: str,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        account_value = str(account or "").strip().lower()
        if not account_value:
            raise ValueError("position projection account is required")
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT record_id, fields_json, expiration, strike, multiplier
                FROM position_lots
                WHERE account = ?
                  AND json_extract(fields_json, '$.status') = 'open'
                ORDER BY expiration ASC, record_id ASC
                """,
                (account_value,),
            ).fetchall()
        return [position_lot_row_to_record(row) for row in rows]

    def get_position_lots_by_ids(
        self,
        record_ids: Sequence[str],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        normalized = tuple(dict.fromkeys(str(item or "").strip() for item in record_ids))
        if not normalized or any(not item for item in normalized):
            return []
        placeholders = ",".join("?" for _item in normalized)
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT record_id, fields_json, expiration, strike, multiplier
                FROM position_lots
                WHERE record_id IN ({placeholders})
                ORDER BY record_id ASC
                """,
                normalized,
            ).fetchall()
        return [position_lot_row_to_record(row) for row in rows]

    def list_position_lots(self, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT record_id, fields_json, expiration, strike, multiplier
                FROM position_lots
                ORDER BY record_id DESC
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
        account = str(payload.get("account") or "").strip()
        if not account or account != account.lower():
            raise ValueError("trade lifecycle case requires lowercase account")
        payload["account"] = account
        target_lot_ids, target_contracts, target_rows = _normalized_lifecycle_case_targets(
            payload,
            case_id=case_id,
            account=account,
        )
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
                    account,
                    str(payload.get("broker") or "").strip().lower() or None,
                    str(payload.get("symbol") or "").strip().upper(),
                    (str(payload.get("option_type") or "").strip().lower() or None),
                    (str(payload.get("position_side") or "").strip().lower() or None),
                    float(payload["strike"]) if payload.get("strike") is not None else None,
                    (str(payload.get("expiration_ymd") or "").strip() or None),
                    (str(payload.get("contract_key") or "").strip() or None),
                    str(payload.get("status") or "pending").strip().lower(),
                    (str(payload.get("decision_type") or "").strip().lower() or None),
                    json.dumps(list(target_lot_ids), ensure_ascii=False, sort_keys=True),
                    json.dumps(
                        target_contracts,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    (int(payload["observation_start_ms"]) if payload.get("observation_start_ms") is not None else None),
                    int(payload["pending_until_ms"]) if payload.get("pending_until_ms") is not None else None,
                    created_at_ms,
                    ts,
                    raw_json,
                ),
            )
            if changed:
                active_conn.execute(
                    "DELETE FROM trade_lifecycle_case_targets WHERE case_id = ?",
                    (case_id,),
                )
                active_conn.executemany(
                    """
                    INSERT INTO trade_lifecycle_case_targets (
                      case_id, account, target_lot_id, target_contracts
                    ) VALUES (?, ?, ?, ?)
                    """,
                    target_rows,
                )
        return changed

    def get_trade_lifecycle_case(
        self, case_id: str, *, conn: sqlite3.Connection | None = None
    ) -> dict[str, Any] | None:
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

    def list_trade_lifecycle_case_targets_for_lots(
        self,
        *,
        account: str,
        target_lot_ids: Sequence[str],
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        account_value = str(account or "").strip()
        if not account_value or account_value != account_value.lower():
            raise ValueError("lifecycle case target account must be lowercase")
        lot_ids = tuple(dict.fromkeys(str(value or "").strip() for value in target_lot_ids))
        if any(not value for value in lot_ids):
            raise ValueError("lifecycle case target lot id is required")
        if not lot_ids:
            return []
        placeholders = ",".join("?" for _value in lot_ids)
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                f"""
                SELECT case_id, account, target_lot_id, target_contracts
                FROM trade_lifecycle_case_targets
                WHERE account = ? AND target_lot_id IN ({placeholders})
                ORDER BY target_lot_id ASC, case_id ASC
                """,
                (account_value, *lot_ids),
            ).fetchall()
        return [dict(row) for row in rows]

    def read_current_decision_storage_state(
        self,
        account: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, dict[str, Any] | None]:
        account_value = str(account or "").strip()
        if not account_value or account_value != account_value.lower():
            raise ValueError("current decision account must be lowercase")
        with self._optional_conn(conn) as active_conn:
            generation = active_conn.execute(
                """
                SELECT *
                FROM current_decision_input_generations
                WHERE account = ?
                """,
                (account_value,),
            ).fetchone()
            projection = active_conn.execute(
                """
                SELECT *
                FROM current_decision_projections
                WHERE account = ?
                """,
                (account_value,),
            ).fetchone()
        return {
            "generation": dict(generation) if generation is not None else None,
            "projection": dict(projection) if projection is not None else None,
        }

    def read_current_decision_projection_inputs(
        self,
        account: str,
        *,
        conn: sqlite3.Connection | None = None,
        include_identities: bool = True,
    ) -> dict[str, Any]:
        """Read one account's bounded current-state inputs from one snapshot."""
        with self._optional_conn(conn) as active_conn:
            return read_current_decision_projection_inputs_from_conn(
                active_conn,
                account,
                include_identities=include_identities,
            )

    def read_current_decision_projection_fence_inputs(
        self,
        accounts: Sequence[str],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Read only the bounded metadata needed by the publication fence."""

        account_values = tuple(
            sorted({str(value or "").strip() for value in accounts})
        )
        if not account_values or any(
            not value or value != value.lower() for value in account_values
        ):
            raise ValueError("current decision fence accounts must be lowercase")
        placeholders = ",".join("?" for _value in account_values)
        with self._optional_conn(conn) as active_conn:
            source = active_conn.execute(
                "SELECT * FROM position_projection_source_state WHERE singleton_id = 1"
            ).fetchone()
            heads = active_conn.execute(
                f"SELECT * FROM position_projection_heads WHERE account IN ({placeholders})",
                account_values,
            ).fetchall()
            generations = active_conn.execute(
                f"""
                SELECT * FROM current_decision_input_generations
                WHERE account IN ({placeholders})
                """,
                account_values,
            ).fetchall()
            projections = active_conn.execute(
                f"""
                SELECT account, projection_schema,
                  projector_implementation_fingerprint,
                  built_position_source_generation,
                  built_position_lots_generation, position_lots_fingerprint,
                  built_decision_input_generation, built_case_generation,
                  built_evidence_generation, built_allocation_generation,
                  built_source_consumption_generation, built_timing_generation,
                  built_combo_identity_generation, built_assigned_stock_generation
                FROM current_decision_projections
                WHERE account IN ({placeholders})
                """,
                account_values,
            ).fetchall()
        heads_by_account = {str(row["account"]): dict(row) for row in heads}
        generations_by_account = {
            str(row["account"]): dict(row) for row in generations
        }
        projections_by_account = {
            str(row["account"]): dict(row) for row in projections
        }
        return {
            "source": dict(source) if source is not None else None,
            "accounts": {
                account: {
                    "head": heads_by_account.get(account),
                    "generation": generations_by_account.get(account),
                    "projection": projections_by_account.get(account),
                }
                for account in account_values
            },
        }

    def list_current_decision_lifecycle_fact_rows(
        self,
        *,
        account: str,
        target_lot_ids: Sequence[str] = (),
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        """Read operational or currently referenced compact lifecycle facts."""

        account_value = str(account or "").strip()
        if not account_value or account_value != account_value.lower():
            raise ValueError("current decision account must be lowercase")
        lot_ids = tuple(
            dict.fromkeys(
                str(value or "").strip()
                for value in target_lot_ids
                if str(value or "").strip()
            )
        )
        select_sql = """
            SELECT lifecycle_case.case_id, lifecycle_case.account,
                   lifecycle_case.status, lifecycle_case.decision_fact_json,
                   lifecycle_case.decision_fact_sha256,
                   evidence_revision.revision AS evidence_revision,
                   evidence_revision.evidence_count AS evidence_count,
                   admission.semantic_schema AS admitted_semantic_schema,
                   admission.semantic_fingerprint AS admitted_semantic_fingerprint,
                   admission.evidence_id AS admitted_evidence_id
            FROM trade_lifecycle_cases AS lifecycle_case
            LEFT JOIN trade_lifecycle_evidence_revisions AS evidence_revision
              ON evidence_revision.case_id = lifecycle_case.case_id
            LEFT JOIN trade_lifecycle_settlement_admission_heads AS admission
              ON admission.case_id = lifecycle_case.case_id
        """
        rows_by_case: dict[str, sqlite3.Row] = {}
        with self._optional_conn(conn) as active_conn:
            for row in active_conn.execute(
                select_sql
                + """
                WHERE lifecycle_case.account = ?
                  AND lifecycle_case.status IN (
                    'pending', 'waiting_settlement_evidence', 'needs_review',
                    'partially_resolved', 'conflict'
                  )
                ORDER BY lifecycle_case.case_id ASC
                """,
                (account_value,),
            ).fetchall():
                rows_by_case[str(row["case_id"])] = row
            if lot_ids:
                placeholders = ",".join("?" for _value in lot_ids)
                for row in active_conn.execute(
                    select_sql
                    + f"""
                    JOIN trade_lifecycle_case_targets AS target
                      ON target.case_id = lifecycle_case.case_id
                    WHERE target.account = ?
                      AND target.target_lot_id IN ({placeholders})
                    ORDER BY lifecycle_case.case_id ASC
                    """,
                    (account_value, *lot_ids),
                ).fetchall():
                    rows_by_case[str(row["case_id"])] = row
        return [dict(rows_by_case[key]) for key in sorted(rows_by_case)]

    def get_current_decision_lifecycle_fact_state(
        self,
        case_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        case_id_value = str(case_id or "").strip()
        if not case_id_value:
            raise ValueError("current decision lifecycle case id is required")
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT lifecycle_case.case_id, lifecycle_case.account,
                       lifecycle_case.status,
                       lifecycle_case.decision_fact_json,
                       lifecycle_case.decision_fact_sha256,
                       COALESCE(evidence_revision.revision, 0)
                         AS evidence_revision,
                       COALESCE(evidence_revision.evidence_count, 0)
                         AS evidence_count,
                       admission.semantic_schema AS admitted_semantic_schema,
                       admission.semantic_fingerprint
                         AS admitted_semantic_fingerprint,
                       admission.evidence_id AS admitted_evidence_id
                FROM trade_lifecycle_cases AS lifecycle_case
                LEFT JOIN trade_lifecycle_evidence_revisions
                  AS evidence_revision
                  ON evidence_revision.case_id = lifecycle_case.case_id
                LEFT JOIN trade_lifecycle_settlement_admission_heads
                  AS admission
                  ON admission.case_id = lifecycle_case.case_id
                WHERE lifecycle_case.case_id = ?
                """,
                (case_id_value,),
            ).fetchone()
        return dict(row) if row is not None else None

    def update_trade_lifecycle_case_decision_fact(
        self,
        *,
        case_id: str,
        account: str,
        status: str,
        decision_fact_json: str,
        decision_fact_sha256: str,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        case_id_value = str(case_id or "").strip()
        account_value = str(account or "").strip()
        status_value = str(status or "").strip().lower()
        if (
            not case_id_value
            or not account_value
            or account_value != account_value.lower()
            or not status_value
        ):
            raise ValueError("current decision lifecycle fact binding is invalid")
        with self._optional_conn(conn, commit=True) as active_conn:
            row = active_conn.execute(
                """
                SELECT account, status, decision_fact_json,
                       decision_fact_sha256
                FROM trade_lifecycle_cases
                WHERE case_id = ?
                """,
                (case_id_value,),
            ).fetchone()
            if row is None:
                raise ValueError(f"lifecycle case not found: {case_id_value}")
            if row["account"] != account_value or row["status"] != status_value:
                raise ValueError("current decision lifecycle fact binding changed")
            if (
                row["decision_fact_json"] == decision_fact_json
                and row["decision_fact_sha256"] == decision_fact_sha256
            ):
                return False
            cursor = active_conn.execute(
                """
                UPDATE trade_lifecycle_cases
                SET decision_fact_json = ?, decision_fact_sha256 = ?
                WHERE case_id = ? AND account = ? AND status = ?
                """,
                (
                    decision_fact_json,
                    decision_fact_sha256,
                    case_id_value,
                    account_value,
                    status_value,
                ),
            )
            if int(cursor.rowcount or 0) != 1:
                raise ValueError("current decision lifecycle fact write lost ownership")
        return True

    def upsert_current_decision_projection(
        self,
        row: Mapping[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        values = dict(row or {})
        columns = (
            "account",
            "projection_schema",
            "projector_implementation_fingerprint",
            "built_position_source_generation",
            "built_position_lots_generation",
            "position_lots_fingerprint",
            "built_decision_input_generation",
            "built_case_generation",
            "built_evidence_generation",
            "built_allocation_generation",
            "built_source_consumption_generation",
            "built_timing_generation",
            "built_combo_identity_generation",
            "built_assigned_stock_generation",
            "decision_state_fingerprint",
            "payload_sha256",
            "payload_json",
            "updated_at_ms",
        )
        if set(values) != set(columns):
            raise ValueError("current decision projection row shape is invalid")
        with self._optional_conn(conn, commit=True) as active_conn:
            cursor = active_conn.execute(
                """
                INSERT INTO current_decision_projections (
                  account, projection_schema,
                  projector_implementation_fingerprint,
                  built_position_source_generation,
                  built_position_lots_generation, position_lots_fingerprint,
                  built_decision_input_generation, built_case_generation,
                  built_evidence_generation, built_allocation_generation,
                  built_source_consumption_generation, built_timing_generation,
                  built_combo_identity_generation,
                  built_assigned_stock_generation, decision_state_fingerprint,
                  payload_sha256, payload_json, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account) DO UPDATE SET
                  projection_schema = excluded.projection_schema,
                  projector_implementation_fingerprint =
                    excluded.projector_implementation_fingerprint,
                  built_position_source_generation =
                    excluded.built_position_source_generation,
                  built_position_lots_generation =
                    excluded.built_position_lots_generation,
                  position_lots_fingerprint = excluded.position_lots_fingerprint,
                  built_decision_input_generation =
                    excluded.built_decision_input_generation,
                  built_case_generation = excluded.built_case_generation,
                  built_evidence_generation = excluded.built_evidence_generation,
                  built_allocation_generation = excluded.built_allocation_generation,
                  built_source_consumption_generation =
                    excluded.built_source_consumption_generation,
                  built_timing_generation = excluded.built_timing_generation,
                  built_combo_identity_generation =
                    excluded.built_combo_identity_generation,
                  built_assigned_stock_generation =
                    excluded.built_assigned_stock_generation,
                  decision_state_fingerprint = excluded.decision_state_fingerprint,
                  payload_sha256 = excluded.payload_sha256,
                  payload_json = excluded.payload_json,
                  updated_at_ms = excluded.updated_at_ms
                WHERE current_decision_projections.payload_sha256
                      IS NOT excluded.payload_sha256
                   OR current_decision_projections.payload_json
                      IS NOT excluded.payload_json
                """,
                tuple(values[column] for column in columns),
            )
        return int(cursor.rowcount or 0) == 1

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
        account = str(payload.get("account") or "").strip()
        if not account or account != account.lower():
            raise ValueError("lifecycle_case.v2 requires lowercase account")
        payload["account"] = account
        _target_lot_ids, target_contracts, target_rows = _normalized_lifecycle_case_targets(
            payload,
            case_id=case_id,
            account=account,
        )
        if not case_id or not case_key or not target_contracts or len(target_rows) != len(target_contracts):
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
                if (
                    not isinstance(existing_payload, dict)
                    or _lifecycle_case_immutable_payload(existing_payload) != immutable
                ):
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
                    json.dumps(sorted(target_contracts), ensure_ascii=False),
                    json.dumps(target_contracts, ensure_ascii=False, sort_keys=True),
                    int(payload["observation_start_ms"]) if payload.get("observation_start_ms") is not None else None,
                    int(payload["pending_until_ms"]) if payload.get("pending_until_ms") is not None else None,
                    ts,
                    ts,
                    raw_json,
                ),
            )
            active_conn.executemany(
                """
                INSERT INTO trade_lifecycle_case_targets (
                  case_id, account, target_lot_id, target_contracts
                ) VALUES (?, ?, ?, ?)
                """,
                target_rows,
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
                if (
                    str(current_summary.get("state_fingerprint") or "").strip()
                    != str(expected_state_fingerprint or "").strip()
                ):
                    raise ValueError("lifecycle case state fingerprint compare-and-set failed")
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

    def get_trade_lifecycle_attempt_audit_head(
        self,
        *,
        case_id: str,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        case_value = str(case_id or "").strip()
        if not case_value:
            raise ValueError("lifecycle attempt audit case_id is required")
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT audit_case_key, case_id, last_ordinal, chain_sha256,
                       current_span_ordinal, last_invocation_id, updated_at_ms
                FROM trade_lifecycle_attempt_audit_heads
                WHERE case_id = ?
                """,
                (case_value,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_trade_lifecycle_attempt_audit_heads_for_account(
        self,
        *,
        account: str,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        account_value = str(account or "").strip().lower()
        if not account_value:
            raise ValueError("lifecycle attempt audit account is required")
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT lifecycle_case.account AS account,
                       audit_head.audit_case_key, audit_head.case_id,
                       audit_head.last_ordinal, audit_head.chain_sha256,
                       audit_head.current_span_ordinal,
                       audit_head.last_invocation_id,
                       audit_head.updated_at_ms
                FROM trade_lifecycle_cases AS lifecycle_case
                JOIN trade_lifecycle_attempt_audit_heads AS audit_head
                  ON audit_head.case_id = lifecycle_case.case_id
                WHERE lifecycle_case.account = ?
                ORDER BY lifecycle_case.case_id ASC
                """,
                (account_value,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_trade_lifecycle_attempt_audit_by_invocation(
        self,
        *,
        case_id: str,
        invocation_id: str | bytes,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        case_value = str(case_id or "").strip()
        if not case_value:
            raise ValueError("lifecycle attempt audit case_id is required")
        invocation_bytes = lifecycle_invocation_id_bytes(invocation_id)
        with self._optional_conn(conn) as active_conn:
            row = active_conn.execute(
                """
                SELECT lifecycle_case.account AS account,
                       audit.audit_case_key, audit_head.case_id AS case_id,
                       audit.ordinal,
                       audit.invocation_id, audit.attempted_at_ms,
                       audit.outcome_code, audit.semantic_fingerprint,
                       audit.receipt_sha256, audit.diagnostic_sha256,
                       audit.span_ordinal, span.semantic_schema,
                       audit_head.last_ordinal,
                       audit_head.chain_sha256,
                       audit_head.last_invocation_id
                FROM trade_lifecycle_attempt_audits AS audit
                JOIN trade_lifecycle_attempt_audit_heads AS audit_head
                  ON audit_head.audit_case_key = audit.audit_case_key
                LEFT JOIN trade_lifecycle_cases AS lifecycle_case
                  ON lifecycle_case.case_id = audit_head.case_id
                LEFT JOIN trade_lifecycle_observation_spans AS span
                  ON span.audit_case_key = audit.audit_case_key
                 AND span.span_ordinal = audit.span_ordinal
                WHERE audit_head.case_id = ?
                  AND audit.invocation_id = ?
                """,
                (case_value, invocation_bytes),
            ).fetchone()
        return dict(row) if row is not None else None

    def match_trade_lifecycle_attempt_audit_invocation(
        self,
        attempt_audit: LifecycleAttemptAuditEnvelope,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        validate_lifecycle_attempt_audit_envelope(attempt_audit)
        stored = self.get_trade_lifecycle_attempt_audit_by_invocation(
            case_id=attempt_audit.case_id,
            invocation_id=attempt_audit.invocation_id,
            conn=conn,
        )
        if stored is None:
            return None
        expected = {
            "attempted_at_ms": attempt_audit.attempted_at_ms,
            "outcome_code": attempt_audit.outcome_code,
            "semantic_schema": attempt_audit.semantic_schema,
            "semantic_fingerprint": attempt_audit.semantic_fingerprint,
            "receipt_sha256": attempt_audit.receipt_sha256,
            "diagnostic_sha256": attempt_audit.diagnostic_sha256,
        }
        mismatched = [
            field
            for field, value in expected.items()
            if stored.get(field) != value
        ]
        if mismatched:
            raise ValueError(
                "lifecycle attempt invocation replay mismatch: "
                + ",".join(mismatched)
            )
        stored_invocation = lifecycle_invocation_id_bytes(
            stored.get("last_invocation_id")
        )
        stored_chain = lifecycle_sha256_bytes(
            stored.get("chain_sha256"),
            field="chain_sha256",
        )
        stored_ordinal = stored.get("ordinal")
        stored_last_ordinal = stored.get("last_ordinal")
        if (
            type(stored_ordinal) is not int
            or stored_ordinal < 1
            or type(stored_last_ordinal) is not int
            or stored_last_ordinal != stored_ordinal
            or stored_invocation != attempt_audit.invocation_id
        ):
            raise ValueError(
                "historical lifecycle attempt invocation requires explicit "
                "reconciliation"
            )
        return {
            "audit_ordinal": stored_ordinal,
            "audit_chain_sha256": stored_chain.hex(),
            "audit_idempotent": True,
            "audit_span_ordinal": stored.get("span_ordinal"),
            "_cleanup_receipt_sha256": None,
        }

    def append_trade_lifecycle_attempt_audit_in_transaction(
        self,
        *,
        attempt_audit: LifecycleAttemptAuditEnvelope,
        first_evidence_id: str | None = None,
        conn: sqlite3.Connection,
    ) -> dict[str, Any]:
        replay = self.match_trade_lifecycle_attempt_audit_invocation(
            attempt_audit,
            conn=conn,
        )
        if replay is not None:
            return replay

        evidence_id = str(first_evidence_id or "").strip()
        observed = attempt_audit.outcome_code in (1, 2)
        if observed and not evidence_id:
            raise ValueError(
                "observed lifecycle attempt requires admitted first evidence"
            )
        if not observed and evidence_id:
            raise ValueError(
                "failed lifecycle attempt cannot carry admitted evidence"
            )

        head = conn.execute(
            """
            SELECT audit_case_key, last_ordinal, chain_sha256,
                   current_span_ordinal, last_invocation_id
            FROM trade_lifecycle_attempt_audit_heads
            WHERE case_id = ?
            """,
            (attempt_audit.case_id,),
        ).fetchone()
        if head is None:
            if conn.execute(
                "SELECT 1 FROM trade_lifecycle_cases WHERE case_id = ?",
                (attempt_audit.case_id,),
            ).fetchone() is None:
                raise ValueError(
                    f"lifecycle case not found: {attempt_audit.case_id}"
                )
            cursor = conn.execute(
                """
                INSERT INTO trade_lifecycle_attempt_audit_heads (
                  case_id, last_ordinal, chain_sha256, current_span_ordinal,
                  last_invocation_id, updated_at_ms
                ) VALUES (?, 0, ?, NULL, NULL, ?)
                """,
                (
                    attempt_audit.case_id,
                    LIFECYCLE_ATTEMPT_CHAIN_GENESIS,
                    int(now_ms()),
                ),
            )
            audit_case_key = int(cursor.lastrowid)
            last_ordinal = 0
            previous_chain = LIFECYCLE_ATTEMPT_CHAIN_GENESIS
            current_span_ordinal: int | None = None
        else:
            audit_case_key = head["audit_case_key"]
            last_ordinal = head["last_ordinal"]
            current_span_ordinal = head["current_span_ordinal"]
            if type(audit_case_key) is not int or audit_case_key < 1:
                raise ValueError("lifecycle attempt audit head key is invalid")
            if type(last_ordinal) is not int or last_ordinal < 0:
                raise ValueError("lifecycle attempt audit head ordinal is invalid")
            previous_chain = lifecycle_sha256_bytes(
                head["chain_sha256"],
                field="chain_sha256",
            )
            if current_span_ordinal is not None and (
                type(current_span_ordinal) is not int
                or current_span_ordinal < 1
            ):
                raise ValueError(
                    "lifecycle attempt audit current span is invalid"
                )
            if last_ordinal == 0 and (
                previous_chain != LIFECYCLE_ATTEMPT_CHAIN_GENESIS
                or current_span_ordinal is not None
                or head["last_invocation_id"] is not None
            ):
                raise ValueError("lifecycle attempt audit genesis head is invalid")
            if last_ordinal > 0:
                lifecycle_invocation_id_bytes(head["last_invocation_id"])

        current_span = None
        if current_span_ordinal is not None:
            current_span = conn.execute(
                """
                SELECT semantic_schema, semantic_fingerprint,
                       first_evidence_id, first_evidence_receipt_sha256,
                       last_receipt_sha256, closed_chain_sha256, closed_at_ms
                FROM trade_lifecycle_observation_spans
                WHERE audit_case_key = ? AND span_ordinal = ?
                """,
                (audit_case_key, current_span_ordinal),
            ).fetchone()
            if (
                current_span is None
                or current_span["closed_chain_sha256"] is not None
                or current_span["closed_at_ms"] is not None
            ):
                raise ValueError(
                    "lifecycle attempt audit current span is missing or closed"
                )
        elif conn.execute(
            """
            SELECT 1
            FROM trade_lifecycle_observation_spans
            WHERE audit_case_key = ?
            LIMIT 1
            """,
            (audit_case_key,),
        ).fetchone() is not None:
            raise ValueError("lifecycle attempt audit head lost its current span")

        ordinal = last_ordinal + 1
        chain = compute_lifecycle_attempt_chain_sha256(
            previous_chain_sha256=previous_chain,
            case_id=attempt_audit.case_id,
            ordinal=ordinal,
            invocation_id=attempt_audit.invocation_id,
            attempted_at_ms=attempt_audit.attempted_at_ms,
            outcome_code=attempt_audit.outcome_code,
            semantic_fingerprint=attempt_audit.semantic_fingerprint,
            receipt_sha256=attempt_audit.receipt_sha256,
            diagnostic_sha256=attempt_audit.diagnostic_sha256,
        )
        cleanup_receipt: bytes | None = None
        audit_span_ordinal: int | None = None

        def ensure_receipt_blob() -> None:
            assert attempt_audit.receipt_sha256 is not None
            assert attempt_audit.receipt_codec is not None
            assert attempt_audit.receipt_codec_version is not None
            assert attempt_audit.receipt_uncompressed_bytes is not None
            assert attempt_audit.receipt_compressed_bytes is not None
            assert attempt_audit.receipt_compressed_payload is not None
            assert attempt_audit.canonical_receipt_bytes is not None
            inserted = conn.execute(
                """
                INSERT INTO trade_lifecycle_receipt_blobs (
                  receipt_sha256, codec, codec_version, uncompressed_bytes,
                  compressed_bytes, compressed_payload, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(receipt_sha256) DO NOTHING
                """,
                (
                    attempt_audit.receipt_sha256,
                    attempt_audit.receipt_codec,
                    attempt_audit.receipt_codec_version,
                    attempt_audit.receipt_uncompressed_bytes,
                    attempt_audit.receipt_compressed_bytes,
                    attempt_audit.receipt_compressed_payload,
                    int(now_ms()),
                ),
            )
            if inserted.rowcount == 1:
                return
            stored = conn.execute(
                """
                SELECT codec, codec_version, uncompressed_bytes,
                       compressed_bytes, compressed_payload
                FROM trade_lifecycle_receipt_blobs
                WHERE receipt_sha256 = ?
                """,
                (attempt_audit.receipt_sha256,),
            ).fetchone()
            if stored is None:
                raise ValueError("lifecycle receipt blob insert was lost")
            if (
                stored["codec"] != LIFECYCLE_RECEIPT_CODEC
                or stored["codec_version"] != LIFECYCLE_RECEIPT_CODEC_VERSION
                or stored["uncompressed_bytes"]
                != attempt_audit.receipt_uncompressed_bytes
                or stored["compressed_bytes"]
                != attempt_audit.receipt_compressed_bytes
                or stored["compressed_payload"]
                != attempt_audit.receipt_compressed_payload
            ):
                raise ValueError("lifecycle receipt blob immutable conflict")
            decompressor = zlib.decompressobj()
            decoded = decompressor.decompress(
                stored["compressed_payload"],
                int(stored["uncompressed_bytes"]) + 1,
            )
            if (
                decoded != attempt_audit.canonical_receipt_bytes
                or not decompressor.eof
                or decompressor.unused_data
                or decompressor.unconsumed_tail
            ):
                raise ValueError("lifecycle receipt blob content mismatch")

        if not observed:
            if current_span_ordinal is not None:
                cursor = conn.execute(
                    """
                    UPDATE trade_lifecycle_observation_spans
                    SET intervening_failed_attempt_count =
                          intervening_failed_attempt_count + 1
                    WHERE audit_case_key = ? AND span_ordinal = ?
                      AND closed_chain_sha256 IS NULL AND closed_at_ms IS NULL
                    """,
                    (audit_case_key, current_span_ordinal),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        "lifecycle attempt failure span update was lost"
                    )
        else:
            assert attempt_audit.semantic_schema is not None
            assert attempt_audit.semantic_fingerprint is not None
            assert attempt_audit.receipt_sha256 is not None
            same_span = current_span is not None and (
                current_span["semantic_schema"] == attempt_audit.semantic_schema
                and current_span["semantic_fingerprint"]
                == attempt_audit.semantic_fingerprint
            )
            if same_span:
                if current_span["first_evidence_id"] != evidence_id:
                    raise ValueError(
                        "lifecycle attempt admitted evidence changed within span"
                    )
                commitment = lifecycle_sha256_bytes(
                    current_span["first_evidence_receipt_sha256"],
                    field="first_evidence_receipt_sha256",
                )
                new_last_receipt = (
                    None
                    if attempt_audit.receipt_sha256 == commitment
                    else attempt_audit.receipt_sha256
                )
                if new_last_receipt is not None:
                    ensure_receipt_blob()
                old_last_receipt = (
                    None
                    if current_span["last_receipt_sha256"] is None
                    else lifecycle_sha256_bytes(
                        current_span["last_receipt_sha256"],
                        field="last_receipt_sha256",
                    )
                )
                cursor = conn.execute(
                    """
                    UPDATE trade_lifecycle_observation_spans
                    SET last_success_ordinal = ?, last_success_at_ms = ?,
                        successful_observation_count =
                          successful_observation_count + 1,
                        last_receipt_sha256 = ?
                    WHERE audit_case_key = ? AND span_ordinal = ?
                      AND closed_chain_sha256 IS NULL AND closed_at_ms IS NULL
                    """,
                    (
                        ordinal,
                        attempt_audit.attempted_at_ms,
                        new_last_receipt,
                        audit_case_key,
                        current_span_ordinal,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        "lifecycle attempt observation span update was lost"
                    )
                if (
                    old_last_receipt is not None
                    and old_last_receipt != new_last_receipt
                ):
                    cleanup_receipt = old_last_receipt
                audit_span_ordinal = current_span_ordinal
            else:
                evidence_row = conn.execute(
                    """
                    SELECT case_id, raw_json
                    FROM trade_lifecycle_evidence
                    WHERE evidence_id = ?
                    """,
                    (evidence_id,),
                ).fetchone()
                if (
                    evidence_row is None
                    or evidence_row["case_id"] != attempt_audit.case_id
                ):
                    raise ValueError(
                        "lifecycle attempt first evidence is missing or misbound"
                    )
                try:
                    evidence = json.loads(evidence_row["raw_json"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        "lifecycle attempt first evidence JSON is invalid"
                    ) from exc
                if type(evidence) is not dict:
                    raise ValueError(
                        "lifecycle attempt first evidence must be an object"
                    )
                _semantic, evidence_fingerprint = (
                    settlement_semantic_from_evidence(evidence)
                )
                observation = evidence.get("observation")
                if type(observation) is not dict:
                    raise ValueError(
                        "lifecycle attempt first evidence observation is invalid"
                    )
                evidence_schema = str(
                    observation.get("semantic_schema") or ""
                ).strip()
                if (
                    evidence_schema != attempt_audit.semantic_schema
                    or lifecycle_sha256_bytes(
                        evidence_fingerprint,
                        field="first_evidence_semantic_fingerprint",
                    )
                    != attempt_audit.semantic_fingerprint
                ):
                    raise ValueError(
                        "lifecycle attempt first evidence semantic mismatch"
                    )
                commitment = lifecycle_receipt_sha256(
                    canonical_lifecycle_observation_bytes(observation)
                )
                new_last_receipt = (
                    None
                    if attempt_audit.receipt_sha256 == commitment
                    else attempt_audit.receipt_sha256
                )
                if new_last_receipt is not None:
                    ensure_receipt_blob()
                if current_span_ordinal is not None:
                    cursor = conn.execute(
                        """
                        UPDATE trade_lifecycle_observation_spans
                        SET closed_chain_sha256 = ?, closed_at_ms = ?
                        WHERE audit_case_key = ? AND span_ordinal = ?
                          AND closed_chain_sha256 IS NULL
                          AND closed_at_ms IS NULL
                        """,
                        (
                            previous_chain,
                            attempt_audit.attempted_at_ms,
                            audit_case_key,
                            current_span_ordinal,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError(
                            "lifecycle attempt prior span close was lost"
                        )
                    audit_span_ordinal = current_span_ordinal + 1
                else:
                    audit_span_ordinal = 1
                conn.execute(
                    """
                    INSERT INTO trade_lifecycle_observation_spans (
                      audit_case_key, span_ordinal, semantic_schema,
                      semantic_fingerprint, first_evidence_id,
                      first_evidence_receipt_sha256,
                      first_success_ordinal, first_success_at_ms,
                      last_success_ordinal, last_success_at_ms,
                      successful_observation_count,
                      intervening_failed_attempt_count,
                      closed_chain_sha256, last_receipt_sha256, closed_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0,
                              NULL, ?, NULL)
                    """,
                    (
                        audit_case_key,
                        audit_span_ordinal,
                        attempt_audit.semantic_schema,
                        attempt_audit.semantic_fingerprint,
                        evidence_id,
                        commitment,
                        ordinal,
                        attempt_audit.attempted_at_ms,
                        ordinal,
                        attempt_audit.attempted_at_ms,
                        new_last_receipt,
                    ),
                )

        conn.execute(
            """
            INSERT INTO trade_lifecycle_attempt_audits (
              audit_case_key, ordinal, invocation_id, attempted_at_ms,
              outcome_code, semantic_fingerprint, receipt_sha256,
              diagnostic_sha256, span_ordinal
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_case_key,
                ordinal,
                attempt_audit.invocation_id,
                attempt_audit.attempted_at_ms,
                attempt_audit.outcome_code,
                attempt_audit.semantic_fingerprint,
                attempt_audit.receipt_sha256,
                attempt_audit.diagnostic_sha256,
                audit_span_ordinal,
            ),
        )
        cursor = conn.execute(
            """
            UPDATE trade_lifecycle_attempt_audit_heads
            SET last_ordinal = ?, chain_sha256 = ?,
                current_span_ordinal = ?, last_invocation_id = ?,
                updated_at_ms = ?
            WHERE audit_case_key = ? AND last_ordinal = ?
              AND chain_sha256 = ?
            """,
            (
                ordinal,
                chain,
                current_span_ordinal if not observed else audit_span_ordinal,
                attempt_audit.invocation_id,
                int(now_ms()),
                audit_case_key,
                last_ordinal,
                previous_chain,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("lifecycle attempt audit head CAS failed")
        return {
            "audit_ordinal": ordinal,
            "audit_chain_sha256": chain.hex(),
            "audit_idempotent": False,
            "audit_span_ordinal": audit_span_ordinal,
            "_cleanup_receipt_sha256": cleanup_receipt,
        }

    def delete_unreferenced_trade_lifecycle_receipt_blob(
        self,
        receipt_sha256: str | bytes,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        receipt_hash = lifecycle_sha256_bytes(
            receipt_sha256,
            field="receipt_sha256",
        )
        with self._optional_conn(conn, commit=True) as active_conn:
            cursor = active_conn.execute(
                """
                DELETE FROM trade_lifecycle_receipt_blobs
                WHERE receipt_sha256 = ?
                  AND NOT EXISTS (
                    SELECT 1
                    FROM trade_lifecycle_observation_spans
                    WHERE last_receipt_sha256 = ?
                  )
                """,
                (receipt_hash, receipt_hash),
            )
        return cursor.rowcount == 1

    def list_trade_lifecycle_attempt_audits(
        self,
        *,
        case_id: str,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        case_value = str(case_id or "").strip()
        if not case_value:
            raise ValueError("lifecycle attempt audit case_id is required")
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT audit.audit_case_key, audit.ordinal,
                       audit.invocation_id, audit.attempted_at_ms,
                       audit.outcome_code, audit.semantic_fingerprint,
                       audit.receipt_sha256, audit.diagnostic_sha256,
                       audit.span_ordinal
                FROM trade_lifecycle_attempt_audits AS audit
                JOIN trade_lifecycle_attempt_audit_heads AS audit_head
                  ON audit_head.audit_case_key = audit.audit_case_key
                WHERE audit_head.case_id = ?
                ORDER BY audit.ordinal ASC
                """,
                (case_value,),
            ).fetchall()
        return [dict(row) for row in rows]

    def verify_trade_lifecycle_attempt_audit_case(
        self,
        *,
        case_id: str,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        case_value = str(case_id or "").strip()
        if not case_value:
            raise ValueError("lifecycle attempt audit case_id is required")
        with self._optional_conn(conn) as active_conn:
            head = self.get_trade_lifecycle_attempt_audit_head(
                case_id=case_value,
                conn=active_conn,
            )
            audits = self.list_trade_lifecycle_attempt_audits(
                case_id=case_value,
                conn=active_conn,
            )
            audit_case_key = head.get("audit_case_key") if head is not None else None
            spans: list[dict[str, Any]] = []
            receipt_blobs: list[dict[str, Any]] = []
            settlement_evidence: list[dict[str, Any]] = []
            if audit_case_key is not None:
                spans = [
                    dict(row)
                    for row in active_conn.execute(
                        """
                        SELECT span.audit_case_key, span.span_ordinal,
                               span.semantic_schema, span.semantic_fingerprint,
                               span.first_evidence_id,
                               span.first_evidence_receipt_sha256,
                               span.first_success_ordinal,
                               span.first_success_at_ms,
                               span.last_success_ordinal,
                               span.last_success_at_ms,
                               span.successful_observation_count,
                               span.intervening_failed_attempt_count,
                               span.closed_chain_sha256,
                               span.last_receipt_sha256, span.closed_at_ms,
                               evidence.evidence_id AS first_evidence_fk_id,
                               evidence.case_id AS first_evidence_case_id,
                               evidence.created_at_ms AS first_evidence_created_at_ms
                        FROM trade_lifecycle_observation_spans AS span
                        LEFT JOIN trade_lifecycle_evidence AS evidence
                          ON evidence.evidence_id = span.first_evidence_id
                        WHERE span.audit_case_key = ?
                        ORDER BY span.span_ordinal ASC
                        """,
                        (audit_case_key,),
                    ).fetchall()
                ]
                settlement_evidence = [
                    dict(row)
                    for row in active_conn.execute(
                        """
                        WITH first_span AS (
                          SELECT first_evidence_id
                          FROM trade_lifecycle_observation_spans
                          WHERE audit_case_key = ?
                          ORDER BY span_ordinal ASC
                          LIMIT 1
                        ), first_evidence AS (
                          SELECT evidence.created_at_ms, evidence.rowid
                          FROM trade_lifecycle_evidence AS evidence
                          JOIN first_span
                            ON first_span.first_evidence_id = evidence.evidence_id
                        )
                        SELECT evidence.evidence_id, evidence.case_id,
                               evidence.created_at_ms,
                               evidence.raw_json
                        FROM trade_lifecycle_evidence AS evidence
                        CROSS JOIN first_evidence
                        WHERE evidence.case_id = ?
                          AND evidence.source_type = 'broker_settlement_observation'
                          AND (
                            evidence.created_at_ms > first_evidence.created_at_ms
                            OR (
                              evidence.created_at_ms = first_evidence.created_at_ms
                              AND evidence.rowid >= first_evidence.rowid
                            )
                          )
                        ORDER BY evidence.created_at_ms ASC, evidence.rowid ASC
                        """,
                        (audit_case_key, case_value),
                    ).fetchall()
                ]
                receipt_blobs = [
                    dict(row)
                    for row in active_conn.execute(
                        """
                        SELECT blob.receipt_sha256, blob.codec,
                               blob.codec_version, blob.uncompressed_bytes,
                               blob.compressed_bytes, blob.compressed_payload,
                               blob.created_at_ms
                        FROM trade_lifecycle_receipt_blobs AS blob
                        JOIN (
                          SELECT DISTINCT last_receipt_sha256
                          FROM trade_lifecycle_observation_spans
                          WHERE audit_case_key = ?
                            AND last_receipt_sha256 IS NOT NULL
                        ) AS referenced
                          ON referenced.last_receipt_sha256 = blob.receipt_sha256
                        ORDER BY blob.receipt_sha256 ASC
                        """,
                        (audit_case_key,),
                    ).fetchall()
                ]
            admission_head = self.get_trade_lifecycle_settlement_admission_head(
                case_id=case_value,
                conn=active_conn,
            )
            foreign_key_rows: list[dict[str, Any]] = []
            if head is not None and active_conn.execute(
                "SELECT 1 FROM trade_lifecycle_cases WHERE case_id = ?",
                (case_value,),
            ).fetchone() is None:
                foreign_key_rows.append(
                    {
                        "table": "trade_lifecycle_attempt_audit_heads",
                        "fkid": 0,
                    }
                )
            existing_blob_hashes = {
                row["receipt_sha256"] for row in receipt_blobs
            }
            for span in spans:
                if span["first_evidence_fk_id"] is None:
                    foreign_key_rows.append(
                        {
                            "table": "trade_lifecycle_observation_spans",
                            "fkid": 1,
                        }
                    )
                last_receipt_sha256 = span["last_receipt_sha256"]
                if (
                    last_receipt_sha256 is not None
                    and last_receipt_sha256 not in existing_blob_hashes
                ):
                    foreign_key_rows.append(
                        {
                            "table": "trade_lifecycle_observation_spans",
                            "fkid": 0,
                        }
                    )

        return verify_lifecycle_attempt_audit_chain(
            case_id=case_value,
            head=head,
            audit_rows=audits,
            span_rows=spans,
            evidence_rows=settlement_evidence,
            receipt_blob_rows=receipt_blobs,
            admission_head=admission_head,
            foreign_key_rows=foreign_key_rows,
        )

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
        shared_wheel_events: Sequence[dict[str, Any]] | None = None,
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
        evidence_revisions = {
            str(row["case_id"]): {
                "revision": int(row["revision"]),
                "evidence_count": (
                    int(row["evidence_count"])
                    if row["evidence_count"] is not None
                    else None
                ),
            }
            for row in conn.execute(
                """
                SELECT revision.case_id, revision.revision,
                       revision.evidence_count
                FROM trade_lifecycle_evidence_revisions AS revision
                JOIN trade_lifecycle_cases AS lifecycle_case
                  ON lifecycle_case.case_id = revision.case_id
                WHERE lifecycle_case.account = ?
                ORDER BY revision.case_id ASC
                """,
                (account_value,),
            ).fetchall()
        }
        admission_heads = {
            str(row["case_id"]): dict(row)
            for row in conn.execute(
                """
                SELECT admission.case_id, admission.semantic_schema,
                       admission.semantic_fingerprint, admission.evidence_id,
                       admission.evidence_created_at_ms,
                       admission.updated_at_ms
                FROM trade_lifecycle_settlement_admission_heads AS admission
                JOIN trade_lifecycle_cases AS lifecycle_case
                  ON lifecycle_case.case_id = admission.case_id
                WHERE lifecycle_case.account = ?
                ORDER BY admission.case_id ASC
                """,
                (account_value,),
            ).fetchall()
        }
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
        wheel_events = (
            list(shared_wheel_events)
            if shared_wheel_events is not None
            else self.list_wheel_events(conn=conn)
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
            "account_lifecycle_evidence_revisions": evidence_revisions,
            "account_lifecycle_settlement_admission_heads": admission_heads,
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
            "account_wheel_events": [
                row
                for row in wheel_events
                if str(row.get("account") or "").strip().lower() == account_value
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
            wheel_events = self.list_wheel_events(conn=conn)
            rows = {
                account: self._read_account_decision_state_rows(
                    account=account,
                    conn=conn,
                    shared_trade_events=events,
                    shared_position_lots=lots,
                    shared_assigned_stock_events=assigned_stock_events,
                    shared_wheel_events=wheel_events,
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


def with_sqlite_repo_transaction(
    repo: Any,
    fn: Any,
    *,
    require_projection_publication: bool = False,
) -> Any:
    sqlite_repo = (
        require_position_projection_publication_repo(repo)
        if require_projection_publication
        else require_option_positions_event_write_repo(repo)
    )
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


def require_option_positions_event_read_repo(
    repo: Any,
    *,
    require_page: bool = False,
) -> OptionPositionsEventReadRepo:
    candidate = require_option_positions_read_repo(repo)
    if callable(getattr(candidate, "list_trade_events", None)) and (
        not require_page or callable(getattr(candidate, "list_trade_events_page", None))
    ):
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


def require_position_projection_publication_repo(
    repo: Any,
) -> PositionProjectionPublicationRepo:
    candidate = require_option_positions_event_write_repo(repo)
    required = (
        "apply_position_lot_diff",
        "publish_full_position_projection_heads",
    )
    if all(callable(getattr(candidate, name, None)) for name in required):
        return cast(PositionProjectionPublicationRepo, candidate)
    raise TypeError("option_positions repo does not satisfy projection publication interface")
