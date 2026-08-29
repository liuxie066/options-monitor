from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import hashlib
import json
from typing import Any
from zoneinfo import ZoneInfo

from domain.domain.ledger.position_fields import (
    EXPIRE_AUTO_CLOSE,
    OpenPositionCommand,
    PositionLotPatch,
    build_close_patch_contract,
    build_open_adjustment_patch_contract,
    build_position_lot_fields,
    effective_multiplier,
    strategy_metadata_fields_from_payload,
)
from domain.domain.trade_contract_identity import normalize_trade_side
from src.application.ledger.interventions import (
    build_manual_repair_preview,
    build_manual_void_preview,
    persist_manual_repair_event,
    persist_manual_void_event,
)
from src.application.ledger.lot_resolver import (
    CloseTargetResolution,
    LotCloseCandidate,
    LotCloseMatch,
    LotCloseResolutionError,
    LotCloseSelector,
    find_unique_open_lot,
    load_close_candidate_records,
    resolve_explicit_close_target,
    resolve_fifo_close_lots,
    resolve_fifo_close_targets,
    resolve_unique_close_lot,
    resolve_unique_close_target,
    summarize_close_candidates,
)
from src.application.ledger.maintenance import (
    _PROJECTION_REFRESH_NOT_PROVIDED,
    auto_close_expired_positions,
    build_expired_close_decisions,
)
from src.application.ledger.preflight import (
    _assert_fields_match_current,
    _current_record_fields,
    _duplicate_open_preflight,
    _existing_open_event_result,
    _manual_open_ledger_inputs,
    _preflight_lot_adjust,
    _preflight_manual_repair_payload,
    _preflight_manual_void_payload,
    _preflight_open_event,
    _split_close_deal_for_target,
    _trade_open_ledger_inputs,
    preflight_broker_trade_close,
    preflight_manual_adjust,
    preflight_manual_close,
    preflight_manual_open,
    preflight_manual_repair,
    preflight_manual_void,
)
from src.application.ledger.repository import (
    require_option_positions_read_repo,
)
from src.application.ledger.lifecycle_attempt_audit import (
    LifecycleAttemptAuditEnvelope,
)
from src.application.ledger.manual_trades import (
    assert_manual_request_event_matches,
    existing_manual_close_event_result,
    persist_manual_adjust_event,
    persist_manual_adjust_events,
    persist_manual_close_event,
    persist_manual_open_event,
)
from src.application.ledger.results import (
    BrokerTradeOpenPreviewResult,
    BrokerTradeOperation,
    ExpiredCloseDecision,
    ExpiredCloseRunResult,
    LedgerPreflightResult,
    LedgerWriteResult,
    ManualAdjustLedgerResult,
    ManualAdjustPreviewResult,
    ManualCloseLedgerResult,
    ManualClosePreviewResult,
    ManualOpenPreviewResult,
    OpenLedgerResult,
    ProjectionRefreshResult,
    TradeEventInterventionLedgerResult,
)
from src.application.ledger.writer import (
    accept_option_close_evidence_atomically,
    adopt_existing_combo_identity_atomically,
    advance_lifecycle_case_state_atomically,
    apply_lifecycle_allocation_atomically,
    bind_lifecycle_timing_policy_atomically,
    discover_expired_lifecycle_cases_atomically,
    persist_normalized_trade_events_atomically,
    persist_trade_event,
    persist_trade_event_with_combo_identity,
    record_assigned_stock_event_atomically,
    record_lifecycle_evidence_issue_atomically,
    rebuild_position_lots_from_trade_events,
)
from src.application.ledger.lifecycle import persist_assignment_events, persist_exercise_events, persist_expire_close_events


def supports_ledger_open_preflight(repo: Any) -> bool:
    candidate = getattr(repo, "primary_repo", repo)
    return callable(getattr(candidate, "list_position_lots", None))


def supports_ledger_close_preflight(repo: Any) -> bool:
    candidate = getattr(repo, "primary_repo", repo)
    return (
        callable(getattr(repo, "get_record_fields", None))
        and callable(getattr(candidate, "list_position_lots", None))
    )


def _ledger_write_result_from_any(value: Any, *, event_id: str | None, record_id: str | None) -> LedgerWriteResult:
    if isinstance(value, LedgerWriteResult):
        return value
    if isinstance(value, dict):
        return LedgerWriteResult.from_payload(value)
    return LedgerWriteResult(event_id=event_id, record_id=record_id, created=None)


def persist_manual_open_event_with_ledger(repo: Any, command: OpenPositionCommand) -> OpenLedgerResult:
    resolved_command, fields, event = _manual_open_ledger_inputs(command)
    duplicate_result = _existing_open_event_result(repo, event_id=event.event_id, record_id=event.lot_id)
    if duplicate_result is not None:
        request_id = str(resolved_command.request_id or "").strip()
        if request_id:
            assert_manual_request_event_matches(
                repo,
                event_id=event.event_id,
                request_id=request_id,
                intent_hash=str(
                    (event.raw_payload or {}).get("manual_request_intent_hash")
                    or ""
                ),
                command=resolved_command,
                fields=fields,
            )
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
    strategy: str | None = None,
    leg_role: str | None = None,
    strategy_group_id: str | None = None,
    strategy_snapshot: dict[str, Any] | None = None,
    as_of_ms: int | None = None,
) -> ManualAdjustLedgerResult:
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
        strategy=strategy,
        leg_role=leg_role,
        strategy_group_id=strategy_group_id,
        strategy_snapshot=strategy_snapshot,
        as_of_ms=as_of_ms,
        source="manual_adjust_preflight",
        operation_label="manual adjust",
    )
    ledger_preflight = preflight_result.ledger_preflight
    event_time_ms = ledger_preflight.event_time_ms
    if event_time_ms is None:
        raise RuntimeError("manual adjust ledger preflight did not provide event_time_ms")
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
        strategy=strategy,
        leg_role=leg_role,
        strategy_group_id=strategy_group_id,
        strategy_snapshot=strategy_snapshot,
        as_of_ms=int(event_time_ms),
    )
    return ManualAdjustLedgerResult(
        result=LedgerWriteResult.from_payload(result),
        fields=current_fields,
        patch=patch,
        ledger_preflight=ledger_preflight,
    )


def record_manual_position_adjustments(
    repo: Any,
    adjustments: list[dict[str, Any]],
) -> list[ManualAdjustLedgerResult]:
    """Preflight and persist multiple lot adjustments atomically."""

    prepared: list[dict[str, Any]] = []
    preflight_results: list[Any] = []
    for raw in adjustments:
        item = dict(raw or {})
        record_id = str(item.pop("record_id", "") or "").strip()
        if not record_id:
            raise ValueError("manual adjustment batch requires record_id")
        item.setdefault("fields", None)
        item.setdefault("contracts", None)
        item.setdefault("strike", None)
        item.setdefault("expiration_ymd", None)
        item.setdefault("premium_per_share", None)
        item.setdefault("multiplier", None)
        item.setdefault("opened_at_ms", None)
        item.setdefault("as_of_ms", None)
        preflight_result = _preflight_lot_adjust(
            repo,
            record_id=record_id,
            source="manual_adjust_batch_preflight",
            operation_label="manual adjustment batch",
            **item,
        )
        event_time_ms = preflight_result.ledger_preflight.event_time_ms
        if event_time_ms is None:
            raise RuntimeError("manual adjustment batch preflight did not provide event_time_ms")
        preflight_results.append(preflight_result)
        prepared.append(
            {
                "record_id": record_id,
                **item,
                "fields": preflight_result.fields,
                "as_of_ms": int(event_time_ms),
            }
        )

    ledger_results = persist_manual_adjust_events(repo, prepared)
    return [
        ManualAdjustLedgerResult(
            result=LedgerWriteResult.from_payload(ledger_result),
            fields=preflight_result.fields,
            patch=preflight_result.patch_contract,
            ledger_preflight=preflight_result.ledger_preflight,
        )
        for preflight_result, ledger_result in zip(preflight_results, ledger_results, strict=True)
    ]


