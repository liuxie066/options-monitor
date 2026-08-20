from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.lifecycle_allocation import allocation_id_for, terminal_event_id_for
from domain.domain.ledger.position_fields import (
    EXPIRE_AUTO_CLOSE,
    effective_contracts_open,
    effective_expiration_ymd,
    effective_multiplier,
    effective_strike,
    normalize_account,
    normalize_broker,
    strategy_metadata_fields_from_payload,
)
from domain.domain.option_position_identity import normalize_currency
from domain.domain.trade_contract_identity import canonical_contract_symbol
from src.application.ledger.lot_resolver import CloseTargetResolution
from src.application.ledger.preflight import _current_record_fields, preflight_broker_trade_close
from src.application.ledger.results import BrokerTradeOperation, LedgerWriteResult
from src.application.ledger.writer import persist_trade_event_objects_atomically


@dataclass(frozen=True)
class LifecycleLedgerWrite:
    operation: BrokerTradeOperation
    event_type: str
    case_id: str | None
    evidence_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "case_id": self.case_id,
            "evidence_ids": list(self.evidence_ids),
            "operation": self.operation.to_payload(),
        }


def persist_assignment_events(
    repo: Any,
    *,
    close_target_resolution: CloseTargetResolution,
    contracts_to_close: int,
    event_time_ms: int | None,
    case_id: str | None,
    evidence_ids: list[str] | tuple[str, ...] | None = None,
    stock_settlement: dict[str, Any] | None = None,
    source: str = "option_lifecycle_decision",
    manual_request_id: str | None = None,
    manual_request_intent_hash: str | None = None,
    wheel_start_enabled: bool = False,
) -> list[LifecycleLedgerWrite]:
    return _persist_lifecycle_close_events(
        repo,
        event_type="assignment",
        close_target_resolution=close_target_resolution,
        contracts_to_close=contracts_to_close,
        event_time_ms=event_time_ms,
        case_id=case_id,
        evidence_ids=evidence_ids,
        stock_settlement=stock_settlement,
        source=source,
        manual_request_id=manual_request_id,
        manual_request_intent_hash=manual_request_intent_hash,
        wheel_start_enabled=wheel_start_enabled,
    )


def persist_exercise_events(
    repo: Any,
    *,
    close_target_resolution: CloseTargetResolution,
    contracts_to_close: int,
    event_time_ms: int | None,
    case_id: str | None,
    evidence_ids: list[str] | tuple[str, ...] | None = None,
    stock_settlement: dict[str, Any] | None = None,
    source: str = "option_lifecycle_decision",
    manual_request_id: str | None = None,
    manual_request_intent_hash: str | None = None,
) -> list[LifecycleLedgerWrite]:
    return _persist_lifecycle_close_events(
        repo,
        event_type="exercise",
        close_target_resolution=close_target_resolution,
        contracts_to_close=contracts_to_close,
        event_time_ms=event_time_ms,
        case_id=case_id,
        evidence_ids=evidence_ids,
        stock_settlement=stock_settlement,
        source=source,
        manual_request_id=manual_request_id,
        manual_request_intent_hash=manual_request_intent_hash,
    )


def persist_expire_close_events(
    repo: Any,
    *,
    close_target_resolution: CloseTargetResolution,
    contracts_to_close: int,
    event_time_ms: int | None,
    case_id: str | None,
    evidence_ids: list[str] | tuple[str, ...] | None = None,
    close_reason: str = "expired_unassigned",
    source: str = "option_lifecycle_decision",
) -> list[LifecycleLedgerWrite]:
    return _persist_lifecycle_close_events(
        repo,
        event_type="expire_close",
        close_target_resolution=close_target_resolution,
        contracts_to_close=contracts_to_close,
        event_time_ms=event_time_ms,
        case_id=case_id,
        evidence_ids=evidence_ids,
        stock_settlement=None,
        source=source,
        close_reason=close_reason,
    )


