from __future__ import annotations

from typing import Any

from src.application.ledger.api import (
    BrokerTradeOpenPreviewResult,
    BrokerTradeOperation,
    preview_broker_trade_close,
    preview_broker_trade_open,
    record_broker_trade_close,
    record_broker_trade_open,
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
