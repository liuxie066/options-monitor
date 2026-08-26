from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from domain.domain.ledger import (
    ContractKey,
    OptionEconomicAllocation,
    PositionLot,
    TradeEvent,
    fee_fact_for_event,
    fee_fact_from_persisted_evidence,
)
from domain.domain.ledger.events import validate_trade_event
from domain.domain.ledger.position_fields import (
    POSITION_LOT_STRATEGY_PATCH_FIELDS,
    strategy_metadata_fields_from_payload,
)
from domain.domain.option_position_identity import (
    BUY_TO_CLOSE,
    EXPIRE_AUTO_CLOSE,
    norm_symbol,
    normalize_account,
    normalize_broker,
    normalize_currency,
    normalize_option_type,
)
from domain.domain.trade_contract_identity import normalize_trade_side
from domain.domain.performance.models import (
    FeeComponent,
    OptionInstrumentKey,
    ValuationMarkFact,
    quantize_money,
    select_valuation_mark,
)


def safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _positive_integral(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite() or number <= 0 or number != number.to_integral_value():
        return None
    return int(number)


def _positive_integer(value: Any) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else None
    )


def _nonnegative_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() and number >= 0 else None


_DAY_MS = 86_400_000

def parse_event_at_ms(value: Any) -> int | None:
    if value in (None, "", 0):
        return None
    try:
        return int(float(value))
    except Exception:
        pass
    try:
        s = str(value).strip()
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.astimezone(timezone.utc).timestamp() * 1000)
    except Exception:
        return None

