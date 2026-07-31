from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from domain.domain.lifecycle_allocation import (
    AllocationResolution,
    normalize_target_manifest,
    resolve_allocations,
)


LIFECYCLE_CASE_SCHEMA = "lifecycle_case.v2"
PENDING_ELAPSED_HOURS = 72

MARKET_TIMEZONES = {
    "US": "America/New_York",
    "HK": "Asia/Hong_Kong",
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


def derive_lifecycle_read_model(
    *,
    expiration_ymd: str,
    market: str,
    target_contracts_by_lot: dict[str, Any],
    allocations: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    void_event_ids: Iterable[str] = (),
    accepted_option_close_contracts_by_lot: dict[str, Any] | None = None,
    now_ms: int | None = None,
    conflict_reason_codes: list[str] | tuple[str, ...] = (),
    orphan_evidence: bool = False,
    quantity_drift: bool = False,
    observation_start_ms_override: int | None = None,
    pending_until_ms_override: int | None = None,
) -> LifecycleReadModel:
    target_manifest = normalize_target_manifest(target_contracts_by_lot)
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
            reasons=("settlement_evidence_deadline_elapsed",),
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
    "LIFECYCLE_CASE_SCHEMA",
    "LifecycleReadModel",
    "MARKET_TIMEZONES",
    "PENDING_ELAPSED_HOURS",
    "build_lifecycle_case",
    "derive_lifecycle_read_model",
    "expiration_observation_start_ms",
    "lifecycle_case_key",
    "normalize_market",
]
