from __future__ import annotations

from dataclasses import replace
from typing import Any

from domain.domain.ledger import ContractKey, TradeEvent, project_trade_events
from domain.domain.ledger.position_fields import (
    EXPIRE_AUTO_CLOSE,
    OpenPositionCommand,
    build_open_adjustment_patch_contract,
    build_position_lot_fields,
    effective_contracts_open,
    effective_expiration_ymd,
    effective_multiplier,
    effective_strike,
    normalize_account,
    normalize_broker,
    normalize_currency,
    normalize_status,
    normalize_trade_price,
    now_ms,
    resolve_open_currency,
)
from src.application.ledger.errors import LedgerPreflightError
from src.application.ledger.event_codec import import_stored_trade_events, stored_trade_event_to_ledger_event
from src.application.ledger.migration import (
    import_position_lot_snapshot,
    shadow_replay_position_lot_snapshot,
)
from src.application.ledger.results import LedgerPreflightResult, ManualAdjustPreflightResult


def preflight_manual_open(
    repo: Any,
    *,
    command: OpenPositionCommand,
) -> LedgerPreflightResult:
    _resolved_command, _fields, event = _manual_open_ledger_inputs(command)
    return _preflight_open_event(
        repo,
        event=event,
        source="manual_open_preflight",
        operation_label="manual open",
    )


def preflight_trade_open(
    repo: Any,
    *,
    deal: Any,
) -> LedgerPreflightResult:
    _resolved_deal, _fields, event = _trade_open_ledger_inputs(deal)
    return _preflight_open_event(
        repo,
        event=event,
        source="broker_trade_open_preflight",
        operation_label="broker trade open",
    )


def preflight_manual_void(
    repo: Any,
    *,
    target_event_id: str,
    void_reason: str,
    as_of_ms: int | None = None,
) -> LedgerPreflightResult:
    return _preflight_manual_void_payload(
        repo,
        target_event_id=target_event_id,
        void_reason=void_reason,
        as_of_ms=as_of_ms,
    )["ledger_preflight"]


def preflight_manual_repair(
    repo: Any,
    *,
    target_event_id: str,
    overrides: dict[str, Any],
    repair_reason: str,
    as_of_ms: int | None = None,
) -> LedgerPreflightResult:
    return _preflight_manual_repair_payload(
        repo,
        target_event_id=target_event_id,
        overrides=overrides,
        repair_reason=repair_reason,
        as_of_ms=as_of_ms,
    )["ledger_preflight"]