def month_from_ms(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m")

def _round_money(value: float | int | None) -> float:
    return round(float(value or 0.0), 6)

def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("raw_payload")
    return payload if isinstance(payload, dict) else {}


def _event_strategy_metadata(event: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(event, dict):
        return {}
    metadata = strategy_metadata_fields_from_payload(_event_payload(event))
    for key in ("strategy", "leg_role", "strategy_group_id", "yield_enhancement_mode"):
        if key not in metadata and event.get(key) not in (None, ""):
            metadata[key] = str(event.get(key)).strip()
    return metadata

def _event_ts(event: dict[str, Any]) -> int | None:
    return parse_event_at_ms(event.get("trade_time_ms"))

def _event_month(event: dict[str, Any]) -> str | None:
    ts = _event_ts(event)
    return month_from_ms(ts) if ts is not None else None

def _event_position_side(event: dict[str, Any]) -> str | None:
    side = str(event.get("side") or "").strip().lower()
    effect = str(event.get("position_effect") or "").strip().lower()
    if effect == "open":
        if side == "sell":
            return "short"
        if side == "buy":
            return "long"
    if effect == "close":
        if side == "buy":
            return "short"
        if side == "sell":
            return "long"
    return None

def _is_expire_close_event(event: dict[str, Any]) -> bool:
    payload = _event_payload(event)
    tokens = {
        str(event.get("event_type") or "").strip().lower(),
        str(payload.get("mode") or "").strip().lower(),
        str(payload.get("close_type") or "").strip().lower(),
        str(payload.get("close_reason") or "").strip().lower(),
        str(event.get("source_name") or "").strip().lower(),
    }
    return EXPIRE_AUTO_CLOSE in tokens or "expired" in tokens or "auto_close_expired_positions" in tokens

def _event_close_type(event: dict[str, Any]) -> str:
    if _is_expire_close_event(event):
        return EXPIRE_AUTO_CLOSE
    payload = _event_payload(event)
    for value in (
        event.get("event_type"),
        payload.get("close_type"),
        payload.get("close_reason"),
        payload.get("mode"),
    ):
        token = str(value or "").strip().lower()
        if token in {"assignment", "exercise"}:
            return token
        if token in {BUY_TO_CLOSE, "sell_to_close"}:
            return token
    return ""

def _event_stock_settlement(event: dict[str, Any]) -> dict[str, Any]:
    payload = _event_payload(event)
    raw = payload.get("stock_settlement")
    return raw if isinstance(raw, dict) else {}


def _settlement_stock_side(
    close_type: str,
    option_type: str,
    position_side: str,
) -> str | None:
    return {
        ("assignment", "put", "short"): "buy",
        ("assignment", "call", "short"): "sell",
        ("exercise", "call", "long"): "buy",
        ("exercise", "put", "long"): "sell",
    }.get((close_type, option_type, position_side))

def _voided_event_ids(events: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for event in events:
        target = _valid_void_target_event_id(event)
        if target:
            out.add(target)
    return out

def _valid_void_target_event_id(event: dict[str, Any]) -> str | None:
    if str(event.get("event_type") or "").strip().lower() != "void":
        return None
    target = str(event.get("target_event_id") or "").strip()
    if not target:
        return None
    raw_contract_key = event.get("contract_key")
    if not isinstance(raw_contract_key, dict) or event.get("event_time_ms") in (None, ""):
        return None
    try:
        decoded = TradeEvent(
            event_id=str(event.get("event_id") or "").strip(),
            event_type="void",
            event_time_ms=int(event.get("event_time_ms") or 0),
            contract_key=ContractKey.from_values(
                broker=raw_contract_key.get("broker"),
                account=raw_contract_key.get("account"),
                underlying_symbol=raw_contract_key.get("underlying_symbol") or raw_contract_key.get("symbol"),
                option_type=raw_contract_key.get("option_type"),
                position_side=raw_contract_key.get("position_side") or raw_contract_key.get("side"),
                strike=raw_contract_key.get("strike"),
                expiration_ymd=raw_contract_key.get("expiration_ymd") or raw_contract_key.get("expiration"),
            ),
            contracts=int(event.get("contracts") or 0),
            price=float(event.get("price") or 0.0),
            currency=str(event.get("currency") or ""),
            source=str(event.get("source") or event.get("source_name") or ""),
            multiplier=float(event.get("multiplier") or 0.0),
            fees=float(event.get("fees") or 0.0),
            target_event_id=target,
            raw_payload=dict(event.get("raw_payload") or {}),
        )
    except Exception:
        return None
    if any(item.severity == "error" for item in validate_trade_event(decoded)):
        return None
    return target

def _active_trade_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    voided = _voided_event_ids(events)
    out: list[dict[str, Any]] = []
    for event in events:
        event_id = str(event.get("event_id") or "").strip()
        if str(event.get("position_effect") or "").strip().lower() == "void":
            continue
        if event_id and event_id in voided:
            continue
        out.append(dict(event))
    return sorted(out, key=lambda x: (int(_event_ts(x) or 0), str(x.get("event_id") or "")))

def _passes_report_filter(event: dict[str, Any], account_norm: str | None, broker_norm: str | None) -> bool:
    if account_norm and normalize_account(event.get("account")) != account_norm:
        return False
    if broker_norm and normalize_broker(event.get("broker")) != broker_norm:
        return False
    return True

def _assigned_stock_lot_id(event_id: str) -> str:
    stable = str(event_id or "").strip()
    return f"assigned-stock-{stable}" if stable else "assigned-stock-unknown"

def _stock_event_id(event: dict[str, Any], *, fallback_index: int) -> str:
    for key in ("stock_event_id", "event_id", "source_deal_id", "deal_id"):
        value = str(event.get(key) or "").strip()
        if value:
            return value
    return f"assigned-stock-event-{fallback_index}"

def _stock_event_type(event: dict[str, Any]) -> str:
    return str(event.get("event_type") or event.get("type") or "").strip().lower()

def _stock_event_time_ms(event: dict[str, Any]) -> int | None:
    for key in ("trade_time_ms", "event_time_ms", "time_ms", "trade_time", "event_time"):
        ts = parse_event_at_ms(event.get(key))
        if ts is not None:
            return ts
    return None

def _stock_event_month(event: dict[str, Any]) -> str | None:
    ts = _stock_event_time_ms(event)
    return month_from_ms(ts) if ts is not None else None

def _stock_event_shares(event: dict[str, Any]) -> int:
    raw = event.get("shares") if event.get("shares") not in (None, "") else event.get("quantity")
    try:
        return int(abs(float(raw or 0)))
    except Exception:
        return 0


def _stock_event_price(event: dict[str, Any]) -> float | None:
    return safe_float(event.get("price") if event.get("price") not in (None, "") else event.get("avg_price"))

def _source_option_open_event_id(event: dict[str, Any], option_rows: list[dict[str, Any]]) -> str | None:
    open_ids = sorted({str(row.get("open_event_id") or "").strip() for row in option_rows if row.get("open_event_id")})
    if len(open_ids) == 1:
        return open_ids[0]
    payload = _event_payload(event)
    value = str(payload.get("close_target_source_event_id") or "").strip()
    return value or None

def _option_premium_attribution(option_rows: list[dict[str, Any]]) -> float:
    total = 0.0
    for row in option_rows:
        total += float(row.get("realized_pnl_gross") or 0.0)
    return _round_money(total)

def _quote_symbol(row: dict[str, Any]) -> str:
    return norm_symbol(row.get("symbol") or row.get("underlying_symbol") or "")

def _quote_time_ms(row: dict[str, Any]) -> int | None:
    for key in ("spot_time_ms", "quote_time_ms", "time_ms", "spot_time", "quote_time", "as_of_ms", "as_of"):
        ts = parse_event_at_ms(row.get(key))
        if ts is not None:
            return ts
    return None

def _quote_spot(row: dict[str, Any]) -> float | None:
    for key in ("spot", "last_price", "price", "underlying_price", "mark"):
        value = safe_float(row.get(key))
        if value is not None and value > 0:
            return float(value)
    return None

def _matching_quote(
    quote_snapshots: list[dict[str, Any]],
    lot: dict[str, Any],
    *,
    as_of_ms: int | None,
) -> dict[str, Any] | None:
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    lot_symbol = norm_symbol(lot.get("symbol") or "")
    lot_account = normalize_account(lot.get("account"))
    lot_broker = normalize_broker(lot.get("broker"))
    for idx, quote in enumerate(quote_snapshots):
        if _quote_symbol(quote) != lot_symbol:
            continue
        quote_account = normalize_account(quote.get("account")) if quote.get("account") not in (None, "") else None
        quote_broker = normalize_broker(quote.get("broker")) if quote.get("broker") not in (None, "") else None
        if quote_account and lot_account and quote_account != lot_account:
            continue
        if quote_broker and lot_broker and quote_broker != lot_broker:
            continue
        quote_time = _quote_time_ms(quote)
        if as_of_ms is not None and quote_time is None:
            continue
        if as_of_ms is not None and quote_time is not None and quote_time > int(as_of_ms):
            continue
        sort_time = int(quote_time or 0)
        candidates.append((sort_time, idx, quote))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item[0], item[1]))[-1][2]

def _normalize_quote_snapshots(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        rows: list[dict[str, Any]] = []
        for key, item in value.items():
            if isinstance(item, dict):
                row = dict(item)
                row.setdefault("symbol", key)
                rows.append(row)
            else:
                rows.append({"symbol": key, "spot": item})
        return rows
    return []

def _normalize_assigned_stock_events(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]

def _market_date(ms: int | None, *, symbol: str, currency: str) -> str | None:
    if ms is None or int(ms) <= 0:
        return None
    tz_name = "Asia/Hong_Kong" if str(symbol).endswith(".HK") or currency == "HKD" else "America/New_York"
    return datetime.fromtimestamp(int(ms) / 1000, tz=ZoneInfo(tz_name)).date().isoformat()

def _elapsed_days(start_ms: int | None, end_ms: int | None) -> float | None:
    if start_ms is None or end_ms is None or int(end_ms) < int(start_ms):
        return None
    return round((int(end_ms) - int(start_ms)) / _DAY_MS, 6)

def _option_fee_fact(event: dict[str, Any], *, component: str) -> dict[str, Any]:
    try:
        fact = fee_fact_for_event(TradeEvent.from_dict(event))
    except (TypeError, ValueError):
        return {
            "component": component,
            "basis": "missing",
            "amount": 0.0,
            "reason": "canonical_option_fee_evidence_unavailable",
        }
    return {
        "component": component,
        "basis": fact.basis.value,
        "amount": _round_money(float(fact.amount)) if fact.amount is not None else 0.0,
        "source": fact.source,
        "reason": fact.reason,
    }

def assigned_stock_fee_fact(
    value: dict[str, Any],
    *,
    component: str,
    transaction_kind: str,
) -> dict[str, Any]:
    del transaction_kind
    event_id = str(value.get("stock_event_id") or value.get("event_id") or component).strip()
    provenance = value.get("fee_provenance")
    compatibility_amount = value.get("fees") if value.get("fees") not in (None, "") else value.get("fee", 0)
    fee_component = FeeComponent.STOCK_SALE if "sale" in component else FeeComponent.STOCK_SETTLEMENT
    try:
        fact = fee_fact_from_persisted_evidence(
            event_id=event_id,
            component=fee_component,
            provenance=provenance,
            compatibility_amount=compatibility_amount,
        )
    except (TypeError, ValueError):
        fact = None
    return {
        "component": component,
        "basis": fact.basis.value if fact is not None else "missing",
        "amount": _round_money(float(fact.amount)) if fact is not None and fact.amount is not None else 0.0,
        "source": fact.source if fact is not None else None,
        "reason": fact.reason if fact is not None else "persisted_stock_fee_evidence_invalid",
    }


_stock_fee_fact = assigned_stock_fee_fact


def _scale_fee_fact(fact: dict[str, Any], ratio: float) -> dict[str, Any]:
    return {**fact, "amount": _round_money(float(fact.get("amount") or 0.0) * max(0.0, ratio))}

def _summarize_fee_facts(facts: list[dict[str, Any]]) -> dict[str, Any]:
    actual = _round_money(sum(float(fact.get("amount") or 0.0) for fact in facts if fact.get("basis") == "actual"))
    estimated = _round_money(
        sum(float(fact.get("amount") or 0.0) for fact in facts if fact.get("basis") == "estimated")
    )
    missing = sorted({str(fact.get("component") or "unknown") for fact in facts if fact.get("basis") == "missing"})
    bases = {str(fact.get("basis") or "") for fact in facts if fact.get("basis") != "missing"}
    if missing and not bases:
        basis = "missing"
    elif missing or len(bases) > 1:
        basis = "mixed"
    elif bases:
        basis = next(iter(bases))
    else:
        basis = "missing"
    return {
        "actual_fees": actual,
        "estimated_fees": estimated,
        "fees_used": _round_money(actual + estimated),
        "fee_basis": basis,
        "fee_missing_components": missing,
        "fee_evidence": [
            {key: value for key, value in fact.items() if value not in (None, "", [])}
            for fact in facts
        ],
    }

def _explicit_stock_lot_id(event: dict[str, Any]) -> str | None:
    payload = _event_payload(event)
    for source in (event, payload):
        for key in ("stock_lot_id", "target_stock_lot_id", "source_stock_lot_id"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return None

def _lot_shares_at(lot: dict[str, Any], at_ms: int) -> int:
    shares = int(lot.get("shares_opened") or 0)
    for sale in lot.get("_sale_rows") or []:
        if int(sale.get("event_at") or 0) <= at_ms:
            shares -= int(sale.get("shares") or 0)
    return max(0, shares)

def _active_reserved_shares(
    reservations: dict[str, list[tuple[int, int, int]]],
    stock_lot_id: str,
    at_ms: int,
) -> int:
    return sum(shares for start, end, shares in reservations.get(stock_lot_id, []) if start <= at_ms < end)


def _minimum_available_shares(
    lot: dict[str, Any],
    reservations: dict[str, list[tuple[int, int, int]]],
    *,
    stock_lot_id: str,
    start_ms: int,
    end_ms: int,
) -> int:
    checkpoints = {int(start_ms)}
    for sale in lot.get("_sale_rows") or []:
        sale_at = int(sale.get("event_at") or 0)
        if start_ms <= sale_at < end_ms:
            checkpoints.add(sale_at)
    for reserved_start, reserved_end, _shares in reservations.get(stock_lot_id, []):
        if start_ms <= reserved_start < end_ms:
            checkpoints.add(reserved_start)
        if start_ms < reserved_end < end_ms:
            checkpoints.add(reserved_end)
    return min(
        _lot_shares_at(lot, checkpoint)
        - _active_reserved_shares(reservations, stock_lot_id, checkpoint)
        for checkpoint in checkpoints
    )

def _attribute_covered_calls(
    lots_by_id: dict[str, dict[str, Any]],
    *,
    trade_events: list[dict[str, Any]],
    option_open_lots: list[dict[str, Any]],
    assignment_option_rows: list[dict[str, Any]],
    as_of_ms: int,
    review_rows: list[dict[str, Any]],
    allocation_rows: list[dict[str, Any]],
) -> None:
    event_by_id = {
        str(event.get("event_id") or "").strip(): event
        for event in trade_events
        if str(event.get("event_id") or "").strip()
    }
    realized_by_open: dict[str, list[dict[str, Any]]] = {}
    for row in assignment_option_rows:
        open_id = str(row.get("open_event_id") or "").strip()
        if open_id:
            realized_by_open.setdefault(open_id, []).append(row)
    reservations: dict[str, list[tuple[int, int, int]]] = {}

    calls = sorted(
        (
            lot
            for lot in option_open_lots
            if str(lot.get("position_side") or "").lower() == "short"
            and str(lot.get("option_type") or "").lower() == "call"
        ),
        key=lambda lot: (int(lot.get("opened_at") or 0), str(lot.get("open_event_id") or "")),
    )
    for call in calls:
        open_id = str(call.get("open_event_id") or "").strip()
        open_event = event_by_id.get(open_id)
        if open_event is None:
            continue
        opened_at = int(call.get("opened_at") or 0)
        contracts = int(call.get("contracts") or 0)
        multiplier = int(call.get("multiplier") or 0)
        required_shares = contracts * multiplier
        if opened_at <= 0 or required_shares <= 0:
            continue
        realized_rows = realized_by_open.get(open_id, [])
        realized_gross = _round_money(sum(float(row.get("realized_pnl_gross") or 0.0) for row in realized_rows))
        remaining = int(call.get("remaining") or 0)
        open_unrealized = safe_float(call.get("unrealized_pnl_gross")) if remaining > 0 else 0.0
        economics_complete = remaining == 0 or open_unrealized is not None
        gross_pnl = _round_money(realized_gross + float(open_unrealized or 0.0))
        closed_times = [int(row.get("closed_at") or 0) for row in realized_rows if int(row.get("closed_at") or 0) > 0]
        reservation_end = (
            max(closed_times)
            if remaining == 0 and closed_times
            else as_of_ms + 1
        )
        if reservation_end <= opened_at:
            reservation_end = max(as_of_ms, opened_at + 1)
        fee_facts = [_option_fee_fact(open_event, component="covered_call_open_option_fee")]
        for row in realized_rows:
            close_event = event_by_id.get(str(row.get("event_id") or ""))
            if close_event is None:
                fee_facts.append({"component": "covered_call_close_option_fee", "basis": "missing", "amount": 0.0})
                continue
            event_contracts = max(1, int(abs(float(close_event.get("contracts") or 0))))
            fee_facts.append(
                _scale_fee_fact(
                    _option_fee_fact(close_event, component="covered_call_close_option_fee"),
                    int(row.get("contracts_closed") or 0) / event_contracts,
                )
            )

        key = (str(call.get("account") or ""), str(call.get("broker") or ""), str(call.get("symbol") or ""))
        explicit_id = _explicit_stock_lot_id(open_event)
        candidates = [
            lot
            for lot in lots_by_id.values()
            if (str(lot.get("account") or ""), str(lot.get("broker") or ""), str(lot.get("symbol") or "")) == key
            and int(lot.get("assigned_at_ms") or 0) <= opened_at
        ]
        if not candidates and not explicit_id:
            continue
        group_id = str(
            call.get("strategy_group_id")
            or _event_strategy_metadata(open_event).get("strategy_group_id")
            or ""
        ).strip()
        linkage_basis = "stock_lot_id" if explicit_id else "strategy_group"
        if explicit_id:
            candidates = [lot for lot in candidates if str(lot.get("stock_lot_id") or "") == explicit_id]
        elif group_id:
            candidates = [
                lot
                for lot in candidates
                if str(lot.get("strategy_group_id") or "") == group_id
            ]
        else:
            review_rows.append(
                _assigned_stock_review_row(
                    status="covered_call_unallocated",
                    event_id=open_id,
                    account=key[0],
                    broker=key[1],
                    symbol=key[2],
                    message="covered call is missing assigned-stock linkage identity",
                    details={"required_shares": required_shares},
                )
            )
            continue
        candidates.sort(key=lambda lot: (int(lot.get("assigned_at_ms") or 0), str(lot.get("stock_lot_id") or "")))
        if len(candidates) != 1:
            review_rows.append(
                _assigned_stock_review_row(
                    status="covered_call_unallocated",
                    event_id=open_id,
                    account=key[0],
                    broker=key[1],
                    symbol=key[2],
                    message="covered call assigned-stock linkage is not unique",
                    details={"candidate_count": len(candidates)},
                )
            )
            continue

        available: list[tuple[dict[str, Any], int]] = []
        for lot in candidates:
            lot_id = str(lot.get("stock_lot_id") or "")
            shares = _minimum_available_shares(
                lot,
                reservations,
                stock_lot_id=lot_id,
                start_ms=opened_at,
                end_ms=reservation_end,
            )
            if shares > 0:
                available.append((lot, shares))
        if sum(shares for _lot, shares in available) < required_shares:
            review_rows.append(
                _assigned_stock_review_row(
                    status="covered_call_unallocated",
                    event_id=open_id,
                    account=key[0],
                    broker=key[1],
                    symbol=key[2],
                    message="covered call cannot be attributed to sufficient assigned-stock shares",
                    details={"required_shares": required_shares, "explicit_stock_lot_id": explicit_id},
                )
            )
            continue

        remaining_shares = required_shares
        for lot, shares in available:
            if remaining_shares <= 0:
                break
            allocated = min(shares, remaining_shares)
            ratio = allocated / required_shares
            lot["_covered_call_pnl"] = _round_money(float(lot.get("_covered_call_pnl") or 0.0) + gross_pnl * ratio)
            lot["_covered_call_realized_pnl"] = _round_money(
                float(lot.get("_covered_call_realized_pnl") or 0.0) + realized_gross * ratio
            )
            lot["_covered_call_unrealized_pnl"] = _round_money(
                float(lot.get("_covered_call_unrealized_pnl") or 0.0) + float(open_unrealized or 0.0) * ratio
            )
            lot["_covered_call_fee_facts"].extend(_scale_fee_fact(fact, ratio) for fact in fee_facts)
            lot["_covered_call_statuses"].add("explicit")
            lot["_covered_call_complete"] = bool(lot.get("_covered_call_complete")) and economics_complete
            evidence_fact_id = str(call.get("valuation_evidence_fact_id") or "").strip()
            if evidence_fact_id:
                lot["_covered_call_evidence_fact_ids"].add(evidence_fact_id)
            lot_id = str(lot.get("stock_lot_id") or "")
            reservations.setdefault(lot_id, []).append((opened_at, reservation_end, allocated))
            allocation_rows.append(
                {
                    "open_event_id": open_id,
                    "stock_lot_id": lot_id,
                    "account": key[0],
                    "broker": key[1],
                    "symbol": key[2],
                    "currency": str(lot.get("currency") or ""),
                    "shares": allocated,
                    "start_at_ms": opened_at,
                    "end_at_ms": reservation_end,
                    "allocation_status": "explicit",
                    "linkage_basis": linkage_basis,
                }
            )
            remaining_shares -= allocated

        if remaining > 0 and open_unrealized is None:
            review_rows.append(
                _assigned_stock_review_row(
                    status="covered_call_unrealized_missing",
                    event_id=open_id,
                    account=key[0],
                    broker=key[1],
                    symbol=key[2],
                    message="open covered call has no usable as-of valuation mark",
                    details={"valuation_status": call.get("valuation_status")},
                )
            )

def _lifecycle_efficiency_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row.get("account") or ""),
            str(row.get("currency") or ""),
            str(row.get("lifecycle_quality") or "unclassified"),
        )
        bucket = buckets.setdefault(
            key,
            {"account": key[0], "currency": key[1], "lifecycle_quality": key[2], "lifecycle_count": 0, "lifecycle_pnl_net": 0.0, "capital_days": 0.0},
        )
        bucket["lifecycle_count"] += 1
        if row.get("lifecycle_pnl_net") is None:
            bucket["lifecycle_pnl_net"] = None
        elif bucket["lifecycle_pnl_net"] is not None:
            bucket["lifecycle_pnl_net"] += float(row["lifecycle_pnl_net"])
        if row.get("capital_days") is not None:
            bucket["capital_days"] += float(row["capital_days"])
    out: list[dict[str, Any]] = []
    for bucket in buckets.values():
        net = (
            _round_money(bucket["lifecycle_pnl_net"])
            if bucket["lifecycle_pnl_net"] is not None
            else None
        )
        capital_days = round(float(bucket["capital_days"]), 6)
        out.append(
            {
                **bucket,
                "lifecycle_pnl_net": net,
                "capital_days": capital_days,
                "annualized_capital_efficiency": (
                    round(net * 365 / capital_days, 8)
                    if net is not None and capital_days > 0
                    else None
                ),
            }
        )
    return sorted(out, key=lambda row: (row["account"], row["currency"], row["lifecycle_quality"]))

def _lot_basis_per_share_with_fees(lot: dict[str, Any]) -> float:
    shares_opened = int(lot.get("shares_opened") or 0)
    if shares_opened <= 0:
        return 0.0
    return float(lot.get("stock_cost_basis_total") or 0.0) / float(shares_opened)

def _assigned_stock_review_row(
    *,
    status: str,
    event_id: str | None = None,
    stock_lot_id: str | None = None,
    stock_event_id: str | None = None,
    month: str | None = None,
    account: str | None = None,
    broker: str | None = None,
    symbol: str | None = None,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "event_id": event_id,
        "stock_lot_id": stock_lot_id,
        "stock_event_id": stock_event_id,
        "month": month,
        "account": account,
        "broker": broker,
        "symbol": symbol,
        "message": message,
        "details": dict(details or {}),
    }

def _lifecycle_row_in_month(row: dict[str, Any], month: str | None) -> bool:
    if not month:
        return True
    if row.get("opened_month") == month or row.get("month") == month:
        return True
    sale_months = row.get("sale_months")
    return isinstance(sale_months, list) and month in sale_months

def _sale_row_in_month(row: dict[str, Any], month: str | None) -> bool:
    return not month or row.get("month") == month

def _review_row_in_month(row: dict[str, Any], month: str | None) -> bool:
    return not month or row.get("month") in (None, month)


def assigned_stock_trade_event_row(event: TradeEvent) -> dict[str, Any]:
    is_open = event.event_type == "open"
    is_close = event.event_type in {"close", "expire_close", "assignment", "exercise"}
    side = (
        "sell"
        if (is_open and event.contract_key.position_side == "short")
        or (is_close and event.contract_key.position_side == "long")
        else "buy"
    )
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "event_time_ms": event.event_time_ms,
        "trade_time_ms": event.event_time_ms,
        "target_event_id": event.target_event_id,
        "contract_key": event.contract_key.to_dict(),
        "broker": event.contract_key.broker,
        "account": event.contract_key.account,
        "symbol": event.contract_key.underlying_symbol,
        "option_type": event.contract_key.option_type,
        "side": side,
        "position_effect": (
            "open" if is_open else "close" if is_close else event.event_type
        ),
        "contracts": event.contracts,
        "price": event.price,
        "strike": event.contract_key.strike,
        "expiration_ymd": event.contract_key.expiration_ymd,
        "currency": event.currency,
        "source": event.source,
        "multiplier": event.multiplier,
        "fees": event.fees,
        "target_lot_id": event.target_lot_id,
        "raw_payload": dict(event.raw_payload or {}),
    }


