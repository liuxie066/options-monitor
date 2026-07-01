from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from domain.domain.ledger.position_fields import (
    OpenPositionCommand,
    effective_expiration_ymd,
    normalize_account,
    normalize_broker,
    normalize_currency,
    norm_symbol,
)
from src.application.ledger.api import (
    LotCloseResolutionError,
    preview_manual_assignment,
    preview_manual_exercise,
    preview_manual_position_adjust,
    preview_manual_position_close,
    preview_manual_position_open,
    record_manual_assignment,
    record_manual_exercise,
    record_manual_position_adjust,
    record_manual_position_close,
    record_manual_position_open,
    resolve_manual_position_close_target,
)
from src.application.positions.reporting import build_monthly_income_report


def _ms_to_iso(value: int | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()


def _apply_result_payload(
    repo: Any,
    *,
    record_id: str,
    result: dict[str, Any],
    payload: dict[str, Any],
    native_event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del repo, record_id, native_event
    idempotent_duplicate = result.get("created") is False
    return payload | {
        "mode": "applied",
        "result": result,
        "idempotent_duplicate": bool(idempotent_duplicate),
    }


def _manual_open_record_id(result: dict[str, Any]) -> str:
    record_id = str(result.get("record_id") or "").strip()
    if record_id:
        return record_id
    event_id = str(result.get("event_id") or "").strip()
    if not event_id:
        return ""
    return f"lot_{event_id}"


def _list_repo_trade_events(repo: Any) -> list[dict[str, Any]]:
    list_trade_events = getattr(repo, "list_trade_events", None)
    rows = list_trade_events() if callable(list_trade_events) else []
    return rows if isinstance(rows, list) else []


def _list_repo_assigned_stock_events(repo: Any) -> list[dict[str, Any]]:
    list_events = getattr(repo, "list_assigned_stock_events", None)
    rows = list_events() if callable(list_events) else []
    return rows if isinstance(rows, list) else []


def _assigned_stock_report(
    repo: Any,
    *,
    account: str | None = None,
    broker: str | None = None,
    assigned_stock_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return build_monthly_income_report(
        [],
        account=account,
        broker=broker,
        trade_events=_list_repo_trade_events(repo),
        assigned_stock_events=assigned_stock_events
        if assigned_stock_events is not None
        else _list_repo_assigned_stock_events(repo),
    )


def _assigned_stock_sale_event_id(payload: dict[str, Any]) -> str:
    source_deal_id = str(payload.get("source_deal_id") or "").strip()
    if source_deal_id:
        return f"assigned-stock-sale-{source_deal_id}"
    stable = {
        key: payload.get(key)
        for key in (
            "target_stock_lot_id",
            "account",
            "broker",
            "symbol",
            "currency",
            "shares",
            "price",
            "fees",
            "trade_time_ms",
            "source",
        )
    }
    digest = hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return f"assigned-stock-sale-manual-{digest}"


def _find_assigned_stock_lot(report: dict[str, Any], stock_lot_id: str) -> dict[str, Any] | None:
    for row in report.get("assigned_stock_lots") or []:
        if isinstance(row, dict) and str(row.get("stock_lot_id") or "") == stock_lot_id:
            return dict(row)
    return None


def _build_assigned_stock_sale_event(
    lot: dict[str, Any],
    *,
    target_stock_lot_id: str,
    shares: int,
    price: float,
    fees: float,
    trade_time_ms: int,
    account: str | None,
    broker: str | None,
    symbol: str | None,
    currency: str | None,
    source_deal_id: str | None,
    source: str,
) -> dict[str, Any]:
    payload = {
        "event_type": "sale",
        "target_stock_lot_id": target_stock_lot_id,
        "account": normalize_account(account) or lot.get("account"),
        "broker": normalize_broker(broker) or lot.get("broker"),
        "symbol": norm_symbol(symbol or lot.get("symbol") or ""),
        "side": "sell",
        "shares": int(shares),
        "price": float(price),
        "currency": normalize_currency(currency) or lot.get("currency"),
        "fees": float(fees or 0.0),
        "trade_time_ms": int(trade_time_ms),
        "source": str(source or "").strip() or "manual",
        "source_deal_id": str(source_deal_id or "").strip() or None,
    }
    payload["stock_event_id"] = _assigned_stock_sale_event_id(payload)
    return payload


def _build_manual_assigned_stock_sale_event(
    lot: dict[str, Any],
    *,
    target_stock_lot_id: str,
    shares: int,
    price: float,
    fees: float,
    trade_time_ms: int,
    account: str | None,
    broker: str | None,
    symbol: str | None,
    currency: str | None,
    source_deal_id: str | None,
) -> dict[str, Any]:
    return _build_assigned_stock_sale_event(
        lot,
        target_stock_lot_id=target_stock_lot_id,
        shares=shares,
        price=price,
        fees=fees,
        trade_time_ms=trade_time_ms,
        account=account,
        broker=broker,
        symbol=symbol,
        currency=currency,
        source_deal_id=source_deal_id,
        source="manual",
    )


class BrokerAssignedStockSaleMatchError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.diagnostics = dict(diagnostics or {})


def _stock_sale_deal_fee(deal: Any) -> tuple[float, str | None]:
    raw = getattr(deal, "raw_payload", None)
    payload = raw if isinstance(raw, dict) else {}
    aggregate_keys = ("fees", "fee", "total_fee", "total_fees", "charges")
    component_keys = ("commission", "platform_fee", "system_fee", "settlement_fee", "trading_fee", "sec_fee", "reg_fee")
    for key in aggregate_keys:
        if payload.get(key) in (None, ""):
            continue
        try:
            return abs(float(payload[key])), key
        except Exception:
            continue
    total = 0.0
    used: list[str] = []
    for key in component_keys:
        if payload.get(key) in (None, ""):
            continue
        try:
            total += abs(float(payload[key]))
            used.append(key)
        except Exception:
            continue
    return (total, ",".join(used) if used else None)


def _safe_positive_int(value: Any) -> int | None:
    try:
        out = int(value)
    except Exception:
        return None
    return out if out > 0 else None


def _safe_non_negative_float(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    return out if out >= 0 else None


def _assigned_stock_candidate_summary(lot: dict[str, Any], *, reject_reasons: list[str] | None = None) -> dict[str, Any]:
    return {
        "stock_lot_id": lot.get("stock_lot_id"),
        "source_assignment_event_id": lot.get("source_assignment_event_id"),
        "account": lot.get("account"),
        "broker": lot.get("broker"),
        "symbol": lot.get("symbol"),
        "currency": lot.get("currency"),
        "opened_at_ms": lot.get("opened_at_ms"),
        "shares_remaining": lot.get("shares_remaining"),
        "stock_cost_per_share": lot.get("stock_cost_per_share"),
        "assignment_price": lot.get("assignment_price"),
        "status": lot.get("status"),
        "reject_reasons": list(reject_reasons or []),
    }


def _broker_assigned_stock_sale_match(repo: Any, deal: Any) -> dict[str, Any]:
    account = normalize_account(getattr(deal, "internal_account", None))
    broker = normalize_broker(getattr(deal, "broker", None))
    symbol = norm_symbol(getattr(deal, "symbol", None) or "")
    currency = normalize_currency(getattr(deal, "currency", None))
    selector = {
        "account": account,
        "broker": broker,
        "symbol": symbol,
        "currency": currency,
        "source_deal_id": str(getattr(deal, "deal_id", None) or "").strip() or None,
    }
    missing_identity = [key for key in ("account", "broker", "symbol") if not selector.get(key)]
    if missing_identity:
        raise BrokerAssignedStockSaleMatchError(
            "missing_required_fields",
            "assigned stock sale requires account, broker, and symbol",
            diagnostics={"selector": selector, "missing_fields": missing_identity},
        )

    existing_events = _list_repo_assigned_stock_events(repo)
    before_report = _assigned_stock_report(repo, account=account, broker=broker, assigned_stock_events=existing_events)
    source_deal_id = str(getattr(deal, "deal_id", None) or "").strip()
    existing_by_source = next(
        (
            event
            for event in existing_events
            if isinstance(event, dict)
            and str(event.get("source_deal_id") or "").strip()
            and str(event.get("source_deal_id") or "").strip() == source_deal_id
        ),
        None,
    )
    if existing_by_source is not None:
        target_stock_lot_id = str(existing_by_source.get("target_stock_lot_id") or "").strip()
        lot = _find_assigned_stock_lot(before_report, target_stock_lot_id)
        if lot is None:
            raise BrokerAssignedStockSaleMatchError(
                "source_conflict",
                "existing assigned stock sale event targets a missing assigned stock lot",
                diagnostics={"selector": selector, "existing_event": dict(existing_by_source)},
            )
        shares = _safe_positive_int(getattr(deal, "contracts", None))
        price = _safe_non_negative_float(getattr(deal, "price", None))
        trade_time_ms = _safe_positive_int(getattr(deal, "trade_time_ms", None))
        required = {
            "deal_id": source_deal_id or None,
            "currency": currency,
            "shares": shares,
            "price": price,
            "trade_time_ms": trade_time_ms,
        }
        missing = [key for key, value in required.items() if value in (None, "")]
        if missing:
            raise BrokerAssignedStockSaleMatchError(
                "missing_required_fields",
                "assigned stock sale duplicate has missing required fields",
                diagnostics={"selector": selector, "missing_fields": missing, "existing_event": dict(existing_by_source)},
            )
        fees, fee_source = _stock_sale_deal_fee(deal)
        return {
            "lot": lot,
            "existing_events": existing_events,
            "before_report": before_report,
            "selector": selector,
            "shares": int(shares or 0),
            "price": float(price or 0.0),
            "fees": float(fees or 0.0),
            "fee_source": fee_source,
            "trade_time_ms": int(trade_time_ms or 0),
            "source_deal_id": source_deal_id,
            "diagnostics": {
                "selector": selector,
                "matched_stock_lot_id": lot.get("stock_lot_id"),
                "existing_stock_event_id": existing_by_source.get("stock_event_id") or existing_by_source.get("event_id"),
                "idempotent_candidate": True,
                "fee_source": fee_source,
            },
        }

    identity_candidates: list[dict[str, Any]] = []
    for row in before_report.get("assigned_stock_lots") or []:
        if not isinstance(row, dict):
            continue
        if int(row.get("shares_remaining") or 0) <= 0:
            continue
        if normalize_account(row.get("account")) != account:
            continue
        if normalize_broker(row.get("broker")) != broker:
            continue
        if norm_symbol(row.get("symbol") or "") != symbol:
            continue
        if currency and (normalize_currency(row.get("currency")) or "") != currency:
            continue
        identity_candidates.append(dict(row))

    if not identity_candidates:
        raise BrokerAssignedStockSaleMatchError(
            "no_match",
            "no open assigned stock lot matches this stock sale identity",
            diagnostics={"selector": selector},
        )

    shares = _safe_positive_int(getattr(deal, "contracts", None))
    price = _safe_non_negative_float(getattr(deal, "price", None))
    trade_time_ms = _safe_positive_int(getattr(deal, "trade_time_ms", None))
    required = {
        "deal_id": source_deal_id or None,
        "currency": currency,
        "shares": shares,
        "price": price,
        "trade_time_ms": trade_time_ms,
    }
    missing = [key for key, value in required.items() if value in (None, "")]
    if missing:
        raise BrokerAssignedStockSaleMatchError(
            "missing_required_fields",
            "assigned stock sale has matching lot candidates but missing required fields",
            diagnostics={
                "selector": selector,
                "missing_fields": missing,
                "candidate_count": len(identity_candidates),
                "candidates": [_assigned_stock_candidate_summary(item) for item in identity_candidates],
            },
        )

    viable: list[dict[str, Any]] = []
    candidate_summaries: list[dict[str, Any]] = []
    for lot in identity_candidates:
        reject_reasons: list[str] = []
        if int(lot.get("shares_remaining") or 0) < int(shares or 0):
            reject_reasons.append("insufficient_shares_remaining")
        try:
            opened_at_ms = int(lot.get("opened_at_ms") or 0)
        except Exception:
            opened_at_ms = 0
        if opened_at_ms <= 0 or int(trade_time_ms or 0) < opened_at_ms:
            reject_reasons.append("trade_time_before_lot_open")
        candidate_summaries.append(_assigned_stock_candidate_summary(lot, reject_reasons=reject_reasons))
        if not reject_reasons:
            viable.append(lot)

    diagnostics = {
        "selector": selector,
        "candidate_count": len(identity_candidates),
        "candidates": candidate_summaries,
        "shares": shares,
        "price": price,
        "trade_time_ms": trade_time_ms,
    }
    if not viable:
        raise BrokerAssignedStockSaleMatchError(
            "no_safe_match",
            "assigned stock sale has candidate lots but no safe unique match",
            diagnostics=diagnostics,
        )
    if len(viable) > 1:
        raise BrokerAssignedStockSaleMatchError(
            "ambiguous_match",
            "assigned stock sale matched multiple open assigned stock lots",
            diagnostics=diagnostics | {"viable_count": len(viable)},
        )

    lot = viable[0]
    fees, fee_source = _stock_sale_deal_fee(deal)
    return {
        "lot": lot,
        "existing_events": existing_events,
        "before_report": before_report,
        "selector": selector,
        "shares": int(shares or 0),
        "price": float(price or 0.0),
        "fees": float(fees or 0.0),
        "fee_source": fee_source,
        "trade_time_ms": int(trade_time_ms or 0),
        "source_deal_id": source_deal_id,
        "diagnostics": diagnostics | {"matched_stock_lot_id": lot.get("stock_lot_id"), "fee_source": fee_source},
    }


@dataclass(frozen=True)
class ManualCloseResolvedMatch:
    record_id: str
    rule: str
    selector: dict[str, Any]
    candidate: dict[str, Any]
    close_target_resolution: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ManualCloseMatchError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        selector: dict[str, Any],
        candidates: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.selector = dict(selector)
        self.candidates = list(candidates or [])


def resolve_manual_close_record_id(
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
) -> ManualCloseResolvedMatch:
    selector_payload = {
        "broker": broker,
        "account": account,
        "symbol": symbol,
        "option_type": option_type,
        "side": position_side,
        "strike": strike,
        "expiration_ymd": expiration_ymd,
        "contracts_to_close": contracts_to_close,
    }
    missing = [
        key
        for key in ("broker", "account", "symbol", "option_type", "side", "strike", "expiration_ymd")
        if selector_payload.get(key) in (None, "")
    ]
    if missing:
        raise ManualCloseMatchError(
            "missing_selectors",
            "manual close auto matching requires " + ",".join(missing),
            selector=selector_payload,
        )
    if int(contracts_to_close) <= 0:
        raise ManualCloseMatchError(
            "invalid_quantity",
            "contracts_to_close must be > 0",
            selector=selector_payload,
        )

    try:
        resolution = resolve_manual_position_close_target(
            repo,
            broker=broker,
            account=account,
            symbol=symbol,
            option_type=option_type,
            position_side=position_side,
            strike=strike,
            expiration_ymd=expiration_ymd,
            contracts_to_close=contracts_to_close,
        )
    except LotCloseResolutionError as exc:
        selector_payload = exc.selector.to_dict()
        candidates = [item.to_dict() for item in exc.candidates]
        messages = {
            "not_found": "no open lot matches the manual close selector",
            "insufficient_contracts": "matching lots do not have enough open contracts",
            "multiple_matches": "multiple open lots match the manual close selector; specify record_id",
        }
        raise ManualCloseMatchError(
            exc.code,
            messages.get(exc.code, str(exc)),
            selector=selector_payload,
            candidates=candidates,
        ) from exc

    match = resolution.single_match
    return ManualCloseResolvedMatch(
        record_id=match.record_id,
        rule=match.matched_by,
        selector=resolution.selector,
        candidate=match.candidate.to_dict() if match.candidate is not None else {},
        close_target_resolution=resolution.to_dict(),
    )


def format_manual_close_match_error(error: ManualCloseMatchError) -> str:
    selector = error.selector
    selector_text = (
        f"broker={selector.get('broker') or '-'} account={selector.get('account') or '-'} "
        f"symbol={selector.get('symbol') or '-'} side={selector.get('side') or '-'} "
        f"option_type={selector.get('option_type') or '-'} exp={selector.get('expiration_ymd') or '-'} "
        f"strike={selector.get('strike') if selector.get('strike') is not None else '-'} "
        f"qty={selector.get('contracts_to_close') or '-'}"
    )
    lines = [f"[MATCH_FAIL] {error.code}: {error}", f"selector: {selector_text}"]
    if error.candidates:
        lines.append("candidates:")
        for row in error.candidates[:10]:
            lines.append(
                f"- {row.get('record_id')} | {row.get('account')} | {row.get('symbol')} | "
                f"{row.get('side')} {row.get('option_type')} | exp {row.get('expiration_ymd') or '-'} | "
                f"strike {row.get('strike') if row.get('strike') is not None else '-'} | "
                f"remaining {row.get('contracts_open')} | opened_at {row.get('opened_at') or '-'}"
            )
        if len(error.candidates) > 10:
            lines.append(f"... {len(error.candidates) - 10} more candidates")
    lines.append("hint: specify --record-id, or narrow account/symbol/exp/strike/side.")
    return "\n".join(lines)


def execute_manual_open(
    repo: Any | None,
    *,
    broker: str,
    account: str,
    symbol: str,
    option_type: str,
    side: str,
    contracts: int,
    currency: str | None,
    strike: float | None,
    multiplier: float | None,
    expiration_ymd: str | None,
    premium_per_share: float | None,
    underlying_share_locked: int | None,
    note: str | None,
    dry_run: bool,
    opened_at_ms: int | None = None,
    strategy_snapshot: dict[str, Any] | None = None,
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
        opened_at_ms=opened_at_ms,
        strategy_snapshot=(dict(strategy_snapshot) if isinstance(strategy_snapshot, dict) else None),
    )
    if dry_run:
        return {"mode": "dry_run", **preview_manual_position_open(repo, command).to_payload()}
    if repo is None:
        raise ValueError("repo is required when dry_run is false")
    payload = record_manual_position_open(repo, command).to_payload()
    result = payload["result"]
    fields = payload["fields"]
    payload_command = payload.get("command")
    command = payload_command if isinstance(payload_command, OpenPositionCommand) else command
    record_id = _manual_open_record_id(result)
    return _apply_result_payload(
        repo,
        record_id=record_id,
        result=result,
        payload=payload,
        native_event={
            "event_id": result.get("event_id"),
            "event_kind": "open_trade",
            "event_at_utc": _ms_to_iso(command.opened_at_ms),
            "source_name": "cli_manual_open",
            "source_type": "manual_trade_event",
            "broker": broker,
            "account": account,
            "symbol": symbol,
            "option_type": option_type,
            "side": side,
            "strike": fields.get("strike"),
            "expiration_ymd": effective_expiration_ymd(fields) or expiration_ymd,
            "currency": fields.get("currency"),
            "multiplier": fields.get("multiplier"),
            "contracts": int(contracts),
            "snapshot_lot_id": record_id or None,
        },
    )


def execute_manual_close(
    repo: Any,
    *,
    record_id: str | None = None,
    contracts_to_close: int,
    close_price: float | None,
    close_reason: str,
    dry_run: bool,
    broker: str = "富途",
    account: str | None = None,
    symbol: str | None = None,
    option_type: str | None = None,
    position_side: str | None = None,
    strike: float | None = None,
    expiration_ymd: str | None = None,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    resolved_record_id = str(record_id or "").strip()
    match_info: dict[str, Any] = {"rule": "explicit_record_id", "record_id": resolved_record_id}
    if not resolved_record_id:
        resolved_match = resolve_manual_close_record_id(
            repo,
            broker=broker,
            account=account,
            symbol=symbol,
            option_type=option_type,
            position_side=position_side,
            strike=strike,
            expiration_ymd=expiration_ymd,
            contracts_to_close=int(contracts_to_close),
        )
        resolved_record_id = resolved_match.record_id
        match_info = resolved_match.to_dict()

    if dry_run:
        return {
            "mode": "dry_run",
            "match": match_info,
            **preview_manual_position_close(
                repo,
                record_id=resolved_record_id,
                contracts_to_close=int(contracts_to_close),
                close_price=close_price,
                close_reason=close_reason,
                as_of_ms=as_of_ms,
            ).to_payload(),
        }
    close_payload = record_manual_position_close(
        repo,
        record_id=resolved_record_id,
        contracts_to_close=int(contracts_to_close),
        close_price=close_price,
        close_reason=close_reason,
        as_of_ms=as_of_ms,
    ).to_payload()
    result = close_payload["result"]
    if "close_target_resolution" in close_payload and "close_target_resolution" not in match_info:
        match_info["close_target_resolution"] = close_payload["close_target_resolution"]
    payload = close_payload | {"match": match_info}
    ledger_preflight = close_payload["ledger_preflight"]
    is_duplicate = result.get("created") is False
    return _apply_result_payload(
        repo,
        record_id=resolved_record_id,
        result=result,
        payload=payload,
        native_event=None if is_duplicate else {
            "event_id": result.get("event_id"),
            "event_kind": "close_trade",
            "event_at_utc": _ms_to_iso(int(ledger_preflight["event_time_ms"])),
            "source_name": "cli_manual_close",
            "source_type": "manual_trade_event",
            "broker": close_payload["fields"].get("broker"),
            "account": close_payload["fields"].get("account"),
            "symbol": close_payload["fields"].get("symbol"),
            "option_type": close_payload["fields"].get("option_type"),
            "side": close_payload["fields"].get("side"),
            "strike": close_payload["fields"].get("strike"),
            "expiration_ymd": effective_expiration_ymd(close_payload["fields"]),
            "currency": close_payload["fields"].get("currency"),
            "multiplier": close_payload["fields"].get("multiplier"),
            "contracts": int(contracts_to_close),
            "snapshot_lot_id": resolved_record_id,
        },
    )


def execute_manual_assignment(
    repo: Any,
    *,
    record_id: str | None = None,
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
    dry_run: bool,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    kwargs = {
        "record_id": record_id,
        "broker": broker,
        "account": account,
        "symbol": symbol,
        "option_type": option_type,
        "position_side": position_side,
        "strike": strike,
        "expiration_ymd": expiration_ymd,
        "contracts_to_close": int(contracts_to_close),
        "stock_side": stock_side,
        "stock_qty": int(stock_qty),
        "stock_price": float(stock_price),
        "as_of_ms": as_of_ms,
    }
    if dry_run:
        return preview_manual_assignment(repo, **kwargs)
    out = record_manual_assignment(repo, **kwargs)
    result = out.get("result") if isinstance(out.get("result"), dict) else {}
    return _apply_result_payload(
        repo,
        record_id=record_id or "",
        result=result,
        payload=out,
        native_event=None,
    )


def execute_manual_exercise(
    repo: Any,
    *,
    record_id: str | None = None,
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
    dry_run: bool,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    kwargs = {
        "record_id": record_id,
        "broker": broker,
        "account": account,
        "symbol": symbol,
        "option_type": option_type,
        "position_side": position_side,
        "strike": strike,
        "expiration_ymd": expiration_ymd,
        "contracts_to_close": int(contracts_to_close),
        "stock_side": stock_side,
        "stock_qty": int(stock_qty),
        "stock_price": float(stock_price),
        "as_of_ms": as_of_ms,
    }
    if dry_run:
        return preview_manual_exercise(repo, **kwargs)
    out = record_manual_exercise(repo, **kwargs)
    result = out.get("result") if isinstance(out.get("result"), dict) else {}
    return _apply_result_payload(
        repo,
        record_id=record_id or "",
        result=result,
        payload=out,
        native_event=None,
    )


def _execute_assigned_stock_sale(
    repo: Any,
    *,
    target_stock_lot_id: str,
    shares: int,
    price: float,
    fees: float = 0.0,
    trade_time_ms: int,
    account: str | None = None,
    broker: str | None = None,
    symbol: str | None = None,
    currency: str | None = None,
    source_deal_id: str | None = None,
    source: str,
    existing_events: list[dict[str, Any]] | None = None,
    before_report: dict[str, Any] | None = None,
    match_diagnostics: dict[str, Any] | None = None,
    dry_run: bool,
) -> dict[str, Any]:
    stock_lot_id = str(target_stock_lot_id or "").strip()
    if not stock_lot_id:
        raise ValueError("assigned stock sale requires target_stock_lot_id")
    if int(shares or 0) <= 0:
        raise ValueError("assigned stock sale requires shares > 0")
    if float(price) < 0:
        raise ValueError("assigned stock sale requires price >= 0")
    if float(fees or 0.0) < 0:
        raise ValueError("assigned stock sale requires fees >= 0")
    if int(trade_time_ms or 0) <= 0:
        raise ValueError("assigned stock sale requires trade_time_ms > 0")

    existing_events = list(existing_events if existing_events is not None else _list_repo_assigned_stock_events(repo))
    before_report = (
        dict(before_report)
        if isinstance(before_report, dict)
        else _assigned_stock_report(repo, account=account, broker=broker, assigned_stock_events=existing_events)
    )
    before_lot = _find_assigned_stock_lot(before_report, stock_lot_id)
    if before_lot is None:
        raise ValueError(f"assigned stock lot not found: {stock_lot_id}")
    sale_event = _build_assigned_stock_sale_event(
        before_lot,
        target_stock_lot_id=stock_lot_id,
        shares=int(shares),
        price=float(price),
        fees=float(fees or 0.0),
        trade_time_ms=int(trade_time_ms),
        account=account,
        broker=broker,
        symbol=symbol,
        currency=currency,
        source_deal_id=source_deal_id,
        source=source,
    )
    existing_same = next(
        (
            event
            for event in existing_events
            if isinstance(event, dict)
            and str(event.get("stock_event_id") or event.get("event_id") or "") == str(sale_event.get("stock_event_id") or "")
        ),
        None,
    )
    if existing_same is not None:
        existing_json = json.dumps(dict(existing_same), ensure_ascii=False, sort_keys=True)
        candidate_json = json.dumps(dict(sale_event), ensure_ascii=False, sort_keys=True)
        if existing_json != candidate_json:
            raise ValueError(f"assigned stock sale conflict for stock_event_id={sale_event.get('stock_event_id')}")
        after_events = list(existing_events)
    else:
        after_events = [*existing_events, sale_event]
    after_report = _assigned_stock_report(repo, account=account, broker=broker, assigned_stock_events=after_events)
    stock_event_id = str(sale_event.get("stock_event_id") or "")
    candidate_reviews = [
        row
        for row in (after_report.get("assigned_stock_review_rows") or [])
        if isinstance(row, dict) and str(row.get("stock_event_id") or "") == stock_event_id
    ]
    if candidate_reviews:
        status = str(candidate_reviews[0].get("status") or "manual_review_required")
        raise ValueError(f"assigned stock sale validation failed: {status}")
    after_lot = _find_assigned_stock_lot(after_report, stock_lot_id)
    payload = {
        "mode": "dry_run",
        "write_model": "assigned_stock_events",
        "sale_event": sale_event,
        "stock_lot_before": before_lot,
        "stock_lot_after": after_lot,
        "review_rows": candidate_reviews,
        "match": dict(match_diagnostics or {}),
    }
    if dry_run:
        return payload
    upsert = getattr(repo, "upsert_assigned_stock_event", None)
    if not callable(upsert):
        raise ValueError("repo does not support assigned stock event writes")
    created = bool(upsert(sale_event))
    result = {"stock_event_id": stock_event_id, "created": created}
    return _apply_result_payload(
        repo,
        record_id=stock_lot_id,
        result=result,
        payload=payload | {"mode": "applied", "result": result, "idempotent_duplicate": not created},
        native_event=None,
    )


def execute_manual_assigned_stock_sale(
    repo: Any,
    *,
    target_stock_lot_id: str,
    shares: int,
    price: float,
    fees: float = 0.0,
    trade_time_ms: int,
    account: str | None = None,
    broker: str | None = None,
    symbol: str | None = None,
    currency: str | None = None,
    source_deal_id: str | None = None,
    dry_run: bool,
) -> dict[str, Any]:
    return _execute_assigned_stock_sale(
        repo,
        target_stock_lot_id=target_stock_lot_id,
        shares=shares,
        price=price,
        fees=fees,
        trade_time_ms=trade_time_ms,
        account=account,
        broker=broker,
        symbol=symbol,
        currency=currency,
        source_deal_id=source_deal_id,
        source="manual",
        dry_run=dry_run,
    )


def execute_broker_assigned_stock_sale(
    repo: Any,
    deal: Any,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    if getattr(deal, "option_type", None):
        raise BrokerAssignedStockSaleMatchError(
            "unsupported_deal",
            "assigned stock broker sale intake only handles stock deals",
            diagnostics={"option_type": getattr(deal, "option_type", None)},
        )
    if str(getattr(deal, "side", "") or "").strip().lower() != "sell":
        raise BrokerAssignedStockSaleMatchError(
            "unsupported_deal",
            "assigned stock broker sale intake only handles stock sell deals",
            diagnostics={"side": getattr(deal, "side", None)},
        )
    match = _broker_assigned_stock_sale_match(repo, deal)
    lot = dict(match["lot"])
    return _execute_assigned_stock_sale(
        repo,
        target_stock_lot_id=str(lot.get("stock_lot_id") or ""),
        shares=int(match["shares"]),
        price=float(match["price"]),
        fees=float(match["fees"]),
        trade_time_ms=int(match["trade_time_ms"]),
        account=getattr(deal, "internal_account", None),
        broker=getattr(deal, "broker", None),
        symbol=getattr(deal, "symbol", None),
        currency=getattr(deal, "currency", None),
        source_deal_id=str(match["source_deal_id"]),
        source="broker",
        existing_events=list(match.get("existing_events") or []),
        before_report=dict(match.get("before_report") or {}),
        match_diagnostics=dict(match.get("diagnostics") or {}),
        dry_run=dry_run,
    )


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
    strategy: str | None = None,
    leg_role: str | None = None,
    strategy_group_id: str | None = None,
    yield_enhancement_mode: str | None = None,
    strategy_snapshot: dict[str, Any] | None = None,
    dry_run: bool,
) -> dict[str, Any]:
    if dry_run:
        return {
            "mode": "dry_run",
            **preview_manual_position_adjust(
                repo,
                record_id=record_id,
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
            ).to_payload(),
        }
    adjust_payload = record_manual_position_adjust(
        repo,
        record_id=record_id,
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
    ).to_payload()
    result = adjust_payload["result"]
    fields = adjust_payload["fields"]
    patch = adjust_payload["patch"]
    raw_target_contracts = patch.get("contracts_open")
    if raw_target_contracts is None:
        raw_target_contracts = fields.get("contracts_open") or fields.get("contracts") or 0
    return _apply_result_payload(
        repo,
        record_id=record_id,
        result=result,
        payload=adjust_payload,
        native_event={
            "event_id": result.get("event_id"),
            "event_kind": "manual_adjustment",
            "event_at_utc": _ms_to_iso(int(adjust_payload["ledger_preflight"]["event_time_ms"])),
            "source_name": "cli_manual_adjust",
            "source_type": "manual_trade_event",
            "broker": fields.get("broker"),
            "account": fields.get("account"),
            "symbol": fields.get("symbol"),
            "option_type": fields.get("option_type"),
            "side": fields.get("side"),
            "strike": patch.get("strike", fields.get("strike")),
            "expiration_ymd": expiration_ymd or effective_expiration_ymd(fields),
            "currency": fields.get("currency"),
            "multiplier": patch.get("multiplier", fields.get("multiplier")),
            "target_contracts": int(raw_target_contracts or 0),
            "snapshot_lot_id": record_id,
        },
    )
