from __future__ import annotations

from .repository_schema import (
    Any,
    OptionPositionsEventReadRepo,
    OptionPositionsEventWriteRepo,
    OptionPositionsReadRepo,
    POSITION_LOTS_COLUMN_CLASSIFICATION,
    POSITION_PROJECTION_SCHEMA,
    PositionLotDiff,
    PositionProjectionPublicationRepo,
    TRADE_EVENTS_COLUMN_CLASSIFICATION,
    TRADE_EVENT_PAGINATION_INDEXES,
    TRADE_EVENT_PAGINATION_TRIGGERS,
    TradeEventPaginationUnavailable,
    _ensure_current_decision_projection_schema,
    _ensure_lifecycle_attempt_audit_schema,
    _ensure_position_projection_schema,
    _load_data_config,
    _normalized_lifecycle_case_targets,
    _position_lot_contract_scalars,
    _projection_schema_cookie,
    cast,
    initialize_ledger_connection,
    option_positions_bootstrap_from_feishu_enabled,
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