def persist_lifecycle_expire_close_events_atomically(
    repo: Any,
    *,
    close_target_resolution: CloseTargetResolution,
    contracts_to_close: int,
    event_time_ms: int | None,
    lifecycle_case: dict[str, Any],
    option_evidence: dict[str, Any],
    close_reason: str = "expired_unassigned",
    source: str = "option_lifecycle_decision",
) -> list[LifecycleLedgerWrite]:
    case = dict(lifecycle_case or {})
    case_id = str(case.get("case_id") or "").strip()
    evidence_id = str(option_evidence.get("evidence_id") or "").strip()
    if not case_id or not evidence_id:
        raise ValueError("lifecycle auto-expire requires case_id and evidence_id")
    return _persist_lifecycle_close_events(
        repo,
        event_type="expire_close",
        close_target_resolution=close_target_resolution,
        contracts_to_close=contracts_to_close,
        event_time_ms=event_time_ms,
        case_id=case_id,
        evidence_ids=[evidence_id],
        stock_settlement=None,
        source=source,
        close_reason=close_reason,
        lifecycle_case_update=case,
        allocation_evidence_id=evidence_id,
    )


def _persist_lifecycle_close_events(
    repo: Any,
    *,
    event_type: str,
    close_target_resolution: CloseTargetResolution,
    contracts_to_close: int,
    event_time_ms: int | None,
    case_id: str | None,
    evidence_ids: list[str] | tuple[str, ...] | None = None,
    stock_settlement: dict[str, Any] | None = None,
    source: str = "option_lifecycle_decision",
    close_reason: str | None = None,
    lifecycle_case_update: dict[str, Any] | None = None,
    allocation_evidence_id: str | None = None,
    manual_request_id: str | None = None,
    manual_request_intent_hash: str | None = None,
    wheel_start_enabled: bool = False,
) -> list[LifecycleLedgerWrite]:
    normalized_event_type = str(event_type or "").strip().lower()
    if normalized_event_type not in {"assignment", "exercise", "expire_close"}:
        raise ValueError("lifecycle close event_type must be assignment, exercise, or expire_close")
    if not close_target_resolution.matches:
        raise ValueError(f"{normalized_event_type} requires at least one close target")
    _assert_lifecycle_resolution_contracts(
        normalized_event_type,
        close_target_resolution=close_target_resolution,
        contracts_to_close=contracts_to_close,
    )
    prepared: list[tuple[Any, int, Any, TradeEvent]] = []
    as_of_ms = int(event_time_ms) if event_time_ms is not None else None
    evidence_tuple = tuple(str(item) for item in (evidence_ids or []) if str(item or "").strip())
    for match in close_target_resolution.matches:
        record_id = str(match.record_id or "").strip()
        contracts = int(match.contracts_to_close or 0)
        if contracts <= 0:
            raise ValueError(f"{normalized_event_type} requires contracts_to_close > 0")

        fields = _current_record_fields(repo, record_id=record_id)
        ledger_preflight = preflight_broker_trade_close(
            repo,
            record_id=record_id,
            fields=fields,
            contracts_to_close=contracts,
            close_price=0.0,
            as_of_ms=as_of_ms,
            event_type=normalized_event_type,
        )
        allocation_id = None
        event_id = None
        if allocation_evidence_id:
            if not case_id:
                raise ValueError("lifecycle allocation requires case_id")
            allocation_id = allocation_id_for(
                case_id=case_id,
                evidence_id=allocation_evidence_id,
                target_lot_id=record_id,
            )
            event_id = terminal_event_id_for(
                case_id=case_id,
                evidence_id=allocation_evidence_id,
                target_lot_id=record_id,
                terminal_type=normalized_event_type,
                contracts_allocated=contracts,
            )
        elif str(manual_request_id or "").strip():
            stable_request = str(manual_request_id).strip()
            digest = hashlib.sha256(
                f"{normalized_event_type}|{stable_request}|{record_id}".encode("utf-8")
            ).hexdigest()[:24]
            event_id = f"manual-{normalized_event_type}-request-{digest}"
        event = _lifecycle_close_event(
            fields=fields,
            record_id=record_id,
            contracts_to_close=contracts,
            event_type=normalized_event_type,
            event_time_ms=int(ledger_preflight.event_time_ms),
            source=source,
            case_id=case_id,
            evidence_ids=evidence_tuple,
            close_target_resolution=close_target_resolution.to_dict(),
            stock_settlement=dict(stock_settlement or {}),
            close_reason=close_reason,
            event_id=event_id,
            evidence_id=allocation_evidence_id,
            allocation_id=allocation_id,
            manual_request_id=manual_request_id,
            manual_request_intent_hash=manual_request_intent_hash,
        )
        prepared.append((match, contracts, ledger_preflight, event))
        as_of_ms = int(ledger_preflight.event_time_ms) + 1

    allocation_rows = [
        {
            "allocation_id": allocation_id_for(
                case_id=str(case_id),
                evidence_id=str(allocation_evidence_id),
                target_lot_id=str(match.record_id),
            ),
            "case_id": str(case_id),
            "evidence_id": str(allocation_evidence_id),
            "target_lot_id": str(match.record_id),
            "terminal_type": normalized_event_type,
            "contracts_allocated": int(contracts),
            "canonical_terminal_event_id": event.event_id,
        }
        for match, contracts, _preflight, event in prepared
    ] if allocation_evidence_id else []
    case_update = dict(lifecycle_case_update or {})
    if case_update:
        case_update.update(
            {
                "status": "ledger_written",
                "decision_type": normalized_event_type,
                "target_lot_ids": [
                    str(match.record_id)
                    for match, _contracts, _preflight, _event in prepared
                ],
                "target_contracts_by_lot": {
                    str(match.record_id): int(contracts)
                    for match, contracts, _preflight, _event in prepared
                },
            }
        )
        first_event = prepared[0][3]
        case_update.setdefault("broker", first_event.contract_key.broker)
        case_update.setdefault("contract_key", first_event.contract_key.position_key)
    persisted = persist_trade_event_objects_atomically(
        repo,
        [event for _match, _contracts, _preflight, event in prepared],
        lifecycle_case_update=case_update or None,
        lifecycle_allocations=allocation_rows,
        wheel_start_enabled=wheel_start_enabled,
    )
    writes: list[LifecycleLedgerWrite] = []
    for (match, contracts, ledger_preflight, _event), result in zip(
        prepared,
        persisted,
        strict=True,
    ):
        record_id = str(match.record_id or "").strip()
        result_payload = _ledger_write_result(result).to_dict()
        operation = BrokerTradeOperation(
            action=normalized_event_type,
            record_id=record_id,
            contracts_to_close=contracts,
            matched_by=match.matched_by,
            event_id=result_payload.get("event_id"),
            result=result_payload,
            ledger_preflight=ledger_preflight,
            close_target_resolution=close_target_resolution.to_dict(),
            details={
                "case_id": case_id,
                "evidence_ids": list(evidence_tuple),
                "stock_settlement": dict(stock_settlement or {}),
            },
        )
        writes.append(
            LifecycleLedgerWrite(
                operation=operation,
                event_type=normalized_event_type,
                case_id=case_id,
                evidence_ids=evidence_tuple,
            )
        )
    return writes


