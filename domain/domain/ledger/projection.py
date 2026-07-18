from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from domain.domain.ledger.events import (
    CLOSE_EVENT_TYPES,
    LedgerDiagnostic,
    TradeEvent,
    lot_id_for_open_event,
    validate_trade_event,
)
from domain.domain.ledger.economics import (
    OptionEconomicAllocation,
    allocate_open_fee_for_close,
    build_option_economic_allocation,
)
from domain.domain.ledger.identity import ContractKey
from domain.domain.ledger.invariants import check_position_lot_invariants
from domain.domain.ledger.lots import PositionLot


@dataclass(frozen=True)
class RiskPositionView:
    position_key: str
    contract_key: ContractKey
    total_contracts_open: int
    lot_ids: tuple[str, ...]
    cash_secured_amount: float
    underlying_share_locked: float
    earliest_expiration_ymd: str
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_key": self.position_key,
            "contract_key": self.contract_key.to_dict(),
            "total_contracts_open": self.total_contracts_open,
            "lot_ids": list(self.lot_ids),
            "cash_secured_amount": self.cash_secured_amount,
            "underlying_share_locked": self.underlying_share_locked,
            "earliest_expiration_ymd": self.earliest_expiration_ymd,
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True)
class ProjectionResult:
    lots: list[PositionLot]
    views: list[RiskPositionView]
    allocations: list[OptionEconomicAllocation] = field(default_factory=list)
    diagnostics: list[LedgerDiagnostic] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(item.severity == "error" for item in self.diagnostics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lots": [item.to_dict() for item in self.lots],
            "views": [item.to_dict() for item in self.views],
            "allocations": [item.to_dict() for item in self.allocations],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "has_errors": self.has_errors,
        }


def project_trade_events(events: list[TradeEvent]) -> ProjectionResult:
    normalized_events = sorted(events, key=lambda item: (int(item.event_time_ms or 0), item.event_id))
    validated_events: list[tuple[TradeEvent, list[LedgerDiagnostic]]] = []
    seen_event_ids: set[str] = set()
    for event in normalized_events:
        event_diagnostics = validate_trade_event(event)
        if event.event_id in seen_event_ids:
            event_diagnostics.append(
                LedgerDiagnostic(
                    event_id=event.event_id,
                    severity="error",
                    code="duplicate_event_id",
                    message="event_id must be unique",
                )
            )
        seen_event_ids.add(event.event_id)
        validated_events.append((event, event_diagnostics))

    voided_event_ids = {
        event.target_event_id
        for event, event_diagnostics in validated_events
        if event.event_type == "void"
        and event.target_event_id
        and not any(item.severity == "error" for item in event_diagnostics)
    }
    lots_by_id: dict[str, PositionLot] = {}
    open_events_by_lot_id: dict[str, TradeEvent] = {}
    allocated_open_fee_by_lot_id: dict[str, Decimal] = {}
    allocation_sequence_by_close_event: dict[str, int] = {}
    allocations: list[OptionEconomicAllocation] = []
    lot_order: list[str] = []
    diagnostics: list[LedgerDiagnostic] = []

    for event, event_diagnostics in validated_events:
        if event.event_type == "void":
            diagnostics.extend(event_diagnostics)
            continue
        if event.event_id in voided_event_ids:
            continue
        if any(item.severity == "error" for item in event_diagnostics):
            diagnostics.extend(event_diagnostics)
            continue
        diagnostics.extend(event_diagnostics)

        if event.event_type == "open":
            lot_id = _apply_open_event(event, lots_by_id=lots_by_id, lot_order=lot_order, diagnostics=diagnostics)
            if lot_id is not None:
                open_events_by_lot_id[lot_id] = event
            continue
        if event.event_type in CLOSE_EVENT_TYPES:
            allocation = _apply_close_event(
                event,
                lots_by_id=lots_by_id,
                open_events_by_lot_id=open_events_by_lot_id,
                allocated_open_fee_by_lot_id=allocated_open_fee_by_lot_id,
                allocation_sequence_by_close_event=allocation_sequence_by_close_event,
                diagnostics=diagnostics,
            )
            if allocation is not None:
                allocations.append(allocation)
            continue
        if event.event_type == "adjust":
            _apply_adjust_event(event, lots_by_id=lots_by_id, diagnostics=diagnostics)
            continue

    lots = [lots_by_id[lot_id] for lot_id in lot_order if lot_id in lots_by_id]
    diagnostics.extend(check_position_lot_invariants(lots))
    return ProjectionResult(
        lots=lots,
        views=build_risk_position_views(lots),
        allocations=allocations,
        diagnostics=diagnostics,
    )


