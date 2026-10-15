from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from domain.domain.expiration_dates import MARKET_TIMEZONES
from domain.domain.symbol_identity import symbol_market
from domain.domain.lifecycle_timing import canonical_hash, derive_effective_lifecycle_timing
from domain.domain.lifecycle_allocation import (
    AllocationResolution,
    normalize_target_manifest,
    resolve_allocations,
)


LIFECYCLE_CASE_SCHEMA = "lifecycle_case.v2"
PENDING_ELAPSED_HOURS = 72
ASSIGNMENT_WAITING_STATUS = "waiting_settlement_evidence"
PENDING_STATUSES = {
    "pending",
    ASSIGNMENT_WAITING_STATUS,
    "needs_review",
    "partially_resolved",
}
FINAL_STATUSES = {"ledger_written"}

SUPPORTED_LAST_TRADE_SOURCES = frozenset({
    "broker_contract_metadata",
    "instrument_policy_registry",
})


INSTRUMENT_POLICY_SCHEMA = "lifecycle_instrument_policy.v1"
INSTRUMENT_POLICY_REGISTRY: dict[str, dict[str, Any]] = {
    "US:standard_equity_option": {
        "schema_version": INSTRUMENT_POLICY_SCHEMA,
        "policy_id": "us_standard_equity_option.v1",
        "market": "US",
        "contract_class": "standard_equity_option",
        "underlying_security_type": "equity",
        "settlement_style": "physical",
        "timezone": "America/New_York",
        "last_trade_session_close": "16:00:00",
        "half_trade_session_close": "13:00:00",
    },
    "HK:standard_equity_option": {
        "schema_version": INSTRUMENT_POLICY_SCHEMA,
        "policy_id": "hk_standard_equity_option.v1",
        "market": "HK",
        "contract_class": "standard_equity_option",
        "underlying_security_type": "equity",
        "settlement_style": "physical",
        "timezone": "Asia/Hong_Kong",
        "last_trade_session_close": "16:00:00",
        "half_trade_session_close": "12:00:00",
    },
}


@dataclass(frozen=True)
class LifecycleReadModel:
    lifecycle_state: str
    lifecycle_reason_codes: tuple[str, ...]
    observation_start_ms: int | None
    pending_until_ms: int | None
    resolved_contracts_by_lot: dict[str, int]
    remaining_contracts_by_lot: dict[str, int]
    resolved_contracts_by_terminal_type: dict[str, int]
    reserved_contracts_by_lot: dict[str, int]
    closure_fact: str
    reason_state: str
    close_reason: str | None
    actionable: bool


def normalize_market(market: Any) -> str:
    value = str(market or "").strip().upper()
    aliases = {"USA": "US", "NYSE": "US", "NASDAQ": "US", "HONG_KONG": "HK"}
    return aliases.get(value, value)


def expiration_observation_start_ms(expiration_ymd: str, market: str) -> int | None:
    observed, _ = _expiration_observation_boundary(expiration_ymd, market)
    return observed


def _expiration_observation_boundary(expiration_ymd: str, market: str) -> tuple[int | None, str | None]:
    market_code = normalize_market(market)
    timezone_name = MARKET_TIMEZONES.get(market_code)
    if not timezone_name:
        return None, "market_expiration_policy_missing"
    try:
        expiration_date = date.fromisoformat(str(expiration_ymd or "").strip())
    except ValueError:
        return None, "expiration_date_invalid"
    next_day = expiration_date + timedelta(days=1)
    observed = datetime.combine(next_day, time.min, tzinfo=ZoneInfo(timezone_name))
    return int(observed.timestamp() * 1000), None


