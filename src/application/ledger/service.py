from __future__ import annotations

from typing import Any

from domain.domain.ledger.position_fields import (
    OpenPositionCommand,
    PositionLotPatch,
    build_close_patch_contract,
)
from src.application.ledger.errors import LedgerPreflightError
from src.application.ledger.preflight import (
    _assert_fields_match_current,
    _current_record_fields,
    _duplicate_open_preflight,
    _existing_open_event_result,
    _manual_open_ledger_inputs,
    _preflight_lot_adjust,
    _preflight_open_event,
    _preflight_manual_repair_payload,
    _preflight_manual_void_payload,
    _split_close_deal_for_target,
    _trade_open_ledger_inputs,
    preflight_broker_trade_close,
    preflight_expire_auto_close,
    preflight_manual_adjust,
    preflight_manual_close,
    preflight_manual_open,
    preflight_manual_repair,
    preflight_manual_void,
    preflight_trade_open,
)
from src.application.ledger.results import (
    BrokerTradeOperation,
    ExpiredCloseDecision,
    ExpiredCloseRunResult,
    LedgerPreflightResult,
    LedgerWriteResult,
    ManualAdjustLedgerResult,
    ManualCloseLedgerResult,
    OpenLedgerResult,
    ProjectionRefreshResult,
    TradeEventInterventionPreview,
    TradeEventInterventionLedgerResult,
)


def load_option_positions_repo(data_config: Any) -> Any:
    from src.application.ledger.bootstrap import load_option_positions_repo as _impl

    return _impl(data_config)


def require_option_positions_read_repo(repo: Any) -> Any:
    from src.application.ledger.repository import require_option_positions_read_repo as _impl

    return _impl(repo)


def require_option_positions_event_write_repo(repo: Any) -> Any:
    from src.application.ledger.repository import require_option_positions_event_write_repo as _impl

    return _impl(repo)


def rebuild_position_lots_from_trade_events(repo: Any) -> ProjectionRefreshResult:
    from src.application.ledger.writer import rebuild_position_lots_from_trade_events as _impl

    return _impl(repo)


def persist_trade_event(repo: Any, deal: Any) -> LedgerWriteResult:
    from src.application.ledger.writer import persist_trade_event as _impl

    return _impl(repo, deal)


def persist_manual_open_event(repo: Any, command: OpenPositionCommand) -> LedgerWriteResult:
    from src.application.ledger.manual_trades import persist_manual_open_event as _impl

    return _impl(repo, command)


def build_manual_void_preview(
    repo: Any,
    *,
    target_event_id: str,
    void_reason: str,
    as_of_ms: int | None = None,
) -> TradeEventInterventionPreview:
    from src.application.ledger.interventions import build_manual_void_preview as _impl

    return _impl(
        repo,
        target_event_id=target_event_id,
        void_reason=void_reason,
        as_of_ms=as_of_ms,
    )


def build_manual_repair_preview(
    repo: Any,
    *,
    target_event_id: str,
    overrides: dict[str, Any],
    repair_reason: str,
    as_of_ms: int | None = None,
) -> TradeEventInterventionPreview:
    from src.application.ledger.interventions import build_manual_repair_preview as _impl

    return _impl(
        repo,
        target_event_id=target_event_id,
        overrides=overrides,
        repair_reason=repair_reason,
        as_of_ms=as_of_ms,
    )


def build_expired_close_decisions(
    positions: list[dict[str, Any]],
    *,
    as_of_ms: int,
    grace_days: int,
) -> list[ExpiredCloseDecision]:
    from src.application.ledger.maintenance import build_expired_close_decisions as _impl

    return _impl(
        positions,
        as_of_ms=as_of_ms,
        grace_days=grace_days,
    )


def auto_close_expired_positions(
    repo: Any,
    positions: list[dict[str, Any]],
    *,
    as_of_ms: int,
    grace_days: int,
    max_close: int,
) -> ExpiredCloseRunResult:
    from src.application.ledger.maintenance import auto_close_expired_positions as _impl

    return _impl(
        repo,
        positions,
        as_of_ms=as_of_ms,
        grace_days=grace_days,
        max_close=max_close,
    )


def _ledger_write_result_from_any(value: Any, *, event_id: str | None, record_id: str | None) -> LedgerWriteResult:
    if isinstance(value, LedgerWriteResult):
        return value
    if isinstance(value, dict):
        return LedgerWriteResult.from_payload(value)
    return LedgerWriteResult(event_id=event_id, record_id=record_id, created=None)


