from __future__ import annotations

from dataclasses import dataclass, field, replace
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
from domain.domain.ledger.projection_state import (
    ResumableLotState,
    ResumableProjectionState,
)


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


@dataclass(frozen=True)
class ProjectionTransition:
    event: TradeEvent
    lot_before: PositionLot | None
    lot_after: PositionLot | None
    previous_close_event_id: str | None = None
    allocation: OptionEconomicAllocation | None = None
    applied: bool = False
    finalized: bool = False


@dataclass(frozen=True)
class ResumableProjectionResult:
    state: ResumableProjectionState | None
    active_lots: tuple[PositionLot, ...]
    retained_lots: tuple[PositionLot, ...]
    views: tuple[RiskPositionView, ...]
    allocations: tuple[OptionEconomicAllocation, ...]
    diagnostics: tuple[LedgerDiagnostic, ...]
    transitions: tuple[ProjectionTransition, ...]
    requires_full_replay: bool = False
    full_replay_reason: str | None = None

    @property
    def eligible(self) -> bool:
        return (
            self.state is not None
            and not self.requires_full_replay
            and not self.diagnostics
        )

    def to_projection_result(self) -> ProjectionResult:
        lots = list(self.retained_lots)
        return ProjectionResult(
            lots=lots,
            views=build_risk_position_views(lots),
            allocations=list(self.allocations),
            diagnostics=list(self.diagnostics),
        )


@dataclass
class _ProjectionAccumulator:
    lots_by_id: dict[str, PositionLot] = field(default_factory=dict)
    open_events_by_lot_id: dict[str, TradeEvent] = field(default_factory=dict)
    allocated_open_fee_by_lot_id: dict[str, Decimal] = field(default_factory=dict)
    allocation_sequence_by_close_event: dict[str, int] = field(default_factory=dict)
    last_close_event_id_by_lot_id: dict[str, str | None] = field(default_factory=dict)


def project_trade_events(events: list[TradeEvent]) -> ProjectionResult:
    return project_resumable_trade_events(
        events,
        entry_mode="full",
    ).to_projection_result()


def project_resumable_trade_events(
    events: list[TradeEvent],
    *,
    initial_state: ResumableProjectionState | None = None,
    entry_mode: str = "full",
) -> ResumableProjectionResult:
    """Project full history or an append-only tail into active resumable state."""

    mode = str(entry_mode or "").strip().lower()
    if mode not in {"full", "tail"}:
        raise ValueError("resumable projection entry_mode must be full or tail")
    if mode == "full" and initial_state is not None:
        raise ValueError("full resumable projection cannot accept initial_state")
    if mode == "tail" and initial_state is None:
        raise ValueError("tail resumable projection requires initial_state")

    validated_events = _validated_events(events)
    if mode == "full":
        _append_target_event_graph_diagnostics(validated_events)
        voided_event_ids = {
            event.target_event_id
            for event, event_diagnostics in validated_events
            if event.event_type == "void"
            and event.target_event_id
            and not any(
                item.severity == "error" for item in event_diagnostics
            )
        }
        accumulator = _ProjectionAccumulator()
    else:
        controls = [
            event.event_id
            for event, _diagnostics in validated_events
            if event.event_type in {"void", "repair"}
        ]
        if controls:
            return _force_full_result(
                initial_state,
                reason="tail_control_event",
            )
        voided_event_ids = set()
        accumulator = _accumulator_from_resumable_state(initial_state)

    diagnostics: list[LedgerDiagnostic] = []
    allocations: list[OptionEconomicAllocation] = []
    transitions: list[ProjectionTransition] = []
    retained_by_id: dict[str, PositionLot] = {}
    retained_order: list[str] = []
    historical_close_event_ids: dict[str, list[str]] = {}
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

        if (
            mode == "tail"
            and event.event_type in CLOSE_EVENT_TYPES | {"adjust"}
            and str(event.target_lot_id or "") not in accumulator.lots_by_id
        ):
            return _force_full_result(
                initial_state,
                reason="target_lot_not_active",
                diagnostics=diagnostics,
                transitions=transitions,
                allocations=allocations,
            )

        transition = _apply_event_transition(
            event,
            accumulator=accumulator,
            diagnostics=diagnostics,
            evict_finalized=mode == "tail",
        )
        if transition.applied and transition.lot_after is not None:
            transitions.append(transition)
            lot_id = transition.lot_after.lot_id
            if lot_id not in retained_by_id:
                retained_order.append(lot_id)
            retained_by_id[lot_id] = transition.lot_after
            if mode == "full" and event.event_type in CLOSE_EVENT_TYPES:
                historical_close_event_ids.setdefault(lot_id, []).append(
                    event.event_id
                )
        if transition.allocation is not None:
            allocations.append(transition.allocation)

    active_lots = tuple(
        replace(accumulator.lots_by_id[lot_id], close_event_ids=())
        for lot_id in sorted(accumulator.lots_by_id)
        if accumulator.lots_by_id[lot_id].contracts_open > 0
    )
    retained_lots = (
        tuple(
            replace(
                retained_by_id[lot_id],
                close_event_ids=tuple(
                    historical_close_event_ids.get(lot_id, ())
                ),
            )
            for lot_id in retained_order
        )
        if mode == "full"
        else active_lots
    )
    diagnostics.extend(
        check_position_lot_invariants(list(retained_lots))
    )
    state = None
    state_error = False
    if not diagnostics:
        try:
            state = _resumable_state_from_accumulator(accumulator)
        except (TypeError, ValueError, OverflowError):
            state_error = True
    requires_full_replay = mode == "tail" and bool(diagnostics or state_error)
    state_failure_reason = (
        "tail_diagnostic"
        if diagnostics and mode == "tail"
        else "resumable_state_invalid"
        if state_error
        else None
    )
    return ResumableProjectionResult(
        state=state,
        active_lots=active_lots,
        retained_lots=retained_lots,
        views=tuple(build_risk_position_views(list(active_lots))),
        allocations=tuple(allocations),
        diagnostics=tuple(diagnostics),
        transitions=tuple(transitions),
        requires_full_replay=requires_full_replay,
        full_replay_reason=state_failure_reason,
    )