def persist_trade_close_events_with_ledger(
    repo: Any,
    *,
    matches: list[Any],
    deal: Any,
    persist_trade_event_fn: Any,
    close_target_resolution: dict[str, Any] | None = None,
) -> list[BrokerTradeOperation]:
    prepared: list[tuple[Any, Any, LedgerPreflightResult]] = []
    close_action = "buy_close" if str(getattr(deal, "side", "") or "").strip().lower() == "buy" else "sell_close"
    broker_close_event_type = _broker_close_event_type(deal)
    broker_close_type = EXPIRE_AUTO_CLOSE if broker_close_event_type == "expire_close" else None
    broker_close_reason = (
        "broker_expiration_zero_price_close"
        if broker_close_event_type == "expire_close"
        else None
    )
    split_count = len(matches)
    expected_contracts = int(getattr(deal, "contracts", 0) or 0)
    source_deal_id = str(getattr(deal, "deal_id", "") or "").strip()
    allocated_contracts = sum(
        int(getattr(match, "contracts_to_close", 0) or 0)
        for match in matches
    )
    if split_count <= 0 or expected_contracts <= 0 or allocated_contracts != expected_contracts:
        raise ValueError(
            "broker close split allocation must exactly match the source deal contracts "
            f"(expected={expected_contracts}, allocated={allocated_contracts}, splits={split_count})"
        )
    for split_index, match in enumerate(matches, start=1):
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
            event_type=broker_close_event_type,
        )
        split_deal = _split_close_deal_for_target(
            deal,
            record_id=record_id,
            fields=fields,
            contracts_to_close=contracts_to_close,
            close_target_resolution=close_target_resolution,
        )
        if broker_close_type:
            raw_payload = dict(getattr(split_deal, "raw_payload", {}) or {})
            raw_payload.setdefault("close_type", broker_close_type)
            raw_payload.setdefault("broker_close_type", "expiration_zero_close")
            if broker_close_reason:
                raw_payload.setdefault("broker_close_reason", broker_close_reason)
            split_deal = replace(split_deal, raw_payload=raw_payload)
        raw_payload = dict(getattr(split_deal, "raw_payload", {}) or {})
        raw_payload["broker_deal_completion"] = {
            "source_deal_id": source_deal_id or None,
            "expected_contracts": expected_contracts,
            "split_count": split_count,
            "split_index": split_index,
            "allocated_contracts": contracts_to_close,
        }
        split_deal = replace(split_deal, raw_payload=raw_payload)
        prepared.append((match, split_deal, ledger_preflight))

    if persist_trade_event_fn in {record_normalized_trade_event, persist_trade_event}:
        persisted = persist_normalized_trade_events_atomically(
            repo,
            [split_deal for _match, split_deal, _preflight in prepared],
        )
    else:
        persisted = [
            _ledger_write_result_from_any(
                persist_trade_event_fn(repo, split_deal),
                event_id=f"{getattr(deal, 'deal_id', '')}:close:{getattr(match, 'record_id', '')}",
                record_id=str(getattr(match, "record_id", "") or "").strip(),
            )
            for match, split_deal, _preflight in prepared
        ]

    operations: list[BrokerTradeOperation] = []
    for (match, _split_deal, ledger_preflight), result in zip(
        prepared,
        persisted,
        strict=True,
    ):
        record_id = str(getattr(match, "record_id", "") or "").strip()
        contracts_to_close = int(getattr(match, "contracts_to_close", 0) or 0)
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


def _broker_close_event_type(deal: Any) -> str:
    return "expire_close" if _is_expiration_zero_price_close(deal) else "close"


def _is_expiration_zero_price_close(deal: Any) -> bool:
    if str(getattr(deal, "position_effect", "") or "").strip().lower() != "close":
        return False
    try:
        if float(getattr(deal, "price")) != 0.0:
            return False
    except (TypeError, ValueError):
        return False
    expiration = str(getattr(deal, "expiration_ymd", "") or "").strip()
    if not expiration:
        return False
    try:
        expiration_date = datetime.strptime(expiration, "%Y-%m-%d").date()
    except ValueError:
        return False
    raw_trade_time_ms = getattr(deal, "trade_time_ms", None)
    if raw_trade_time_ms in (None, ""):
        return False
    try:
        trade_time_ms = int(raw_trade_time_ms)
    except (TypeError, ValueError):
        return False
    for tz_name in ("America/New_York", "Asia/Shanghai"):
        trade_date = datetime.fromtimestamp(trade_time_ms / 1000, tz=ZoneInfo(tz_name)).date()
        if trade_date >= expiration_date:
            return True
    return False


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
                event_id=duplicate_result.event_id,
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
    event_time_ms = int(ledger_preflight.event_time_ms)
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


def preview_manual_position_open(repo: Any | None, command: OpenPositionCommand) -> ManualOpenPreviewResult:
    fields_contract = build_position_lot_fields(command)
    fields = fields_contract.to_dict()
    fields.update(
        strategy_metadata_fields_from_payload(
            {
                "strategy_snapshot": (
                    dict(command.strategy_snapshot) if isinstance(command.strategy_snapshot, dict) else None
                )
            }
        )
    )
    resolved_command = command
    if resolved_command.opened_at_ms is None:
        resolved_command = replace(resolved_command, opened_at_ms=int(fields_contract.opened_at))
    ledger_preflight = None
    if repo is not None and supports_ledger_open_preflight(repo):
        ledger_preflight = preflight_manual_open(repo, command=resolved_command)
    return ManualOpenPreviewResult(fields=fields, command=resolved_command, ledger_preflight=ledger_preflight)


def record_manual_position_open(repo: Any, command: OpenPositionCommand) -> OpenLedgerResult:
    if supports_ledger_open_preflight(repo):
        return persist_manual_open_event_with_ledger(repo, command)
    result = persist_manual_open_event(repo, command)
    fields = build_position_lot_fields(command).to_dict()
    fields.update(
        strategy_metadata_fields_from_payload(
            {
                "strategy_snapshot": (
                    dict(command.strategy_snapshot) if isinstance(command.strategy_snapshot, dict) else None
                )
            }
        )
    )
    return OpenLedgerResult(
        result=LedgerWriteResult.from_payload(result),
        fields=fields,
        command=command,
        ledger_preflight=LedgerPreflightResult(
            status="skipped",
            read_model="unavailable",
            fail_closed=False,
            event_type="open",
            details={"reason": "repo_does_not_support_ledger_open_preflight"},
        ),
    )


def resolve_manual_position_close_lot(
    repo: Any,
    *,
    broker: str = "富途",
    account: str | None,
    symbol: str | None,
    option_type: str | None,
    position_side: str | None,
    strike: float | None,
    expiration_ymd: str | None,
    contracts_to_close: int,
) -> LotCloseMatch:
    selector = LotCloseSelector.from_values(
        broker=broker,
        account=account,
        symbol=symbol,
        option_type=option_type,
        position_side=position_side,
        strike=strike,
        expiration_ymd=expiration_ymd,
        contracts_to_close=contracts_to_close,
    )
    return resolve_unique_close_lot(repo, selector)


def resolve_manual_position_close_target(
    repo: Any,
    *,
    broker: str = "富途",
    account: str | None,
    symbol: str | None,
    option_type: str | None,
    position_side: str | None,
    strike: float | None,
    expiration_ymd: str | None,
    contracts_to_close: int,
) -> CloseTargetResolution:
    selector = LotCloseSelector.from_values(
        broker=broker,
        account=account,
        symbol=symbol,
        option_type=option_type,
        position_side=position_side,
        strike=strike,
        expiration_ymd=expiration_ymd,
        contracts_to_close=contracts_to_close,
    )
    return resolve_unique_close_target(repo, selector, source="manual_close")


def _manual_assignment_target_resolution(
    repo: Any,
    *,
    record_id: str | None,
    broker: str,
    account: str | None,
    symbol: str | None,
    option_type: str | None,
    position_side: str | None,
    strike: float | None,
    expiration_ymd: str | None,
    contracts_to_close: int,
) -> CloseTargetResolution:
    resolved_record_id = str(record_id or "").strip()
    if resolved_record_id:
        fields = _current_record_fields(repo, record_id=resolved_record_id)
        return resolve_explicit_close_target(
            repo,
            record_id=resolved_record_id,
            contracts_to_close=int(contracts_to_close),
            source="manual_assignment",
            fields=fields,
        )
    selector = LotCloseSelector.from_values(
        broker=broker,
        account=account,
        symbol=symbol,
        option_type=option_type,
        position_side=position_side,
        strike=strike,
        expiration_ymd=expiration_ymd,
        contracts_to_close=int(contracts_to_close),
    )
    return resolve_fifo_close_targets(repo, selector, source="manual_assignment")


