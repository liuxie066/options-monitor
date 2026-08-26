from __future__ import annotations

from .repository_common import (
    Any,
    AssignedStockEventRepo,
    LIFECYCLE_ATTEMPT_CHAIN_GENESIS,
    LIFECYCLE_RECEIPT_CODEC,
    LIFECYCLE_RECEIPT_CODEC_VERSION,
    LifecycleAttemptAuditEnvelope,
    Mapping,
    OptionPositionsEventReadRepo,
    OptionPositionsEventWriteRepo,
    OptionPositionsReadRepo,
    POSITION_LOTS_COLUMN_CLASSIFICATION,
    POSITION_PROJECTION_SCHEMA,
    Path,
    PositionLotDiff,
    PositionLotRecord,
    PositionProjectionAccountSnapshot,
    PositionProjectionPublicationRepo,
    Protocol,
    Sequence,
    TRADE_EVENTS_COLUMN_CLASSIFICATION,
    _CURRENT_DECISION_GENERATION_COUNTERS,
    _add_column_if_missing,
    _assert_same_combo_pair_inference_identity,
    _canonical_existing_fields_json,
    _canonical_text_values,
    _combo_pair_inference_sql_values,
    _json_object,
    _json_text,
    _lifecycle_case_immutable_payload,
    _load_data_config,
    _normalize_combo_pair_inference_payload,
    _normalized_lifecycle_case_targets,
    _notification_delivery_batch_row,
    _notification_outbox_row,
    _position_lot_contract_scalars,
    _position_lot_storage_values,
    _same_lifecycle_evidence_source,
    _storage_scalar_matches,
    _validate_position_lot_fields,
    canonical_lifecycle_observation_bytes,
    cast,
    compute_lifecycle_attempt_chain_sha256,
    connect_private_sqlite,
    contextmanager,
    dataclass,
    effective_expiration,
    encode_trade_event_for_storage,
    exclusive_private_file_lock,
    hashlib,
    json,
    lifecycle_invocation_id_bytes,
    lifecycle_receipt_sha256,
    lifecycle_sha256_bytes,
    normalize_wheel_event,
    now_ms,
    option_positions_bootstrap_from_feishu_enabled,
    ordered_position_lots_fingerprint,
    parse_note_kv,
    position_lot_row_to_record,
    private_path,
    read_current_decision_projection_inputs_from_conn,
    resolve_ledger_store,
    resolve_option_positions_sqlite_path,
    safe_float,
    secure_sqlite_artifacts,
    settlement_semantic_from_evidence,
    sqlite3,
    stored_trade_event_to_ledger_event,
    symbol_market,
    trade_event_application_payload,
    trade_event_position_effect,
    valid_void_target_event_id,
    validate_lifecycle_attempt_audit_envelope,
    verify_lifecycle_attempt_audit_chain,
    zlib,
)

from .repository_decision_schema import (
    _ensure_current_decision_projection_schema,
)

from .repository_lifecycle_schema import (
    _ensure_lifecycle_attempt_audit_schema,
    _ensure_lifecycle_delivery_status_revision_v1,
    _ensure_notification_delivery_batches_v1,
    _ensure_notification_outbox_v2,
)

from .repository_projection_schema import (
    _create_current_decision_case_scope_guards,
    _create_current_decision_generation_triggers,
    _current_decision_generation_statement,
    _ensure_lifecycle_evidence_count_triggers,
    _ensure_position_projection_schema,
    _position_projection_column_contract,
    _position_projection_column_contract_is_closed,
    _projection_schema_cookie,
)

from .repository_trade_schema import (
    TRADE_EVENT_PAGINATION_INDEXES,
    TRADE_EVENT_PAGINATION_TRIGGERS,
    TradeEventPaginationUnavailable,
    _TRADE_EVENT_PAGINATION_MISSING,
    _backfill_trade_event_pagination_schema,
    _create_index_if_table_empty,
    _ensure_trade_event_pagination_schema,
    _publish_trade_event_pagination_schema,
    _trade_event_pagination_missing_row,
    _trade_event_pagination_projections,
    _trade_event_pagination_schema_ready,
    _trade_event_query_projections,
    _valid_voided_trade_event_ids,
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