def assigned_stock_allocation_row(
    allocation: OptionEconomicAllocation,
) -> dict[str, Any]:
    return {
        "event_id": allocation.close_event_id,
        "open_event_id": allocation.open_event_id,
        "source_record_id": allocation.target_lot_id,
        "close_type": allocation.close_type,
        "contracts_closed": allocation.contracts,
        "realized_pnl_gross": float(allocation.realized_pnl_gross),
        "realized_pnl_net": (
            None
            if allocation.realized_pnl_net is None
            else float(allocation.realized_pnl_net)
        ),
        "closed_at": allocation.closed_at_ms,
    }


def assigned_stock_position_lot_row(
    lot: PositionLot,
    *,
    current_fields: Mapping[str, Any] | None = None,
    valuation_marks: Sequence[ValuationMarkFact] = (),
    at_ms: int,
) -> dict[str, Any]:
    row = {
        "record_id": lot.lot_id,
        "open_event_id": lot.open_event_id,
        "opened_at": lot.opened_at_ms,
        "account": lot.contract_key.account,
        "broker": lot.contract_key.broker,
        "symbol": lot.contract_key.underlying_symbol,
        "option_type": lot.contract_key.option_type,
        "position_side": lot.contract_key.position_side,
        "currency": lot.currency,
        "contracts": lot.contracts_opened,
        "remaining": lot.contracts_open,
        "price": lot.premium_open,
        "multiplier": lot.multiplier,
        "strike": lot.contract_key.strike,
        "expiration_ymd": lot.contract_key.expiration_ymd,
    }
    for field in POSITION_LOT_STRATEGY_PATCH_FIELDS:
        if current_fields is not None and field in current_fields:
            value = current_fields[field]
            row[field] = dict(value) if isinstance(value, dict) else value
    if int(lot.contracts_open) <= 0:
        row.update(
            unrealized_pnl_gross=0.0,
            valuation_status="not_required",
            valuation_evidence_fact_id=None,
        )
        return row
    try:
        instrument = OptionInstrumentKey.from_contract_key(
            lot.contract_key,
            currency=lot.currency,
            multiplier=lot.multiplier,
        )
        selection = select_valuation_mark(
            list(valuation_marks),
            instrument_key=instrument.instrument_key,
            at_ms=at_ms,
        )
    except (TypeError, ValueError):
        selection = None
    fact = selection.fact if selection is not None else None
    if fact is None:
        row.update(
            unrealized_pnl_gross=None,
            valuation_status=(
                "missing_mark" if selection is None else selection.status
            ),
            valuation_evidence_fact_id=None,
        )
        return row
    open_value = (
        float(lot.premium_open) * float(lot.multiplier) * int(lot.contracts_open)
    )
    mark_value = float(fact.price) * float(lot.multiplier) * int(lot.contracts_open)
    gross = (
        open_value - mark_value
        if lot.contract_key.position_side == "short"
        else mark_value - open_value
    )
    row.update(
        unrealized_pnl_gross=float(quantize_money(gross)),
        valuation_status=selection.status,
        valuation_evidence_fact_id=fact.fact_id,
    )
    return row


