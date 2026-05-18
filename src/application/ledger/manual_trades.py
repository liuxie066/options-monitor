from __future__ import annotations

import hashlib
import json
from typing import Any

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.ledger.position_fields import (
    OpenPositionCommand,
    PositionLotPatch,
    build_close_patch_contract,
    build_open_adjustment_patch_contract,
    build_position_lot_fields,
    effective_expiration_ymd,
    effective_multiplier,
    effective_strike,
    exp_ms_to_ymd,
    normalize_account,
    normalize_broker,
    normalize_currency,
    normalize_trade_price,
    now_ms,
    resolve_open_currency,
)
from domain.domain.trade_contract_identity import canonical_contract_symbol
from src.application.ledger.publisher import project_stored_trade_events_to_position_lots
from src.application.ledger.results import LedgerWriteResult
from src.application.ledger.targets import assert_position_lot_target_matches_current_state
from src.application.ledger.writer import (
    persist_trade_event_object,
    projection_diagnostics_summary,
)
from src.infrastructure.feishu_bitable import safe_float


def _canonical_trade_symbol(value: Any) -> str:
    return canonical_contract_symbol(value)


def _manual_open_event_id(
    *,
    broker: str,
    account: str,
    symbol: str,
    option_type: str,
    side: str,
    contracts: int,
    price: float,
    strike: float | None,
    expiration_ymd: str | None,
    trade_time_ms: int,
) -> str:
    key_parts = [
        str(broker).strip().lower(),
        str(account).strip().lower(),
        str(symbol).strip().upper(),
        str(option_type).strip().lower(),
        str(side).strip().lower(),
        "open",
        str(int(contracts)),
        repr(float(price)),
        repr(float(strike)) if strike is not None else "",
        str(expiration_ymd or "").strip(),
        str(int(trade_time_ms)),
    ]
    key_str = "|".join(key_parts)
    h = hashlib.sha256(key_str.encode()).hexdigest()[:16]
    return f"manual-open-{h}"


