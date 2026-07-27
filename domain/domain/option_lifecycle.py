from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
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
    pieces = (account_value, broker_value, contract, side, expiration, ",".join(lot_ids))
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
    now_ms: int | None = None,
    conflict_reason_codes: list[str] | tuple[str, ...] = (),
    orphan_evidence: bool = False,
    quantity_drift: bool = False,
) -> LifecycleReadModel:
    observation_start, boundary_reason = _expiration_observation_boundary(expiration_ymd, market)
    if observation_start is None:
        target = resolve_allocations(target_contracts_by_lot, allocations)
        return _read_model(
            state="needs_review",
            reasons=(boundary_reason or "market_expiration_policy_missing",),
            observation_start=None,
            pending_until=None,
            resolution=target,
        )
    pending_until = observation_start + PENDING_ELAPSED_HOURS * 60 * 60 * 1000
    resolution = resolve_allocations(target_contracts_by_lot, allocations)
    explicit_conflicts = tuple(sorted(set(str(item) for item in conflict_reason_codes if str(item))))
    if resolution.status == "conflict" or explicit_conflicts:
        return _read_model(
            state="conflict",
            reasons=tuple(sorted(set(resolution.reason_codes + explicit_conflicts))),
            observation_start=observation_start,
            pending_until=pending_until,
            resolution=resolution,
        )
    if quantity_drift:
        return _read_model(
            state="conflict",
            reasons=("target_lot_quantity_drift",),
            observation_start=observation_start,
            pending_until=pending_until,
            resolution=resolution,
        )
    if orphan_evidence:
        return _read_model(
            state="needs_review",
            reasons=("evidence_without_allocation",),
            observation_start=observation_start,
            pending_until=pending_until,
            resolution=resolution,
        )
    current_ms = int(now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000)
    if current_ms < observation_start:
        return _read_model(
            state="open",
            reasons=(),
            observation_start=observation_start,
            pending_until=pending_until,
            resolution=resolution,
        )
    if resolution.remaining_contracts == 0:
        terminal_types = set(resolution.resolved_contracts_by_terminal_type)
        if terminal_types == {"assignment"}:
            state = "assigned"
        elif terminal_types == {"exercise"}:
            state = "exercised"
        elif terminal_types == {"expire_close"}:
            state = "expired_unassigned"
        else:
            state = "resolved_mixed"
        return _read_model(
            state=state,
            reasons=(),
            observation_start=observation_start,
            pending_until=pending_until,
            resolution=resolution,
        )
    if resolution.resolved_contracts > 0 and current_ms < pending_until:
        return _read_model(
            state="partially_resolved",
            reasons=("terminal_evidence_partial",),
            observation_start=observation_start,
            pending_until=pending_until,
            resolution=resolution,
        )
    if current_ms >= pending_until:
        return _read_model(
            state="needs_review",
            reasons=("settlement_evidence_deadline_elapsed",),
            observation_start=observation_start,
            pending_until=pending_until,
            resolution=resolution,
        )
    return _read_model(
        state="settlement_pending",
        reasons=("awaiting_settlement_evidence",),
        observation_start=observation_start,
        pending_until=pending_until,
        resolution=resolution,
    )


def _read_model(
    *,
    state: str,
    reasons: tuple[str, ...],
    observation_start: int | None,
    pending_until: int | None,
    resolution: AllocationResolution,
) -> LifecycleReadModel:
    return LifecycleReadModel(
        lifecycle_state=state,
        lifecycle_reason_codes=reasons,
        observation_start_ms=observation_start,
        pending_until_ms=pending_until,
        resolved_contracts_by_lot=resolution.resolved_contracts_by_lot,
        remaining_contracts_by_lot=resolution.remaining_contracts_by_lot,
        resolved_contracts_by_terminal_type=resolution.resolved_contracts_by_terminal_type,
        actionable=state == "open",
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
