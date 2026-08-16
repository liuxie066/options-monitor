from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence

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
    normalize_trade_price,
    now_ms,
    resolve_open_currency,
    strategy_metadata_fields_from_payload,
)
from domain.domain.option_position_identity import normalize_currency
from domain.domain.trade_contract_identity import canonical_contract_symbol
from src.application.ledger.position_projection_runtime import (
    run_position_projection_in_transaction,
)
from src.application.ledger.current_decision_projection import (
    capture_trade_event_decision_projection_fence,
)
from src.application.ledger.results import LedgerWriteResult
from src.application.ledger.targets import assert_position_lot_target_matches_current_state
from src.application.ledger.writer import (
    _finish_trade_event_decision_projection,
    persist_trade_event_object,
    projection_diagnostics_summary,
)
from src.application.ledger.repository import with_sqlite_repo_transaction
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
    multiplier: float | None,
    expiration_ymd: str | None,
    currency: str,
    trade_time_ms: int,
    request_id: str | None = None,
) -> str:
    request_id_value = str(request_id or "").strip()
    if request_id_value:
        digest = hashlib.sha256(request_id_value.encode("utf-8")).hexdigest()[:24]
        return f"manual-open-request-{digest}"
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
        repr(float(multiplier)) if multiplier is not None else "",
        str(expiration_ymd or "").strip(),
        normalize_currency(currency),
        str(int(trade_time_ms)),
    ]
    key_str = "|".join(key_parts)
    h = hashlib.sha256(key_str.encode()).hexdigest()[:16]
    return f"manual-open-{h}"