def assigned_stock_event_time_ms(item: Mapping[str, Any]) -> int:
    for key in ("trade_time_ms", "event_time_ms", "sold_at_ms", "closed_at_ms"):
        try:
            value = int(item.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0

def project_assigned_stock_lifecycle(
    trade_events: list[dict[str, Any]],
    *,
    assignment_option_rows: list[dict[str, Any]],
    option_open_lots: list[dict[str, Any]],
    assigned_stock_events: list[dict[str, Any]] | None,
    quote_snapshots: Any = None,
    stock_holdings: list[dict[str, Any]] | None = None,
    account_norm: str | None,
    broker_norm: str | None,
    month: str | None,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    event_by_id = {
        str(event.get("event_id") or "").strip(): event
        for event in _active_trade_events(trade_events)
        if str(event.get("event_id") or "").strip()
    }
    option_rows_by_event: dict[str, list[dict[str, Any]]] = {}
    for row in assignment_option_rows:
        if str(row.get("close_type") or "").strip().lower() not in {
            "assignment",
            "exercise",
        }:
            continue
        event_id = str(row.get("event_id") or row.get("record_id") or "").strip()
        if event_id:
            option_rows_by_event.setdefault(event_id, []).append(row)
    option_lots_by_id: dict[str, list[dict[str, Any]]] = {}
    for lot in option_open_lots:
        record_id = str(lot.get("record_id") or "").strip()
        if record_id:
            option_lots_by_id.setdefault(record_id, []).append(lot)

    lots_by_id: dict[str, dict[str, Any]] = {}
    review_rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    settlement_sales: list[dict[str, Any]] = []
    for event in _active_trade_events(trade_events):
        if not _passes_report_filter(event, account_norm, broker_norm):
            continue
        lifecycle_close_type = _event_close_type(event)
        if lifecycle_close_type not in {"assignment", "exercise"}:
            continue
        event_id = str(event.get("event_id") or "").strip()
        event_month = _event_month(event)
        event_at = int(_event_ts(event) or 0)
        account = normalize_account(event.get("account")) or "-"
        broker = normalize_broker(event.get("broker")) or "-"
        symbol = norm_symbol(event.get("symbol") or "-")
        option_type = normalize_option_type(event.get("option_type")) or "-"
        position_side = _event_position_side(event) or str(event.get("position_side") or "").strip().lower()
        currency = normalize_currency(event.get("currency")) or "USD"
        expected_stock_side = _settlement_stock_side(
            lifecycle_close_type,
            option_type,
            position_side,
        )
        if expected_stock_side is None:
            review_rows.append(
                _assigned_stock_review_row(
                    status="incomplete_inventory_basis",
                    event_id=event_id,
                    month=event_month,
                    account=account,
                    broker=broker,
                    symbol=symbol,
                    message="assignment/exercise settlement lacks a supported assigned-stock inventory basis",
                    details={
                        "close_type": lifecycle_close_type,
                        "option_type": option_type,
                        "position_side": position_side,
                        "stock_settlement": dict(_event_stock_settlement(event)),
                    },
                )
            )
            continue
        stock = _event_stock_settlement(event)
        stock_side = normalize_trade_side(stock.get("side") or stock.get("stock_side")) if stock else ""
        raw_shares = stock.get("shares") if stock.get("shares") not in (None, "") else stock.get("stock_qty")
        raw_price = stock.get("price") if stock.get("price") not in (None, "") else stock.get("stock_price")
        shares_opened = _positive_integer(raw_shares)
        assignment_price_number = _nonnegative_decimal(raw_price)
        assignment_price = (
            float(assignment_price_number)
            if assignment_price_number is not None
            else None
        )
        raw_stock_fee = (
            stock.get("fees")
            if stock.get("fees") is not None
            else stock.get("fee")
        )
        raw_settlement_at = (
            stock.get("event_time_ms")
            if stock.get("event_time_ms") is not None
            else stock.get("trade_time_ms")
            if stock.get("trade_time_ms") is not None
            else event_at
        )
        settlement_at = _positive_integer(raw_settlement_at)
        if (
            not stock
            or stock_side != expected_stock_side
            or shares_opened is None
            or assignment_price is None
            or (
                raw_stock_fee is not None
                and _nonnegative_decimal(raw_stock_fee) is None
            )
            or settlement_at is None
        ):
            review_rows.append(
                _assigned_stock_review_row(
                    status="missing_stock_settlement",
                    event_id=event_id,
                    month=event_month,
                    account=account,
                    broker=broker,
                    symbol=symbol,
                    message=(
                        "assignment/exercise event is missing confirmed "
                        f"{expected_stock_side}-side stock settlement facts"
                    ),
                    details={"stock_settlement": dict(stock or {})},
                )
            )
            continue
        event_at = settlement_at
        event_month = month_from_ms(settlement_at)
        option_rows = option_rows_by_event.get(event_id, [])
        contracts = _positive_integer(event.get("contracts"))
        multiplier = _positive_integral(event.get("multiplier"))
        allocation = option_rows[0] if len(option_rows) == 1 else None
        allocation_contracts = (
            _positive_integer(allocation.get("contracts_closed"))
            if allocation is not None
            else None
        )
        source_option_lot_id = (
            str(allocation.get("source_record_id") or "").strip()
            if allocation is not None
            else None
        ) or None
        explicit_option_lot_id = str(
            event.get("target_lot_id")
            or _event_payload(event).get("target_lot_id")
            or _event_payload(event).get("record_id")
            or ""
        ).strip()
        source_option_lots = option_lots_by_id.get(source_option_lot_id or "", [])
        source_option_lot = source_option_lots[0] if len(source_option_lots) == 1 else None
        source_option_opened_at = (
            _positive_integer(source_option_lot.get("opened_at"))
            if source_option_lot is not None
            else None
        )
        allocation_open_event_id = (
            str(allocation.get("open_event_id") or "").strip()
            if allocation is not None
            else ""
        )
        source_option_open_event_id = (
            str(source_option_lot.get("open_event_id") or "").strip()
            if source_option_lot is not None
            else ""
        )
        binding_error = None
        if contracts is None or multiplier is None:
            binding_error = "assignment/exercise contracts or multiplier is invalid"
        elif shares_opened != contracts * multiplier:
            binding_error = "assignment/exercise stock settlement quantity mismatch"
        elif allocation is None or allocation_contracts != contracts:
            binding_error = "assignment/exercise option allocation is incomplete"
        elif str(allocation.get("close_type") or "").strip().lower() != lifecycle_close_type:
            binding_error = "assignment/exercise option allocation type conflicts with terminal event"
        elif source_option_lot_id is None or source_option_lot is None:
            binding_error = "assignment/exercise option-lot binding is not unique"
        elif (
            not allocation_open_event_id
            or allocation_open_event_id != source_option_open_event_id
        ):
            binding_error = "assignment/exercise option allocation open-event binding conflicts with final lot"
        elif explicit_option_lot_id and explicit_option_lot_id != source_option_lot_id:
            binding_error = "assignment/exercise option-lot binding conflicts with allocation"
        elif (
            normalize_account(source_option_lot.get("account")) != account
            or normalize_broker(source_option_lot.get("broker")) != broker
            or norm_symbol(source_option_lot.get("symbol") or "") != symbol
            or normalize_currency(source_option_lot.get("currency")) != (
                normalize_currency(stock.get("currency")) or currency
            )
            or normalize_option_type(source_option_lot.get("option_type")) != option_type
            or str(source_option_lot.get("position_side") or "").strip().lower()
            != position_side
            or source_option_opened_at is None
            or source_option_opened_at <= 0
            or source_option_opened_at > settlement_at
        ):
            binding_error = "assignment/exercise final option lot is inconsistent"
        if binding_error:
            review_rows.append(
                _assigned_stock_review_row(
                    status="incomplete_inventory_basis",
                    event_id=event_id,
                    month=event_month,
                    account=account,
                    broker=broker,
                    symbol=symbol,
                    message=binding_error,
                    details={
                        "contracts": contracts,
                        "multiplier": multiplier,
                        "shares": shares_opened,
                        "source_option_lot_id": source_option_lot_id,
                    },
                )
            )
            continue
        if expected_stock_side == "sell":
            payload = _event_payload(event)
            target_stock_lot_id = next(
                (
                    str(source.get(key) or "").strip()
                    for source in (stock, payload, event)
                    for key in (
                        "stock_lot_id",
                        "target_stock_lot_id",
                        "source_stock_lot_id",
                    )
                    if str(source.get(key) or "").strip()
                ),
                "",
            )
            strategy = _event_strategy_metadata(event)
            strategy_group_id = (
                strategy.get("strategy_group_id")
                or source_option_lot.get("strategy_group_id")
            )
            settlement_sales.append(
                {
                    "event_type": "sale",
                    "stock_event_id": event_id,
                    "target_stock_lot_id": target_stock_lot_id,
                    "strategy_group_id": strategy_group_id,
                    "account": account,
                    "broker": broker,
                    "symbol": symbol,
                    "side": "sell",
                    "shares": shares_opened,
                    "price": assignment_price,
                    "currency": normalize_currency(stock.get("currency")) or currency,
                    "fees": stock.get("fees") if stock.get("fees") is not None else stock.get("fee", 0),
                    "fee_provenance": stock.get("fee_provenance"),
                    "trade_time_ms": event_at,
                    "source": f"option_{lifecycle_close_type}_stock_settlement",
                    "_settlement_transition": True,
                }
            )
            continue
        option_premium_attribution = _option_premium_attribution(option_rows)
        stock_lot_id = _assigned_stock_lot_id(event_id)
        assigned_contracts = sum(int(row.get("contracts_closed") or 0) for row in option_rows)
        fee_facts: list[dict[str, Any]] = []
        source_open_event = event_by_id.get(str(_source_option_open_event_id(event, option_rows) or ""))
        strategy_metadata = _event_strategy_metadata(event)
        if not strategy_metadata.get("strategy_group_id"):
            strategy_metadata = {**_event_strategy_metadata(source_open_event), **strategy_metadata}
        source_option_leg_role = str(strategy_metadata.get("leg_role") or "").strip() or None
        stock_snapshot = (
            dict(strategy_metadata.get("strategy_snapshot"))
            if isinstance(strategy_metadata.get("strategy_snapshot"), dict)
            else {}
        )
        if stock_snapshot:
            stock_snapshot["source_option_leg_role"] = source_option_leg_role
            stock_snapshot["leg_role"] = "assigned_stock"
        stock_strategy_fields = {
            key: value
            for key, value in {
                "strategy": strategy_metadata.get("strategy"),
                "leg_role": "assigned_stock" if strategy_metadata.get("strategy_group_id") else None,
                "strategy_group_id": strategy_metadata.get("strategy_group_id"),
                "yield_enhancement_mode": strategy_metadata.get("yield_enhancement_mode"),
                "strategy_snapshot": stock_snapshot or None,
                "structure_mode": stock_snapshot.get("structure_mode") if stock_snapshot else None,
                "expiry_structure": stock_snapshot.get("expiry_structure") if stock_snapshot else None,
                "source_option_leg_role": source_option_leg_role,
            }.items()
            if value not in (None, "", {})
        }
        open_fee_component = f"{option_type}_open_option_fee"
        close_fee_component = (
            f"{option_type}_{lifecycle_close_type}_option_fee"
        )
        stock_fee_component = f"{lifecycle_close_type}_stock_fee"
        if source_open_event is not None:
            open_contracts = max(1, int(abs(float(source_open_event.get("contracts") or 0))))
            fee_facts.append(
                _scale_fee_fact(
                    _option_fee_fact(source_open_event, component=open_fee_component),
                    assigned_contracts / open_contracts,
                )
            )
        else:
            fee_facts.append({"component": open_fee_component, "basis": "missing", "amount": 0.0})
        close_contracts = max(1, int(abs(float(event.get("contracts") or 0))))
        fee_facts.append(
            _scale_fee_fact(
                _option_fee_fact(event, component=close_fee_component),
                assigned_contracts / close_contracts,
            )
        )
        assignment_stock_fee = assigned_stock_fee_fact(
            {
                **stock,
                "account": account,
                "broker": broker,
                "symbol": symbol,
                "currency": normalize_currency(stock.get("currency")) or currency,
                "contracts": assigned_contracts,
            },
            component=stock_fee_component,
            transaction_kind="assignment",
        )
        fee_facts.append(assignment_stock_fee)
        assignment_fees = _round_money(assignment_stock_fee.get("amount"))
        assignment_notional = _round_money(float(assignment_price) * shares_opened)
        lots_by_id[stock_lot_id] = {
            "stock_lot_id": stock_lot_id,
            "source_assignment_event_id": event_id,
            "source_option_lot_id": source_option_lot_id,
            **stock_strategy_fields,
            "account": account,
            "broker": broker,
            "symbol": symbol,
            "currency": normalize_currency(stock.get("currency")) or currency,
            "opened_at_ms": event_at,
            "assigned_at_ms": event_at,
            "assigned_date": _market_date(event_at, symbol=symbol, currency=normalize_currency(stock.get("currency")) or currency),
            "opened_month": event_month,
            "month": event_month,
            "shares_opened": shares_opened,
            "shares_remaining": shares_opened,
            "shares_sold": 0,
            "assignment_price": float(assignment_price),
            "assignment_notional": assignment_notional,
            "assignment_fees": assignment_fees,
            "stock_cost_per_share": float(assignment_price),
            "stock_cost_basis_total": _round_money(assignment_notional + assignment_fees),
            "stock_principal_basis_total": assignment_notional,
            "basis_policy": "assignment_stock_cost_basis",
            "option_premium_attribution": option_premium_attribution,
            "stock_sale_cash_in_net": 0.0,
            "stock_sale_cash_in_gross": 0.0,
            "stock_sale_fees": 0.0,
            "stock_cost_basis_sold": 0.0,
            "assigned_stock_realized_pnl": 0.0,
            "sale_event_ids": [],
            "sale_months": [],
            "_sale_rows": [],
            "_fee_facts": fee_facts,
            "_assigned_contracts": assigned_contracts,
            "_option_open_event": source_open_event,
            "_covered_call_pnl": 0.0,
            "_covered_call_realized_pnl": 0.0,
            "_covered_call_unrealized_pnl": 0.0,
            "_covered_call_fee_facts": [],
            "_covered_call_statuses": set(),
            "_covered_call_complete": True,
            "_covered_call_evidence_fact_ids": set(),
        }

    stock_events = sorted(
        [
            *settlement_sales,
            *_normalize_assigned_stock_events(assigned_stock_events),
        ],
        key=lambda row: (
            int(_stock_event_time_ms(row) or 0),
            str(row.get("stock_event_id") or row.get("event_id") or ""),
        ),
    )
    seen_stock_events: set[str] = set()
    for idx, sale in enumerate(stock_events, start=1):
        if _stock_event_type(sale) != "sale":
            continue
        settlement_transition = bool(sale.get("_settlement_transition"))
        stock_event_id = _stock_event_id(sale, fallback_index=idx)
        if stock_event_id in seen_stock_events:
            review_rows.append(
                _assigned_stock_review_row(
                    status="manual_review_required",
                    stock_event_id=stock_event_id,
                    month=_stock_event_month(sale),
                    account=normalize_account(sale.get("account")),
                    broker=normalize_broker(sale.get("broker")),
                    symbol=norm_symbol(sale.get("symbol") or ""),
                    message="duplicate assigned stock sale event id",
                )
            )
            continue
        seen_stock_events.add(stock_event_id)
        target_stock_lot_id = str(sale.get("target_stock_lot_id") or "").strip()
        sale_account_filter = normalize_account(sale.get("account")) if sale.get("account") not in (None, "") else None
        sale_broker_filter = normalize_broker(sale.get("broker")) if sale.get("broker") not in (None, "") else None
        if account_norm and sale_account_filter != account_norm:
            continue
        if broker_norm and sale_broker_filter != broker_norm:
            continue
        sale_month = _stock_event_month(sale)
        sale_at = _stock_event_time_ms(sale)
        shares = _stock_event_shares(sale)
        if settlement_transition and not target_stock_lot_id:
            group_id = str(sale.get("strategy_group_id") or "").strip()
            candidates = [
                lot
                for lot in lots_by_id.values()
                if lot.get("account") == sale_account_filter
                and lot.get("broker") == sale_broker_filter
                and lot.get("symbol") == norm_symbol(sale.get("symbol") or "")
                and lot.get("currency") == normalize_currency(sale.get("currency"))
                and int(lot.get("shares_remaining") or 0) >= shares
                and int(lot.get("assigned_at_ms") or 0) <= int(sale_at or 0)
                and (not group_id or lot.get("strategy_group_id") == group_id)
            ]
            if len(candidates) == 1:
                target_stock_lot_id = str(candidates[0]["stock_lot_id"])
            else:
                review_rows.append(
                    _assigned_stock_review_row(
                        status="incomplete_inventory_basis",
                        event_id=stock_event_id,
                        month=sale_month,
                        account=sale_account_filter,
                        broker=sale_broker_filter,
                        symbol=norm_symbol(sale.get("symbol") or ""),
                        message="assignment/exercise stock-lot binding is not unique",
                        details={"candidate_count": len(candidates)},
                    )
                )
                continue
        lot = lots_by_id.get(target_stock_lot_id)
        if lot is None:
            review_rows.append(
                _assigned_stock_review_row(
                    status=(
                        "incomplete_inventory_basis"
                        if settlement_transition
                        else "manual_review_required"
                    ),
                    stock_event_id=stock_event_id,
                    stock_lot_id=target_stock_lot_id or None,
                    month=sale_month,
                    account=normalize_account(sale.get("account")),
                    broker=normalize_broker(sale.get("broker")),
                    symbol=norm_symbol(sale.get("symbol") or ""),
                    message=(
                        "assignment/exercise stock settlement must target an existing assigned stock lot"
                        if settlement_transition
                        else "assigned stock sale must target an existing assigned stock lot"
                    ),
                )
            )
            continue
        sale_side = normalize_trade_side(sale.get("side"))
        sale_account = normalize_account(sale.get("account")) or lot.get("account")
        sale_broker = normalize_broker(sale.get("broker")) or lot.get("broker")
        sale_symbol = norm_symbol(sale.get("symbol") or lot.get("symbol") or "")
        sale_currency = normalize_currency(sale.get("currency")) or lot.get("currency")
        price = _stock_event_price(sale)
        sale_fee_fact = assigned_stock_fee_fact(
            sale,
            component="stock_sale_fee",
            transaction_kind="sale",
        )
        fees = _round_money(sale_fee_fact.get("amount"))
        mismatch_fields = []
        if sale_side != "sell":
            mismatch_fields.append("side")
        if sale_account != lot.get("account"):
            mismatch_fields.append("account")
        if sale_broker != lot.get("broker"):
            mismatch_fields.append("broker")
        if sale_symbol != lot.get("symbol"):
            mismatch_fields.append("symbol")
        if sale_currency != lot.get("currency"):
            mismatch_fields.append("currency")
        if sale_at is None or int(sale_at) < int(lot.get("opened_at_ms") or 0):
            mismatch_fields.append("trade_time_ms")
        if shares <= 0:
            mismatch_fields.append("shares")
        if price is None or price < 0:
            mismatch_fields.append("price")
        if fees < 0:
            mismatch_fields.append("fees")
        if shares > int(lot.get("shares_remaining") or 0):
            mismatch_fields.append("shares_remaining")
        if mismatch_fields:
            review_rows.append(
                _assigned_stock_review_row(
                    status="source_conflict",
                    stock_event_id=stock_event_id,
                    stock_lot_id=target_stock_lot_id,
                    month=sale_month,
                    account=sale_account,
                    broker=sale_broker,
                    symbol=sale_symbol,
                    message="assigned stock sale event failed validation",
                    details={"fields": mismatch_fields, "shares_remaining": lot.get("shares_remaining")},
                )
            )
            continue
        proceeds_gross = _round_money(float(price) * shares)
        proceeds_net = _round_money(proceeds_gross - fees)
        cost_basis_sold = _round_money(_lot_basis_per_share_with_fees(lot) * shares)
        principal_basis_sold = _round_money(float(lot.get("assignment_price") or 0.0) * shares)
        realized_pnl = _round_money(proceeds_net - cost_basis_sold)
        lot["shares_remaining"] = int(lot.get("shares_remaining") or 0) - shares
        lot["shares_sold"] = int(lot.get("shares_sold") or 0) + shares
        lot["stock_sale_cash_in_net"] = _round_money(float(lot.get("stock_sale_cash_in_net") or 0.0) + proceeds_net)
        lot["stock_sale_cash_in_gross"] = _round_money(
            float(lot.get("stock_sale_cash_in_gross") or 0.0) + proceeds_gross
        )
        lot["stock_sale_fees"] = _round_money(float(lot.get("stock_sale_fees") or 0.0) + fees)
        lot["stock_cost_basis_sold"] = _round_money(float(lot.get("stock_cost_basis_sold") or 0.0) + cost_basis_sold)
        lot["assigned_stock_realized_pnl"] = _round_money(
            float(lot.get("assigned_stock_realized_pnl") or 0.0) + realized_pnl
        )
        if not settlement_transition:
            lot["sale_event_ids"].append(stock_event_id)
        if sale_month and sale_month not in lot["sale_months"]:
            lot["sale_months"].append(sale_month)
        lot["_fee_facts"].append(sale_fee_fact)
        sale_row = {
            "stock_event_id": stock_event_id,
            "stock_lot_id": target_stock_lot_id,
            "source_assignment_event_id": lot.get("source_assignment_event_id"),
            "account": sale_account,
            "broker": sale_broker,
            "symbol": sale_symbol,
            "currency": sale_currency,
            "month": sale_month,
            "event_at": int(sale_at or 0),
            "shares": shares,
            "price": float(price),
            "fees": fees,
            "fee_basis": sale_fee_fact.get("basis"),
            "fee_source": sale_fee_fact.get("source"),
            "fee_reason": sale_fee_fact.get("reason"),
            "cash_in_gross": proceeds_gross,
            "stock_sale_cash_in_net": proceeds_net,
            "stock_cost_basis_sold": cost_basis_sold,
            "stock_principal_basis_sold": principal_basis_sold,
            "assigned_stock_realized_pnl": realized_pnl,
            "source": str(sale.get("source") or "").strip() or None,
            "source_deal_id": str(sale.get("source_deal_id") or "").strip() or None,
        }
        if isinstance(sale.get("cash_conversions"), dict):
            sale_row["cash_conversions"] = dict(sale["cash_conversions"])
        for key in (
            "strategy",
            "leg_role",
            "strategy_group_id",
            "yield_enhancement_mode",
            "strategy_snapshot",
            "structure_mode",
            "expiry_structure",
            "source_option_leg_role",
        ):
            if lot.get(key) not in (None, "", {}):
                sale_row[key] = lot.get(key)
        lot["_sale_rows"].append(sale_row)

    effective_as_of_ms = int(as_of_ms) if as_of_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    covered_call_allocation_rows: list[dict[str, Any]] = []
    _attribute_covered_calls(
        lots_by_id,
        trade_events=_active_trade_events(trade_events),
        option_open_lots=option_open_lots,
        assignment_option_rows=assignment_option_rows,
        as_of_ms=effective_as_of_ms,
        review_rows=review_rows,
        allocation_rows=covered_call_allocation_rows,
    )

    quote_rows = _normalize_quote_snapshots(quote_snapshots)
    lifecycle_rows: list[dict[str, Any]] = []
    sale_rows: list[dict[str, Any]] = []
    lot_rows: list[dict[str, Any]] = []
    for lot in lots_by_id.values():
        shares_remaining = int(lot.get("shares_remaining") or 0)
        status = "closed" if shares_remaining == 0 else ("partially_sold" if int(lot.get("shares_sold") or 0) > 0 else "open")
        remaining_stock_cost_basis = _round_money(_lot_basis_per_share_with_fees(lot) * shares_remaining)
        remaining_stock_principal_basis = _round_money(float(lot.get("assignment_price") or 0.0) * shares_remaining)
        quote = _matching_quote(quote_rows, lot, as_of_ms=as_of_ms) if shares_remaining > 0 else None
        spot = _quote_spot(quote or {}) if quote is not None else None
        quote_time = _quote_time_ms(quote or {}) if quote is not None else None
        quote_source = str((quote or {}).get("quote_source") or (quote or {}).get("source") or "").strip() or None
        quote_status = (
            str((quote or {}).get("quote_status") or "").strip().lower()
            if quote is not None
            else ("not_required" if shares_remaining == 0 else "missing_quote")
        )
        if quote is not None and quote_status not in {"fresh", "stale", "missing_quote"}:
            quote_status = "fresh" if spot is not None else "missing_quote"
        remaining_market_value = _round_money(spot * shares_remaining) if spot is not None and shares_remaining > 0 else None
        assigned_stock_unrealized_pnl = (
            _round_money(float(remaining_market_value) - remaining_stock_cost_basis)
            if remaining_market_value is not None
            else None
        )
        assigned_stock_unrealized_pnl_gross = (
            _round_money(float(remaining_market_value) - remaining_stock_principal_basis)
            if remaining_market_value is not None
            else None
        )
        lifecycle_pnl = None
        if shares_remaining == 0 or remaining_market_value is not None:
            lifecycle_pnl = _round_money(
                float(lot.get("option_premium_attribution") or 0.0)
                - float(lot.get("stock_cost_basis_total") or 0.0)
                + float(lot.get("stock_sale_cash_in_net") or 0.0)
                + float(remaining_market_value or 0.0)
            )
        sale_rows_sorted = sorted(lot.get("_sale_rows") or [], key=lambda item: int(item.get("event_at") or 0))
        assigned_at_ms = int(lot.get("assigned_at_ms") or 0) or None
        inventory_end_at_ms = (
            max((int(item.get("event_at") or 0) for item in sale_rows_sorted), default=0) or assigned_at_ms
            if shares_remaining == 0
            else effective_as_of_ms
        )
        inventory_days = _elapsed_days(assigned_at_ms, inventory_end_at_ms)

        open_event = lot.get("_option_open_event") if isinstance(lot.get("_option_open_event"), dict) else None
        put_days = _elapsed_days(_event_ts(open_event or {}), assigned_at_ms)
        assigned_contracts = int(lot.get("_assigned_contracts") or 0)
        put_strike = safe_float((open_event or {}).get("strike"))
        put_multiplier = safe_float((open_event or {}).get("multiplier"))
        put_capital_days = (
            round(put_strike * put_multiplier * assigned_contracts * put_days, 6)
            if put_days is not None and put_strike is not None and put_multiplier is not None and assigned_contracts > 0
            else None
        )

        stock_capital_days = 0.0
        stock_capital_known = assigned_at_ms is not None and inventory_end_at_ms is not None
        capital_cursor = assigned_at_ms
        remaining_basis_for_days = float(lot.get("stock_cost_basis_total") or 0.0)
        if stock_capital_known:
            for sale_row in sale_rows_sorted:
                sale_at = int(sale_row.get("event_at") or 0)
                interval = _elapsed_days(capital_cursor, sale_at)
                if interval is None:
                    stock_capital_known = False
                    break
                stock_capital_days += remaining_basis_for_days * interval
                remaining_basis_for_days = max(
                    0.0,
                    remaining_basis_for_days - float(sale_row.get("stock_cost_basis_sold") or 0.0),
                )
                capital_cursor = sale_at
            if stock_capital_known:
                final_interval = _elapsed_days(capital_cursor, inventory_end_at_ms)
                if final_interval is None:
                    stock_capital_known = False
                else:
                    stock_capital_days += remaining_basis_for_days * final_interval
        stock_capital_days_value = round(stock_capital_days, 6) if stock_capital_known else None
        capital_days = (
            round(float(put_capital_days) + float(stock_capital_days_value), 6)
            if put_capital_days is not None and stock_capital_days_value is not None
            else None
        )

        stock_pnl_gross = None
        if shares_remaining == 0 or remaining_market_value is not None:
            stock_pnl_gross = _round_money(
                float(lot.get("stock_sale_cash_in_gross") or 0.0)
                + float(remaining_market_value or 0.0)
                - float(lot.get("assignment_notional") or 0.0)
            )
        covered_call_pnl = _round_money(lot.get("_covered_call_pnl"))
        fee_facts = list(lot.get("_fee_facts") or []) + list(lot.get("_covered_call_fee_facts") or [])
        fee_summary = _summarize_fee_facts(fee_facts)
        lifecycle_pnl_gross = (
            _round_money(float(lot.get("option_premium_attribution") or 0.0) + stock_pnl_gross + covered_call_pnl)
            if stock_pnl_gross is not None
            else None
        )
        fees_complete = not fee_summary["fee_missing_components"]
        lifecycle_pnl_net = (
            _round_money(lifecycle_pnl_gross - float(fee_summary["fees_used"]))
            if lifecycle_pnl_gross is not None and fees_complete
            else None
        )
        annualized_capital_efficiency = (
            round(lifecycle_pnl_net * 365 / capital_days, 8)
            if lifecycle_pnl_net is not None and capital_days is not None and capital_days > 0
            else None
        )
        covered_call_statuses = set(lot.get("_covered_call_statuses") or set())
        covered_call_allocation_status = (
            "none"
            if not covered_call_statuses
            else next(iter(covered_call_statuses))
            if len(covered_call_statuses) == 1
            else "mixed"
        )
        covered_call_allocation_quality = (
            "exact"
            if not covered_call_statuses or covered_call_statuses == {"explicit"}
            else "heuristic"
            if covered_call_statuses == {"derived_fifo"}
            else "mixed"
        )
        if status == "closed":
            if (
                not fee_summary["fee_missing_components"]
                and bool(lot.get("_covered_call_complete"))
                and capital_days is not None
            ):
                lifecycle_quality = (
                    "closed_heuristic" if covered_call_allocation_quality == "heuristic" else "complete_closed"
                )
            else:
                lifecycle_quality = "closed_incomplete"
        elif remaining_market_value is not None:
            lifecycle_quality = (
                "open_marked_heuristic" if covered_call_allocation_quality == "heuristic" else "open_marked"
            )
        else:
            lifecycle_quality = None
        review_status = "ready"
        if shares_remaining > 0 and remaining_market_value is None:
            review_status = "missing_quote"
            review_rows.append(
                _assigned_stock_review_row(
                    status="missing_quote",
                    event_id=str(lot.get("source_assignment_event_id") or ""),
                    stock_lot_id=str(lot.get("stock_lot_id") or ""),
                    month=str(lot.get("opened_month") or ""),
                    account=str(lot.get("account") or ""),
                    broker=str(lot.get("broker") or ""),
                    symbol=str(lot.get("symbol") or ""),
                    message="open assigned stock lot has no usable as-of quote",
                )
            )
        row = {
            **{key: value for key, value in lot.items() if not str(key).startswith("_")},
            "status": status,
            "review_status": review_status,
            "remaining_stock_cost_basis": remaining_stock_cost_basis,
            "remaining_stock_principal_basis": remaining_stock_principal_basis,
            "spot": spot,
            "spot_time": quote_time,
            "quote_source": quote_source,
            "quote_status": quote_status,
            "quote_evidence_fact_id": str((quote or {}).get("evidence_fact_id") or "") or None,
            "remaining_market_value": remaining_market_value,
            "assigned_stock_unrealized_pnl": assigned_stock_unrealized_pnl,
            "assigned_stock_unrealized_pnl_gross": assigned_stock_unrealized_pnl_gross,
            "assignment_lifecycle_pnl": lifecycle_pnl,
            "inventory_end_at_ms": inventory_end_at_ms,
            "inventory_days": inventory_days,
            **fee_summary,
            "covered_call_pnl": covered_call_pnl,
            "covered_call_realized_pnl": _round_money(lot.get("_covered_call_realized_pnl")),
            "covered_call_unrealized_pnl": _round_money(lot.get("_covered_call_unrealized_pnl")),
            "covered_call_allocation_status": covered_call_allocation_status,
            "covered_call_allocation_quality": covered_call_allocation_quality,
            "covered_call_evidence_fact_ids": sorted(lot.get("_covered_call_evidence_fact_ids") or set()),
            "put_capital_days": put_capital_days,
            "stock_capital_days": stock_capital_days_value,
            "capital_days": capital_days,
            "lifecycle_pnl_gross": lifecycle_pnl_gross,
            "lifecycle_pnl_net": lifecycle_pnl_net,
            "annualized_capital_efficiency": annualized_capital_efficiency,
            "lifecycle_quality": lifecycle_quality,
        }
        lot_rows.append(row)
        lifecycle_rows.append(row)
        sale_rows.extend(lot.get("_sale_rows") or [])

    _append_holding_reconciliation_reviews(
        review_rows,
        lot_rows,
        stock_holdings=stock_holdings,
        month=month,
    )

    filtered_lifecycle_rows = sorted(
        [row for row in lifecycle_rows if _lifecycle_row_in_month(row, month)],
        key=_assigned_stock_row_sort_key,
    )
    return {
        "_all_assigned_stock_lots": sorted(lot_rows, key=_assigned_stock_row_sort_key),
        "assigned_stock_lots": sorted(
            [row for row in lot_rows if _lifecycle_row_in_month(row, month)],
            key=_assigned_stock_row_sort_key,
        ),
        "assignment_lifecycle_rows": filtered_lifecycle_rows,
        "lifecycle_efficiency_rows": filtered_lifecycle_rows,
        "lifecycle_efficiency_summary": _lifecycle_efficiency_summary(filtered_lifecycle_rows),
        "assigned_stock_sale_rows": sorted(
            [row for row in sale_rows if _sale_row_in_month(row, month)],
            key=_event_detail_sort_key,
        ),
        "assigned_stock_review_rows": sorted(
            [row for row in review_rows if _review_row_in_month(row, month)],
            key=_assigned_stock_row_sort_key,
        ),
        "unsupported_inventory_rows": sorted(
            [
                row
                for row in review_rows
                if row.get("status") == "incomplete_inventory_basis" and _review_row_in_month(row, month)
            ],
            key=_assigned_stock_row_sort_key,
        ),
        "covered_call_allocations": sorted(
            covered_call_allocation_rows,
            key=lambda row: (
                int(row.get("start_at_ms") or 0),
                str(row.get("open_event_id") or ""),
                str(row.get("stock_lot_id") or ""),
            ),
        ),
        "warnings": warnings,
    }

def _append_holding_reconciliation_reviews(
    review_rows: list[dict[str, Any]],
    lot_rows: list[dict[str, Any]],
    *,
    stock_holdings: list[dict[str, Any]] | None,
    month: str | None,
) -> None:
    if not isinstance(stock_holdings, list):
        return
    expected_by_key: dict[tuple[str, str, str, str], float] = {}
    for lot in lot_rows:
        key = (
            normalize_account(lot.get("account")) or "-",
            normalize_broker(lot.get("broker")) or "-",
            norm_symbol(lot.get("symbol") or ""),
            normalize_currency(lot.get("currency")) or "",
        )
        expected_by_key[key] = expected_by_key.get(key, 0.0) + float(lot.get("shares_remaining") or 0.0)

    actual_by_key: dict[tuple[str, str, str, str], float] = {}
    for holding in stock_holdings:
        if not isinstance(holding, dict):
            continue
        shares = safe_float(holding.get("shares") if holding.get("shares") not in (None, "") else holding.get("quantity"))
        if shares is None:
            continue
        key = (
            normalize_account(holding.get("account")) or "-",
            normalize_broker(holding.get("broker")) or "-",
            norm_symbol(holding.get("symbol") or holding.get("underlying_symbol") or ""),
            normalize_currency(holding.get("currency")) or "",
        )
        actual_by_key[key] = actual_by_key.get(key, 0.0) + float(shares)

    for key, expected in sorted(expected_by_key.items()):
        actual = actual_by_key.get(key, 0.0)
        if abs(actual - expected) < 1e-9:
            continue
        account, broker, symbol, currency = key
        status = "missing_stock_sale" if actual < expected else "source_conflict"
        review_rows.append(
            _assigned_stock_review_row(
                status=status,
                month=month,
                account=account,
                broker=broker,
                symbol=symbol,
                message="assigned stock lots and external holdings disagree; holdings are reconciliation evidence only",
                details={"currency": currency, "assigned_stock_shares_remaining": expected, "holding_shares": actual},
            )
        )

def _event_detail_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(row.get("month") or ""),
        str(row.get("account") or ""),
        str(row.get("currency") or ""),
        int(row.get("event_at") or 0),
        str(row.get("event_id") or row.get("record_id") or ""),
    )


def _assigned_stock_row_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(row.get("month") or row.get("opened_month") or ""),
        str(row.get("account") or ""),
        str(row.get("symbol") or ""),
        int(row.get("opened_at_ms") or row.get("event_at") or 0),
        str(row.get("stock_lot_id") or row.get("event_id") or ""),
    )


__all__ = [
    "assigned_stock_allocation_row",
    "assigned_stock_event_time_ms",
    "assigned_stock_fee_fact",
    "assigned_stock_position_lot_row",
    "assigned_stock_trade_event_row",
    "project_assigned_stock_lifecycle",
]