def preflight_manual_adjust(
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
) -> LedgerPreflightResult:
    result = _preflight_lot_adjust(
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
    return result.ledger_preflight


def preflight_manual_close(
    repo: Any,
    *,
    record_id: str,
    fields: dict[str, Any] | None = None,
    contracts_to_close: int,
    close_price: float | None,
    close_reason: str,
    as_of_ms: int | None = None,
) -> LedgerPreflightResult:
    del close_reason
    return _preflight_lot_close(
        repo,
        record_id=record_id,
        fields=fields,
        contracts_to_close=contracts_to_close,
        close_price=close_price,
        as_of_ms=as_of_ms,
        event_type="close",
        source="manual_close_preflight",
        operation_label="manual close",
    )


def preflight_expire_auto_close(
    repo: Any,
    *,
    record_id: str,
    fields: dict[str, Any] | None = None,
    contracts_to_close: int,
    as_of_ms: int | None = None,
    exp_source: str | None = None,
    grace_days: int | None = None,
) -> LedgerPreflightResult:
    result = _preflight_lot_close(
        repo,
        record_id=record_id,
        fields=fields,
        contracts_to_close=contracts_to_close,
        close_price=0.0,
        as_of_ms=as_of_ms,
        event_type="expire_close",
        source="expire_auto_close_preflight",
        operation_label="expire auto-close",
    )
    return result.with_details(
        close_type=EXPIRE_AUTO_CLOSE,
        auto_close_exp_src=str(exp_source or ""),
        auto_close_grace_days=int(grace_days) if grace_days is not None else None,
    )


def preflight_broker_trade_close(
    repo: Any,
    *,
    record_id: str,
    fields: dict[str, Any] | None = None,
    contracts_to_close: int,
    close_price: float | None,
    as_of_ms: int | None = None,
) -> LedgerPreflightResult:
    return _preflight_lot_close(
        repo,
        record_id=record_id,
        fields=fields,
        contracts_to_close=contracts_to_close,
        close_price=close_price,
        as_of_ms=as_of_ms,
        event_type="close",
        source="broker_trade_close_preflight",
        operation_label="broker trade close",
    )


def _preflight_manual_void_payload(
    repo: Any,
    *,
    target_event_id: str,
    void_reason: str,
    as_of_ms: int | None,
) -> dict[str, Any]:
    from src.application.ledger.interventions import build_manual_void_preview

    preview = build_manual_void_preview(
        repo,
        target_event_id=target_event_id,
        void_reason=void_reason,
        as_of_ms=as_of_ms,
    )
    ledger_preflight = _preflight_trade_event_append(
        repo,
        appended_events=[preview["void_event"]],
        target_event_id=target_event_id,
        event_type="void",
        source="manual_void_preflight",
        operation_label="manual void",
    )
    return {"preview": preview, "ledger_preflight": ledger_preflight}


def _preflight_manual_repair_payload(
    repo: Any,
    *,
    target_event_id: str,
    overrides: dict[str, Any],
    repair_reason: str,
    as_of_ms: int | None,
) -> dict[str, Any]:
    from src.application.ledger.interventions import build_manual_repair_preview

    preview = build_manual_repair_preview(
        repo,
        target_event_id=target_event_id,
        overrides=overrides,
        repair_reason=repair_reason,
        as_of_ms=as_of_ms,
    )
    ledger_preflight = _preflight_trade_event_append(
        repo,
        appended_events=[preview["void_event"], preview["repair_event"]],
        target_event_id=target_event_id,
        event_type="repair",
        source="manual_repair_preflight",
        operation_label="manual repair",
    )
    return {"preview": preview, "ledger_preflight": ledger_preflight}


def _preflight_trade_event_append(
    repo: Any,
    *,
    appended_events: list[dict[str, Any]],
    target_event_id: str,
    event_type: str,
    source: str,
    operation_label: str,
) -> LedgerPreflightResult:
    current_events = _list_trade_events(repo)
    before_imported_events, before_import_diagnostics = import_stored_trade_events(current_events)
    before_projection = project_trade_events(before_imported_events)
    combined_events = [*current_events, *appended_events]
    imported_events, import_diagnostics = import_stored_trade_events(combined_events)
    voided_by_append = _voided_target_event_ids(appended_events)
    import_errors = [
        item.to_dict()
        for item in import_diagnostics
        if item.severity == "error" and item.event_id not in voided_by_append
    ]
    projection = project_trade_events(imported_events)
    projection_errors = [item.to_dict() for item in projection.diagnostics if item.severity == "error"]
    if import_errors or projection_errors:
        raise LedgerPreflightError(
            f"{event_type}_projection_invalid",
            f"{operation_label} ledger preflight rejected projected trade-event intervention",
            details={
                "target_event_id": str(target_event_id or "").strip(),
                "import_errors": import_errors,
                "projection_errors": projection_errors,
            },
        )
    return LedgerPreflightResult(
        status="ok",
        read_model="ledger_shadow",
        fail_closed=False,
        target_event_id=str(target_event_id or "").strip(),
        event_type=event_type,
        imported_event_count=len(imported_events),
        details={
            "appended_event_ids": [
                str(item.get("event_id") or "").strip()
                for item in appended_events
                if str(item.get("event_id") or "").strip()
            ],
            "source_event_count": len(current_events),
            "appended_event_count": len(appended_events),
            "before_projection_diagnostic_count": len(before_projection.diagnostics),
            "before_import_diagnostic_count": len(before_import_diagnostics),
            "after_projection_diagnostic_count": len(projection.diagnostics),
            "after_open_lot_count": sum(1 for lot in projection.lots if lot.contracts_open > 0),
            "source": source,
        },
    )


def _voided_target_event_ids(events: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for event in events:
        decoded, diagnostics = stored_trade_event_to_ledger_event(event)
        has_decode_errors = any(item.severity == "error" for item in diagnostics)
        if decoded is not None and not has_decode_errors:
            if decoded.event_type != "void":
                continue
            target = str(decoded.target_event_id or "").strip()
            if target:
                out.add(target)
            continue
        if str(event.get("position_effect") or "").strip().lower() != "void":
            continue
        payload = event.get("raw_payload") or {}
        if not isinstance(payload, dict):
            continue
        target = str(payload.get("void_target_event_id") or "").strip()
        if target:
            out.add(target)
    return out


def _preflight_open_event(
    repo: Any,
    *,
    event: TradeEvent,
    source: str,
    operation_label: str,
) -> LedgerPreflightResult:
    if int(event.contracts) <= 0:
        raise LedgerPreflightError(
            "invalid_quantity",
            f"{operation_label} ledger preflight requires contracts > 0",
            details={"event_id": event.event_id, "contracts": int(event.contracts)},
        )

    records = _list_position_lots(repo)
    shadow = shadow_replay_position_lot_snapshot(records, source=source)
    shadow_errors = _shadow_error_details(shadow)
    if shadow_errors:
        raise LedgerPreflightError(
            "ledger_shadow_invalid",
            f"{operation_label} ledger preflight found invalid current position projection",
            details={"event_id": event.event_id, "errors": shadow_errors},
        )

    imported_events, import_diagnostics = import_position_lot_snapshot(records, source=source)
    if any(item.severity == "error" for item in import_diagnostics):
        raise LedgerPreflightError(
            "ledger_shadow_invalid",
            f"{operation_label} ledger preflight could not import current position snapshot",
            details={"event_id": event.event_id, "errors": [item.to_dict() for item in import_diagnostics]},
        )
    open_projection = project_trade_events([*imported_events, event])
    open_errors = [item.to_dict() for item in open_projection.diagnostics if item.severity == "error"]
    if open_errors:
        raise LedgerPreflightError(
            "open_projection_invalid",
            f"{operation_label} ledger preflight rejected projected open event",
            details={"event_id": event.event_id, "errors": open_errors},
        )

    target_lot = next((lot for lot in open_projection.lots if lot.lot_id == event.lot_id), None)
    if target_lot is None:
        raise LedgerPreflightError(
            "target_lot_not_projected",
            f"{operation_label} ledger preflight did not project the new lot",
            details={"event_id": event.event_id, "target_lot_id": event.lot_id},
        )
    matching_before = sum(
        int(lot.contracts_open)
        for lot in shadow.projection.lots
        if lot.contract_key == event.contract_key and int(lot.contracts_open) > 0
    )
    matching_after = sum(
        int(lot.contracts_open)
        for lot in open_projection.lots
        if lot.contract_key == event.contract_key and int(lot.contracts_open) > 0
    )
    return LedgerPreflightResult(
        status="ok",
        read_model="ledger_shadow",
        fail_closed=False,
        target_lot_id=event.lot_id,
        event_id=event.event_id,
        event_type="open",
        contract_key=event.contract_key.to_dict(),
        position_key=event.contract_key.position_key,
        contracts_open_before=int(matching_before),
        contracts_to_open=int(event.contracts),
        contracts_open_after=int(target_lot.contracts_open),
        position_contracts_open_after=int(matching_after),
        event_time_ms=int(event.event_time_ms),
        source_record_count=shadow.source_record_count,
        imported_event_count=shadow.imported_event_count,
        projection_diagnostic_count=len(shadow.projection.diagnostics),
        reconciliation_issue_count=len(shadow.reconciliation.issues) if shadow.reconciliation is not None else 0,
    )


def _preflight_lot_close(
    repo: Any,
    *,
    record_id: str,
    fields: dict[str, Any] | None,
    contracts_to_close: int,
    close_price: float | None,
    as_of_ms: int | None,
    event_type: str,
    source: str,
    operation_label: str,
) -> LedgerPreflightResult:
    resolved_record_id = str(record_id or "").strip()
    if not resolved_record_id:
        raise LedgerPreflightError("record_id_required", f"{operation_label} ledger preflight requires record_id")
    if int(contracts_to_close) <= 0:
        raise LedgerPreflightError(
            "invalid_quantity",
            f"{operation_label} ledger preflight requires contracts_to_close > 0",
            details={"record_id": resolved_record_id, "contracts_to_close": int(contracts_to_close)},
        )
    try:
        normalized_close_price = normalize_trade_price(
            close_price,
            "close_price",
            allow_zero=(event_type == "expire_close"),
        )
    except ValueError as exc:
        raise LedgerPreflightError(
            "invalid_close_price",
            f"{operation_label} ledger preflight requires valid close_price",
            details={"record_id": resolved_record_id, "close_price": close_price, "error": str(exc)},
        ) from exc

    current_fields = _current_record_fields(repo, record_id=resolved_record_id)
    if fields is not None:
        _assert_fields_match_current(
            record_id=resolved_record_id,
            fields=fields,
            current_fields=current_fields,
            operation_label=operation_label,
        )
    current_key = _contract_key_from_fields(current_fields)
    current_open = effective_contracts_open(current_fields)
    if normalize_status(current_fields.get("status")) == "close" or current_open <= 0:
        raise LedgerPreflightError(
            "target_lot_not_open",
            f"{operation_label} ledger preflight target lot is not open",
            details={"record_id": resolved_record_id, "contracts_open": current_open},
        )

    records = _list_position_lots(repo)
    shadow = shadow_replay_position_lot_snapshot(records, source=source)
    shadow_errors = _shadow_error_details(shadow)
    if shadow_errors:
        raise LedgerPreflightError(
            "ledger_shadow_invalid",
            f"{operation_label} ledger preflight found invalid current position projection",
            details={"record_id": resolved_record_id, "errors": shadow_errors},
        )

    target_lots = [lot for lot in shadow.projection.lots if lot.lot_id == resolved_record_id and lot.contracts_open > 0]
    if not target_lots:
        raise LedgerPreflightError(
            "target_lot_not_found",
            f"{operation_label} ledger preflight target lot is missing from current projection",
            details={"record_id": resolved_record_id},
        )
    if len(target_lots) > 1:
        raise LedgerPreflightError(
            "duplicate_target_lot",
            f"{operation_label} ledger preflight found duplicate target lot ids",
            details={"record_id": resolved_record_id, "count": len(target_lots)},
        )

    target_lot = target_lots[0]
    if target_lot.contract_key != current_key:
        raise LedgerPreflightError(
            "target_contract_mismatch",
            f"{operation_label} ledger preflight target identity differs from current record fields",
            details={
                "record_id": resolved_record_id,
                "current_contract_key": current_key.to_dict(),
                "projection_contract_key": target_lot.contract_key.to_dict(),
            },
        )
    if int(contracts_to_close) > int(target_lot.contracts_open):
        raise LedgerPreflightError(
            "close_contracts_exceed_open",
            f"{operation_label} ledger preflight close quantity exceeds open contracts",
            details={
                "record_id": resolved_record_id,
                "contracts_to_close": int(contracts_to_close),
                "contracts_open": int(target_lot.contracts_open),
            },
        )

    imported_events, import_diagnostics = import_position_lot_snapshot(records, source=source)
    if any(item.severity == "error" for item in import_diagnostics):
        raise LedgerPreflightError(
            "ledger_shadow_invalid",
            f"{operation_label} ledger preflight could not import current position snapshot",
            details={"record_id": resolved_record_id, "errors": [item.to_dict() for item in import_diagnostics]},
        )
    event_time_ms = _preflight_event_time_ms(imported_events, as_of_ms=as_of_ms)
    close_event = TradeEvent(
        event_id=f"preflight:{source}:{resolved_record_id}:{event_time_ms}",
        event_type=event_type,
        event_time_ms=event_time_ms,
        contract_key=current_key,
        contracts=int(contracts_to_close),
        price=float(normalized_close_price),
        currency=normalize_currency(current_fields.get("currency")),
        source=source,
        multiplier=float(effective_multiplier(current_fields) or 100),
        target_lot_id=resolved_record_id,
        raw_payload={"record_id": resolved_record_id},
    )
    close_projection = project_trade_events([*imported_events, close_event])
    close_errors = [item.to_dict() for item in close_projection.diagnostics if item.severity == "error"]
    if close_errors:
        raise LedgerPreflightError(
            "close_projection_invalid",
            f"{operation_label} ledger preflight rejected projected close event",
            details={"record_id": resolved_record_id, "errors": close_errors},
        )
    projected_target = next((lot for lot in close_projection.lots if lot.lot_id == resolved_record_id), None)
    after_open = int(projected_target.contracts_open) if projected_target is not None else 0
    return LedgerPreflightResult(
        status="ok",
        read_model="ledger_shadow",
        fail_closed=False,
        target_lot_id=resolved_record_id,
        event_type=event_type,
        contract_key=current_key.to_dict(),
        contracts_open_before=int(target_lot.contracts_open),
        contracts_to_close=int(contracts_to_close),
        contracts_open_after=after_open,
        event_time_ms=event_time_ms,
        source_record_count=shadow.source_record_count,
        imported_event_count=shadow.imported_event_count,
        projection_diagnostic_count=len(shadow.projection.diagnostics),
        reconciliation_issue_count=len(shadow.reconciliation.issues) if shadow.reconciliation is not None else 0,
    )


def _preflight_lot_adjust(
    repo: Any,
    *,
    record_id: str,
    fields: dict[str, Any] | None,
    contracts: int | None,
    strike: float | None,
    expiration_ymd: str | None,
    premium_per_share: float | None,
    multiplier: float | None,
    opened_at_ms: int | None,
    as_of_ms: int | None,
    source: str,
    operation_label: str,
) -> ManualAdjustPreflightResult:
    resolved_record_id = str(record_id or "").strip()
    if not resolved_record_id:
        raise LedgerPreflightError("record_id_required", f"{operation_label} ledger preflight requires record_id")

    current_fields = _current_record_fields(repo, record_id=resolved_record_id)
    if fields is not None:
        _assert_fields_match_current(
            record_id=resolved_record_id,
            fields=fields,
            current_fields=current_fields,
            operation_label=operation_label,
        )
    current_key = _contract_key_from_fields(current_fields)
    current_open = effective_contracts_open(current_fields)
    if normalize_status(current_fields.get("status")) == "close" or current_open <= 0:
        raise LedgerPreflightError(
            "target_lot_not_open",
            f"{operation_label} ledger preflight target lot is not open",
            details={"record_id": resolved_record_id, "contracts_open": current_open},
        )

    records = _list_position_lots(repo)
    shadow = shadow_replay_position_lot_snapshot(records, source=source)
    shadow_errors = _shadow_error_details(shadow)
    if shadow_errors:
        raise LedgerPreflightError(
            "ledger_shadow_invalid",
            f"{operation_label} ledger preflight found invalid current position projection",
            details={"record_id": resolved_record_id, "errors": shadow_errors},
        )

    target_lots = [lot for lot in shadow.projection.lots if lot.lot_id == resolved_record_id and lot.contracts_open > 0]
    if not target_lots:
        raise LedgerPreflightError(
            "target_lot_not_found",
            f"{operation_label} ledger preflight target lot is missing from current projection",
            details={"record_id": resolved_record_id},
        )
    if len(target_lots) > 1:
        raise LedgerPreflightError(
            "duplicate_target_lot",
            f"{operation_label} ledger preflight found duplicate target lot ids",
            details={"record_id": resolved_record_id, "count": len(target_lots)},
        )
    target_lot = target_lots[0]
    if target_lot.contract_key != current_key:
        raise LedgerPreflightError(
            "target_contract_mismatch",
            f"{operation_label} ledger preflight target identity differs from current record fields",
            details={
                "record_id": resolved_record_id,
                "current_contract_key": current_key.to_dict(),
                "projection_contract_key": target_lot.contract_key.to_dict(),
            },
        )

    imported_events, import_diagnostics = import_position_lot_snapshot(records, source=source)
    if any(item.severity == "error" for item in import_diagnostics):
        raise LedgerPreflightError(
            "ledger_shadow_invalid",
            f"{operation_label} ledger preflight could not import current position snapshot",
            details={"record_id": resolved_record_id, "errors": [item.to_dict() for item in import_diagnostics]},
        )
    event_time_ms = _preflight_event_time_ms(imported_events, as_of_ms=as_of_ms)
    patch_contract = build_open_adjustment_patch_contract(
        current_fields,
        contracts=contracts,
        strike=strike,
        expiration_ymd=expiration_ymd,
        premium_per_share=premium_per_share,
        multiplier=multiplier,
        opened_at_ms=opened_at_ms,
        as_of_ms=event_time_ms,
    )
    patch = patch_contract.to_dict()
    adjusted_fields = dict(current_fields)
    adjusted_fields.update(patch)
    adjusted_key = _contract_key_from_fields(adjusted_fields)
    adjust_event = TradeEvent(
        event_id=f"preflight:{source}:{resolved_record_id}:{event_time_ms}",
        event_type="adjust",
        event_time_ms=event_time_ms,
        contract_key=current_key,
        contracts=0,
        price=float(adjusted_fields.get("premium") or current_fields.get("premium") or 0.0),
        currency=normalize_currency(adjusted_fields.get("currency") or current_fields.get("currency")),
        source=source,
        multiplier=float(effective_multiplier(adjusted_fields) or effective_multiplier(current_fields) or 100),
        target_lot_id=resolved_record_id,
        raw_payload={
            "record_id": resolved_record_id,
            "adjust_target_source_event_id": str(current_fields.get("source_event_id") or "").strip() or None,
            "patch": patch,
        },
    )
    adjust_projection = project_trade_events([*imported_events, adjust_event])
    adjust_errors = [item.to_dict() for item in adjust_projection.diagnostics if item.severity == "error"]
    if adjust_errors:
        raise LedgerPreflightError(
            "adjust_projection_invalid",
            f"{operation_label} ledger preflight rejected projected adjust event",
            details={"record_id": resolved_record_id, "errors": adjust_errors},
        )
    return ManualAdjustPreflightResult(
        fields=current_fields,
        patch_contract=patch_contract,
        ledger_preflight=LedgerPreflightResult(
            status="ok",
            read_model="ledger_shadow",
            fail_closed=False,
            target_lot_id=resolved_record_id,
            event_type="adjust",
            contract_key=current_key.to_dict(),
            contracts_open_before=int(target_lot.contracts_open),
            contracts_open_after=effective_contracts_open(adjusted_fields),
            event_time_ms=event_time_ms,
            source_record_count=shadow.source_record_count,
            imported_event_count=shadow.imported_event_count,
            projection_diagnostic_count=len(shadow.projection.diagnostics),
            reconciliation_issue_count=len(shadow.reconciliation.issues) if shadow.reconciliation is not None else 0,
            details={"adjusted_contract_key": adjusted_key.to_dict()},
        ),
    )


def _split_close_deal_for_target(
    deal: Any,
    *,
    record_id: str,
    fields: dict[str, Any],
    contracts_to_close: int,
    close_target_resolution: dict[str, Any] | None = None,
) -> Any:
    source_deal_id = str(getattr(deal, "deal_id", "") or "").strip()
    event_id = f"{source_deal_id}:close:{record_id}" if source_deal_id else f"close:{record_id}"
    raw_payload = dict(getattr(deal, "raw_payload", {}) or {})
    raw_payload.update(
        {
            "source_deal_id": source_deal_id or None,
            "record_id": str(record_id),
            "target_lot_id": str(record_id),
            "close_target_source_event_id": str(fields.get("source_event_id") or "").strip() or None,
            "close_target_account": normalize_account(fields.get("account")),
            "close_target_broker": normalize_broker(fields.get("broker") or fields.get("market")),
        }
    )
    if close_target_resolution is not None:
        raw_payload["close_target_resolution"] = close_target_resolution
    return replace(
        deal,
        deal_id=event_id,
        contracts=int(contracts_to_close),
        raw_payload=raw_payload,
    )


def _manual_open_ledger_inputs(command: OpenPositionCommand) -> tuple[OpenPositionCommand, dict[str, Any], TradeEvent]:
    from src.application.ledger.manual_trades import _manual_open_event_id

    event_time_ms = int(command.opened_at_ms or now_ms())
    resolved_command = replace(command, opened_at_ms=event_time_ms)
    fields = build_position_lot_fields(resolved_command).to_dict()
    contract_key = _contract_key_from_fields(fields)
    trade_side = "sell" if str(resolved_command.side or "").strip().lower() == "short" else "buy"
    event_id = _manual_open_event_id(
        broker=str(resolved_command.broker),
        account=str(resolved_command.account),
        symbol=contract_key.underlying_symbol,
        option_type=str(resolved_command.option_type),
        side=trade_side,
        contracts=int(resolved_command.contracts),
        price=float(fields["premium"]),
        strike=effective_strike(fields),
        expiration_ymd=str(resolved_command.expiration_ymd or "").strip() or None,
        trade_time_ms=event_time_ms,
    )
    event = TradeEvent(
        event_id=event_id,
        event_type="open",
        event_time_ms=event_time_ms,
        contract_key=contract_key,
        contracts=int(resolved_command.contracts),
        price=float(fields["premium"]),
        currency=resolve_open_currency(fields.get("symbol"), fields.get("currency")),
        source="cli_manual_open",
        multiplier=float(effective_multiplier(fields) or 100),
        lot_id=f"lot_{event_id}",
        raw_payload={"source": "om option-positions", "mode": "manual_open"},
    )
    return resolved_command, fields, event


def _trade_open_ledger_inputs(deal: Any) -> tuple[Any, dict[str, Any], TradeEvent]:
    event_time_ms = int(getattr(deal, "trade_time_ms", None) or now_ms())
    resolved_deal = replace(deal, trade_time_ms=event_time_ms)
    command = _open_command_from_trade_deal(resolved_deal)
    fields = build_position_lot_fields(command).to_dict()
    contract_key = _contract_key_from_fields(fields)
    event_id = str(getattr(resolved_deal, "deal_id", "") or "").strip()
    event = TradeEvent(
        event_id=event_id,
        event_type="open",
        event_time_ms=event_time_ms,
        contract_key=contract_key,
        contracts=int(getattr(resolved_deal, "contracts", 0) or 0),
        price=float(fields["premium"]),
        currency=resolve_open_currency(fields.get("symbol"), fields.get("currency")),
        source="opend_push",
        multiplier=float(effective_multiplier(fields) or 100),
        lot_id=f"lot_{event_id}",
        raw_payload=dict(getattr(resolved_deal, "raw_payload", {}) or {}),
    )
    return resolved_deal, fields, event


def _open_command_from_trade_deal(deal: Any) -> OpenPositionCommand:
    side = str(getattr(deal, "side", "") or "").strip().lower()
    return OpenPositionCommand(
        broker=str(getattr(deal, "broker", None) or "富途"),
        account=str(getattr(deal, "internal_account", "") or ""),
        symbol=str(getattr(deal, "symbol", "") or ""),
        option_type=str(getattr(deal, "option_type", "") or ""),
        side="short" if side == "sell" else "long",
        contracts=int(getattr(deal, "contracts", 0) or 0),
        currency=str(getattr(deal, "currency", "") or ""),
        strike=(
            float(getattr(deal, "strike"))
            if getattr(deal, "strike", None) is not None
            else None
        ),
        multiplier=(
            float(getattr(deal, "multiplier"))
            if getattr(deal, "multiplier", None) is not None
            else None
        ),
        expiration_ymd=(str(getattr(deal, "expiration_ymd", "") or "").strip() or None),
        premium_per_share=(
            float(getattr(deal, "price"))
            if getattr(deal, "price", None) not in (None, "")
            else None
        ),
        note=(
            f"source=opend_push "
            f"deal_id={getattr(deal, 'deal_id', '') or ''} "
            f"order_id={getattr(deal, 'order_id', '') or ''} "
            f"multiplier_source={getattr(deal, 'multiplier_source', '') or ''} "
            f"trade_time_ms={getattr(deal, 'trade_time_ms', '') or ''}"
        ).strip(),
        opened_at_ms=getattr(deal, "trade_time_ms", None),
    )


def _duplicate_open_preflight(*, event: TradeEvent, result: dict[str, Any]) -> LedgerPreflightResult:
    return LedgerPreflightResult(
        status="duplicate",
        read_model="legacy_trade_events",
        fail_closed=False,
        target_lot_id=event.lot_id,
        event_id=result.get("event_id") or event.event_id,
        event_type="open",
        contract_key=event.contract_key.to_dict(),
        position_key=event.contract_key.position_key,
    )


def _existing_open_event_result(repo: Any, *, event_id: str, record_id: str | None) -> dict[str, Any] | None:
    candidate = getattr(repo, "primary_repo", repo)
    list_trade_events = getattr(candidate, "list_trade_events", None)
    if not callable(list_trade_events):
        return None
    raw_events = list_trade_events()
    events = [item for item in raw_events if isinstance(item, dict)] if isinstance(raw_events, list) else []
    if not any(str(item.get("event_id") or "").strip() == str(event_id).strip() for item in events):
        return None
    list_position_lots = getattr(candidate, "list_position_lots", None)
    raw_position_lots = list_position_lots() if callable(list_position_lots) else []
    return {
        "event_id": str(event_id),
        "record_id": str(record_id).strip() if record_id else None,
        "created": False,
        "position_lot_count": len(raw_position_lots) if isinstance(raw_position_lots, list) else 0,
    }


def _current_record_fields(repo: Any, *, record_id: str) -> dict[str, Any]:
    get_record_fields = getattr(repo, "get_record_fields", None)
    if not callable(get_record_fields):
        raise TypeError("option_positions repo does not expose get_record_fields")
    fields = get_record_fields(str(record_id))
    if not isinstance(fields, dict):
        raise TypeError(f"option_positions repo returned non-dict fields for record_id={record_id}")
    return dict(fields)


def _list_position_lots(repo: Any) -> list[dict[str, Any]]:
    candidate = getattr(repo, "primary_repo", repo)
    list_position_lots = getattr(candidate, "list_position_lots", None)
    if not callable(list_position_lots):
        raise TypeError("option_positions repo does not expose list_position_lots")
    rows = list_position_lots()
    if not isinstance(rows, list):
        raise TypeError("option_positions repo returned non-list position_lots")
    return [item for item in rows if isinstance(item, dict)]


def _list_trade_events(repo: Any) -> list[dict[str, Any]]:
    candidate = getattr(repo, "primary_repo", repo)
    list_trade_events = getattr(candidate, "list_trade_events", None)
    if not callable(list_trade_events):
        raise TypeError("option_positions repo does not expose list_trade_events")
    rows = list_trade_events()
    if not isinstance(rows, list):
        raise TypeError("option_positions repo returned non-list trade_events")
    return [item for item in rows if isinstance(item, dict)]


def _contract_key_from_fields(fields: dict[str, Any]) -> ContractKey:
    return ContractKey.from_values(
        broker=fields.get("broker") or fields.get("market"),
        account=fields.get("account"),
        underlying_symbol=fields.get("symbol"),
        option_type=fields.get("option_type"),
        position_side=fields.get("side"),
        strike=effective_strike(fields),
        expiration_ymd=fields.get("expiration_ymd") or effective_expiration_ymd(fields),
    )


def _assert_fields_match_current(
    *,
    record_id: str,
    fields: dict[str, Any],
    current_fields: dict[str, Any],
    operation_label: str,
) -> None:
    expected_key = _contract_key_from_fields(current_fields)
    provided_key = _contract_key_from_fields(fields)
    mismatches: list[str] = []
    if expected_key != provided_key:
        mismatches.append("contract_key")
    if normalize_currency(current_fields.get("currency")) != normalize_currency(fields.get("currency")):
        mismatches.append("currency")
    if _optional_float(effective_multiplier(current_fields)) != _optional_float(effective_multiplier(fields)):
        mismatches.append("multiplier")
    if str(current_fields.get("source_event_id") or "").strip() != str(fields.get("source_event_id") or "").strip():
        mismatches.append("source_event_id")
    if mismatches:
        raise LedgerPreflightError(
            "target_fields_mismatch",
            f"{operation_label} ledger preflight target fields do not match current lot state",
            details={"record_id": record_id, "mismatches": mismatches},
        )


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _preflight_event_time_ms(events: list[TradeEvent], *, as_of_ms: int | None) -> int:
    requested = int(as_of_ms or now_ms())
    latest_snapshot_time = max((int(event.event_time_ms or 0) for event in events), default=0)
    return max(requested, latest_snapshot_time + 1)


def _shadow_error_details(shadow: Any) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    errors.extend(item.to_dict() for item in shadow.import_diagnostics if item.severity == "error")
    errors.extend(item.to_dict() for item in shadow.projection.diagnostics if item.severity == "error")
    if shadow.reconciliation is not None:
        errors.extend(item.to_dict() for item in shadow.reconciliation.issues if item.severity == "error")
    return errors