def _validated_events(
    events: list[TradeEvent],
) -> list[tuple[TradeEvent, list[LedgerDiagnostic]]]:
    normalized_events = sorted(
        events,
        key=lambda item: (int(item.event_time_ms or 0), item.event_id),
    )
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
    return validated_events


def _append_target_event_graph_diagnostics(
    validated_events: list[tuple[TradeEvent, list[LedgerDiagnostic]]],
) -> None:
    events_by_id: dict[str, list[TradeEvent]] = {}
    for event, _diagnostics in validated_events:
        if event.event_id:
            events_by_id.setdefault(event.event_id, []).append(event)
    forbidden_target_types = {"void", "repair", "verification"}
    for event, diagnostics in validated_events:
        if event.event_type not in {"void", "repair"} or not event.target_event_id:
            continue
        target_id = event.target_event_id
        if target_id == event.event_id:
            diagnostics.append(
                LedgerDiagnostic(
                    event_id=event.event_id,
                    severity="error",
                    code="target_event_self_reference",
                    message="event cannot target itself",
                    details={"target_event_id": target_id},
                )
            )
            continue
        targets = events_by_id.get(target_id, [])
        if not targets:
            diagnostics.append(
                LedgerDiagnostic(
                    event_id=event.event_id,
                    severity="error",
                    code="target_event_not_found",
                    message="target_event_id was not found",
                    details={"target_event_id": target_id},
                )
            )
            continue
        if len(targets) != 1:
            diagnostics.append(
                LedgerDiagnostic(
                    event_id=event.event_id,
                    severity="error",
                    code="target_event_ambiguous",
                    message="target_event_id is not unique",
                    details={"target_event_id": target_id, "match_count": len(targets)},
                )
            )
            continue
        target = targets[0]
        if target.event_type in forbidden_target_types:
            diagnostics.append(
                LedgerDiagnostic(
                    event_id=event.event_id,
                    severity="error",
                    code="target_event_type_invalid",
                    message="void/repair cannot target a control or verification event",
                    details={
                        "target_event_id": target_id,
                        "target_event_type": target.event_type,
                    },
                )
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


def _apply_event_transition(
    event: TradeEvent,
    *,
    accumulator: _ProjectionAccumulator,
    diagnostics: list[LedgerDiagnostic],
    evict_finalized: bool,
) -> ProjectionTransition:
    if event.event_type == "open":
        lot_id = _apply_open_event(
            event,
            lots_by_id=accumulator.lots_by_id,
            diagnostics=diagnostics,
        )
        lot_after = accumulator.lots_by_id.get(lot_id or "")
        if lot_id is not None and lot_after is not None:
            accumulator.open_events_by_lot_id[lot_id] = event
            accumulator.allocated_open_fee_by_lot_id[lot_id] = Decimal(0)
            accumulator.last_close_event_id_by_lot_id[lot_id] = None
        return ProjectionTransition(
            event=event,
            lot_before=None,
            lot_after=lot_after,
            applied=lot_after is not None,
        )

    if event.event_type in CLOSE_EVENT_TYPES:
        target_lot_id = str(event.target_lot_id or "")
        lot_before = accumulator.lots_by_id.get(target_lot_id)
        previous_close_event_id = (
            accumulator.last_close_event_id_by_lot_id.get(target_lot_id)
        )
        allocation = _apply_close_event(
            event,
            lots_by_id=accumulator.lots_by_id,
            open_events_by_lot_id=accumulator.open_events_by_lot_id,
            allocated_open_fee_by_lot_id=(
                accumulator.allocated_open_fee_by_lot_id
            ),
            allocation_sequence_by_close_event=(
                accumulator.allocation_sequence_by_close_event
            ),
            diagnostics=diagnostics,
        )
        lot_after = accumulator.lots_by_id.get(target_lot_id)
        applied = lot_before is not None and lot_after != lot_before
        finalized = bool(applied and lot_after and lot_after.contracts_open == 0)
        if applied:
            accumulator.last_close_event_id_by_lot_id[target_lot_id] = (
                event.event_id
            )
        transition = ProjectionTransition(
            event=event,
            lot_before=lot_before,
            lot_after=lot_after,
            previous_close_event_id=previous_close_event_id,
            allocation=allocation,
            applied=applied,
            finalized=finalized,
        )
        if finalized and evict_finalized:
            accumulator.lots_by_id.pop(target_lot_id, None)
            accumulator.open_events_by_lot_id.pop(target_lot_id, None)
            accumulator.allocated_open_fee_by_lot_id.pop(target_lot_id, None)
            accumulator.last_close_event_id_by_lot_id.pop(target_lot_id, None)
        return transition

    if event.event_type == "adjust":
        target_lot_id = str(event.target_lot_id or "")
        lot_before = accumulator.lots_by_id.get(target_lot_id)
        previous_close_event_id = (
            accumulator.last_close_event_id_by_lot_id.get(target_lot_id)
        )
        _apply_adjust_event(
            event,
            lots_by_id=accumulator.lots_by_id,
            diagnostics=diagnostics,
        )
        lot_after = accumulator.lots_by_id.get(target_lot_id)
        applied = lot_before is not None and lot_after != lot_before
        finalized = bool(applied and lot_after and lot_after.contracts_open == 0)
        transition = ProjectionTransition(
            event=event,
            lot_before=lot_before,
            lot_after=lot_after,
            previous_close_event_id=previous_close_event_id,
            applied=applied,
            finalized=finalized,
        )
        if finalized and evict_finalized:
            accumulator.lots_by_id.pop(target_lot_id, None)
            accumulator.open_events_by_lot_id.pop(target_lot_id, None)
            accumulator.allocated_open_fee_by_lot_id.pop(target_lot_id, None)
            accumulator.last_close_event_id_by_lot_id.pop(target_lot_id, None)
        return transition

    return ProjectionTransition(
        event=event,
        lot_before=None,
        lot_after=None,
    )


def _accumulator_from_resumable_state(
    state: ResumableProjectionState | None,
) -> _ProjectionAccumulator:
    if state is None:
        return _ProjectionAccumulator()
    accumulator = _ProjectionAccumulator()
    for item in state.active_lots:
        accumulator.lots_by_id[item.lot_id] = item.to_position_lot()
        accumulator.open_events_by_lot_id[item.lot_id] = item.open_event
        accumulator.allocated_open_fee_by_lot_id[item.lot_id] = (
            item.allocated_open_fee
        )
        accumulator.last_close_event_id_by_lot_id[item.lot_id] = (
            item.last_close_event_id
        )
    return accumulator


def _resumable_state_from_accumulator(
    accumulator: _ProjectionAccumulator,
) -> ResumableProjectionState:
    return ResumableProjectionState.from_lots(
        ResumableLotState.from_position_lot(
            accumulator.lots_by_id[lot_id],
            open_event=accumulator.open_events_by_lot_id[lot_id],
            allocated_open_fee=accumulator.allocated_open_fee_by_lot_id.get(
                lot_id,
                Decimal(0),
            ),
            last_close_event_id=(
                accumulator.last_close_event_id_by_lot_id.get(lot_id)
            ),
        )
        for lot_id in sorted(accumulator.lots_by_id)
        if accumulator.lots_by_id[lot_id].contracts_open > 0
    )


def _force_full_result(
    state: ResumableProjectionState | None,
    *,
    reason: str,
    diagnostics: list[LedgerDiagnostic] | None = None,
    transitions: list[ProjectionTransition] | None = None,
    allocations: list[OptionEconomicAllocation] | None = None,
) -> ResumableProjectionResult:
    active_lots = tuple(
        item.to_position_lot() for item in (state.active_lots if state else ())
    )
    return ResumableProjectionResult(
        state=None,
        active_lots=active_lots,
        retained_lots=active_lots,
        views=tuple(build_risk_position_views(list(active_lots))),
        allocations=tuple(allocations or ()),
        diagnostics=tuple(diagnostics or ()),
        transitions=tuple(transitions or ()),
        requires_full_replay=True,
        full_replay_reason=reason,
    )


def _apply_open_event(
    event: TradeEvent,
    *,
    lots_by_id: dict[str, PositionLot],
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
    lots_by_id[target_lot_id] = lot.apply_close(
        event,
        retain_close_event_ids=False,
    )
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