def _stable_manual_event_id(prefix: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _existing_trade_event_result(repo: Any, *, event_id: str, record_id: str | None = None) -> LedgerWriteResult | None:
    list_trade_events = getattr(repo, "list_trade_events", None)
    if not callable(list_trade_events):
        return None
    raw_events = list_trade_events()
    events = [item for item in raw_events if isinstance(item, dict)] if isinstance(raw_events, list) else []
    if not any(str(item.get("event_id") or "").strip() == str(event_id).strip() for item in events):
        return None
    list_position_lots = getattr(repo, "list_position_lots", None)
    raw_position_lots = list_position_lots() if callable(list_position_lots) else []
    current_lot_count = len(raw_position_lots) if isinstance(raw_position_lots, list) else 0
    projection = project_stored_trade_events_to_position_lots(events)
    result = {
        "event_id": str(event_id),
        "record_id": str(record_id).strip() if record_id else None,
        "created": False,
        "position_lot_count": int(current_lot_count),
    }
    result.update(projection_diagnostics_summary(projection.diagnostics))
    return LedgerWriteResult.from_payload(result)


def _manual_close_event_id(
    *,
    broker: str,
    account: str,
    symbol: str,
    option_type: str,
    side: str,
    contracts_to_close: int,
    close_price: float | None,
    strike: float | None,
    multiplier: int | None,
    expiration_ymd: str | None,
    currency: str,
    record_id: str,
    target_source_event_id: str,
    close_reason: str,
) -> str:
    return _stable_manual_event_id(
        "manual-close",
        {
            "broker": normalize_broker(broker),
            "account": normalize_account(account),
            "symbol": _canonical_trade_symbol(symbol),
            "option_type": str(option_type or "").strip().lower(),
            "side": str(side or "").strip().lower(),
            "position_effect": "close",
            "contracts": int(contracts_to_close),
            "price": float(close_price or 0.0),
            "strike": float(strike) if strike is not None else None,
            "multiplier": int(float(multiplier)) if multiplier is not None else None,
            "expiration_ymd": str(expiration_ymd or "").strip() or None,
            "currency": normalize_currency(currency),
            "record_id": str(record_id or "").strip(),
            "target_source_event_id": str(target_source_event_id or "").strip(),
            "close_reason": str(close_reason or "").strip(),
        },
    )


def existing_manual_close_event_result(
    repo: Any,
    *,
    record_id: str,
    fields: dict[str, Any],
    contracts_to_close: int,
    close_price: float | None,
    close_reason: str,
) -> LedgerWriteResult | None:
    broker = normalize_broker(fields.get("broker"))
    if not broker:
        raise ValueError(f"position lot missing broker: {record_id}")
    normalized_close_price = normalize_trade_price(close_price, "close_price")
    current_fields = assert_position_lot_target_matches_current_state(
        repo,
        record_id=record_id,
        fields=fields,
        operation="manual_close",
    )
    multiplier = effective_multiplier(current_fields)
    strike = effective_strike(current_fields)
    target_source_event_id = str(current_fields.get("source_event_id") or "").strip()
    event_id = _manual_close_event_id(
        broker=broker,
        account=normalize_account(current_fields.get("account")),
        symbol=_canonical_trade_symbol(current_fields.get("symbol")),
        option_type=str(current_fields.get("option_type") or ""),
        side="buy" if str(current_fields.get("side") or "").strip().lower() == "short" else "sell",
        contracts_to_close=int(contracts_to_close),
        close_price=normalized_close_price,
        strike=(float(strike) if strike is not None else None),
        multiplier=(int(float(multiplier)) if multiplier is not None else None),
        expiration_ymd=effective_expiration_ymd(current_fields),
        currency=normalize_currency(current_fields.get("currency")),
        record_id=str(record_id),
        target_source_event_id=target_source_event_id,
        close_reason=str(close_reason or ""),
    )
    return _existing_trade_event_result(repo, event_id=event_id, record_id=str(record_id))


def _manual_adjust_event_id(
    *,
    broker: str,
    account: str,
    symbol: str,
    option_type: str,
    side: str,
    strike: float | None,
    multiplier: int | None,
    expiration_ymd: str | None,
    currency: str,
    record_id: str,
    target_source_event_id: str,
    patch: PositionLotPatch,
) -> str:
    stable_patch = {key: value for key, value in patch.to_dict().items() if key != "last_action_at"}
    return _stable_manual_event_id(
        "manual-adjust",
        {
            "broker": normalize_broker(broker),
            "account": normalize_account(account),
            "symbol": _canonical_trade_symbol(symbol),
            "option_type": str(option_type or "").strip().lower(),
            "side": str(side or "").strip().lower(),
            "position_effect": "adjust",
            "strike": float(strike) if strike is not None else None,
            "multiplier": int(float(multiplier)) if multiplier is not None else None,
            "expiration_ymd": str(expiration_ymd or "").strip() or None,
            "currency": normalize_currency(currency),
            "record_id": str(record_id or "").strip(),
            "target_source_event_id": str(target_source_event_id or "").strip(),
            "patch": stable_patch,
        },
    )


def persist_manual_open_event(repo: Any, command: OpenPositionCommand) -> LedgerWriteResult:
    fields = build_position_lot_fields(command).to_dict()
    premium_per_share = normalize_trade_price(fields.get("premium"), "premium_per_share")
    currency = resolve_open_currency(command.symbol, command.currency)
    normalized_side = "sell" if str(command.side).strip().lower() == "short" else "buy"
    canonical_symbol = _canonical_trade_symbol(command.symbol)
    strike = float(command.strike) if command.strike is not None else None
    expiration_ymd = str(command.expiration_ymd or "").strip() or None
    trade_time_ms = int(command.opened_at_ms or now_ms())
    event_id = _manual_open_event_id(
        broker=str(command.broker),
        account=str(command.account),
        symbol=canonical_symbol,
        option_type=str(command.option_type),
        side=normalized_side,
        contracts=int(command.contracts),
        price=float(premium_per_share),
        strike=strike,
        expiration_ymd=expiration_ymd,
        trade_time_ms=trade_time_ms,
    )
    event = TradeEvent(
        event_id=event_id,
        event_type="open",
        event_time_ms=trade_time_ms,
        contract_key=ContractKey.from_values(
            broker=str(command.broker),
            account=str(command.account),
            underlying_symbol=canonical_symbol,
            option_type=str(command.option_type),
            position_side=str(command.side),
            strike=strike,
            expiration_ymd=expiration_ymd,
        ),
        contracts=int(command.contracts),
        price=float(premium_per_share),
        currency=currency,
        source="cli_manual_open",
        multiplier=(float(command.multiplier) if command.multiplier is not None else 100.0),
        lot_id=f"lot_{event_id}",
        raw_payload={
            "source": "om option-positions",
            "source_type": "manual_trade_event",
            "mode": "manual_open",
            "side": normalized_side,
            "multiplier_source": "payload" if command.multiplier is not None else None,
        },
    )
    return persist_trade_event_object(repo, event)


def persist_manual_close_event(
    repo: Any,
    *,
    record_id: str,
    fields: dict[str, Any],
    contracts_to_close: int,
    close_price: float | None,
    close_reason: str,
    as_of_ms: int | None = None,
) -> LedgerWriteResult:
    broker = normalize_broker(fields.get("broker"))
    if not broker:
        raise ValueError(f"position lot missing broker: {record_id}")
    normalized_close_price = normalize_trade_price(close_price, "close_price")
    fields = assert_position_lot_target_matches_current_state(
        repo,
        record_id=record_id,
        fields=fields,
        operation="manual_close",
    )
    multiplier = effective_multiplier(fields)
    strike = effective_strike(fields)
    target_source_event_id = str(fields.get("source_event_id") or "").strip()
    normalized_account = normalize_account(fields.get("account"))
    canonical_symbol = _canonical_trade_symbol(fields.get("symbol"))
    expiration_ymd = effective_expiration_ymd(fields)
    currency = normalize_currency(fields.get("currency"))
    event_id = _manual_close_event_id(
        broker=broker,
        account=normalized_account,
        symbol=canonical_symbol,
        option_type=str(fields.get("option_type") or ""),
        side="buy" if str(fields.get("side") or "").strip().lower() == "short" else "sell",
        contracts_to_close=int(contracts_to_close),
        close_price=normalized_close_price,
        strike=(float(strike) if strike is not None else None),
        multiplier=(int(float(multiplier)) if multiplier is not None else None),
        expiration_ymd=expiration_ymd,
        currency=currency,
        record_id=str(record_id),
        target_source_event_id=target_source_event_id,
        close_reason=str(close_reason or ""),
    )
    existing_result = _existing_trade_event_result(repo, event_id=event_id, record_id=str(record_id))
    if existing_result is not None:
        return existing_result
    close_patch_contract = build_close_patch_contract(
        fields,
        contracts_to_close=int(contracts_to_close),
        close_price=normalized_close_price,
        close_reason=close_reason,
        as_of_ms=as_of_ms,
    )
    close_patch = close_patch_contract.to_dict()
    event = TradeEvent(
        event_id=event_id,
        event_type="close",
        event_time_ms=int(as_of_ms or now_ms()),
        contract_key=ContractKey.from_values(
            broker=broker,
            account=normalized_account,
            underlying_symbol=canonical_symbol,
            option_type=str(fields.get("option_type") or ""),
            position_side=str(fields.get("side") or "").strip().lower(),
            strike=(float(strike) if strike is not None else None),
            expiration_ymd=expiration_ymd,
        ),
        contracts=int(contracts_to_close),
        price=float(normalized_close_price),
        currency=currency,
        source="cli_manual_close",
        multiplier=(float(multiplier) if multiplier is not None else 100.0),
        target_lot_id=str(record_id),
        raw_payload={
            "source": "om option-positions",
            "source_type": "manual_trade_event",
            "mode": "manual_close",
            "record_id": str(record_id),
            "target_lot_id": str(record_id),
            "side": "buy" if str(fields.get("side") or "").strip().lower() == "short" else "sell",
            "close_target_source_event_id": target_source_event_id,
            "close_target_account": normalized_account,
            "close_target_broker": broker,
            "close_reason": str(close_reason or ""),
            "idempotency_key": event_id,
            "projected_patch": close_patch,
        },
    )
    return persist_trade_event_object(repo, event)


def persist_manual_adjust_event(
    repo: Any,
    *,
    record_id: str,
    fields: dict[str, Any],
    contracts: int | None = None,
    strike: float | None = None,
    expiration_ymd: str | None = None,
    premium_per_share: float | None = None,
    multiplier: float | None = None,
    opened_at_ms: int | None = None,
    as_of_ms: int | None = None,
) -> LedgerWriteResult:
    fields = assert_position_lot_target_matches_current_state(
        repo,
        record_id=record_id,
        fields=fields,
        operation="manual_adjust",
    )
    target_source_event_id = str(fields.get("source_event_id") or "").strip()
    patch_contract = build_open_adjustment_patch_contract(
        fields,
        contracts=contracts,
        strike=strike,
        expiration_ymd=expiration_ymd,
        premium_per_share=premium_per_share,
        multiplier=multiplier,
        opened_at_ms=opened_at_ms,
        as_of_ms=as_of_ms,
    )
    patch = patch_contract.to_dict()
    raw_multiplier = safe_float(fields.get("multiplier"))
    current_multiplier = int(float(raw_multiplier)) if raw_multiplier is not None else None
    event_id = _manual_adjust_event_id(
        broker=normalize_broker(fields.get("broker")),
        account=normalize_account(fields.get("account")),
        symbol=_canonical_trade_symbol(fields.get("symbol")),
        option_type=str(fields.get("option_type") or ""),
        side=str(fields.get("side") or "").strip().lower(),
        strike=(float(fields["strike"]) if fields.get("strike") is not None else None),
        multiplier=current_multiplier,
        expiration_ymd=exp_ms_to_ymd(fields.get("expiration")),
        currency=normalize_currency(fields.get("currency")),
        record_id=str(record_id),
        target_source_event_id=target_source_event_id,
        patch=patch_contract,
    )
    existing_result = _existing_trade_event_result(repo, event_id=event_id, record_id=str(record_id))
    if existing_result is not None:
        return existing_result.with_details(patch=patch)
    event = TradeEvent(
        event_id=event_id,
        event_type="adjust",
        event_time_ms=int(as_of_ms or now_ms()),
        contract_key=ContractKey.from_values(
            broker=normalize_broker(fields.get("broker")),
            account=normalize_account(fields.get("account")),
            underlying_symbol=_canonical_trade_symbol(fields.get("symbol")),
            option_type=str(fields.get("option_type") or ""),
            position_side=str(fields.get("side") or "").strip().lower(),
            strike=(float(fields["strike"]) if fields.get("strike") is not None else None),
            expiration_ymd=effective_expiration_ymd(fields),
        ),
        contracts=0,
        price=0.0,
        currency=normalize_currency(fields.get("currency")),
        source="cli_manual_adjust",
        multiplier=(float(current_multiplier) if current_multiplier is not None else 100.0),
        target_lot_id=str(record_id),
        raw_payload={
            "source": "om option-positions",
            "source_type": "manual_trade_event",
            "mode": "manual_adjust",
            "record_id": str(record_id),
            "target_lot_id": str(record_id),
            "adjust_target_source_event_id": target_source_event_id or None,
            "idempotency_key": event_id,
            "patch": patch,
        },
    )
    return persist_trade_event_object(repo, event).with_record_id(str(record_id)).with_details(patch=patch)