def persist_manual_open_event_with_ledger(repo: Any, command: OpenPositionCommand) -> OpenLedgerResult:
    from src.application.ledger.manual_trades import persist_manual_open_event

    resolved_command, fields, event = _manual_open_ledger_inputs(command)
    duplicate_result = _existing_open_event_result(repo, event_id=event.event_id, record_id=event.lot_id)
    if duplicate_result is not None:
        return OpenLedgerResult(
            result=LedgerWriteResult.from_payload(duplicate_result),
            fields=fields,
            command=resolved_command,
            ledger_preflight=_duplicate_open_preflight(event=event, result=duplicate_result),
            duplicate_checked_before_write=True,
        )

    ledger_preflight = _preflight_open_event(
        repo,
        event=event,
        source="manual_open_preflight",
        operation_label="manual open",
    )
    result = persist_manual_open_event(repo, resolved_command)
    return OpenLedgerResult(
        result=LedgerWriteResult.from_payload(result),
        fields=fields,
        command=resolved_command,
        ledger_preflight=ledger_preflight,
    )


def persist_trade_open_event_with_ledger(
    repo: Any,
    *,
    deal: Any,
    persist_trade_event_fn: Any,
) -> OpenLedgerResult:
    resolved_deal, fields, event = _trade_open_ledger_inputs(deal)
    duplicate_result = _existing_open_event_result(repo, event_id=event.event_id, record_id=event.lot_id)
    if duplicate_result is not None:
        return OpenLedgerResult(
            result=LedgerWriteResult.from_payload(duplicate_result),
            fields=fields,
            ledger_preflight=_duplicate_open_preflight(event=event, result=duplicate_result),
            duplicate_checked_before_write=True,
        )

    ledger_preflight = _preflight_open_event(
        repo,
        event=event,
        source="broker_trade_open_preflight",
        operation_label="broker trade open",
    )
    result = persist_trade_event_fn(repo, resolved_deal)
    return OpenLedgerResult(
        result=_ledger_write_result_from_any(result, event_id=event.event_id, record_id=event.lot_id),
        fields=fields,
        ledger_preflight=ledger_preflight,
    )


def persist_manual_void_event_with_ledger(
    repo: Any,
    *,
    target_event_id: str,
    void_reason: str,
    as_of_ms: int | None = None,
) -> TradeEventInterventionLedgerResult:
    from src.application.ledger.interventions import persist_manual_void_event

    payload = _preflight_manual_void_payload(
        repo,
        target_event_id=target_event_id,
        void_reason=void_reason,
        as_of_ms=as_of_ms,
    )
    result = persist_manual_void_event(
        repo,
        target_event_id=target_event_id,
        void_reason=void_reason,
        as_of_ms=as_of_ms,
    )
    return TradeEventInterventionLedgerResult(
        result=LedgerWriteResult.from_payload(result),
        preview=payload["preview"],
        ledger_preflight=payload["ledger_preflight"],
    )


def persist_manual_repair_event_with_ledger(
    repo: Any,
    *,
    target_event_id: str,
    overrides: dict[str, Any],
    repair_reason: str,
    as_of_ms: int | None = None,
) -> TradeEventInterventionLedgerResult:
    from src.application.ledger.interventions import persist_manual_repair_event

    payload = _preflight_manual_repair_payload(
        repo,
        target_event_id=target_event_id,
        overrides=overrides,
        repair_reason=repair_reason,
        as_of_ms=as_of_ms,
    )
    result = persist_manual_repair_event(
        repo,
        target_event_id=target_event_id,
        overrides=overrides,
        repair_reason=repair_reason,
        as_of_ms=as_of_ms,
    )
    return TradeEventInterventionLedgerResult(
        result=LedgerWriteResult.from_payload(result),
        preview=payload["preview"],
        ledger_preflight=payload["ledger_preflight"],
    )


def persist_manual_adjust_event_with_ledger(
    repo: Any,
    *,
    record_id: str,
    fields: dict[str, Any] | None = None,
    contracts: int | None = None,
    strike: float | None = None,
    expiration_ymd: str | None = None,
    premium_per_share: float | None = None,
    multiplier: float | None = None,
    opened_at_ms: int | None = None,
    as_of_ms: int | None = None,
) -> ManualAdjustLedgerResult:
    from src.application.ledger.manual_trades import persist_manual_adjust_event

    preflight_result = _preflight_lot_adjust(
        repo,
        record_id=record_id,
        fields=fields,
        contracts=contracts,
        strike=strike,
        expiration_ymd=expiration_ymd,
        premium_per_share=premium_per_share,
        multiplier=multiplier,
        opened_at_ms=opened_at_ms,
        as_of_ms=as_of_ms,
        source="manual_adjust_preflight",
        operation_label="manual adjust",
    )
    ledger_preflight = preflight_result.ledger_preflight
    current_fields = preflight_result.fields
    patch = preflight_result.patch_contract
    result = persist_manual_adjust_event(
        repo,
        record_id=str(record_id),
        fields=current_fields,
        contracts=contracts,
        strike=strike,
        expiration_ymd=expiration_ymd,
        premium_per_share=premium_per_share,
        multiplier=multiplier,
        opened_at_ms=opened_at_ms,
        as_of_ms=int(ledger_preflight["event_time_ms"]),
    )
    return ManualAdjustLedgerResult(
        result=LedgerWriteResult.from_payload(result),
        fields=current_fields,
        patch=patch,
        ledger_preflight=ledger_preflight,
    )


