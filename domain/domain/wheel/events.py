"""Wheel event identity: validation, payload hashing and event construction."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from domain.domain.decision_state_fingerprint import canonical_sha256

from ._common import (
    WHEEL_EVENT_SCHEMA_V1,
    WHEEL_EVENT_SCHEMA_V2,
    WHEEL_EVENT_TYPES,
    WHEEL_EVENT_TYPES_V1,
    _wheel_market,
)


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"wheel event requires {field}")
    return text


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a positive integer") from None
    if number <= 0 or str(value).strip() not in {str(number), f"{number}.0"}:
        raise ValueError(f"{field} must be a positive integer")
    return number


def wheel_event_payload_hash(event: Mapping[str, Any]) -> str:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("wheel event payload must be an object")
    schema_version = str(
        event.get("event_schema_version") or WHEEL_EVENT_SCHEMA_V1
    ).strip()
    if schema_version not in {WHEEL_EVENT_SCHEMA_V1, WHEEL_EVENT_SCHEMA_V2}:
        raise ValueError(f"unsupported wheel event schema: {schema_version}")
    canonical = {
            "schema_version": schema_version,
            "account": str(event.get("account") or "").strip().lower(),
            "stock_lot_id": str(event.get("stock_lot_id") or "").strip(),
            "event_type": str(event.get("event_type") or "").strip().lower(),
            "occurred_at_ms": int(event.get("occurred_at_ms") or 0),
            "intent_id": str(event.get("intent_id") or "").strip() or None,
            "source_trade_event_id": (
                str(event.get("source_trade_event_id") or "").strip() or None
            ),
            "payload": dict(payload),
        }
    if schema_version == WHEEL_EVENT_SCHEMA_V2:
        canonical["wheel_branch_id"] = str(
            event.get("wheel_branch_id") or ""
        ).strip()
        canonical["stock_lot_id"] = (
            str(event.get("stock_lot_id") or "").strip() or None
        )
    return canonical_sha256(canonical)


def normalize_wheel_event(event: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise TypeError("wheel event must be an object")
    event_id = _required_text(event.get("event_id"), "event_id")
    account = _required_text(event.get("account"), "account")
    if account != account.lower():
        raise ValueError("wheel event account must be lowercase")
    event_schema_version = str(
        event.get("event_schema_version") or WHEEL_EVENT_SCHEMA_V1
    ).strip()
    if event_schema_version not in {WHEEL_EVENT_SCHEMA_V1, WHEEL_EVENT_SCHEMA_V2}:
        raise ValueError(
            f"unsupported wheel event schema: {event_schema_version}"
        )
    stock_lot_id = str(event.get("stock_lot_id") or "").strip() or None
    if event_schema_version == WHEEL_EVENT_SCHEMA_V1 and stock_lot_id is None:
        raise ValueError("wheel event requires stock_lot_id")
    wheel_branch_id = str(event.get("wheel_branch_id") or "").strip()
    if event_schema_version == WHEEL_EVENT_SCHEMA_V1:
        if wheel_branch_id and wheel_branch_id != stock_lot_id:
            raise ValueError("wheel_event.v1 branch must equal stock_lot_id")
        wheel_branch_id = str(stock_lot_id)
    else:
        wheel_branch_id = _required_text(wheel_branch_id, "wheel_branch_id")
    event_type = _required_text(event.get("event_type"), "event_type").lower()
    allowed_types = (
        WHEEL_EVENT_TYPES_V1
        if event_schema_version == WHEEL_EVENT_SCHEMA_V1
        else WHEEL_EVENT_TYPES
    )
    if event_type not in allowed_types:
        raise ValueError(f"unsupported wheel event type: {event_type}")
    occurred_at_ms = _positive_int(event.get("occurred_at_ms"), "occurred_at_ms")
    recorded_at_ms = _positive_int(event.get("recorded_at_ms"), "recorded_at_ms")
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("wheel event payload must be an object")
    intent_id = str(event.get("intent_id") or "").strip() or None
    source_trade_event_id = (
        str(event.get("source_trade_event_id") or "").strip() or None
    )
    if (
        event_type.startswith("wheel_call_intent_")
        or event_type.startswith("wheel_put_intent_")
    ) and not intent_id:
        raise ValueError(f"{event_type} requires intent_id")
    if event_type == "wheel_branch_created":
        direction = str(payload.get("direction") or "").strip().lower()
        if direction not in {"call", "put"}:
            raise ValueError("wheel_branch_created requires direction=call|put")
    if event_type == "wheel_branch_decided":
        decision = str(payload.get("decision") or "").strip().lower()
        if decision not in {"start", "end"}:
            raise ValueError("wheel_branch_decided requires decision=start|end")
    if event_type == "wheel_event_voided":
        _required_text(payload.get("target_wheel_event_id"), "target_wheel_event_id")
    normalized = {
        "event_id": event_id,
        "event_schema_version": event_schema_version,
        "account": account,
        "wheel_branch_id": wheel_branch_id,
        "stock_lot_id": stock_lot_id,
        "event_type": event_type,
        "occurred_at_ms": occurred_at_ms,
        "recorded_at_ms": recorded_at_ms,
        "intent_id": intent_id,
        "source_trade_event_id": source_trade_event_id,
        "payload": dict(payload),
    }
    payload_hash = wheel_event_payload_hash(normalized)
    supplied_hash = str(event.get("payload_hash") or "").strip()
    if supplied_hash and supplied_hash != payload_hash:
        raise ValueError(f"wheel event payload hash mismatch: event_id={event_id}")
    normalized["payload_hash"] = payload_hash
    return normalized


def build_wheel_event(
    *,
    event_id: str,
    account: str,
    stock_lot_id: str | None,
    wheel_branch_id: str | None = None,
    event_schema_version: str = WHEEL_EVENT_SCHEMA_V2,
    event_type: str,
    occurred_at_ms: int,
    recorded_at_ms: int,
    payload: Mapping[str, Any],
    intent_id: str | None = None,
    source_trade_event_id: str | None = None,
) -> dict[str, Any]:
    return normalize_wheel_event(
        {
            "event_id": event_id,
            "event_schema_version": event_schema_version,
            "account": account,
            "wheel_branch_id": wheel_branch_id or stock_lot_id,
            "stock_lot_id": stock_lot_id,
            "event_type": event_type,
            "occurred_at_ms": occurred_at_ms,
            "recorded_at_ms": recorded_at_ms,
            "intent_id": intent_id,
            "source_trade_event_id": source_trade_event_id,
            "payload": dict(payload),
        }
    )


def deterministic_wheel_branch_id(
    account: str,
    source_assignment_event_id: str,
    direction: str,
) -> str:
    account_value = _required_text(account, "account").lower()
    source_event_id = _required_text(
        source_assignment_event_id,
        "source_assignment_event_id",
    )
    direction_value = _required_text(direction, "direction").lower()
    if direction_value not in {"call", "put"}:
        raise ValueError("Wheel branch direction must be call or put")
    digest = canonical_sha256(
        {
            "schema_version": "wheel_branch_identity.v1",
            "account": account_value,
            "source_assignment_event_id": source_event_id,
            "direction": direction_value,
        }
    )[:32]
    return f"wheel-{direction_value}-{digest}"


def build_wheel_branch_created_event(
    *,
    account: str,
    source_assignment_event_id: str,
    direction: str,
    occurred_at_ms: int,
    recorded_at_ms: int,
    symbol: str,
    contracts: int,
    multiplier: int | None,
    multiplier_source: str,
    multiplier_evidence_hash: str,
    currency: str | None,
    principal_anchor: str | None,
    principal_anchor_reason: str | None = None,
    principal_anchor_fact_ids: Sequence[str] = (),
    stock_lot_id: str | None = None,
    parent_branch_id: str | None = None,
    lifecycle_status: str = "active",
    activation_window: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    direction_value = _required_text(direction, "direction").lower()
    if direction_value not in {"call", "put"}:
        raise ValueError("Wheel branch direction must be call or put")
    status = _required_text(lifecycle_status, "lifecycle_status").lower()
    if status not in {"active", "pending_decision"}:
        raise ValueError("Wheel branch lifecycle_status must be active or pending_decision")
    account_value = _required_text(account, "account").lower()
    source_event_id = _required_text(
        source_assignment_event_id,
        "source_assignment_event_id",
    )
    stock_lot_value = str(stock_lot_id or "").strip() or None
    if direction_value == "call" and stock_lot_value is None:
        raise ValueError("Wheel Call branch requires stock_lot_id")
    branch_id = (
        stock_lot_value
        if direction_value == "call"
        else deterministic_wheel_branch_id(
            account_value,
            source_event_id,
            direction_value,
        )
    )
    symbol_value = _required_text(symbol, "symbol").upper()
    payload = {
        "schema_version": "wheel_branch_created.v1",
        "market": _wheel_market(symbol_value),
        "direction": direction_value,
        "parent_branch_id": str(parent_branch_id or "").strip() or None,
        "source_assignment_event_id": source_event_id,
        "symbol": symbol_value,
        "contracts": _positive_int(contracts, "contracts"),
        "multiplier": (
            _positive_int(multiplier, "multiplier") if multiplier is not None else None
        ),
        "multiplier_source": _required_text(
            multiplier_source,
            "multiplier_source",
        ),
        "multiplier_evidence_hash": _required_text(
            multiplier_evidence_hash,
            "multiplier_evidence_hash",
        ),
        "currency": str(currency or "").strip().upper() or None,
        "principal_anchor": (
            str(principal_anchor).strip() if principal_anchor is not None else None
        ),
        "principal_anchor_reason": (
            str(principal_anchor_reason or "").strip() or None
        ),
        "principal_anchor_fact_ids": sorted(
            {str(value).strip() for value in principal_anchor_fact_ids if str(value).strip()}
        ),
        "initial_lifecycle_status": status,
        "activation_window": dict(activation_window or {}) or None,
    }
    return build_wheel_event(
        event_id=f"wheel-branch-created:{source_event_id}:{direction_value}",
        event_schema_version=WHEEL_EVENT_SCHEMA_V2,
        account=account_value,
        wheel_branch_id=branch_id,
        stock_lot_id=stock_lot_value,
        event_type="wheel_branch_created",
        occurred_at_ms=occurred_at_ms,
        recorded_at_ms=recorded_at_ms,
        source_trade_event_id=source_event_id,
        payload=payload,
    )
