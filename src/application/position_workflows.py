from __future__ import annotations

from typing import Any

from scripts.option_positions_core.domain import OpenPositionCommand, build_buy_to_close_patch, build_open_fields
from scripts.option_positions_core.service import persist_manual_close_event, persist_manual_open_event, persist_trade_event
from scripts.trade_event_normalizer import NormalizedTradeDeal


def _build_trade_open_command(deal: NormalizedTradeDeal) -> OpenPositionCommand:
    return OpenPositionCommand(
        broker="富途",
        account=str(deal.internal_account),
        symbol=str(deal.symbol),
        option_type=str(deal.option_type),
        side="short",
        contracts=int(deal.contracts or 0),
        currency=str(deal.currency),
        strike=float(deal.strike),
        multiplier=float(deal.multiplier) if deal.multiplier is not None else None,
        expiration_ymd=str(deal.expiration_ymd),
        premium_per_share=float(deal.price),
        note=(
            f"source=opend_push "
            f"deal_id={deal.deal_id} "
            f"order_id={deal.order_id or ''} "
            f"multiplier_source={deal.multiplier_source or ''} "
            f"trade_time_ms={deal.trade_time_ms or ''}"
        ).strip(),
        opened_at_ms=deal.trade_time_ms,
    )


def preview_trade_open(deal: NormalizedTradeDeal) -> dict[str, Any]:
    command = _build_trade_open_command(deal)
    return {"command": command, "fields": build_open_fields(command)}


def apply_trade_open_with(repo: Any, deal: NormalizedTradeDeal, *, persist_trade_event_fn: Any) -> dict[str, Any]:
    persist_trade_event_fn(repo, deal)
    return {"action": "open", "event_id": deal.deal_id}


def preview_trade_close(repo: Any, *, matches: list[Any], deal: NormalizedTradeDeal) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for match in matches:
        fields = repo.get_record_fields(match.record_id)
        operations.append(
            {
                "action": "buy_close",
                "record_id": match.record_id,
                "contracts_to_close": match.contracts_to_close,
                "patch": build_buy_to_close_patch(
                    fields,
                    contracts_to_close=match.contracts_to_close,
                    close_price=float(deal.price),
                    close_reason="auto_trade_buy_to_close",
                    as_of_ms=deal.trade_time_ms,
                ),
                "matched_by": match.matched_by,
            }
        )
    return operations


def apply_trade_close_with(
    repo: Any,
    *,
    matches: list[Any],
    deal: NormalizedTradeDeal,
    persist_trade_event_fn: Any,
) -> list[dict[str, Any]]:
    persist_trade_event_fn(repo, deal)
    return [
        {
            "action": "buy_close",
            "record_id": match.record_id,
            "contracts_to_close": match.contracts_to_close,
        }
        for match in matches
    ]


def execute_manual_open(
    repo: Any | None,
    *,
    broker: str,
    account: str,
    symbol: str,
    option_type: str,
    side: str,
    contracts: int,
    currency: str,
    strike: float | None,
    multiplier: float | None,
    expiration_ymd: str | None,
    premium_per_share: float | None,
    underlying_share_locked: int | None,
    note: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    command = OpenPositionCommand(
        broker=broker,
        account=account,
        symbol=symbol,
        option_type=option_type,
        side=side,
        contracts=int(contracts),
        currency=currency,
        strike=strike,
        multiplier=multiplier,
        expiration_ymd=expiration_ymd,
        premium_per_share=premium_per_share,
        underlying_share_locked=underlying_share_locked,
        note=note,
    )
    fields = build_open_fields(command)
    if dry_run:
        return {"mode": "dry_run", "fields": fields, "command": command}
    if repo is None:
        raise ValueError("repo is required when dry_run is false")
    result = persist_manual_open_event(repo, command)
    return {"mode": "applied", "fields": fields, "result": result, "command": command}


def execute_manual_close(
    repo: Any,
    *,
    record_id: str,
    contracts_to_close: int,
    close_price: float | None,
    close_reason: str,
    dry_run: bool,
) -> dict[str, Any]:
    fields = repo.get_record_fields(record_id)
    patch = build_buy_to_close_patch(
        fields,
        contracts_to_close=int(contracts_to_close),
        close_price=close_price,
        close_reason=close_reason,
    )
    if dry_run:
        return {"mode": "dry_run", "fields": fields, "patch": patch}
    result = persist_manual_close_event(
        repo,
        record_id=record_id,
        fields=fields,
        contracts_to_close=int(contracts_to_close),
        close_price=close_price,
        close_reason=close_reason,
    )
    return {"mode": "applied", "fields": fields, "patch": patch, "result": result}
