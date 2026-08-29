from __future__ import annotations

import json

from dataclasses import replace

from datetime import date, datetime, timezone

from decimal import Decimal, InvalidOperation

from typing import Any, Mapping, Sequence

from domain.domain.combo_identity import (
    build_combo_identity_intent,
    identity_from_intent,
    validate_combo_identity,
)

from domain.domain.fee_calc import estimate_futu_executed_option_fee

from domain.domain.ledger import ContractKey, TradeEvent

from domain.domain.ledger.position_fields import (
    effective_contracts_open,
    effective_expiration_ymd,
    effective_multiplier,
    effective_strike,
    strip_retired_strategy_metadata,
)

from domain.domain.lifecycle_allocation import (
    allocation_id_for,
    resolve_allocations,
    terminal_event_id_for,
)

from domain.domain.option_position_identity import normalize_broker, normalize_currency

from domain.domain.option_lifecycle import (
    build_lifecycle_case,
    expiration_observation_start_ms,
)

from domain.domain.performance.models import canonical_decimal_text, quantize_money, to_decimal

from domain.domain.symbol_identity import canonical_symbol, symbol_market

from domain.domain.trade_contract_identity import (
    canonical_contract_symbol,
    normalize_contract_expiration,
    normalize_position_effect,
    normalize_trade_side,
)

from src.application.ledger.lot_resolver import LotCloseResolutionError, LotCloseSelector, resolve_fifo_close_targets

from src.application.ledger.combo_membership import (
    ComboMembershipResolution,
    resolve_combo_group_membership,
)

from src.application.ledger.current_decision_projection import (
    advance_lifecycle_case_decision_fact,
    build_initial_lifecycle_case_decision_fact,
    capture_current_decision_projection_fence,
    capture_trade_event_decision_projection_fence,
    defer_current_decision_projection,
    finalize_current_decision_projection,
    read_current_assigned_stock_fact,
    read_lifecycle_case_decision_fact,
    update_assigned_stock_fact,
    validate_assigned_stock_fact,
    write_lifecycle_case_decision_fact,
)

from src.application.ledger.lifecycle_overlay import (
    advance_direct_lifecycle_anchor_resolution,
    lifecycle_case_generation_token,
    lifecycle_evidence_facts,
    resolve_lifecycle_account_rows,
)

from src.application.ledger.lifecycle_attempt_audit import (
    LifecycleAttemptAuditEnvelope,
)

from src.application.ledger.lifecycle_settlement_semantics import (
    LegacySettlementSemanticUnavailable,
    SETTLEMENT_SEMANTIC_SCHEMA,
    SettlementAdmissionStateIncoherent,
    SettlementSemanticUnavailable,
    settlement_evidence_id,
    settlement_semantic_from_evidence,
)

from src.application.ledger.notification_outbox import (
    build_notification_intent,
    canonical_payload_hash,
    canonical_state_fingerprint,
)

from src.application.ledger.source_consumption import (
    build_source_consumption_claim,
    canonical_source_economic_payload,
    canonical_source_payload_hash,
)

from src.application.ledger.event_codec import (
    encode_trade_event_for_storage,
    valid_void_target_event_id,
)

from src.application.ledger.external_event_key import broker_external_event_key

from src.application.ledger.position_projection_runtime import (
    projection_diagnostics_summary as _projection_diagnostics_summary,
    projection_refresh_result_from_runtime,
    run_position_projection_in_transaction,
)

from src.application.ledger.projection_verify import compare_projection_lots

from src.application.ledger.publisher import (
    ensure_projection_publishable,
    project_stored_trade_events_to_position_lots,
)

from src.application.ledger.repository import with_sqlite_repo_transaction

from src.application.ledger.results import LedgerWriteResult, ProjectionRefreshResult

from src.application.cash_conversion import (
    attach_trade_event_cash_conversions,
    load_cash_fx_payload,
    utc_now_ms,
)

_APPEND_SAFE_EVENT_TYPES = frozenset(
    {
        "open",
        "close",
        "expire_close",
        "assignment",
        "exercise",
        "adjust",
        "verification",
    }
)

_ACTUAL_FEE_TOTAL_KEYS = (
    "fee_amount",
    "total_fee",
    "total_fees",
    "fee_total",
    "fees_total",
    "fees",
)

_ACTUAL_FEE_COMPONENT_KEYS = (
    "commission",
    "commission_fee",
    "platform_fee",
    "system_fee",
    "settlement_fee",
    "trading_fee",
    "transaction_fee",
    "stamp_duty",
    "transaction_levy",
    "trading_tariff",
    "orf",
    "occ_fee",
    "cat_fee",
    "sec_fee",
    "taf",
    "exercise_fee",
)

_ACTUAL_FEE_RAW_SOURCE_KEYS = (
    "raw",
    "raw_payload",
    "broker_payload",
    "deal",
)

def projection_diagnostics_summary(diagnostics: Sequence[Any]) -> dict[str, Any]:
    return _projection_diagnostics_summary(diagnostics)

def safe_int_count(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