def lifecycle_case_key(
    *,
    account: str,
    broker: str,
    contract_key: str,
    position_side: str,
    expiration_ymd: str,
    target_lot_ids: list[str] | tuple[str, ...],
    futu_account_id: str | None = None,
) -> str:
    account_value = str(account or "").strip().lower()
    broker_value = str(broker or "").strip().lower()
    contract = str(contract_key or "").strip()
    side = str(position_side or "").strip().lower()
    expiration = str(expiration_ymd or "").strip()
    lot_ids = sorted(str(item or "").strip() for item in target_lot_ids if str(item or "").strip())
    if not account_value or not broker_value or not contract or not side or not expiration or not lot_ids:
        raise ValueError("lifecycle case key requires account, broker, contract, side, expiration and target lots")
    if len(lot_ids) != len(set(lot_ids)):
        raise ValueError("lifecycle case key target lot ids must be unique")
    pieces = (
        account_value,
        broker_value,
        contract,
        side,
        expiration,
        ",".join(lot_ids),
    )
    futu_account_value = str(futu_account_id or "").strip()
    if futu_account_value:
        pieces = (*pieces, futu_account_value)
    return hashlib.sha256("\x1f".join(pieces).encode("utf-8")).hexdigest()


def build_lifecycle_case(
    *,
    account: str,
    broker: str,
    contract_key: str,
    position_side: str,
    expiration_ymd: str,
    market: str,
    target_contracts_by_lot: dict[str, Any],
    futu_account_id: str | None = None,
) -> dict[str, Any]:
    target_manifest = normalize_target_manifest(target_contracts_by_lot)
    observation_start, boundary_reason = _expiration_observation_boundary(expiration_ymd, market)
    case_key = lifecycle_case_key(
        account=account,
        broker=broker,
        contract_key=contract_key,
        position_side=position_side,
        expiration_ymd=expiration_ymd,
        target_lot_ids=tuple(target_manifest),
        futu_account_id=futu_account_id,
    )
    if observation_start is None:
        status = "needs_review"
        reason_codes = [boundary_reason or "market_expiration_policy_missing"]
        pending_until = None
    else:
        status = "waiting_settlement_evidence"
        reason_codes = []
        pending_until = observation_start + PENDING_ELAPSED_HOURS * 60 * 60 * 1000
    return {
        "schema_version": LIFECYCLE_CASE_SCHEMA,
        "case_id": case_key,
        "case_key": case_key,
        "account": str(account or "").strip().lower(),
        "broker": str(broker or "").strip().lower(),
        "futu_account_id": str(futu_account_id or "").strip() or None,
        "contract_key": str(contract_key or "").strip(),
        "position_side": str(position_side or "").strip().lower(),
        "expiration_ymd": str(expiration_ymd or "").strip(),
        "target_contracts_by_lot": target_manifest,
        "observation_start_ms": observation_start,
        "pending_until_ms": pending_until,
        "status": status,
        "reason_codes": reason_codes,
    }


def pending_close_quantities(
    allocations: Iterable[dict[str, Any]],
    trade_events: Iterable[dict[str, Any]],
    *,
    void_event_ids: Iterable[str] = (),
) -> dict[str, int]:
    """Read reason-pending quantities from effective close events."""
    voided = set(void_event_ids)
    events = {
        str(event.get("event_id") or ""): event
        for event in trade_events
        if isinstance(event, dict)
    }
    pending: dict[str, int] = {}
    for allocation in allocations:
        if str(allocation.get("terminal_type") or "").strip().lower() != "close":
            continue
        event_id = str(allocation.get("canonical_terminal_event_id") or "").strip()
        if event_id in voided or bool(allocation.get("voided")):
            continue
        event = events.get(event_id)
        if event is None:
            raise ValueError("lifecycle_close_event_missing")
        payload = event.get("raw_payload") or {}
        if not isinstance(payload, dict):
            raise ValueError("lifecycle_close_payload_invalid")
        if str(payload.get("close_type") or "").strip().lower() != "cause_pending":
            continue
        lot_id = str(allocation.get("target_lot_id") or "").strip()
        quantity = int(allocation.get("contracts_allocated") or 0)
        if not lot_id or quantity <= 0:
            raise ValueError("lifecycle_pending_close_allocation_invalid")
        pending[lot_id] = pending.get(lot_id, 0) + quantity
    return pending