def manual_open_request_intent_hash(
    command: OpenPositionCommand,
    *,
    fields: dict[str, Any] | None = None,
) -> str:
    resolved_fields = dict(fields or build_position_lot_fields(command).to_dict())
    payload = {
        "broker": normalize_broker(command.broker),
        "account": normalize_account(command.account),
        "symbol": _canonical_trade_symbol(command.symbol),
        "option_type": str(command.option_type or "").strip().lower(),
        "side": str(command.side or "").strip().lower(),
        "contracts": int(command.contracts),
        "currency": resolve_open_currency(command.symbol, command.currency),
        "strike": float(command.strike) if command.strike is not None else None,
        "multiplier": float(effective_multiplier(resolved_fields) or 100),
        "expiration_ymd": str(command.expiration_ymd or "").strip() or None,
        "premium_per_share": float(resolved_fields.get("premium")),
        "underlying_share_locked": command.underlying_share_locked,
        "note": command.note,
        "strategy_snapshot": (
            dict(command.strategy_snapshot)
            if isinstance(command.strategy_snapshot, dict)
            else None
        ),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def assert_manual_request_event_matches(
    repo: Any,
    *,
    event_id: str,
    request_id: str,
    intent_hash: str,
) -> None:
    candidate = getattr(repo, "primary_repo", repo)
    getter = getattr(candidate, "get_trade_events_by_ids", None)
    rows = (
        getter((event_id,))
        if callable(getter)
        else [
            item
            for item in candidate.list_trade_events()
            if str(item.get("event_id") or "").strip() == event_id
        ]
    )
    for item in rows:
        raw = item.get("raw_payload")
        payload = raw if isinstance(raw, dict) else {}
        if (
            str(payload.get("manual_request_id") or "").strip() != request_id
            or str(payload.get("manual_request_intent_hash") or "").strip() != intent_hash
        ):
            raise ValueError(f"manual request conflict for request_id={request_id}")
        return


def _stable_manual_event_id(prefix: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _existing_trade_event_result(repo: Any, *, event_id: str, record_id: str | None = None) -> LedgerWriteResult | None:
    candidate = getattr(repo, "primary_repo", repo)
    getter = getattr(candidate, "get_trade_events_by_ids", None)
    rows = (
        getter((event_id,))
        if callable(getter)
        else [
            item
            for item in candidate.list_trade_events()
            if str(item.get("event_id") or "").strip() == str(event_id).strip()
        ]
    )
    if not rows:
        return None
    return LedgerWriteResult.from_payload(
        {
            "event_id": str(event_id),
            "record_id": str(record_id).strip() if record_id else None,
            "created": False,
            "position_lot_count": int(candidate.count_position_lots()),
            **projection_diagnostics_summary(()),
        }
    )


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
    request_id = str(command.request_id or "").strip()
    intent_hash = manual_open_request_intent_hash(command, fields=fields)
    event_id = _manual_open_event_id(
        broker=str(command.broker),
        account=str(command.account),
        symbol=canonical_symbol,
        option_type=str(command.option_type),
        side=normalized_side,
        contracts=int(command.contracts),
        price=float(premium_per_share),
        strike=strike,
        multiplier=effective_multiplier(fields),
        expiration_ymd=expiration_ymd,
        currency=currency,
        trade_time_ms=trade_time_ms,
        request_id=request_id or None,
    )
    existing_result = _existing_trade_event_result(
        repo,
        event_id=event_id,
        record_id=f"lot_{event_id}",
    )
    if existing_result is not None:
        if request_id:
            assert_manual_request_event_matches(
                repo,
                event_id=event_id,
                request_id=request_id,
                intent_hash=intent_hash,
            )
        return existing_result
    strategy_payload = strategy_metadata_fields_from_payload(
        {
            "strategy_snapshot": (
                dict(command.strategy_snapshot) if isinstance(command.strategy_snapshot, dict) else None
            )
        }
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
            "manual_request_id": request_id or None,
            "manual_request_intent_hash": intent_hash if request_id else None,
            **strategy_payload,
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


def _build_manual_adjust_event(
    repo: Any,
    *,
    record_id: str,
    fields: dict[str, Any],
    current_fields: dict[str, Any] | None = None,
    contracts: int | None = None,
    strike: float | None = None,
    expiration_ymd: str | None = None,
    premium_per_share: float | None = None,
    multiplier: float | None = None,
    opened_at_ms: int | None = None,
    strategy: str | None = None,
    leg_role: str | None = None,
    strategy_group_id: str | None = None,
    yield_enhancement_mode: str | None = None,
    strategy_snapshot: dict[str, Any] | None = None,
    as_of_ms: int | None = None,
) -> tuple[TradeEvent, PositionLotPatch]:
    fields = assert_position_lot_target_matches_current_state(
        repo,
        record_id=record_id,
        fields=fields,
        operation="manual_adjust",
        current_fields=current_fields,
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
        strategy=strategy,
        leg_role=leg_role,
        strategy_group_id=strategy_group_id,
        yield_enhancement_mode=yield_enhancement_mode,
        strategy_snapshot=strategy_snapshot,
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
    return event, patch_contract


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
    strategy: str | None = None,
    leg_role: str | None = None,
    strategy_group_id: str | None = None,
    yield_enhancement_mode: str | None = None,
    strategy_snapshot: dict[str, Any] | None = None,
    as_of_ms: int | None = None,
) -> LedgerWriteResult:
    event, patch_contract = _build_manual_adjust_event(
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
        yield_enhancement_mode=yield_enhancement_mode,
        strategy_snapshot=strategy_snapshot,
        as_of_ms=as_of_ms,
    )
    existing_result = _existing_trade_event_result(repo, event_id=event.event_id, record_id=str(record_id))
    if existing_result is not None:
        return existing_result.with_details(patch=patch_contract.to_dict())
    return (
        persist_trade_event_object(repo, event)
        .with_record_id(str(record_id))
        .with_details(patch=patch_contract.to_dict())
    )


def persist_manual_adjust_events(
    repo: Any,
    adjustments: Sequence[dict[str, Any]],
) -> list[LedgerWriteResult]:
    """Persist multiple lot adjustments and refresh projection in one transaction."""

    validated: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    seen_record_ids: set[str] = set()
    for raw in adjustments:
        item = dict(raw or {})
        record_id = str(item.pop("record_id", "") or "").strip()
        fields = item.pop("fields", None)
        if not record_id:
            raise ValueError("manual adjustment batch requires record_id")
        if record_id in seen_record_ids:
            raise ValueError(f"manual adjustment batch contains duplicate record_id: {record_id}")
        if not isinstance(fields, dict):
            raise ValueError(f"manual adjustment batch requires fields for record_id={record_id}")
        validated.append((record_id, dict(fields), item))
        seen_record_ids.add(record_id)

    if not validated:
        raise ValueError("manual adjustment batch requires at least one adjustment")

    def _run(sqlite_repo: Any, conn: Any | None) -> list[LedgerWriteResult]:
        if conn is None:
            raise TypeError("manual adjustment batch requires SQLite transaction authority")
        current_rows = sqlite_repo.get_position_lots_by_ids(
            tuple(seen_record_ids),
            conn=conn,
        )
        current_by_record_id = {
            str(row.get("record_id") or "").strip(): dict(row.get("fields") or {})
            for row in current_rows
            if str(row.get("record_id") or "").strip()
        }
        desired_group_ids = {
            str(item.get("strategy_group_id") or "").strip()
            for _record_id, _fields, item in validated
            if str(item.get("strategy_group_id") or "").strip()
        }
        if desired_group_ids:
            group_placeholders = ",".join("?" for _item in desired_group_ids)
            target_placeholders = ",".join("?" for _item in seen_record_ids)
            collision = conn.execute(
                f"""
                SELECT record_id
                FROM position_lots
                WHERE json_extract(fields_json, '$.strategy_group_id')
                      IN ({group_placeholders})
                  AND record_id NOT IN ({target_placeholders})
                ORDER BY record_id ASC
                LIMIT 1
                """,
                (*sorted(desired_group_ids), *sorted(seen_record_ids)),
            ).fetchone()
            if collision is not None:
                raise ValueError(
                    "strategy_group_id is already assigned to another "
                    f"position lot: record_id={collision['record_id']}"
                )

        prepared: list[tuple[str, TradeEvent, PositionLotPatch]] = []
        for record_id, fields, item in validated:
            current_fields = current_by_record_id.get(record_id)
            if current_fields is None:
                raise ValueError(f"manual adjustment batch target lot not found: {record_id}")
            event, patch_contract = _build_manual_adjust_event(
                sqlite_repo,
                record_id=record_id,
                fields=fields,
                current_fields=current_fields,
                **item,
            )
            prepared.append((record_id, event, patch_contract))

        decision_fence = capture_trade_event_decision_projection_fence(
            sqlite_repo,
            conn=conn,
        )
        runtime = run_position_projection_in_transaction(
            sqlite_repo,
            [event for _record_id, event, _patch_contract in prepared],
            conn=conn,
            mode="fast_if_safe",
        )
        decision_projection = _finish_trade_event_decision_projection(
            sqlite_repo,
            conn=conn,
            fence=decision_fence,
            events=[event for _record_id, event, _patch_contract in prepared],
            created_flags=runtime.created_flags,
        )
        diagnostics = projection_diagnostics_summary(runtime.diagnostics)
        out: list[LedgerWriteResult] = []
        for (record_id, event, patch_contract), created in zip(
            prepared,
            runtime.created_flags,
            strict=True,
        ):
            payload = {
                "event_id": event.event_id,
                "record_id": record_id,
                "created": created,
                "position_lot_count": int(runtime.position_lot_count),
                **diagnostics,
                "patch": patch_contract.to_dict(),
                "decision_projection": decision_projection,
            }
            out.append(LedgerWriteResult.from_payload(payload))
        return out

    return with_sqlite_repo_transaction(
        repo,
        _run,
        require_projection_publication=True,
    )
