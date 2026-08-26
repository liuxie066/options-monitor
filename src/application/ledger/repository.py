from __future__ import annotations

from .repository_schema import (
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
    TRADE_EVENT_PAGINATION_INDEXES,
    TRADE_EVENT_PAGINATION_TRIGGERS,
    TradeEventPaginationUnavailable,
    _CURRENT_DECISION_GENERATION_COUNTERS,
    _TRADE_EVENT_PAGINATION_MISSING,
    _add_column_if_missing,
    _assert_same_combo_pair_inference_identity,
    _backfill_trade_event_pagination_schema,
    _canonical_existing_fields_json,
    _canonical_text_values,
    _combo_pair_inference_sql_values,
    _create_current_decision_case_scope_guards,
    _create_current_decision_generation_triggers,
    _create_index_if_table_empty,
    _current_decision_generation_statement,
    _ensure_current_decision_projection_schema,
    _ensure_lifecycle_attempt_audit_schema,
    _ensure_lifecycle_delivery_status_revision_v1,
    _ensure_lifecycle_evidence_count_triggers,
    _ensure_notification_delivery_batches_v1,
    _ensure_notification_outbox_v2,
    _ensure_position_projection_schema,
    _ensure_trade_event_pagination_schema,
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
    _position_projection_column_contract,
    _position_projection_column_contract_is_closed,
    _projection_schema_cookie,
    _publish_trade_event_pagination_schema,
    _same_lifecycle_evidence_source,
    _storage_scalar_matches,
    _trade_event_pagination_missing_row,
    _trade_event_pagination_projections,
    _trade_event_pagination_schema_ready,
    _trade_event_query_projections,
    _valid_voided_trade_event_ids,
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
    initialize_ledger_connection,
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

from .repository_core import RepositoryCoreMixin
from .repository_projection import PositionProjectionRepositoryMixin
from .repository_trade_events import TradeEventRepositoryMixin
from .repository_assigned_stock import AssignedStockRepositoryMixin
from .repository_projection_tail import PositionProjectionTailRepositoryMixin
from .repository_lifecycle_cases import LifecycleCaseRepositoryMixin
from .repository_lifecycle_attempts import LifecycleAttemptRepositoryMixin
from .repository_lifecycle_settlement import LifecycleSettlementRepositoryMixin
from .repository_lifecycle_notifications import LifecycleNotificationRepositoryMixin
from .repository_strategy_groups import StrategyGroupRepositoryMixin
from .repository_decision_reads import DecisionReadRepositoryMixin

from .repository_schema import (
    Any,
    OptionPositionsEventReadRepo,
    OptionPositionsEventWriteRepo,
    OptionPositionsReadRepo,
    PositionProjectionPublicationRepo,
    cast,
)

class SQLiteOptionPositionsRepository(
    RepositoryCoreMixin,
    PositionProjectionRepositoryMixin,
    TradeEventRepositoryMixin,
    AssignedStockRepositoryMixin,
    PositionProjectionTailRepositoryMixin,
    LifecycleCaseRepositoryMixin,
    LifecycleAttemptRepositoryMixin,
    LifecycleSettlementRepositoryMixin,
    LifecycleNotificationRepositoryMixin,
    StrategyGroupRepositoryMixin,
    DecisionReadRepositoryMixin,
):
    pass


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
    if isinstance(sqlite_repo, SQLiteOptionPositionsRepository):
        with sqlite_repo._writer_connection(begin_immediate=True) as conn:
            return fn(sqlite_repo, conn)
    return fn(sqlite_repo, None)

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