def derive_lifecycle_read_model(
    *,
    expiration_ymd: str,
    market: str,
    target_contracts_by_lot: dict[str, Any],
    allocations: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    void_event_ids: Iterable[str] = (),
    accepted_option_close_contracts_by_lot: dict[str, Any] | None = None,
    pending_close_contracts_by_lot: dict[str, int] | None = None,
    now_ms: int | None = None,
    conflict_reason_codes: list[str] | tuple[str, ...] = (),
    orphan_evidence: bool = False,
    quantity_drift: bool = False,
    observation_start_ms_override: int | None = None,
    pending_until_ms_override: int | None = None,
) -> LifecycleReadModel:
    target_manifest = normalize_target_manifest(target_contracts_by_lot)
    pending_close = dict(pending_close_contracts_by_lot or {})
    reservation, reservation_reasons = _reservation_overlay(
        target_manifest=target_manifest,
        allocations=allocations,
        void_event_ids=void_event_ids,
        accepted_option_close_contracts_by_lot=accepted_option_close_contracts_by_lot,
    )
    observation_start, boundary_reason = _expiration_observation_boundary(
        expiration_ymd,
        market,
    )
    if observation_start_ms_override is not None:
        observation_start = int(observation_start_ms_override)
        boundary_reason = None
    if observation_start is None:
        target = resolve_allocations(
            target_manifest,
            allocations,
            void_event_ids=void_event_ids,
        )
        return _read_model(
            state="needs_review",
            reasons=tuple(
                sorted(
                    set(
                        (
                            boundary_reason
                            or "market_expiration_policy_missing",
                            *reservation_reasons,
                        )
                    )
                )
            ),
            observation_start=None,
            pending_until=None,
            resolution=target,
            reservation=reservation,
        )
    pending_until = (
        int(pending_until_ms_override)
        if pending_until_ms_override is not None
        else observation_start
        + PENDING_ELAPSED_HOURS * 60 * 60 * 1000
    )
    resolution = resolve_allocations(
        target_manifest,
        allocations,
        void_event_ids=void_event_ids,
    )
    explicit_conflicts = tuple(sorted(set(str(item) for item in conflict_reason_codes if str(item))))
    if resolution.status == "conflict" or explicit_conflicts or reservation_reasons:
        return _read_model(
            state="conflict",
            reasons=tuple(
                sorted(
                    set(
                        resolution.reason_codes
                        + explicit_conflicts
                        + reservation_reasons
                    )
                )
            ),
            observation_start=observation_start,
            pending_until=pending_until,
            resolution=resolution,
            reservation=reservation,
        )
    if quantity_drift:
        return _read_model(
            state="conflict",
            reasons=("target_lot_quantity_drift",),
            observation_start=observation_start,
            pending_until=pending_until,
            resolution=resolution,
            reservation=reservation,
        )
    if orphan_evidence:
        return _read_model(
            state="needs_review",
            reasons=("evidence_without_allocation",),
            observation_start=observation_start,
            pending_until=pending_until,
            resolution=resolution,
            reservation=reservation,
        )
    current_ms = int(now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000)
    if any(pending_close.values()):
        if any(
            lot_id not in target_manifest
            or int(quantity) <= 0
            or int(quantity) > int(resolution.resolved_contracts_by_lot.get(lot_id, 0))
            for lot_id, quantity in pending_close.items()
        ):
            return _read_model(
                state="conflict", reasons=("pending_close_quantity_conflict",),
                observation_start=observation_start, pending_until=pending_until,
                resolution=resolution, reservation=reservation,
            )
        return _read_model(
            state=("settlement_pending" if resolution.remaining_contracts == 0
                   else "partially_resolved"),
            reasons=("close_reason_pending",),
            observation_start=observation_start, pending_until=pending_until,
            resolution=resolution, reservation=reservation,
            pending_close=pending_close,
        )
    if current_ms < observation_start and not any(reservation.values()):
        return _read_model(
            state="open",
            reasons=(),
            observation_start=observation_start,
            pending_until=pending_until,
            resolution=resolution,
            reservation=reservation,
        )
    if resolution.remaining_contracts == 0:
        terminal_types = set(resolution.resolved_contracts_by_terminal_type)
        if terminal_types == {"assignment"}:
            state = "assigned"
        elif terminal_types == {"exercise"}:
            state = "exercised"
        elif terminal_types == {"expire_close"}:
            state = "expired_unassigned"
        elif terminal_types == {"close"}:
            state = "closed"
        else:
            state = "resolved_mixed"
        return _read_model(
            state=state,
            reasons=(),
            observation_start=observation_start,
            pending_until=pending_until,
            resolution=resolution,
            reservation=reservation,
        )
    if resolution.resolved_contracts > 0 and current_ms < pending_until:
        return _read_model(
            state="partially_resolved",
            reasons=("terminal_evidence_partial",),
            observation_start=observation_start,
            pending_until=pending_until,
            resolution=resolution,
            reservation=reservation,
        )
    if any(reservation.values()):
        return _read_model(
            state="settlement_pending",
            reasons=("awaiting_settlement_evidence",),
            observation_start=observation_start,
            pending_until=pending_until,
            resolution=resolution,
            reservation=reservation,
        )
    if current_ms >= pending_until:
        return _read_model(
            state="needs_review",
            reasons=(
                ("settlement_evidence_deadline_elapsed",)
                if pending_until_ms_override is not None
                else ("lifecycle_timing_policy_unavailable",)
            ),
            observation_start=observation_start,
            pending_until=pending_until,
            resolution=resolution,
            reservation=reservation,
        )
    return _read_model(
        state="settlement_pending",
        reasons=("awaiting_settlement_evidence",),
        observation_start=observation_start,
        pending_until=pending_until,
        resolution=resolution,
        reservation=reservation,
    )


