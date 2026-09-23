"""Wheel domain module.

The former single-file `domain/domain/wheel.py` lived here; it is now split
along its eight responsibilities:

* ``_common``    - event schema constants and numeric/market micro-helpers.
* ``events``     - event validation, payload hashing and event construction.
* ``evaluation`` - Call/Put candidate economics and rank keys.
* ``intents``    - decision plans, capacity binding and intent lifecycle.
* ``projection`` - row-reading micro-helpers and the Wheel read models.

Imports flow one way only: ``_common`` <- ``events`` <- ``evaluation``,
``events`` <- ``projection`` <- ``intents``.

This facade re-exports every name the pre-split module exposed, public and
private, so existing ``from domain.domain.wheel import X`` call sites keep
working unchanged.
"""

from __future__ import annotations

from ._common import (
    WHEEL_EVENT_SCHEMA,
    WHEEL_EVENT_SCHEMA_V1,
    WHEEL_EVENT_SCHEMA_V2,
    WHEEL_EVENT_TYPES,
    WHEEL_EVENT_TYPES_V1,
    WHEEL_PROJECTION_SCHEMA,
    _finite_float,
    _wheel_abs_delta_bounds,
    _wheel_market,
)
from .evaluation import (
    build_wheel_call_rank_key,
    build_wheel_put_rank_key,
    evaluate_wheel_call_candidate,
    evaluate_wheel_put_candidate,
)
from .events import (
    _positive_int,
    _required_text,
    build_wheel_branch_created_event,
    build_wheel_event,
    deterministic_wheel_branch_id,
    normalize_wheel_event,
    wheel_event_payload_hash,
)
from .intents import (
    _coverage_capacity,
    _stock_settlement,
    _trade_event_fact,
    _validate_put_intent_reservation,
    build_wheel_intent_capacity_binding,
    plan_wheel_branch_decision,
    plan_wheel_call_intent_cancel,
    plan_wheel_call_intent_consume,
    plan_wheel_call_intent_create,
    plan_wheel_manual_end,
    plan_wheel_put_intent_cancel,
    plan_wheel_put_intent_consume,
    plan_wheel_put_intent_create,
    wheel_called_away_event_from_call_assignment,
    wheel_started_event_from_assignment,
)
from .projection import (
    STRATEGY_METADATA_KEYS,
    _active_trade_events,
    _contracts_open,
    _event_type,
    _intent_contracts,
    _intent_state,
    _lot_fields,
    _stable_stock_fact,
    _trade_account,
    _trade_option_type,
    _trade_position_side,
    _trade_symbol,
    attach_lot_strategy_metadata,
    effective_wheel_events,
    project_wheel_coverage,
    lot_contract_key,
    lot_strategy_metadata_for_lot,
    lot_strategy_metadata_from_trade_events,
    merge_lot_strategy_metadata,
    project_wheel_branches,
    project_wheel_call_intents,
    project_wheel_call_linkage_candidates,
    project_wheel_intents,
    project_wheel_lifecycles,
    project_wheel_linkage_candidates,
)


__all__ = [
    "WHEEL_EVENT_SCHEMA",
    "WHEEL_EVENT_SCHEMA_V1",
    "WHEEL_EVENT_SCHEMA_V2",
    "WHEEL_EVENT_TYPES",
    "WHEEL_EVENT_TYPES_V1",
    "WHEEL_PROJECTION_SCHEMA",
    "build_wheel_intent_capacity_binding",
    "build_wheel_call_rank_key",
    "build_wheel_put_rank_key",
    "build_wheel_event",
    "build_wheel_branch_created_event",
    "deterministic_wheel_branch_id",
    "effective_wheel_events",
    "evaluate_wheel_call_candidate",
    "evaluate_wheel_put_candidate",
    "normalize_wheel_event",
    "plan_wheel_call_intent_cancel",
    "plan_wheel_call_intent_consume",
    "plan_wheel_call_intent_create",
    "plan_wheel_put_intent_cancel",
    "plan_wheel_put_intent_consume",
    "plan_wheel_put_intent_create",
    "plan_wheel_manual_end",
    "plan_wheel_branch_decision",
    "project_wheel_call_linkage_candidates",
    "project_wheel_call_intents",
    "project_wheel_intents",
    "project_wheel_linkage_candidates",
    "project_wheel_lifecycles",
    "project_wheel_branches",
    "wheel_called_away_event_from_call_assignment",
    "wheel_event_payload_hash",
    "wheel_started_event_from_assignment",
]
