from __future__ import annotations

from dataclasses import replace
from typing import Any

from domain.domain.ledger.position_fields import (
    OpenPositionCommand,
    build_close_patch_contract,
    build_open_adjustment_patch_contract,
    build_position_lot_fields,
)
from src.application.ledger.interventions import (
    build_manual_repair_preview,
    build_manual_void_preview,
)
from src.application.ledger.lot_resolver import (
    CloseTargetResolution,
    LotCloseCandidate,
    LotCloseMatch,
    LotCloseResolutionError,
    LotCloseSelector,
    load_close_candidate_records,
    resolve_explicit_close_target,
    resolve_fifo_close_lots,
    resolve_fifo_close_targets,
    resolve_unique_close_lot,
    resolve_unique_close_target,
)
from src.application.ledger.maintenance import (
    auto_close_expired_positions,
    build_expired_close_decisions,
)
from src.application.ledger.preflight import (
    preflight_manual_adjust,
    preflight_manual_close,
    preflight_manual_open,
    preflight_manual_repair,
    preflight_manual_void,
)
from src.application.ledger.repository import (
    require_option_positions_read_repo,
    require_option_positions_sync_meta_repo,
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
)
from src.application.ledger.sync_metadata import PositionLotSyncMetadataPatch
from src.application.ledger.service import (
    persist_manual_adjust_event_with_ledger,
    persist_manual_close_event_with_ledger,
    persist_manual_open_event,
    persist_manual_open_event_with_ledger,
    persist_manual_repair_event_with_ledger,
    persist_manual_void_event_with_ledger,
    persist_trade_close_events_with_ledger,
    persist_trade_event,
    persist_trade_open_event_with_ledger,
    rebuild_position_lots_from_trade_events,
)


def supports_ledger_open_preflight(repo: Any) -> bool:
    candidate = getattr(repo, "primary_repo", repo)
    return callable(getattr(candidate, "list_position_lots", None))


def supports_ledger_close_preflight(repo: Any) -> bool:
    candidate = getattr(repo, "primary_repo", repo)
    return (
        callable(getattr(repo, "get_record_fields", None))
        and callable(getattr(candidate, "list_position_lots", None))
    )


def preview_manual_position_open(repo: Any | None, command: OpenPositionCommand) -> ManualOpenPreviewResult:
    fields_contract = build_position_lot_fields(command)
    fields = fields_contract.to_dict()
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
    return OpenLedgerResult(
        result=LedgerWriteResult.from_payload(result),
        fields=build_position_lot_fields(command).to_dict(),
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


def preview_manual_position_close(
    repo: Any,
    *,
    record_id: str,
    contracts_to_close: int,
    close_price: float | None,
    close_reason: str,
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
    )
    patch_contract = build_close_patch_contract(
        fields,
        contracts_to_close=int(contracts_to_close),
        close_price=close_price,
        close_reason=close_reason,
        as_of_ms=int(ledger_preflight["event_time_ms"]),
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
    )
    patch_contract = build_open_adjustment_patch_contract(
        fields,
        contracts=contracts,
        strike=strike,
        expiration_ymd=expiration_ymd,
        premium_per_share=premium_per_share,
        multiplier=multiplier,
        opened_at_ms=opened_at_ms,
        as_of_ms=int(ledger_preflight["event_time_ms"]),
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
    return BrokerTradeOpenPreviewResult(command=command, fields=build_position_lot_fields(command).to_dict())


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
    close_reason = "auto_trade_buy_to_close" if close_action == "buy_close" else "auto_trade_sell_to_close"
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
) -> ExpiredCloseRunResult:
    return auto_close_expired_positions(
        repo,
        positions,
        as_of_ms=as_of_ms,
        grace_days=grace_days,
        max_close=max_close,
    )


def refresh_position_lot_projection(repo: Any) -> ProjectionRefreshResult:
    return rebuild_position_lots_from_trade_events(getattr(repo, "primary_repo", repo))


def record_position_lot_sync_metadata(
    repo: Any,
    record_id: str,
    patch: PositionLotSyncMetadataPatch,
) -> None:
    primary_repo = require_option_positions_sync_meta_repo(repo)
    primary_repo.update_position_lot_sync_metadata(record_id, patch)


def preview_trade_event_void(repo: Any, *, event_id: str, reason: str) -> dict[str, Any]:
    from src.application.ledger.queries import trade_event_log, trade_event_projection_preview

    preview = build_manual_void_preview(repo, target_event_id=event_id, void_reason=reason)
    events = trade_event_log(repo) + [preview["void_event"]]
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
    events = trade_event_log(repo) + [preview["void_event"], preview["repair_event"]]
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


def reconcile_position_snapshot(*, base: Any, repo: Any, verification_snapshot: dict[str, Any]) -> dict[str, Any]:
    from src.application.ledger.reconciliation import reconcile_option_positions_snapshot

    return reconcile_option_positions_snapshot(
        base=base,
        repo=repo,
        verification_snapshot=verification_snapshot,
    )


__all__ = [
    "BrokerTradeOpenPreviewResult",
    "BrokerTradeOperation",
    "ExpiredCloseDecision",
    "ExpiredCloseRunResult",
    "LotCloseCandidate",
    "LotCloseMatch",
    "LotCloseResolutionError",
    "CloseTargetResolution",
    "list_close_lot_candidates",
    "list_expiry_close_position_lots",
    "plan_expired_position_closes",
    "preview_broker_trade_close",
    "preview_broker_trade_open",
    "preview_manual_position_adjust",
    "preview_manual_position_close",
    "preview_manual_position_open",
    "preview_trade_event_repair",
    "preview_trade_event_void",
    "record_broker_trade_close",
    "record_broker_trade_open",
    "record_expired_position_closes",
    "record_manual_position_adjust",
    "record_manual_position_close",
    "record_manual_position_open",
    "record_normalized_trade_event",
    "record_position_lot_sync_metadata",
    "record_trade_event_repair",
    "record_trade_event_void",
    "reconcile_position_snapshot",
    "refresh_position_lot_projection",
    "resolve_broker_trade_close_lots",
    "resolve_broker_trade_close_targets",
    "resolve_manual_position_close_lot",
    "resolve_manual_position_close_target",
]