def _reservation_overlay(
    *,
    target_manifest: dict[str, int],
    allocations: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    void_event_ids: Iterable[str],
    accepted_option_close_contracts_by_lot: dict[str, Any] | None,
) -> tuple[dict[str, int], tuple[str, ...]]:
    resolution = resolve_allocations(
        target_manifest,
        allocations,
        void_event_ids=void_event_ids,
    )
    raw = accepted_option_close_contracts_by_lot or {}
    if not isinstance(raw, dict):
        return ({lot_id: 0 for lot_id in target_manifest}, ("reservation_manifest_invalid",))
    reservation = {lot_id: 0 for lot_id in target_manifest}
    reasons: set[str] = set()
    for raw_lot_id, raw_contracts in raw.items():
        lot_id = str(raw_lot_id or "").strip()
        if lot_id not in target_manifest or isinstance(raw_contracts, bool):
            reasons.add("reservation_target_unknown")
            continue
        try:
            numeric = Decimal(str(raw_contracts))
            contracts = int(numeric)
        except (InvalidOperation, TypeError, ValueError, OverflowError):
            reasons.add("reservation_quantity_invalid")
            continue
        if not numeric.is_finite() or contracts <= 0 or numeric != contracts:
            reasons.add("reservation_quantity_invalid")
            continue
        effective_remaining = resolution.remaining_contracts_by_lot.get(lot_id, 0)
        if contracts > effective_remaining:
            reasons.add("reservation_exceeds_effective_remaining")
            continue
        reservation[lot_id] = contracts
    return reservation, tuple(sorted(reasons))