def _manual_lifecycle_request_intent_hash(
    event_type: str,
    *,
    record_id: str | None,
    broker: str,
    account: str | None,
    symbol: str | None,
    option_type: str | None,
    position_side: str | None,
    strike: float | None,
    expiration_ymd: str | None,
    contracts_to_close: int,
    stock_side: str,
    stock_qty: int,
    stock_price: float,
) -> str:
    payload = {
        "event_type": str(event_type or "").strip().lower(),
        "record_id": str(record_id or "").strip() or None,
        "broker": str(broker or "").strip().lower(),
        "account": str(account or "").strip().lower() or None,
        "symbol": str(symbol or "").strip().upper() or None,
        "option_type": str(option_type or "").strip().lower() or None,
        "position_side": str(position_side or "").strip().lower() or None,
        "strike": float(strike) if strike is not None else None,
        "expiration_ymd": str(expiration_ymd or "").strip() or None,
        "contracts_to_close": int(contracts_to_close),
        "stock_side": str(stock_side or "").strip().lower(),
        "stock_qty": int(stock_qty),
        "stock_price": float(stock_price),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _manual_lifecycle_request_replay(
    repo: Any,
    *,
    event_type: str,
    request_id: str,
    intent_hash: str,
) -> dict[str, Any] | None:
    list_events = getattr(repo, "list_trade_events", None)
    if not callable(list_events):
        return None
    matches: list[dict[str, Any]] = []
    for event in list_events():
        if not isinstance(event, dict):
            continue
        raw = event.get("raw_payload")
        payload = raw if isinstance(raw, dict) else {}
        if str(payload.get("manual_request_id") or "").strip() == request_id:
            matches.append(event)
    if not matches:
        return None
    for event in matches:
        raw = event.get("raw_payload")
        payload = raw if isinstance(raw, dict) else {}
        if (
            str(event.get("event_type") or "").strip().lower() != event_type
            or str(payload.get("manual_request_intent_hash") or "").strip()
            != intent_hash
        ):
            raise ValueError(f"manual request conflict for request_id={request_id}")
    matches.sort(
        key=lambda item: (
            int(item.get("trade_time_ms") or 0),
            str(item.get("event_id") or ""),
        )
    )
    first_raw = matches[0].get("raw_payload")
    first_payload = first_raw if isinstance(first_raw, dict) else {}
    list_lots = getattr(repo, "list_position_lots", None)
    lots = list_lots() if callable(list_lots) else []
    lot_count = len(lots) if isinstance(lots, list) else 0
    resolution = first_payload.get("close_target_resolution")
    operations = []
    for event in matches:
        raw = event.get("raw_payload")
        payload = raw if isinstance(raw, dict) else {}
        operations.append(
            {
                "action": event_type,
                "record_id": payload.get("record_id") or payload.get("target_lot_id"),
                "contracts_to_close": int(event.get("contracts") or 0),
                "event_id": event.get("event_id"),
                "result": {
                    "event_id": event.get("event_id"),
                    "record_id": payload.get("record_id") or payload.get("target_lot_id"),
                    "created": False,
                    "position_lot_count": lot_count,
                },
                "close_target_resolution": resolution,
                "details": {
                    "stock_settlement": dict(
                        payload.get("stock_settlement")
                        if isinstance(payload.get("stock_settlement"), dict)
                        else {}
                    )
                },
            }
        )
    return {
        "mode": "applied",
        "event_type": event_type,
        "manual_request_id": request_id,
        "manual_request_intent_hash": intent_hash,
        "stock_settlement": dict(
            first_payload.get("stock_settlement")
            if isinstance(first_payload.get("stock_settlement"), dict)
            else {}
        ),
        "close_target_resolution": dict(resolution) if isinstance(resolution, dict) else {},
        "operations": operations,
        "result": operations[0]["result"],
        "idempotent_duplicate": True,
    }


def _validate_lifecycle_stock_settlement(
    repo: Any,
    *,
    resolution: CloseTargetResolution,
    stock_side: str,
    stock_qty: int,
    stock_price: float,
    lifecycle_type: str,
) -> dict[str, Any]:
    normalized_lifecycle_type = str(lifecycle_type or "").strip().lower()
    if normalized_lifecycle_type not in {"assignment", "exercise"}:
        raise ValueError("lifecycle stock settlement type must be assignment or exercise")
    normalized_stock_side = normalize_trade_side(stock_side)
    if normalized_stock_side not in {"buy", "sell"}:
        raise ValueError(f"manual {normalized_lifecycle_type} stock side must be buy or sell")
    expected_qty = 0
    expected_stock_side = ""
    strike: float | None = None
    for match in resolution.matches:
        fields = _current_record_fields(repo, record_id=match.record_id)
        option_type = str(fields.get("option_type") or "").strip().lower()
        position_side = str(fields.get("side") or "").strip().lower()
        expected_position_side = "short" if normalized_lifecycle_type == "assignment" else "long"
        if position_side != expected_position_side:
            raise ValueError(f"manual {normalized_lifecycle_type} currently supports {expected_position_side} option lots only")
        if normalized_lifecycle_type == "assignment":
            expected = "buy" if option_type == "put" else "sell" if option_type == "call" else ""
        else:
            expected = "buy" if option_type == "call" else "sell" if option_type == "put" else ""
        if not expected:
            raise ValueError(f"manual {normalized_lifecycle_type} requires put/call option_type")
        if expected_stock_side and expected_stock_side != expected:
            raise ValueError(f"manual {normalized_lifecycle_type} target lots require one settlement stock side")
        expected_stock_side = expected
        multiplier = effective_multiplier(fields) or 100
        expected_qty += int(match.contracts_to_close) * int(multiplier)
        current_strike = float(fields.get("strike")) if fields.get("strike") is not None else None
        if strike is None:
            strike = current_strike
        elif current_strike is not None and abs(float(strike) - float(current_strike)) > 1e-9:
            raise ValueError(f"manual {normalized_lifecycle_type} target lots require one strike")
    if normalized_stock_side != expected_stock_side:
        raise ValueError(f"manual {normalized_lifecycle_type} stock side must be {expected_stock_side} for target lots")
    if int(stock_qty) != int(expected_qty):
        raise ValueError(f"manual {normalized_lifecycle_type} stock qty must equal settled shares: expected {expected_qty}")
    if strike is not None:
        tolerance = max(0.01, abs(float(strike)) * 0.001)
        if abs(float(stock_price) - float(strike)) > tolerance:
            raise ValueError(f"manual {normalized_lifecycle_type} stock price must be close to strike={strike}")
    return {
        "side": normalized_stock_side,
        "shares": int(stock_qty),
        "price": float(stock_price),
        "expected_shares": int(expected_qty),
        "expected_side": expected_stock_side,
        "strike": strike,
    }


def _validate_assignment_stock_settlement(
    repo: Any,
    *,
    resolution: CloseTargetResolution,
    stock_side: str,
    stock_qty: int,
    stock_price: float,
) -> dict[str, Any]:
    return _validate_lifecycle_stock_settlement(
        repo,
        resolution=resolution,
        stock_side=stock_side,
        stock_qty=stock_qty,
        stock_price=stock_price,
        lifecycle_type="assignment",
    )


def _validate_exercise_stock_settlement(
    repo: Any,
    *,
    resolution: CloseTargetResolution,
    stock_side: str,
    stock_qty: int,
    stock_price: float,
) -> dict[str, Any]:
    return _validate_lifecycle_stock_settlement(
        repo,
        resolution=resolution,
        stock_side=stock_side,
        stock_qty=stock_qty,
        stock_price=stock_price,
        lifecycle_type="exercise",
    )


def preview_manual_assignment(
    repo: Any,
    *,
    record_id: str | None,
    broker: str = "富途",
    account: str | None = None,
    symbol: str | None = None,
    option_type: str | None = None,
    position_side: str | None = "short",
    strike: float | None = None,
    expiration_ymd: str | None = None,
    contracts_to_close: int,
    stock_side: str,
    stock_qty: int,
    stock_price: float,
    as_of_ms: int | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    resolution = _manual_assignment_target_resolution(
        repo,
        record_id=record_id,
        broker=broker,
        account=account,
        symbol=symbol,
        option_type=option_type,
        position_side=position_side,
        strike=strike,
        expiration_ymd=expiration_ymd,
        contracts_to_close=int(contracts_to_close),
    )
    stock_settlement = _validate_assignment_stock_settlement(
        repo,
        resolution=resolution,
        stock_side=stock_side,
        stock_qty=int(stock_qty),
        stock_price=float(stock_price),
    )
    operations: list[dict[str, Any]] = []
    for match in resolution.matches:
        fields = _current_record_fields(repo, record_id=match.record_id)
        ledger_preflight = preflight_broker_trade_close(
            repo,
            record_id=match.record_id,
            fields=fields,
            contracts_to_close=int(match.contracts_to_close),
            close_price=0.0,
            as_of_ms=as_of_ms,
            event_type="assignment",
        )
        operations.append(
            BrokerTradeOperation(
                action="assignment",
                record_id=match.record_id,
                contracts_to_close=int(match.contracts_to_close),
                matched_by=match.matched_by,
                ledger_preflight=ledger_preflight,
                close_target_resolution=resolution.to_dict(),
                details={"stock_settlement": stock_settlement},
            ).to_payload()
        )
    request_id_value = str(request_id or "").strip()
    intent_hash = (
        _manual_lifecycle_request_intent_hash(
            "assignment",
            record_id=record_id,
            broker=broker,
            account=account,
            symbol=symbol,
            option_type=option_type,
            position_side=position_side,
            strike=strike,
            expiration_ymd=expiration_ymd,
            contracts_to_close=contracts_to_close,
            stock_side=stock_side,
            stock_qty=stock_qty,
            stock_price=stock_price,
        )
        if request_id_value
        else None
    )
    return {
        "mode": "dry_run",
        "event_type": "assignment",
        "manual_request_id": request_id_value or None,
        "manual_request_intent_hash": intent_hash,
        "stock_settlement": stock_settlement,
        "close_target_resolution": resolution.to_dict(),
        "operations": operations,
    }


def record_manual_assignment(
    repo: Any,
    *,
    record_id: str | None,
    broker: str = "富途",
    account: str | None = None,
    symbol: str | None = None,
    option_type: str | None = None,
    position_side: str | None = "short",
    strike: float | None = None,
    expiration_ymd: str | None = None,
    contracts_to_close: int,
    stock_side: str,
    stock_qty: int,
    stock_price: float,
    as_of_ms: int | None = None,
    request_id: str | None = None,
    wheel_start_enabled: bool = False,
) -> dict[str, Any]:
    request_id_value = str(request_id or "").strip()
    intent_hash = (
        _manual_lifecycle_request_intent_hash(
            "assignment",
            record_id=record_id,
            broker=broker,
            account=account,
            symbol=symbol,
            option_type=option_type,
            position_side=position_side,
            strike=strike,
            expiration_ymd=expiration_ymd,
            contracts_to_close=contracts_to_close,
            stock_side=stock_side,
            stock_qty=stock_qty,
            stock_price=stock_price,
        )
        if request_id_value
        else None
    )
    if request_id_value and intent_hash:
        replay = _manual_lifecycle_request_replay(
            repo,
            event_type="assignment",
            request_id=request_id_value,
            intent_hash=intent_hash,
        )
        if replay is not None:
            return replay
    preview = preview_manual_assignment(
        repo,
        record_id=record_id,
        broker=broker,
        account=account,
        symbol=symbol,
        option_type=option_type,
        position_side=position_side,
        strike=strike,
        expiration_ymd=expiration_ymd,
        contracts_to_close=contracts_to_close,
        stock_side=stock_side,
        stock_qty=stock_qty,
        stock_price=stock_price,
        as_of_ms=as_of_ms,
        request_id=request_id_value or None,
    )
    resolution = _manual_assignment_target_resolution(
        repo,
        record_id=record_id,
        broker=broker,
        account=account,
        symbol=symbol,
        option_type=option_type,
        position_side=position_side,
        strike=strike,
        expiration_ymd=expiration_ymd,
        contracts_to_close=int(contracts_to_close),
    )
    writes = persist_assignment_events(
        repo,
        close_target_resolution=resolution,
        contracts_to_close=int(contracts_to_close),
        event_time_ms=as_of_ms,
        case_id=None,
        evidence_ids=[],
        stock_settlement=preview["stock_settlement"],
        source="manual_assignment",
        manual_request_id=request_id_value or None,
        manual_request_intent_hash=intent_hash,
        wheel_start_enabled=wheel_start_enabled,
    )
    operations = [write.operation.to_payload() for write in writes]
    return {
        **preview,
        "mode": "applied",
        "operations": operations,
        "result": operations[0].get("result") if operations else {},
    }


def _manual_exercise_target_resolution(
    repo: Any,
    *,
    record_id: str | None,
    broker: str,
    account: str | None,
    symbol: str | None,
    option_type: str | None,
    position_side: str | None,
    strike: float | None,
    expiration_ymd: str | None,
    contracts_to_close: int,
) -> CloseTargetResolution:
    resolved_record_id = str(record_id or "").strip()
    if resolved_record_id:
        fields = _current_record_fields(repo, record_id=resolved_record_id)
        return resolve_explicit_close_target(
            repo,
            record_id=resolved_record_id,
            contracts_to_close=int(contracts_to_close),
            source="manual_exercise",
            fields=fields,
        )
    selector = LotCloseSelector.from_values(
        broker=broker,
        account=account,
        symbol=symbol,
        option_type=option_type,
        position_side=position_side,
        strike=strike,
        expiration_ymd=expiration_ymd,
        contracts_to_close=int(contracts_to_close),
    )
    return resolve_fifo_close_targets(repo, selector, source="manual_exercise")


def preview_manual_exercise(
    repo: Any,
    *,
    record_id: str | None,
    broker: str = "富途",
    account: str | None = None,
    symbol: str | None = None,
    option_type: str | None = None,
    position_side: str | None = "long",
    strike: float | None = None,
    expiration_ymd: str | None = None,
    contracts_to_close: int,
    stock_side: str,
    stock_qty: int,
    stock_price: float,
    as_of_ms: int | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    resolution = _manual_exercise_target_resolution(
        repo,
        record_id=record_id,
        broker=broker,
        account=account,
        symbol=symbol,
        option_type=option_type,
        position_side=position_side,
        strike=strike,
        expiration_ymd=expiration_ymd,
        contracts_to_close=int(contracts_to_close),
    )
    stock_settlement = _validate_exercise_stock_settlement(
        repo,
        resolution=resolution,
        stock_side=stock_side,
        stock_qty=int(stock_qty),
        stock_price=float(stock_price),
    )
    operations: list[dict[str, Any]] = []
    for match in resolution.matches:
        fields = _current_record_fields(repo, record_id=match.record_id)
        ledger_preflight = preflight_broker_trade_close(
            repo,
            record_id=match.record_id,
            fields=fields,
            contracts_to_close=int(match.contracts_to_close),
            close_price=0.0,
            as_of_ms=as_of_ms,
            event_type="exercise",
        )
        operations.append(
            BrokerTradeOperation(
                action="exercise",
                record_id=match.record_id,
                contracts_to_close=int(match.contracts_to_close),
                matched_by=match.matched_by,
                ledger_preflight=ledger_preflight,
                close_target_resolution=resolution.to_dict(),
                details={"stock_settlement": stock_settlement},
            ).to_payload()
        )
    request_id_value = str(request_id or "").strip()
    intent_hash = (
        _manual_lifecycle_request_intent_hash(
            "exercise",
            record_id=record_id,
            broker=broker,
            account=account,
            symbol=symbol,
            option_type=option_type,
            position_side=position_side,
            strike=strike,
            expiration_ymd=expiration_ymd,
            contracts_to_close=contracts_to_close,
            stock_side=stock_side,
            stock_qty=stock_qty,
            stock_price=stock_price,
        )
        if request_id_value
        else None
    )
    return {
        "mode": "dry_run",
        "event_type": "exercise",
        "manual_request_id": request_id_value or None,
        "manual_request_intent_hash": intent_hash,
        "stock_settlement": stock_settlement,
        "close_target_resolution": resolution.to_dict(),
        "operations": operations,
    }


def record_manual_exercise(
    repo: Any,
    *,
    record_id: str | None,
    broker: str = "富途",
    account: str | None = None,
    symbol: str | None = None,
    option_type: str | None = None,
    position_side: str | None = "long",
    strike: float | None = None,
    expiration_ymd: str | None = None,
    contracts_to_close: int,
    stock_side: str,
    stock_qty: int,
    stock_price: float,
    as_of_ms: int | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    request_id_value = str(request_id or "").strip()
    intent_hash = (
        _manual_lifecycle_request_intent_hash(
            "exercise",
            record_id=record_id,
            broker=broker,
            account=account,
            symbol=symbol,
            option_type=option_type,
            position_side=position_side,
            strike=strike,
            expiration_ymd=expiration_ymd,
            contracts_to_close=contracts_to_close,
            stock_side=stock_side,
            stock_qty=stock_qty,
            stock_price=stock_price,
        )
        if request_id_value
        else None
    )
    if request_id_value and intent_hash:
        replay = _manual_lifecycle_request_replay(
            repo,
            event_type="exercise",
            request_id=request_id_value,
            intent_hash=intent_hash,
        )
        if replay is not None:
            return replay
    preview = preview_manual_exercise(
        repo,
        record_id=record_id,
        broker=broker,
        account=account,
        symbol=symbol,
        option_type=option_type,
        position_side=position_side,
        strike=strike,
        expiration_ymd=expiration_ymd,
        contracts_to_close=contracts_to_close,
        stock_side=stock_side,
        stock_qty=stock_qty,
        stock_price=stock_price,
        as_of_ms=as_of_ms,
        request_id=request_id_value or None,
    )
    resolution = _manual_exercise_target_resolution(
        repo,
        record_id=record_id,
        broker=broker,
        account=account,
        symbol=symbol,
        option_type=option_type,
        position_side=position_side,
        strike=strike,
        expiration_ymd=expiration_ymd,
        contracts_to_close=int(contracts_to_close),
    )
    writes = persist_exercise_events(
        repo,
        close_target_resolution=resolution,
        contracts_to_close=int(contracts_to_close),
        event_time_ms=as_of_ms,
        case_id=None,
        evidence_ids=[],
        stock_settlement=preview["stock_settlement"],
        source="manual_exercise",
        manual_request_id=request_id_value or None,
        manual_request_intent_hash=intent_hash,
    )
    operations = [write.operation.to_payload() for write in writes]
    return {
        **preview,
        "mode": "applied",
        "operations": operations,
        "result": operations[0].get("result") if operations else {},
    }


def record_lifecycle_assignment(
    repo: Any,
    *,
    broker: str,
    account: str | None,
    symbol: str | None,
    option_type: str | None,
    position_side: str | None,
    strike: float | None,
    expiration_ymd: str | None,
    contracts_to_close: int,
    event_time_ms: int | None,
    case_id: str | None,
    evidence_ids: list[str] | tuple[str, ...] | None,
    stock_settlement: dict[str, Any] | None,
    wheel_start_enabled: bool = False,
) -> dict[str, Any]:
    selector = LotCloseSelector.from_values(
        broker=broker,
        account=account,
        symbol=symbol,
        option_type=option_type,
        position_side=position_side,
        strike=strike,
        expiration_ymd=expiration_ymd,
        contracts_to_close=int(contracts_to_close),
    )
    close_target_resolution = resolve_fifo_close_targets(repo, selector, source="lifecycle_assignment")
    writes = persist_assignment_events(
        repo,
        close_target_resolution=close_target_resolution,
        contracts_to_close=int(contracts_to_close),
        event_time_ms=event_time_ms,
        case_id=case_id,
        evidence_ids=evidence_ids or [],
        stock_settlement=dict(stock_settlement or {}),
        wheel_start_enabled=wheel_start_enabled,
    )
    return {
        "close_target_resolution": close_target_resolution,
        "operations": [write.operation for write in writes],
        "writes": writes,
    }


def record_lifecycle_exercise(
    repo: Any,
    *,
    broker: str,
    account: str | None,
    symbol: str | None,
    option_type: str | None,
    position_side: str | None,
    strike: float | None,
    expiration_ymd: str | None,
    contracts_to_close: int,
    event_time_ms: int | None,
    case_id: str | None,
    evidence_ids: list[str] | tuple[str, ...] | None,
    stock_settlement: dict[str, Any] | None,
) -> dict[str, Any]:
    selector = LotCloseSelector.from_values(
        broker=broker,
        account=account,
        symbol=symbol,
        option_type=option_type,
        position_side=position_side,
        strike=strike,
        expiration_ymd=expiration_ymd,
        contracts_to_close=int(contracts_to_close),
    )
    close_target_resolution = resolve_fifo_close_targets(repo, selector, source="lifecycle_exercise")
    writes = persist_exercise_events(
        repo,
        close_target_resolution=close_target_resolution,
        contracts_to_close=int(contracts_to_close),
        event_time_ms=event_time_ms,
        case_id=case_id,
        evidence_ids=evidence_ids or [],
        stock_settlement=dict(stock_settlement or {}),
    )
    return {
        "close_target_resolution": close_target_resolution,
        "operations": [write.operation for write in writes],
        "writes": writes,
    }


def preview_lifecycle_expire_close(
    repo: Any,
    *,
    broker: str,
    account: str | None,
    symbol: str | None,
    option_type: str | None,
    position_side: str | None,
    strike: float | None,
    expiration_ymd: str | None,
    contracts_to_close: int,
    event_time_ms: int | None,
) -> dict[str, Any]:
    selector = LotCloseSelector.from_values(
        broker=broker,
        account=account,
        symbol=symbol,
        option_type=option_type,
        position_side=position_side,
        strike=strike,
        expiration_ymd=expiration_ymd,
        contracts_to_close=int(contracts_to_close),
    )
    close_target_resolution = resolve_fifo_close_targets(repo, selector, source="lifecycle_expire_close")
    operations: list[dict[str, Any]] = []
    for match in close_target_resolution.matches:
        fields = _current_record_fields(repo, record_id=match.record_id)
        ledger_preflight = preflight_broker_trade_close(
            repo,
            record_id=match.record_id,
            fields=fields,
            contracts_to_close=int(match.contracts_to_close),
            close_price=0.0,
            as_of_ms=event_time_ms,
            event_type="expire_close",
        )
        operations.append(
            BrokerTradeOperation(
                action="expire_close",
                record_id=match.record_id,
                contracts_to_close=int(match.contracts_to_close),
                matched_by=match.matched_by,
                ledger_preflight=ledger_preflight,
                close_target_resolution=close_target_resolution.to_dict(),
            ).to_payload()
        )
    return {
        "mode": "dry_run",
        "event_type": "expire_close",
        "close_target_resolution": close_target_resolution.to_dict(),
        "operations": operations,
    }


def record_lifecycle_expire_close(
    repo: Any,
    *,
    broker: str,
    account: str | None,
    symbol: str | None,
    option_type: str | None,
    position_side: str | None,
    strike: float | None,
    expiration_ymd: str | None,
    contracts_to_close: int,
    event_time_ms: int | None,
    case_id: str | None,
    evidence_ids: list[str] | tuple[str, ...] | None,
    close_reason: str = "expired_unassigned",
) -> dict[str, Any]:
    selector = LotCloseSelector.from_values(
        broker=broker,
        account=account,
        symbol=symbol,
        option_type=option_type,
        position_side=position_side,
        strike=strike,
        expiration_ymd=expiration_ymd,
        contracts_to_close=int(contracts_to_close),
    )
    close_target_resolution = resolve_fifo_close_targets(repo, selector, source="lifecycle_expire_close")
    writes = persist_expire_close_events(
        repo,
        close_target_resolution=close_target_resolution,
        contracts_to_close=int(contracts_to_close),
        event_time_ms=event_time_ms,
        case_id=case_id,
        evidence_ids=evidence_ids or [],
        close_reason=close_reason,
    )
    return {
        "close_target_resolution": close_target_resolution,
        "operations": [write.operation for write in writes],
        "writes": writes,
    }


def preview_manual_position_close(
    repo: Any,
    *,
    record_id: str,
    contracts_to_close: int,
    close_price: float | None,
    close_reason: str,
    as_of_ms: int | None = None,
) -> ManualClosePreviewResult:
    close_target_resolution = resolve_explicit_close_target(
        repo,
        record_id=record_id,
        contracts_to_close=int(contracts_to_close),
        source="manual_close_explicit",
    )
    fields = close_target_resolution.single_candidate.raw_fields
    ledger_preflight = preflight_manual_close(
        repo,
        record_id=record_id,
        fields=fields,
        contracts_to_close=int(contracts_to_close),
        close_price=close_price,
        close_reason=close_reason,
        as_of_ms=as_of_ms,
    )
    patch_contract = build_close_patch_contract(
        fields,
        contracts_to_close=int(contracts_to_close),
        close_price=close_price,
        close_reason=close_reason,
        as_of_ms=int(ledger_preflight.event_time_ms),
    )
    return ManualClosePreviewResult(
        fields=fields,
        patch=patch_contract,
        close_target_resolution=close_target_resolution.to_dict(),
        ledger_preflight=ledger_preflight,
    )


def record_manual_position_close(
    repo: Any,
    *,
    record_id: str,
    contracts_to_close: int,
    close_price: float | None,
    close_reason: str,
    fields: dict[str, Any] | None = None,
    as_of_ms: int | None = None,
) -> ManualCloseLedgerResult:
    current_fields = repo.get_record_fields(record_id)
    from src.application.ledger.manual_trades import existing_manual_close_event_result

    duplicate_result = existing_manual_close_event_result(
        repo,
        record_id=str(record_id),
        fields=current_fields,
        contracts_to_close=int(contracts_to_close),
        close_price=close_price,
        close_reason=close_reason,
    )
    if duplicate_result is not None:
        ledger_result = persist_manual_close_event_with_ledger(
            repo,
            record_id=record_id,
            fields=current_fields,
            contracts_to_close=int(contracts_to_close),
            close_price=close_price,
            close_reason=close_reason,
            as_of_ms=as_of_ms,
        )
        return ledger_result.with_close_target_resolution(
            _duplicate_close_target_resolution_payload(
                record_id=record_id,
                fields=current_fields,
                contracts_to_close=int(contracts_to_close),
            )
        )

    close_target_resolution = resolve_explicit_close_target(
        repo,
        record_id=record_id,
        contracts_to_close=int(contracts_to_close),
        source="manual_close_explicit",
        fields=fields,
    )
    return persist_manual_close_event_with_ledger(
        repo,
        record_id=close_target_resolution.single_match.record_id,
        fields=close_target_resolution.single_candidate.raw_fields,
        contracts_to_close=int(contracts_to_close),
        close_price=close_price,
        close_reason=close_reason,
        as_of_ms=as_of_ms,
    ).with_close_target_resolution(close_target_resolution.to_dict())


def _duplicate_close_target_resolution_payload(
    *,
    record_id: str,
    fields: dict[str, Any],
    contracts_to_close: int,
) -> dict[str, Any]:
    selector = LotCloseSelector.from_values(
        broker=fields.get("broker"),
        account=fields.get("account"),
        symbol=fields.get("symbol"),
        option_type=fields.get("option_type"),
        position_side=fields.get("side"),
        strike=fields.get("strike"),
        expiration_ymd=fields.get("expiration_ymd") or fields.get("expiration"),
        contracts_to_close=contracts_to_close,
    ).to_dict()
    return {
        "status": "duplicate",
        "source": "manual_close_explicit",
        "strategy": "duplicate_existing_close_event",
        "selector": selector,
        "target_count": 1,
        "record_ids": [str(record_id)],
        "contracts_to_close": int(contracts_to_close),
        "targets": [],
    }


def preview_manual_position_adjust(
    repo: Any,
    *,
    record_id: str,
    contracts: int | None,
    strike: float | None,
    expiration_ymd: str | None,
    premium_per_share: float | None,
    multiplier: float | None,
    opened_at_ms: int | None,
    strategy: str | None = None,
    leg_role: str | None = None,
    strategy_group_id: str | None = None,
    strategy_snapshot: dict[str, Any] | None = None,
) -> ManualAdjustPreviewResult:
    fields = repo.get_record_fields(record_id)
    ledger_preflight = preflight_manual_adjust(
        repo,
        record_id=record_id,
        fields=fields,
        contracts=contracts,
        strike=strike,
        expiration_ymd=expiration_ymd,
        premium_per_share=premium_per_share,
        multiplier=multiplier,
        opened_at_ms=opened_at_ms,
        strategy=strategy,
        leg_role=leg_role,
        strategy_group_id=strategy_group_id,
        strategy_snapshot=strategy_snapshot,
    )
    patch_contract = build_open_adjustment_patch_contract(
        fields,
        contracts=contracts,
        strike=strike,
        expiration_ymd=expiration_ymd,
        premium_per_share=premium_per_share,
        multiplier=multiplier,
        opened_at_ms=opened_at_ms,
        strategy=strategy,
        leg_role=leg_role,
        strategy_group_id=strategy_group_id,
        strategy_snapshot=strategy_snapshot,
        as_of_ms=int(ledger_preflight.event_time_ms),
    )
    return ManualAdjustPreviewResult(
        fields=fields,
        patch=patch_contract,
        ledger_preflight=ledger_preflight,
    )


def record_manual_position_adjust(
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
    strategy: str | None = None,
    leg_role: str | None = None,
    strategy_group_id: str | None = None,
    strategy_snapshot: dict[str, Any] | None = None,
) -> ManualAdjustLedgerResult:
    return persist_manual_adjust_event_with_ledger(
        repo,
        record_id=record_id,
        fields=fields,
        contracts=contracts,
        strike=strike,
        expiration_ymd=expiration_ymd,
        premium_per_share=premium_per_share,
        multiplier=multiplier,
        opened_at_ms=opened_at_ms,
        strategy=strategy,
        leg_role=leg_role,
        strategy_group_id=strategy_group_id,
        strategy_snapshot=strategy_snapshot,
    )


def record_broker_trade_open(repo: Any, deal: Any, *, persist_trade_event_fn: Any) -> BrokerTradeOperation:
    if supports_ledger_open_preflight(repo):
        open_result = persist_trade_open_event_with_ledger(
            repo,
            deal=deal,
            persist_trade_event_fn=persist_trade_event_fn,
        )
        result = open_result.result.to_dict()
        return BrokerTradeOperation(
            action="open",
            event_id=result.get("event_id") or getattr(deal, "deal_id", None),
            result=open_result.result,
            fields=open_result.fields,
            ledger_preflight=open_result.ledger_preflight,
        )
    result = persist_trade_event_fn(repo, deal)
    result_payload = LedgerWriteResult.from_payload(result).to_dict() if isinstance(result, (dict, LedgerWriteResult)) else {}
    event_id = result_payload.get("event_id")
    return BrokerTradeOperation(
        action="open",
        event_id=event_id or getattr(deal, "deal_id", None),
        result=result_payload,
    )


def _broker_trade_open_command(deal: Any) -> OpenPositionCommand:
    side = str(getattr(deal, "side", "") or "").strip().lower()
    raw_price = getattr(deal, "price", None)
    return OpenPositionCommand(
        broker="富途",
        account=str(getattr(deal, "internal_account", "") or ""),
        symbol=str(getattr(deal, "symbol", "") or ""),
        option_type=str(getattr(deal, "option_type", "") or ""),
        side="short" if side == "sell" else "long",
        contracts=int(getattr(deal, "contracts", 0) or 0),
        currency=str(getattr(deal, "currency", "") or ""),
        strike=(float(getattr(deal, "strike")) if getattr(deal, "strike", None) is not None else None),
        multiplier=float(getattr(deal, "multiplier")) if getattr(deal, "multiplier", None) is not None else None,
        expiration_ymd=(str(getattr(deal, "expiration_ymd", "") or "").strip() or None),
        premium_per_share=float(raw_price) if raw_price not in (None, "") else None,
        note=(
            f"source=opend_push "
            f"deal_id={getattr(deal, 'deal_id', None)} "
            f"order_id={getattr(deal, 'order_id', None) or ''} "
            f"multiplier_source={getattr(deal, 'multiplier_source', None) or ''} "
            f"trade_time_ms={getattr(deal, 'trade_time_ms', None) or ''}"
        ).strip(),
        opened_at_ms=getattr(deal, "trade_time_ms", None),
    )


def preview_broker_trade_open(deal: Any) -> BrokerTradeOpenPreviewResult:
    command = _broker_trade_open_command(deal)
    fields = build_position_lot_fields(command).to_dict()
    fields.update(strategy_metadata_fields_from_payload(getattr(deal, "raw_payload", None)))
    return BrokerTradeOpenPreviewResult(command=command, fields=fields)


def record_normalized_trade_event(repo: Any, deal: Any) -> LedgerWriteResult:
    return persist_trade_event(repo, deal)


def preview_broker_trade_close(
    repo: Any,
    *,
    matches: list[Any],
    deal: Any,
    close_target_resolution: CloseTargetResolution | None = None,
) -> list[BrokerTradeOperation]:
    operations: list[BrokerTradeOperation] = []
    close_action = "buy_close" if str(getattr(deal, "side", "") or "").strip().lower() == "buy" else "sell_close"
    broker_close_event_type = _broker_close_event_type(deal)
    broker_close_type = EXPIRE_AUTO_CLOSE if broker_close_event_type == "expire_close" else None
    close_reason = (
        "broker_expiration_zero_price_close"
        if broker_close_event_type == "expire_close"
        else "auto_trade_buy_to_close"
        if close_action == "buy_close"
        else "auto_trade_sell_to_close"
    )
    for match in matches:
        fields = repo.get_record_fields(match.record_id)
        operation = BrokerTradeOperation(
            action=close_action,
            record_id=match.record_id,
            contracts_to_close=match.contracts_to_close,
            patch=build_close_patch_contract(
                fields,
                contracts_to_close=match.contracts_to_close,
                close_price=(float(getattr(deal, "price")) if getattr(deal, "price", None) is not None else None),
                close_reason=close_reason,
                close_type=broker_close_type,
                as_of_ms=getattr(deal, "trade_time_ms", None),
            ),
            matched_by=match.matched_by,
            close_target_resolution=(
                close_target_resolution.to_dict() if close_target_resolution is not None else None
            ),
        )
        operations.append(operation)
    return operations


def record_broker_trade_close(
    repo: Any,
    *,
    matches: list[Any],
    deal: Any,
    persist_trade_event_fn: Any,
    close_target_resolution: CloseTargetResolution | None = None,
) -> list[BrokerTradeOperation]:
    if supports_ledger_close_preflight(repo):
        return persist_trade_close_events_with_ledger(
            repo,
            matches=matches,
            deal=deal,
            persist_trade_event_fn=persist_trade_event_fn,
            close_target_resolution=close_target_resolution.to_dict() if close_target_resolution is not None else None,
        )
    persist_trade_event_fn(repo, deal)
    close_action = "buy_close" if str(getattr(deal, "side", "") or "").strip().lower() == "buy" else "sell_close"
    operations = [
        BrokerTradeOperation(
            action=close_action,
            record_id=match.record_id,
            contracts_to_close=match.contracts_to_close,
            close_target_resolution=(
                close_target_resolution.to_dict() if close_target_resolution is not None else None
            ),
        )
        for match in matches
    ]
    return operations


def resolve_broker_trade_close_lots(repo: Any, *, deal: Any) -> list[LotCloseMatch]:
    deal_side = str(getattr(deal, "side", "") or "").strip().lower()
    target_position_side = "short" if deal_side == "buy" else "long"
    selector = LotCloseSelector.from_values(
        broker="富途",
        account=getattr(deal, "internal_account", None),
        symbol=getattr(deal, "symbol", None),
        option_type=getattr(deal, "option_type", None),
        position_side=target_position_side,
        strike=getattr(deal, "strike", None),
        expiration_ymd=getattr(deal, "expiration_ymd", None),
        contracts_to_close=getattr(deal, "contracts", None),
    )
    return resolve_fifo_close_lots(repo, selector)


def resolve_broker_trade_close_targets(repo: Any, *, deal: Any) -> CloseTargetResolution:
    deal_side = str(getattr(deal, "side", "") or "").strip().lower()
    target_position_side = "short" if deal_side == "buy" else "long"
    selector = LotCloseSelector.from_values(
        broker="富途",
        account=getattr(deal, "internal_account", None),
        symbol=getattr(deal, "symbol", None),
        option_type=getattr(deal, "option_type", None),
        position_side=target_position_side,
        strike=getattr(deal, "strike", None),
        expiration_ymd=getattr(deal, "expiration_ymd", None),
        contracts_to_close=getattr(deal, "contracts", None),
    )
    return resolve_fifo_close_targets(repo, selector, source="broker_trade_close")


def summarize_broker_trade_close_candidates(repo: Any, *, deal: Any) -> dict[str, Any]:
    deal_side = str(getattr(deal, "side", "") or "").strip().lower()
    target_position_side = "short" if deal_side == "buy" else "long"
    selector = LotCloseSelector.from_values(
        broker=getattr(deal, "broker", None) or "富途",
        account=getattr(deal, "internal_account", None),
        symbol=getattr(deal, "symbol", None),
        option_type=getattr(deal, "option_type", None),
        position_side=target_position_side,
        strike=getattr(deal, "strike", None),
        expiration_ymd=getattr(deal, "expiration_ymd", None),
        contracts_to_close=getattr(deal, "contracts", None) or 0,
    )
    return summarize_close_candidates(repo, selector)


def find_unique_open_position_lot(
    repo: Any,
    *,
    broker: Any = None,
    account: Any,
    symbol: Any,
    option_type: Any,
    side: Any,
    expiration_ymd: Any = None,
) -> dict[str, Any] | None:
    candidate = find_unique_open_lot(
        repo,
        broker=broker,
        account=account,
        symbol=symbol,
        option_type=option_type,
        side=side,
        expiration_ymd=expiration_ymd,
    )
    if candidate is None:
        return None
    return {
        "record_id": candidate.record_id,
        "contracts_open": int(candidate.contracts_open or 0),
        "strike": candidate.strike,
        "expiration_ymd": candidate.expiration_ymd,
        "strategy": str(candidate.raw_fields.get("strategy") or "").strip() or None,
        "leg_role": str(candidate.raw_fields.get("leg_role") or "").strip() or None,
        "strategy_group_id": str(candidate.raw_fields.get("strategy_group_id") or "").strip() or None,
    }


def list_close_lot_candidates(repo: Any) -> list[dict[str, Any]]:
    return load_close_candidate_records(repo)


def list_expiry_close_position_lots(repo: Any) -> list[dict[str, Any]]:
    read_repo = require_option_positions_read_repo(repo)
    rows = read_repo.list_position_lots()
    return rows if isinstance(rows, list) else []


def plan_expired_position_closes(
    positions: list[dict[str, Any]],
    *,
    as_of_ms: int,
    grace_days: int,
) -> list[ExpiredCloseDecision]:
    return build_expired_close_decisions(positions, as_of_ms=as_of_ms, grace_days=grace_days)


def record_expired_position_closes(
    repo: Any,
    positions: list[dict[str, Any]],
    *,
    as_of_ms: int,
    grace_days: int,
    max_close: int,
    projection_refresh: ProjectionRefreshResult | None = _PROJECTION_REFRESH_NOT_PROVIDED,
) -> ExpiredCloseRunResult:
    return auto_close_expired_positions(
        repo,
        positions,
        as_of_ms=as_of_ms,
        grace_days=grace_days,
        max_close=max_close,
        projection_refresh=projection_refresh,
    )


def refresh_position_lot_projection(repo: Any) -> ProjectionRefreshResult:
    return rebuild_position_lots_from_trade_events(getattr(repo, "primary_repo", repo))


def record_combo_trade_open(
    repo: Any,
    *,
    event: Any,
    combo_identity_intent: dict[str, Any],
) -> dict[str, Any]:
    return persist_trade_event_with_combo_identity(
        repo,
        event,
        combo_identity_intent=combo_identity_intent,
    )


def adopt_existing_combo_identity(
    repo: Any,
    *,
    strategy_group_id: str,
    funding_put_record_id: str,
    funding_put_open_event_id: str,
    participation_call_record_id: str,
    participation_call_open_event_id: str,
    expected_contracts: int,
    apply_changes: bool = False,
) -> dict[str, Any]:
    return adopt_existing_combo_identity_atomically(
        repo,
        group_id=strategy_group_id,
        funding_put_record_id=funding_put_record_id,
        funding_put_open_event_id=funding_put_open_event_id,
        participation_call_record_id=participation_call_record_id,
        participation_call_open_event_id=(participation_call_open_event_id),
        expected_contracts=expected_contracts,
        apply_changes=apply_changes,
    )


def record_lifecycle_allocation(
    repo: Any,
    *,
    case_id: str,
    evidence: dict[str, Any],
    terminal_events: list[Any],
    allocations: list[dict[str, Any]],
    derived_status: str,
    derived_summary: dict[str, Any],
    expected_resolution_revision: int | None = None,
    expected_lifecycle_generation_token: str | None = None,
    correction_void_events: list[Any] | None = None,
    notification_transition_type: str | None = None,
    attempt_evidence: dict[str, Any] | None = None,
    attempt_audit: LifecycleAttemptAuditEnvelope | None = None,
    wheel_start_enabled: bool = False,
) -> dict[str, Any]:
    return apply_lifecycle_allocation_atomically(
        repo,
        case_id=case_id,
        evidence=evidence,
        terminal_events=terminal_events,
        allocations=allocations,
        derived_status=derived_status,
        derived_summary=derived_summary,
        expected_resolution_revision=expected_resolution_revision,
        expected_lifecycle_generation_token=(
            expected_lifecycle_generation_token
        ),
        correction_void_events=list(
            correction_void_events or []
        ),
        notification_transition_type=notification_transition_type,
        attempt_evidence=attempt_evidence,
        attempt_audit=attempt_audit,
        wheel_start_enabled=wheel_start_enabled,
    )


def record_assigned_stock_event(
    repo: Any,
    *,
    sale_event: dict[str, Any],
    assigned_stock_after: dict[str, Any],
) -> dict[str, Any]:
    return record_assigned_stock_event_atomically(
        repo,
        sale_event=sale_event,
        assigned_stock_after=assigned_stock_after,
    )


def accept_option_close_evidence(
    repo: Any,
    *,
    contract_identity: dict[str, Any],
    evidence: dict[str, Any],
    apply_changes: bool = True,
) -> dict[str, Any]:
    return accept_option_close_evidence_atomically(
        repo,
        contract_identity=contract_identity,
        evidence=evidence,
        apply_changes=apply_changes,
    )


def discover_expired_lifecycle_cases(
    repo: Any,
    *,
    account: str | None = None,
    observed_at_ms: int | None = None,
    apply_changes: bool = True,
) -> dict[str, Any]:
    return discover_expired_lifecycle_cases_atomically(
        repo,
        account=account,
        observed_at_ms=observed_at_ms,
        apply_changes=apply_changes,
    )


def record_lifecycle_timing_policy(
    repo: Any,
    *,
    case_id: str,
    policy: dict[str, Any],
    apply_changes: bool,
) -> dict[str, Any]:
    return bind_lifecycle_timing_policy_atomically(
        repo,
        case_id=case_id,
        policy=policy,
        apply_changes=apply_changes,
    )


def record_lifecycle_evidence_issue(
    repo: Any,
    *,
    case_id: str,
    evidence: dict[str, Any],
    status: str,
    reason_codes: list[str],
    expected_lifecycle_generation_token: str | None = None,
    attempt_evidence: dict[str, Any] | None = None,
    attempt_audit: LifecycleAttemptAuditEnvelope | None = None,
) -> dict[str, Any]:
    return record_lifecycle_evidence_issue_atomically(
        repo,
        case_id=case_id,
        evidence=evidence,
        status=status,
        reason_codes=reason_codes,
        expected_lifecycle_generation_token=(
            expected_lifecycle_generation_token
        ),
        attempt_evidence=attempt_evidence,
        attempt_audit=attempt_audit,
    )


def advance_lifecycle_case_state(
    repo: Any,
    *,
    case_id: str,
    status: str,
    derived_summary: dict[str, Any],
    public_transition: str | None,
    expected_lifecycle_generation_token: str | None = None,
    evidence: dict[str, Any] | None = None,
    attempt_audit: LifecycleAttemptAuditEnvelope | None = None,
) -> dict[str, Any]:
    return advance_lifecycle_case_state_atomically(
        repo,
        case_id=case_id,
        status=status,
        derived_summary=derived_summary,
        public_transition=public_transition,
        expected_lifecycle_generation_token=(
            expected_lifecycle_generation_token
        ),
        evidence=evidence,
        attempt_audit=attempt_audit,
    )


def preview_trade_event_void(repo: Any, *, event_id: str, reason: str) -> dict[str, Any]:
    from src.application.ledger.queries import trade_event_log, trade_event_projection_preview

    preview = build_manual_void_preview(repo, target_event_id=event_id, void_reason=reason)
    events = trade_event_log(repo) + [preview.void_event]
    ledger_preflight = preflight_manual_void(repo, target_event_id=event_id, void_reason=reason)
    preview_payload = preview.to_payload()
    return {
        "mode": "dry_run",
        "target_event_id": str(event_id),
        "void_reason": str(reason or ""),
        **preview_payload,
        "ledger_preflight": ledger_preflight.to_dict(),
        "projection_preview": trade_event_projection_preview(events),
    }


def record_trade_event_void(repo: Any, *, event_id: str, reason: str) -> dict[str, Any]:
    ledger_result = persist_manual_void_event_with_ledger(repo, target_event_id=event_id, void_reason=reason)
    return ledger_result.result.to_dict() | {
        "mode": "applied",
        "ledger_preflight": ledger_result.ledger_preflight.to_dict(),
    }


def preview_trade_event_repair(
    repo: Any,
    *,
    event_id: str,
    overrides: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    from src.application.ledger.queries import trade_event_log, trade_event_projection_preview

    preview = build_manual_repair_preview(
        repo,
        target_event_id=event_id,
        overrides=overrides,
        repair_reason=reason,
    )
    events = trade_event_log(repo) + [preview.void_event, preview.repair_event]
    ledger_preflight = preflight_manual_repair(
        repo,
        target_event_id=event_id,
        overrides=overrides,
        repair_reason=reason,
    )
    preview_payload = preview.to_payload()
    return {
        "mode": "dry_run",
        **preview_payload,
        "ledger_preflight": ledger_preflight.to_dict(),
        "projection_preview": trade_event_projection_preview(events),
    }


def record_trade_event_repair(
    repo: Any,
    *,
    event_id: str,
    overrides: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    ledger_result = persist_manual_repair_event_with_ledger(
        repo,
        target_event_id=event_id,
        overrides=overrides,
        repair_reason=reason,
    )
    return ledger_result.result.to_dict() | {
        "mode": "applied",
        "ledger_preflight": ledger_result.ledger_preflight.to_dict(),
    }


def verify_position_lot_projection(
    *,
    base: Any,
    repo: Any,
    mode: str = "auto",
    publish_evidence: bool = False,
) -> dict[str, Any]:
    from src.application.ledger.projection_verify import verify_position_projection

    return verify_position_projection(
        base=base,
        repo=repo,
        mode=mode,
        publish_evidence=publish_evidence,
    )


__all__ = [
    "adopt_existing_combo_identity",
    "BrokerTradeOpenPreviewResult",
    "BrokerTradeOperation",
    "ExpiredCloseDecision",
    "ExpiredCloseRunResult",
    "LotCloseCandidate",
    "LotCloseMatch",
    "LotCloseResolutionError",
    "CloseTargetResolution",
    "find_unique_open_position_lot",
    "list_close_lot_candidates",
    "list_expiry_close_position_lots",
    "plan_expired_position_closes",
    "preview_broker_trade_close",
    "preview_broker_trade_open",
    "preview_lifecycle_expire_close",
    "preview_manual_exercise",
    "preview_manual_assignment",
    "preview_manual_position_adjust",
    "preview_manual_position_close",
    "preview_manual_position_open",
    "preview_trade_event_repair",
    "preview_trade_event_void",
    "record_broker_trade_close",
    "record_broker_trade_open",
    "record_assigned_stock_event",
    "record_combo_trade_open",
    "record_expired_position_closes",
    "record_lifecycle_assignment",
    "record_lifecycle_exercise",
    "record_lifecycle_expire_close",
    "record_lifecycle_allocation",
    "accept_option_close_evidence",
    "discover_expired_lifecycle_cases",
    "record_lifecycle_evidence_issue",
    "record_lifecycle_timing_policy",
    "record_manual_exercise",
    "record_manual_assignment",
    "record_manual_position_adjust",
    "record_manual_position_close",
    "record_manual_position_open",
    "record_normalized_trade_event",
    "record_trade_event_repair",
    "record_trade_event_void",
    "refresh_position_lot_projection",
    "resolve_broker_trade_close_lots",
    "resolve_broker_trade_close_targets",
    "resolve_manual_position_close_lot",
    "resolve_manual_position_close_target",
    "summarize_broker_trade_close_candidates",
    "verify_position_lot_projection",
]
