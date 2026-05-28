from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.ledger.position_fields import (
    effective_contracts_open,
    effective_expiration_ymd,
    effective_multiplier,
    effective_strike,
    normalize_account,
    normalize_broker,
    normalize_currency,
)
from domain.domain.trade_contract_identity import canonical_contract_symbol
from src.application.ledger.lot_resolver import CloseTargetResolution
from src.application.ledger.preflight import _current_record_fields, preflight_broker_trade_close
from src.application.ledger.results import BrokerTradeOperation, LedgerWriteResult
from src.application.ledger.writer import persist_trade_event_object


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
) -> list[LifecycleLedgerWrite]:
    normalized_event_type = str(event_type or "").strip().lower()
    if normalized_event_type not in {"assignment", "exercise"}:
        raise ValueError("lifecycle close event_type must be assignment or exercise")
    if not close_target_resolution.matches:
        raise ValueError(f"{normalized_event_type} requires at least one close target")
    writes: list[LifecycleLedgerWrite] = []
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
        event = _lifecycle_close_event(
            fields=fields,
            record_id=record_id,
            contracts_to_close=contracts,
            event_type=normalized_event_type,
            event_time_ms=int(ledger_preflight["event_time_ms"]),
            source=source,
            case_id=case_id,
            evidence_ids=evidence_tuple,
            close_target_resolution=close_target_resolution.to_dict(),
            stock_settlement=dict(stock_settlement or {}),
        )
        result = persist_trade_event_object(repo, event)
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
        as_of_ms = int(ledger_preflight["event_time_ms"]) + 1
    return writes


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
) -> LifecycleLedgerWrite:
    writes = persist_assignment_events(
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
) -> TradeEvent:
    strike = effective_strike(fields)
    multiplier = effective_multiplier(fields)
    event_id = f"{event_type}-{record_id}-{uuid.uuid4().hex}"
    return TradeEvent(
        event_id=event_id,
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
            "close_type": event_type,
            "close_reason": event_type,
            "case_id": case_id,
            "evidence_ids": list(evidence_ids),
            "stock_settlement": dict(stock_settlement),
            "close_target_resolution": dict(close_target_resolution),
            "contracts_open_before": effective_contracts_open(fields),
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
]
