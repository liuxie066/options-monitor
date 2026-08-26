from __future__ import annotations

from .current_decision_common import (
    CURRENT_ASSIGNED_STOCK_SCHEMA,
    CURRENT_COMBO_GROUP_FACT_SCHEMA,
    CURRENT_COMBO_SCHEMA,
    CURRENT_DECISION_MIGRATION_INVENTORY_SCHEMA,
    CURRENT_DECISION_PROJECTION_SCHEMA,
    CURRENT_DECISION_READ_SCHEMA,
    CURRENT_LIFECYCLE_QUALITY_SCHEMA,
    LIFECYCLE_CASE_DECISION_FACT_SCHEMA,
    CurrentDecisionAccountFence,
    CurrentDecisionProjectionFence,
    CurrentDecisionProjectionError,
    _position_migration,
    validate_combo_identity,
)

from .current_decision_lifecycle import (
    advance_lifecycle_case_decision_fact,
    build_initial_lifecycle_case_decision_fact,
    build_lifecycle_case_decision_fact,
    validate_lifecycle_case_decision_fact,
)

from .current_decision_assigned_stock import (
    advance_assigned_stock_fact_for_trade_events,
    compact_assigned_stock_view,
    empty_assigned_stock_fact,
    update_assigned_stock_fact,
    validate_assigned_stock_fact,
)

from .current_decision_combo import (
    build_current_combo_facts,
    validate_current_combo_facts,
)

from .current_decision_quality import (
    arbitrate_lifecycle_case_facts,
    build_lifecycle_quality_fact,
    derive_lifecycle_quality_view,
    lifecycle_views_by_lot,
    validate_lifecycle_quality_fact,
)

from .current_decision_payload import (
    build_current_decision_projection,
    build_current_decision_projection_payload,
    current_decision_projection_row,
    encode_current_decision_projection,
    encode_lifecycle_case_decision_fact,
    read_lifecycle_case_decision_fact,
    validate_current_decision_projection_payload,
    write_lifecycle_case_decision_fact,
)

from .current_decision_oracle import (
    _oracle_assigned_stock_report,
    _oracle_lifecycle_case_facts,
    preview_current_decision_projection_oracle,
)

from .current_decision_migration import (
    apply_current_decision_projection_migration,
    build_current_decision_projection_migration_inventory,
    current_decision_projection_migration_status,
    verify_current_decision_projection_migration,
)

from .current_decision_runtime import (
    __all__,
    capture_current_decision_projection_fence,
    capture_trade_event_decision_projection_fence,
    defer_current_decision_projection,
    finalize_current_decision_projection,
    read_current_assigned_stock_fact,
    read_current_decision_projection,
    verify_current_decision_projection,
)
