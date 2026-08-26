from __future__ import annotations

from .writer_common import (
    project_stored_trade_events_to_position_lots,
    projection_diagnostics_summary,
    safe_int_count,
)

from .writer_decision import (
    _finish_trade_event_decision_projection,
    _lifecycle_resolution_after_allocations,
)

from .writer_trade_events import (
    _trade_event_from_normalized_deal,
    adopt_existing_combo_identity_atomically,
    persist_normalized_trade_events_atomically,
    persist_trade_event,
    persist_trade_event_object,
    persist_trade_event_objects_atomically,
    persist_trade_event_with_combo_identity,
    persist_trade_event_with_wheel_intent,
    rebuild_position_lots_from_trade_events,
)

from .writer_lifecycle_allocation import (
    apply_lifecycle_allocation_atomically,
)

from .writer_lifecycle_evidence import (
    accept_option_close_evidence_atomically,
    bind_lifecycle_timing_policy_atomically,
    discover_expired_lifecycle_cases_atomically,
    record_assigned_stock_event_atomically,
    record_lifecycle_attempt_audit_atomically,
    record_lifecycle_evidence_issue_atomically,
    record_lifecycle_observation_attempt_atomically,
)

from .writer_lifecycle_state import (
    advance_lifecycle_case_state_atomically,
)