def _assert_lifecycle_resolution_contracts(
    event_type: str,
    *,
    close_target_resolution: CloseTargetResolution,
    contracts_to_close: int,
) -> None:
    requested = int(contracts_to_close or 0)
    if requested <= 0:
        raise ValueError(f"{event_type} requires contracts_to_close > 0")
    resolved = int(close_target_resolution.contracts_to_close)
    if requested != resolved:
        raise ValueError(
            f"{event_type} contracts_to_close does not match resolved close targets: "
            f"requested={requested} resolved={resolved}"
        )


def persist_assignment_event(
    repo: Any,
    *,
    close_target_resolution: CloseTargetResolution,
    contracts_to_close: int,
    event_time_ms: int | None,
    case_id: str | None,
    evidence_ids: list[str] | tuple[str, ...] | None = None,
    stock_settlement: dict[str, Any] | None = None,
    source: str = "option_lifecycle_decision",
    wheel_start_enabled: bool = False,
) -> LifecycleLedgerWrite:
    if len(close_target_resolution.matches) != 1:
        raise ValueError(f"expected one assignment target, got {len(close_target_resolution.matches)}")
    writes = persist_assignment_events(
        repo,
        close_target_resolution=close_target_resolution,
        contracts_to_close=contracts_to_close,
        event_time_ms=event_time_ms,
        case_id=case_id,
        evidence_ids=evidence_ids,
        stock_settlement=stock_settlement,
        source=source,
        wheel_start_enabled=wheel_start_enabled,
    )
    if len(writes) != 1:
        raise ValueError(f"expected one assignment write, got {len(writes)}")
    return writes[0]