def _read_model(
    *,
    state: str,
    reasons: tuple[str, ...],
    observation_start: int | None,
    pending_until: int | None,
    resolution: AllocationResolution,
    reservation: dict[str, int],
    pending_close: dict[str, int] | None = None,
) -> LifecycleReadModel:
    covered_by_lot = {
        lot_id: int(resolution.resolved_contracts_by_lot.get(lot_id, 0))
        + int(reservation.get(lot_id, 0))
        for lot_id in resolution.target_contracts_by_lot
    }
    covered_total = sum(covered_by_lot.values())
    target_total = sum(resolution.target_contracts_by_lot.values())
    if state == "conflict":
        closure_fact = "closure_conflict"
    elif covered_total <= 0:
        closure_fact = "open"
    elif covered_total < target_total:
        closure_fact = "partial_close_observed"
    else:
        closure_fact = "option_leg_closed"

    if state == "conflict":
        reason_state = "conflict"
    elif state == "needs_review":
        reason_state = "needs_review"
    elif any((pending_close or {}).values()):
        reason_state = "cause_pending"
    elif resolution.remaining_contracts == 0:
        reason_state = "resolved"
    elif resolution.resolved_contracts > 0:
        reason_state = "partially_resolved"
    elif any(reservation.values()):
        reason_state = "cause_pending"
    else:
        reason_state = "not_started"

    terminal_types = set(resolution.resolved_contracts_by_terminal_type)
    close_reason = None
    if reason_state == "resolved" and len(terminal_types) == 1:
        terminal_type = next(iter(terminal_types))
        close_reason = {
            "close": "trade_close",
            "assignment": "assignment",
            "exercise": "exercise",
            "expire_close": "expiration_no_settlement",
        }.get(terminal_type)
    return LifecycleReadModel(
        lifecycle_state=state,
        lifecycle_reason_codes=reasons,
        observation_start_ms=observation_start,
        pending_until_ms=pending_until,
        resolved_contracts_by_lot=resolution.resolved_contracts_by_lot,
        remaining_contracts_by_lot=resolution.remaining_contracts_by_lot,
        resolved_contracts_by_terminal_type=resolution.resolved_contracts_by_terminal_type,
        reserved_contracts_by_lot=dict(reservation),
        closure_fact=closure_fact,
        reason_state=reason_state,
        close_reason=close_reason,
        actionable=state == "open" and not any(reservation.values()),
    )


__all__ = [
    "ASSIGNMENT_WAITING_STATUS",
    "FINAL_STATUSES",
    "INSTRUMENT_POLICY_REGISTRY",
    "INSTRUMENT_POLICY_SCHEMA",
    "LIFECYCLE_CASE_SCHEMA",
    "LifecycleReadModel",
    "MARKET_TIMEZONES",
    "SUPPORTED_LAST_TRADE_SOURCES",
    "PENDING_ELAPSED_HOURS",
    "PENDING_STATUSES",
    "build_lifecycle_case",
    "derive_lifecycle_read_model",
    "expiration_observation_start_ms",
    "lifecycle_case_key",
    "normalize_market",
    "pending_close_quantities",
]


class LifecycleCaseDataError(ValueError):
    """A malformed case-local fact that may be isolated by a batch reader."""


