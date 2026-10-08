from __future__ import annotations

from typing import Any

from src.application.ledger.api import (
    BrokerTradeOpenPreviewResult,
    BrokerTradeOperation,
    lot_id_for_open_event,
    preview_broker_trade_close,
    preview_broker_trade_open,
    record_broker_trade_close,
    record_broker_trade_open,
)
from src.application.positions.workflows import (
    BrokerAssignedStockSaleMatchError as BrokerAssignedStockSaleMatchError,
    execute_broker_assigned_stock_sale as _execute_position_broker_assigned_stock_sale,
)
from src.application.trades.normalizer import NormalizedTradeDeal


def preview_trade_open(deal: NormalizedTradeDeal) -> BrokerTradeOpenPreviewResult:
    return preview_broker_trade_open(deal)


def apply_trade_open_with(repo: Any, deal: NormalizedTradeDeal, *, persist_trade_event_fn: Any) -> BrokerTradeOperation:
    return record_broker_trade_open(repo, deal, persist_trade_event_fn=persist_trade_event_fn)


def preview_trade_close(
    repo: Any,
    *,
    matches: list[Any],
    deal: NormalizedTradeDeal,
    close_target_resolution: Any | None = None,
) -> list[BrokerTradeOperation]:
    return preview_broker_trade_close(
        repo,
        matches=matches,
        deal=deal,
        close_target_resolution=close_target_resolution,
    )


def apply_trade_close_with(
    repo: Any,
    *,
    matches: list[Any],
    deal: NormalizedTradeDeal,
    persist_trade_event_fn: Any,
    close_target_resolution: Any | None = None,
) -> list[BrokerTradeOperation]:
    return record_broker_trade_close(
        repo,
        matches=matches,
        deal=deal,
        persist_trade_event_fn=persist_trade_event_fn,
        close_target_resolution=close_target_resolution,
    )


def execute_broker_assigned_stock_sale(
    repo: Any,
    deal: NormalizedTradeDeal,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    return _execute_position_broker_assigned_stock_sale(repo, deal, dry_run=dry_run)


def recorded_trade_operations(events: list[dict[str, Any]], *,
                              notification_outbox_id: str | None = None) -> list[BrokerTradeOperation]:
    """Reconstruct presentation from a complete recorded group without economic writes."""
    return [BrokerTradeOperation(
        action=str(event["event_type"]), event_id=event["event_id"],
        lot_id=(lot_id_for_open_event(event) if event["event_type"] == "open"
                else event.get("target_lot_id") or event.get("lot_id")),
        contracts_to_close=int(event["contracts"]) if event["event_type"] != "open" else None,
        details={"contracts": int(event["contracts"]), "position_side": event.get("position_side")},
        result={"event": dict(event), "replayed": True,
                **({"notification_outbox_id": notification_outbox_id} if notification_outbox_id else {})},
    ) for event in events]