def build_risk_position_views(lots: list[PositionLot]) -> list[RiskPositionView]:
    grouped: dict[ContractKey, list[PositionLot]] = {}
    for lot in lots:
        if lot.contracts_open <= 0:
            continue
        grouped.setdefault(lot.contract_key, []).append(lot)

    views: list[RiskPositionView] = []
    for contract_key, group in grouped.items():
        ordered = sorted(group, key=lambda item: (item.opened_at_ms, item.lot_id))
        total_open = sum(int(item.contracts_open) for item in ordered)
        cash_secured = 0.0
        locked_shares = 0.0
        if contract_key.position_side == "short" and contract_key.option_type == "put":
            cash_secured = sum(item.contracts_open * contract_key.strike * item.multiplier for item in ordered)
        if contract_key.position_side == "short" and contract_key.option_type == "call":
            locked_shares = sum(item.contracts_open * item.multiplier for item in ordered)
        diagnostics = ("multiple_lots",) if len(ordered) > 1 else ()
        views.append(
            RiskPositionView(
                position_key=contract_key.position_key,
                contract_key=contract_key,
                total_contracts_open=total_open,
                lot_ids=tuple(item.lot_id for item in ordered),
                cash_secured_amount=cash_secured,
                underlying_share_locked=locked_shares,
                earliest_expiration_ymd=contract_key.expiration_ymd,
                diagnostics=diagnostics,
            )
        )
    return sorted(views, key=lambda item: item.position_key)


def _apply_open_event(
    event: TradeEvent,
    *,
    lots_by_id: dict[str, PositionLot],
    lot_order: list[str],
    diagnostics: list[LedgerDiagnostic],
) -> str | None:
    lot_id = lot_id_for_open_event(event)
    if lot_id in lots_by_id:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event.event_id,
                severity="error",
                code="duplicate_lot_id",
                message="open event produced an existing lot_id",
                details={"lot_id": lot_id},
            )
        )
        return None
    lots_by_id[lot_id] = PositionLot.from_open_event(event, lot_id=lot_id)
    lot_order.append(lot_id)
    return lot_id


def _apply_close_event(
    event: TradeEvent,
    *,
    lots_by_id: dict[str, PositionLot],
    open_events_by_lot_id: dict[str, TradeEvent],
    allocated_open_fee_by_lot_id: dict[str, Decimal],
    allocation_sequence_by_close_event: dict[str, int],
    diagnostics: list[LedgerDiagnostic],
) -> OptionEconomicAllocation | None:
    target_lot_id = event.target_lot_id or ""
    lot = lots_by_id.get(target_lot_id)
    if lot is None:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event.event_id,
                severity="error",
                code="target_lot_not_found",
                message="close event target_lot_id was not found",
                details={"target_lot_id": target_lot_id},
            )
        )
        return None
    if lot.contract_key != event.contract_key:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event.event_id,
                severity="error",
                code="target_contract_mismatch",
                message="close event contract_key does not match target lot",
                details={
                    "target_lot_id": target_lot_id,
                    "lot_contract_key": lot.contract_key.to_dict(),
                    "event_contract_key": event.contract_key.to_dict(),
                },
            )
        )
        return None
    if lot.contracts_open <= 0:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event.event_id,
                severity="error",
                code="target_lot_already_closed",
                message="close event targeted a lot with no open contracts",
                details={"target_lot_id": target_lot_id},
            )
        )
        return None
    if event.contracts > lot.contracts_open:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event.event_id,
                severity="error",
                code="close_contracts_exceed_open",
                message="close event contracts exceed target lot contracts_open",
                details={
                    "target_lot_id": target_lot_id,
                    "contracts_requested": event.contracts,
                    "contracts_open": lot.contracts_open,
                },
            )
        )
        return None
    open_event = open_events_by_lot_id.get(target_lot_id)
    if open_event is None:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event.event_id,
                severity="error",
                code="open_event_not_found_for_lot",
                message="close allocation could not resolve the lot open event",
                details={"target_lot_id": target_lot_id, "open_event_id": lot.open_event_id},
            )
        )
        return None
    allocation: OptionEconomicAllocation | None = None
    allocated_before = allocated_open_fee_by_lot_id.get(target_lot_id, Decimal(0))
    allocated_after = allocated_before
    unit_mismatch = _economic_unit_mismatch(lot, event)
    if unit_mismatch:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event.event_id,
                severity="error",
                code="target_economic_units_mismatch",
                message="close event currency or multiplier does not match target lot",
                details={"target_lot_id": target_lot_id, **unit_mismatch},
            )
        )
        allocated_after = _advance_open_fee_state_without_allocation(
            lot=lot,
            open_event=open_event,
            close_event=event,
            allocated_before=allocated_before,
            diagnostics=diagnostics,
        )
    else:
        sequence = int(allocation_sequence_by_close_event.get(event.event_id, 0))
        try:
            allocation, allocated_after = build_option_economic_allocation(
                lot=lot,
                open_event=open_event,
                close_event=event,
                sequence=sequence,
                open_fee_allocated_before=allocated_before,
            )
        except (TypeError, ValueError) as exc:
            diagnostics.append(
                LedgerDiagnostic(
                    event_id=event.event_id,
                    severity="error",
                    code="economic_allocation_failed",
                    message="close event economic allocation failed",
                    details={"target_lot_id": target_lot_id, "error": str(exc)},
                )
            )
            allocated_after = _advance_open_fee_state_without_allocation(
                lot=lot,
                open_event=open_event,
                close_event=event,
                allocated_before=allocated_before,
                diagnostics=diagnostics,
            )
    lots_by_id[target_lot_id] = lot.apply_close(event)
    allocated_open_fee_by_lot_id[target_lot_id] = allocated_after
    if allocation is not None:
        allocation_sequence_by_close_event[event.event_id] = sequence + 1
    return allocation