def build_lifecycle_read_models_from_resolved_account(
    *,
    cases: list[dict[str, Any]],
    allocations: list[dict[str, Any]],
    trade_events: list[dict[str, Any]],
    timing_policies: list[dict[str, Any]],
    position_lots: list[dict[str, Any]],
    account_resolution: Mapping[str, Any],
    void_event_ids: tuple[str, ...] | list[str] = (),
    now_ms: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Build lot models from one already-resolved account generation."""

    cases_by_id = {
        str(item.get("case_id") or "").strip(): dict(item)
        for item in cases
        if isinstance(item, dict)
        and str(item.get("case_id") or "").strip()
    }
    allocations_by_case: dict[str, list[dict[str, Any]]] = {}
    for item in allocations:
        if not isinstance(item, dict):
            continue
        allocations_by_case.setdefault(
            str(item.get("case_id") or "").strip(),
            [],
        ).append(dict(item))
    timing_by_case = {
        str(item.get("case_id") or "").strip(): dict(item)
        for item in timing_policies
        if isinstance(item, dict)
        and str(item.get("case_id") or "").strip()
    }
    lots_by_id = {
        str(item.get("record_id") or "").strip(): dict(item.get("fields") or {})
        for item in position_lots
        if isinstance(item, dict)
        and str(item.get("record_id") or "").strip()
    }
    tokens_by_case = {
        str(item.get("case_id") or "").strip(): dict(item)
        for item in account_resolution.get("generation_tokens") or []
        if isinstance(item, Mapping)
        and str(item.get("case_id") or "").strip()
    }
    output: dict[str, dict[str, Any]] = {}
    for raw_resolution in account_resolution.get("case_resolutions") or []:
        if not isinstance(raw_resolution, Mapping):
            continue
        case_resolution = dict(raw_resolution)
        case_id = str(case_resolution.get("case_id") or "").strip()
        lifecycle_case = cases_by_id.get(case_id)
        generation_token = tokens_by_case.get(case_id)
        if lifecycle_case is None or generation_token is None:
            continue
        target_ids = sorted(
            str(item or "").strip()
            for item in dict(
                lifecycle_case.get("target_contracts_by_lot") or {}
            )
            if str(item or "").strip()
        )
        try:
            model = build_lifecycle_case_read_model_from_resolved_facts(
                lifecycle_case=lifecycle_case,
                case_resolution=case_resolution,
                generation_token=generation_token,
                allocations=allocations_by_case.get(case_id, []),
                trade_events=trade_events,
                timing_policy=timing_by_case.get(case_id),
                position_lot_fields_by_id=lots_by_id,
                void_event_ids=tuple(void_event_ids),
                now_ms=now_ms,
            )
        except LifecycleCaseDataError as exc:
            model = _isolated_case_error_model(
                lifecycle_case=lifecycle_case,
                case_id=case_id,
                generation_token=generation_token,
                error=exc,
            )
        for lot_id in target_ids:
            if lot_id not in output:
                output[lot_id] = dict(model)
                continue
            prior = output[lot_id]
            case_ids = sorted(
                {
                    str(prior.get("lifecycle_case_id") or ""),
                    case_id,
                }
                - {""}
            )
            output[lot_id] = {
                **prior,
                "lifecycle_state": "conflict",
                "lifecycle_case_ids": case_ids,
                "lifecycle_evidence_status": "conflict",
                "lifecycle_reason_codes": sorted(
                    {
                        *prior.get("lifecycle_reason_codes", []),
                        *model.get("lifecycle_reason_codes", []),
                        "reservation_target_overlap",
                    }
                ),
                "reason_state": "conflict",
                "actionable": False,
            }
    return output


def build_lifecycle_case_read_model_from_resolved_facts(
    *,
    lifecycle_case: dict[str, Any],
    case_resolution: dict[str, Any],
    generation_token: dict[str, Any],
    allocations: list[dict[str, Any]],
    trade_events: list[dict[str, Any]],
    timing_policy: dict[str, Any] | None,
    position_lot_fields_by_id: dict[str, dict[str, Any]],
    void_event_ids: tuple[str, ...] | list[str],
    now_ms: int | None,
) -> dict[str, Any]:
    case_id = str(lifecycle_case.get("case_id") or "").strip()
    if not case_id or str(case_resolution.get("case_id") or "") != case_id:
        raise LifecycleCaseDataError("lifecycle_case_resolution_mismatch")
    try:
        target_manifest = {
            str(key): int(value)
            for key, value in dict(
                lifecycle_case.get("target_contracts_by_lot") or {}
            ).items()
        }
        resolution = resolve_allocations(
            target_manifest,
            allocations,
            void_event_ids=void_event_ids,
        )
        quantity_drift = any(
            lot_id not in position_lot_fields_by_id
            or int(
                position_lot_fields_by_id[lot_id].get("contracts_open")
                or 0
            )
            != expected_remaining
            for lot_id, expected_remaining in (
                resolution.remaining_contracts_by_lot.items()
            )
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise LifecycleCaseDataError(
            "lifecycle_case_quantity_invalid"
        ) from exc

    anchor_facts = [
        dict(item)
        for item in case_resolution.get("anchor_facts") or []
        if isinstance(item, Mapping)
    ]
    effective_timing: dict[str, Any] | None = None
    timing_error: str | None = None
    if timing_policy is not None and anchor_facts:
        try:
            effective_timing = derive_effective_lifecycle_timing(
                policy=timing_policy,
                option_close_evidence=[
                    {
                        "evidence_type": "option_zero_price_close",
                        "received_at_ms": item.get("received_at_ms"),
                    }
                    for item in anchor_facts
                ],
            )
        except (TypeError, ValueError, OverflowError) as exc:
            timing_error = str(exc)

    persisted_status = str(
        lifecycle_case.get("status") or ""
    ).strip().lower()
    summary = (
        dict(lifecycle_case.get("derived_summary") or {})
        if isinstance(lifecycle_case.get("derived_summary"), dict)
        else {}
    )
    conflict_reasons = set(
        str(item)
        for item in case_resolution.get("reason_codes") or []
        if str(item or "").strip()
    )
    if persisted_status == "conflict":
        conflict_reasons.update(
            str(item)
            for item in summary.get("lifecycle_reason_codes") or []
            if str(item or "").strip()
        )
    if timing_error and case_resolution.get("status") in {"direct", "bridged"}:
        conflict_reasons.add("lifecycle_effective_timing_invalid")

    try:
        pending_close = pending_close_quantities(
            allocations, trade_events, void_event_ids=void_event_ids,
        )
        read_model = derive_lifecycle_read_model(
            expiration_ymd=str(lifecycle_case.get("expiration_ymd") or ""),
            market=str(
                lifecycle_case.get("market")
                or symbol_market(lifecycle_case.get("symbol"))
                or ""
            ),
            target_contracts_by_lot=target_manifest,
            allocations=allocations,
            pending_close_contracts_by_lot=pending_close,
            void_event_ids=void_event_ids,
            accepted_option_close_contracts_by_lot=dict(
                case_resolution.get("effective_reservations_by_lot") or {}
            ),
            now_ms=now_ms,
            conflict_reason_codes=tuple(sorted(conflict_reasons)),
            quantity_drift=quantity_drift,
            observation_start_ms_override=(
                int(lifecycle_case["observation_start_ms"])
                if lifecycle_case.get("observation_start_ms") is not None
                else None
            ),
            pending_until_ms_override=(
                int(
                    (effective_timing or timing_policy or {}).get(
                        "settlement_deadline_ms"
                    )
                )
                if (effective_timing or timing_policy or {}).get(
                    "settlement_deadline_ms"
                )
                is not None
                else None
            ),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise LifecycleCaseDataError(
            "lifecycle_case_read_model_invalid"
        ) from exc

    effective_allocations = [
        item
        for item in allocations
        if not bool(item.get("voided"))
        and str(item.get("canonical_terminal_event_id") or "").strip()
        not in set(void_event_ids)
    ]
    anchor_status = str(case_resolution.get("status") or "missing")
    if read_model.lifecycle_state == "conflict":
        evidence_status = "conflict"
    elif anchor_status in {"direct", "bridged"} and any(
        read_model.reserved_contracts_by_lot.values()
    ):
        evidence_status = "closure_observed_cause_pending"
    elif pending_close:
        evidence_status = "closure_observed_cause_pending"
    elif anchor_status == "missing" and not effective_allocations:
        evidence_status = "missing"
    elif any(read_model.remaining_contracts_by_lot.values()):
        evidence_status = "partial"
    else:
        evidence_status = "complete"

    persisted_reason_state = str(
        summary.get("reason_state") or ""
    ).strip().lower()
    effective_reason_state = (
        persisted_reason_state
        if persisted_status in {"needs_review", "conflict"}
        and persisted_reason_state in {"needs_review", "conflict"}
        else read_model.reason_state
    )
    effective_reason_codes = sorted(
        {
            *read_model.lifecycle_reason_codes,
            *(
                str(item)
                for item in summary.get("lifecycle_reason_codes") or []
                if str(item or "").strip()
            ),
        }
    )
    effective_close_reason = (
        str(summary.get("close_reason") or "").strip()
        if effective_reason_state in {"needs_review", "conflict"}
        else ""
    ) or read_model.close_reason
    return {
        "schema_version": "option_lifecycle_read_model.v3",
        "lifecycle_state": read_model.lifecycle_state,
        "lifecycle_case_id": case_id,
        "lifecycle_evidence_status": evidence_status,
        "lifecycle_reason_codes": effective_reason_codes,
        "observation_start_ms": read_model.observation_start_ms,
        "pending_until_ms": read_model.pending_until_ms,
        "pairing_until_ms": (
            int(effective_timing["pairing_until_ms"])
            if effective_timing is not None
            else None
        ),
        "first_option_close_received_at_ms": (
            int(effective_timing["first_option_close_received_at_ms"])
            if effective_timing is not None
            else None
        ),
        "last_option_close_received_at_ms": (
            int(effective_timing["last_option_close_received_at_ms"])
            if effective_timing is not None
            else None
        ),
        "timing_policy_hash": (
            str(effective_timing["timing_policy_hash"])
            if effective_timing is not None
            else (
                canonical_hash(timing_policy)
                if timing_policy is not None
                else None
            )
        ),
        "timing_error": timing_error,
        "terminal_event_ids": sorted(
            str(item.get("canonical_terminal_event_id") or "").strip()
            for item in effective_allocations
            if str(item.get("canonical_terminal_event_id") or "").strip()
        ),
        "target_contracts_by_lot": target_manifest,
        "resolved_contracts_by_lot": read_model.resolved_contracts_by_lot,
        "remaining_contracts_by_lot": read_model.remaining_contracts_by_lot,
        "resolved_contracts_by_terminal_type": (
            read_model.resolved_contracts_by_terminal_type
        ),
        "reserved_contracts_by_lot": read_model.reserved_contracts_by_lot,
        "pending_close_contracts_by_lot": pending_close,
        "closure_fact": read_model.closure_fact,
        "reason_state": effective_reason_state,
        "close_reason": effective_close_reason,
        "allocation_ids": sorted(
            str(item.get("allocation_id") or "").strip()
            for item in effective_allocations
            if str(item.get("allocation_id") or "").strip()
        ),
        "voided_terminal_event_ids": sorted(
            {
                str(item.get("canonical_terminal_event_id") or "").strip()
                for item in allocations
                if str(item.get("canonical_terminal_event_id") or "").strip()
                in set(void_event_ids)
            }
        ),
        "reservation_evidence_ids": sorted(
            str(item.get("source_owner_evidence_id") or "").strip()
            for item in anchor_facts
            if str(item.get("source_owner_evidence_id") or "").strip()
            and any(read_model.reserved_contracts_by_lot.values())
        ),
        "orphan_evidence_ids": [],
        "lifecycle_generation_token": str(
            generation_token.get("generation_token") or ""
        ),
        "actionable": (
            read_model.actionable
            and effective_reason_state
            not in {
                "cause_pending",
                "partially_resolved",
                "needs_review",
                "conflict",
            }
        ),
    }


def _isolated_case_error_model(
    *,
    lifecycle_case: dict[str, Any],
    case_id: str,
    generation_token: dict[str, Any],
    error: LifecycleCaseDataError,
) -> dict[str, Any]:
    return {
        "schema_version": "option_lifecycle_read_model.v3",
        "lifecycle_state": "conflict",
        "lifecycle_case_id": case_id,
        "lifecycle_evidence_status": "conflict",
        "lifecycle_reason_codes": [str(error)],
        "target_contracts_by_lot": dict(
            lifecycle_case.get("target_contracts_by_lot") or {}
        ),
        "resolved_contracts_by_lot": {},
        "remaining_contracts_by_lot": {},
        "resolved_contracts_by_terminal_type": {},
        "reserved_contracts_by_lot": {},
        "closure_fact": "unknown",
        "reason_state": "conflict",
        "close_reason": None,
        "allocation_ids": [],
        "terminal_event_ids": [],
        "voided_terminal_event_ids": [],
        "reservation_evidence_ids": [],
        "orphan_evidence_ids": [],
        "pairing_until_ms": None,
        "pending_until_ms": None,
        "timing_error": str(error),
        "lifecycle_generation_token": str(
            generation_token.get("generation_token") or ""
        ),
        "actionable": False,
    }
