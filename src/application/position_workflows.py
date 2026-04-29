from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from scripts.option_positions_core.domain import (
    OpenPositionCommand,
    build_buy_to_close_patch,
    build_open_adjustment_patch,
    build_open_fields,
)
from scripts.option_positions_core.service import persist_manual_adjust_event, persist_manual_close_event, persist_manual_open_event
from scripts.sync_option_positions_to_feishu import sync_single_option_position_record
from scripts.trade_event_normalizer import NormalizedTradeDeal


def _auto_sync_record_if_possible(repo: Any, *, record_id: str) -> dict[str, Any] | None:
    try:
        data_config = getattr(repo, "data_config_path", None)
        if data_config is None:
            return None
        return sync_single_option_position_record(repo=repo, data_config=Path(str(data_config)), record_id=record_id, apply_mode=True)
    except Exception as exc:
        print(f"[WARN] option_positions post-write Feishu sync skipped for {record_id}: {exc}", file=sys.stderr)
        return None


def _manual_open_record_id(result: dict[str, Any]) -> str:
    record_id = str(result.get("record_id") or "").strip()
    if record_id:
        return record_id
    event_id = str(result.get("event_id") or "").strip()
    if not event_id:
        return ""
    return f"lot_{event_id}"


def _build_trade_open_command(deal: NormalizedTradeDeal) -> OpenPositionCommand:
    return OpenPositionCommand(
        broker="富途",
        account=str(deal.internal_account or ""),
        symbol=str(deal.symbol or ""),
        option_type=str(deal.option_type or ""),
        side="short",
        contracts=int(deal.contracts or 0),
        currency=str(deal.currency or ""),
        strike=(float(deal.strike) if deal.strike is not None else None),
        multiplier=float(deal.multiplier) if deal.multiplier is not None else None,
        expiration_ymd=(str(deal.expiration_ymd or "").strip() or None),
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
    synced_record_id = _manual_open_record_id(result)
    sync_result = _auto_sync_record_if_possible(repo, record_id=synced_record_id) if synced_record_id else None
    return {"mode": "applied", "fields": fields, "result": result, "command": command, "sync_result": sync_result}


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
    sync_result = _auto_sync_record_if_possible(repo, record_id=record_id)
    return {"mode": "applied", "fields": fields, "patch": patch, "result": result, "sync_result": sync_result}


def execute_manual_adjust(
    repo: Any,
    *,
    record_id: str,
    contracts: int | None,
    strike: float | None,
    expiration_ymd: str | None,
    premium_per_share: float | None,
    multiplier: float | None,
    opened_at_ms: int | None,
    dry_run: bool,
) -> dict[str, Any]:
    fields = repo.get_record_fields(record_id)
    patch = build_open_adjustment_patch(
        fields,
        contracts=contracts,
        strike=strike,
        expiration_ymd=expiration_ymd,
        premium_per_share=premium_per_share,
        multiplier=multiplier,
        opened_at_ms=opened_at_ms,
    )
    if dry_run:
        return {"mode": "dry_run", "fields": fields, "patch": patch}
    result = persist_manual_adjust_event(
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
    sync_result = _auto_sync_record_if_possible(repo, record_id=record_id)
    return {"mode": "applied", "fields": fields, "patch": patch, "result": result, "sync_result": sync_result}