def persist_exercise_event(
    repo: Any,
    *,
    close_target_resolution: CloseTargetResolution,
    contracts_to_close: int,
    event_time_ms: int | None,
    case_id: str | None,
    evidence_ids: list[str] | tuple[str, ...] | None = None,
    stock_settlement: dict[str, Any] | None = None,
    source: str = "option_lifecycle_decision",
) -> LifecycleLedgerWrite:
    if len(close_target_resolution.matches) != 1:
        raise ValueError(f"expected one exercise target, got {len(close_target_resolution.matches)}")
    writes = persist_exercise_events(
        repo,
        close_target_resolution=close_target_resolution,
        contracts_to_close=contracts_to_close,
        event_time_ms=event_time_ms,
        case_id=case_id,
        evidence_ids=evidence_ids,
        stock_settlement=stock_settlement,
        source=source,
    )
    if len(writes) != 1:
        raise ValueError(f"expected one exercise write, got {len(writes)}")
    return writes[0]


def _lifecycle_close_event(
    *,
    fields: dict[str, Any],
    record_id: str,
    contracts_to_close: int,
    event_type: str,
    event_time_ms: int,
    source: str,
    case_id: str | None,
    evidence_ids: tuple[str, ...],
    close_target_resolution: dict[str, Any],
    stock_settlement: dict[str, Any],
    close_reason: str | None = None,
    event_id: str | None = None,
    evidence_id: str | None = None,
    allocation_id: str | None = None,
    manual_request_id: str | None = None,
    manual_request_intent_hash: str | None = None,
) -> TradeEvent:
    strike = effective_strike(fields)
    multiplier = effective_multiplier(fields)
    canonical_event_id = str(event_id or "").strip() or f"{event_type}-{record_id}-{uuid.uuid4().hex}"
    raw_close_type = EXPIRE_AUTO_CLOSE if event_type == "expire_close" else event_type
    strategy_payload = strategy_metadata_fields_from_payload(fields)
    return TradeEvent(
        event_id=canonical_event_id,
        event_type=event_type,
        event_time_ms=int(event_time_ms),
        contract_key=ContractKey.from_values(
            broker=normalize_broker(fields.get("broker")),
            account=normalize_account(fields.get("account")),
            underlying_symbol=canonical_contract_symbol(fields.get("symbol")),
            option_type=str(fields.get("option_type") or ""),
            position_side=str(fields.get("side") or "").strip().lower(),
            strike=(float(strike) if strike is not None else None),
            expiration_ymd=effective_expiration_ymd(fields),
        ),
        contracts=int(contracts_to_close),
        price=0.0,
        currency=normalize_currency(fields.get("currency")),
        source=source,
        multiplier=(float(multiplier) if multiplier is not None else 100.0),
        target_lot_id=str(record_id),
        raw_payload={
            "source": "om option lifecycle",
            "source_type": "system_trade_event",
            "record_id": str(record_id),
            "target_lot_id": str(record_id),
            "close_target_source_event_id": str(fields.get("source_event_id") or "").strip() or None,
            "close_target_account": normalize_account(fields.get("account")),
            "close_target_broker": normalize_broker(fields.get("broker")),
            "close_type": raw_close_type,
            "close_reason": str(close_reason or event_type),
            "case_id": case_id,
            "evidence_ids": list(evidence_ids),
            "evidence_id": str(evidence_id or "").strip() or None,
            "allocation_id": str(allocation_id or "").strip() or None,
            "contracts": int(contracts_to_close),
            "manual_request_id": str(manual_request_id or "").strip() or None,
            "manual_request_intent_hash": (
                str(manual_request_intent_hash or "").strip() or None
            ),
            "stock_settlement": dict(stock_settlement),
            "close_target_resolution": dict(close_target_resolution),
            "contracts_open_before": effective_contracts_open(fields),
            **strategy_payload,
        },
    )


def _ledger_write_result(value: Any) -> LedgerWriteResult:
    if isinstance(value, LedgerWriteResult):
        return value
    if isinstance(value, dict):
        return LedgerWriteResult.from_payload(value)
    return LedgerWriteResult()


__all__ = [
    "LifecycleLedgerWrite",
    "persist_assignment_event",
    "persist_assignment_events",
    "persist_exercise_event",
    "persist_exercise_events",
    "persist_expire_close_events",
    "persist_lifecycle_expire_close_events_atomically",
]