def _economic_unit_mismatch(lot: PositionLot, event: TradeEvent) -> dict[str, Any]:
    details: dict[str, Any] = {}
    lot_currency = str(lot.currency or "").strip().upper()
    event_currency = str(event.currency or "").strip().upper()
    if lot_currency != event_currency:
        details.update(lot_currency=lot_currency, event_currency=event_currency)
    try:
        lot_multiplier = Decimal(str(lot.multiplier))
        event_multiplier = Decimal(str(event.multiplier))
    except Exception as exc:
        details["multiplier_error"] = str(exc)
    else:
        if lot_multiplier != event_multiplier:
            details.update(lot_multiplier=str(lot_multiplier), event_multiplier=str(event_multiplier))
    return details


def _advance_open_fee_state_without_allocation(
    *,
    lot: PositionLot,
    open_event: TradeEvent,
    close_event: TradeEvent,
    allocated_before: Decimal,
    diagnostics: list[LedgerDiagnostic],
) -> Decimal:
    try:
        _fee, allocated_after = allocate_open_fee_for_close(
            lot=lot,
            open_event=open_event,
            close_contracts=int(close_event.contracts),
            open_fee_allocated_before=allocated_before,
        )
        return allocated_after
    except (TypeError, ValueError) as exc:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=close_event.event_id,
                severity="error",
                code="open_fee_allocation_state_failed",
                message="open fee allocation state could not advance for an unallocated close",
                details={"target_lot_id": lot.lot_id, "error": str(exc)},
            )
        )
        return allocated_before


def _apply_adjust_event(
    event: TradeEvent,
    *,
    lots_by_id: dict[str, PositionLot],
    diagnostics: list[LedgerDiagnostic],
) -> None:
    target_lot_id = event.target_lot_id or ""
    lot = lots_by_id.get(target_lot_id)
    if lot is None:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event.event_id,
                severity="error",
                code="target_lot_not_found",
                message="adjust event target_lot_id was not found",
                details={"target_lot_id": target_lot_id},
            )
        )
        return
    if lot.contract_key != event.contract_key:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event.event_id,
                severity="error",
                code="target_contract_mismatch",
                message="adjust event contract_key does not match target lot",
                details={
                    "target_lot_id": target_lot_id,
                    "lot_contract_key": lot.contract_key.to_dict(),
                    "event_contract_key": event.contract_key.to_dict(),
                },
            )
        )
        return
    try:
        lots_by_id[target_lot_id] = lot.apply_adjust(event)
    except ValueError as exc:
        diagnostics.append(
            LedgerDiagnostic(
                event_id=event.event_id,
                severity="error",
                code="adjust_patch_invalid",
                message="adjust event patch is invalid",
                details={"target_lot_id": target_lot_id, "error": str(exc)},
            )
        )