def persist_trade_close_events_with_ledger(
    repo: Any,
    *,
    matches: list[Any],
    deal: Any,
    persist_trade_event_fn: Any,
    close_target_resolution: dict[str, Any] | None = None,
) -> list[BrokerTradeOperation]:
    operations: list[BrokerTradeOperation] = []
    close_action = "buy_close" if str(getattr(deal, "side", "") or "").strip().lower() == "buy" else "sell_close"
    for match in matches:
        record_id = str(getattr(match, "record_id", "") or "").strip()
        contracts_to_close = int(getattr(match, "contracts_to_close", 0) or 0)
        fields = _current_record_fields(repo, record_id=record_id)
        ledger_preflight = preflight_broker_trade_close(
            repo,
            record_id=record_id,
            fields=fields,
            contracts_to_close=contracts_to_close,
            close_price=(float(getattr(deal, "price")) if getattr(deal, "price", None) is not None else None),
            as_of_ms=(int(getattr(deal, "trade_time_ms")) if getattr(deal, "trade_time_ms", None) is not None else None),
        )
        split_deal = _split_close_deal_for_target(
            deal,
            record_id=record_id,
            fields=fields,
            contracts_to_close=contracts_to_close,
            close_target_resolution=close_target_resolution,
        )
        result = persist_trade_event_fn(repo, split_deal)
        result_payload = _ledger_write_result_from_any(
            result,
            event_id=f"{getattr(deal, 'deal_id', '')}:close:{record_id}",
            record_id=record_id,
        ).to_dict()
        operation = BrokerTradeOperation(
            action=close_action,
            record_id=record_id,
            contracts_to_close=contracts_to_close,
            matched_by=str(getattr(match, "matched_by", "") or ""),
            event_id=result_payload.get("event_id"),
            result=result_payload,
            ledger_preflight=ledger_preflight,
            close_target_resolution=close_target_resolution,
        )
        operations.append(operation)
    return operations


def persist_manual_close_event_with_ledger(
    repo: Any,
    *,
    record_id: str,
    fields: dict[str, Any] | None = None,
    contracts_to_close: int,
    close_price: float | None,
    close_reason: str,
    as_of_ms: int | None = None,
) -> ManualCloseLedgerResult:
    from src.application.ledger.manual_trades import (
        existing_manual_close_event_result,
        persist_manual_close_event,
    )

    resolved_record_id = str(record_id or "").strip()
    current_fields = _current_record_fields(repo, record_id=resolved_record_id)
    if fields is not None:
        _assert_fields_match_current(
            record_id=resolved_record_id,
            fields=fields,
            current_fields=current_fields,
            operation_label="manual close",
        )

    duplicate_result = existing_manual_close_event_result(
        repo,
        record_id=resolved_record_id,
        fields=current_fields,
        contracts_to_close=int(contracts_to_close),
        close_price=close_price,
        close_reason=close_reason,
    )
    if duplicate_result is not None:
        return ManualCloseLedgerResult(
            result=LedgerWriteResult.from_payload(duplicate_result),
            fields=current_fields,
            patch=PositionLotPatch(),
            ledger_preflight=LedgerPreflightResult(
                status="duplicate",
                read_model="legacy_trade_events",
                fail_closed=False,
                target_lot_id=resolved_record_id,
                event_id=duplicate_result.get("event_id"),
            ),
            duplicate_checked_before_patch=True,
        )

    ledger_preflight = preflight_manual_close(
        repo,
        record_id=resolved_record_id,
        fields=current_fields,
        contracts_to_close=int(contracts_to_close),
        close_price=close_price,
        close_reason=close_reason,
        as_of_ms=as_of_ms,
    )
    event_time_ms = int(ledger_preflight["event_time_ms"])
    patch = build_close_patch_contract(
        current_fields,
        contracts_to_close=int(contracts_to_close),
        close_price=close_price,
        close_reason=close_reason,
        as_of_ms=event_time_ms,
    )
    result = persist_manual_close_event(
        repo,
        record_id=resolved_record_id,
        fields=current_fields,
        contracts_to_close=int(contracts_to_close),
        close_price=close_price,
        close_reason=close_reason,
        as_of_ms=event_time_ms,
    )
    return ManualCloseLedgerResult(
        result=LedgerWriteResult.from_payload(result),
        fields=current_fields,
        patch=patch,
        ledger_preflight=ledger_preflight,
    )
