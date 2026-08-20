from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence

from domain.domain.decision_state_fingerprint import canonical_sha256


WHEEL_EVENT_TYPES = frozenset(
    {
        "wheel_started",
        "wheel_manual_ended",
        "wheel_called_away",
        "wheel_call_intent_created",
        "wheel_call_intent_cancelled",
        "wheel_call_intent_consumed",
        "wheel_call_linkage_rejected",
        "wheel_event_voided",
    }
)
WHEEL_EVENT_SCHEMA = "wheel_event.v1"
WHEEL_PROJECTION_SCHEMA = "wheel_projection.v1"


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"wheel event requires {field}")
    return text


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a positive integer") from None
    if number <= 0 or str(value).strip() not in {str(number), f"{number}.0"}:
        raise ValueError(f"{field} must be a positive integer")
    return number


def wheel_event_payload_hash(event: Mapping[str, Any]) -> str:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("wheel event payload must be an object")
    return canonical_sha256(
        {
            "schema_version": WHEEL_EVENT_SCHEMA,
            "account": str(event.get("account") or "").strip().lower(),
            "stock_lot_id": str(event.get("stock_lot_id") or "").strip(),
            "event_type": str(event.get("event_type") or "").strip().lower(),
            "occurred_at_ms": int(event.get("occurred_at_ms") or 0),
            "intent_id": str(event.get("intent_id") or "").strip() or None,
            "source_trade_event_id": (
                str(event.get("source_trade_event_id") or "").strip() or None
            ),
            "payload": dict(payload),
        }
    )


def normalize_wheel_event(event: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise TypeError("wheel event must be an object")
    event_id = _required_text(event.get("event_id"), "event_id")
    account = _required_text(event.get("account"), "account")
    if account != account.lower():
        raise ValueError("wheel event account must be lowercase")
    stock_lot_id = _required_text(event.get("stock_lot_id"), "stock_lot_id")
    event_type = _required_text(event.get("event_type"), "event_type").lower()
    if event_type not in WHEEL_EVENT_TYPES:
        raise ValueError(f"unsupported wheel event type: {event_type}")
    occurred_at_ms = _positive_int(event.get("occurred_at_ms"), "occurred_at_ms")
    recorded_at_ms = _positive_int(event.get("recorded_at_ms"), "recorded_at_ms")
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("wheel event payload must be an object")
    intent_id = str(event.get("intent_id") or "").strip() or None
    source_trade_event_id = (
        str(event.get("source_trade_event_id") or "").strip() or None
    )
    if event_type.startswith("wheel_call_intent_") and not intent_id:
        raise ValueError(f"{event_type} requires intent_id")
    if event_type == "wheel_event_voided":
        _required_text(payload.get("target_wheel_event_id"), "target_wheel_event_id")
    normalized = {
        "event_id": event_id,
        "account": account,
        "stock_lot_id": stock_lot_id,
        "event_type": event_type,
        "occurred_at_ms": occurred_at_ms,
        "recorded_at_ms": recorded_at_ms,
        "intent_id": intent_id,
        "source_trade_event_id": source_trade_event_id,
        "payload": dict(payload),
    }
    payload_hash = wheel_event_payload_hash(normalized)
    supplied_hash = str(event.get("payload_hash") or "").strip()
    if supplied_hash and supplied_hash != payload_hash:
        raise ValueError(f"wheel event payload hash mismatch: event_id={event_id}")
    normalized["payload_hash"] = payload_hash
    return normalized


def build_wheel_event(
    *,
    event_id: str,
    account: str,
    stock_lot_id: str,
    event_type: str,
    occurred_at_ms: int,
    recorded_at_ms: int,
    payload: Mapping[str, Any],
    intent_id: str | None = None,
    source_trade_event_id: str | None = None,
) -> dict[str, Any]:
    return normalize_wheel_event(
        {
            "event_id": event_id,
            "account": account,
            "stock_lot_id": stock_lot_id,
            "event_type": event_type,
            "occurred_at_ms": occurred_at_ms,
            "recorded_at_ms": recorded_at_ms,
            "intent_id": intent_id,
            "source_trade_event_id": source_trade_event_id,
            "payload": dict(payload),
        }
    )


def _trade_event_fact(event: Any) -> dict[str, Any]:
    if isinstance(event, Mapping):
        return dict(event)
    contract_key = getattr(event, "contract_key", None)
    key = contract_key.to_dict() if hasattr(contract_key, "to_dict") else {}
    return {
        "event_id": getattr(event, "event_id", None),
        "event_type": getattr(event, "event_type", None),
        "event_time_ms": getattr(event, "event_time_ms", None),
        "account": key.get("account"),
        "symbol": key.get("underlying_symbol"),
        "option_type": key.get("option_type"),
        "position_side": key.get("position_side"),
        "contracts": getattr(event, "contracts", None),
        "multiplier": getattr(event, "multiplier", None),
        "currency": getattr(event, "currency", None),
        "target_lot_id": getattr(event, "target_lot_id", None),
        "raw_payload": dict(getattr(event, "raw_payload", None) or {}),
    }


def _stock_settlement(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event.get("raw_payload")
    payload = payload if isinstance(payload, Mapping) else {}
    stock = payload.get("stock_settlement")
    return dict(stock) if isinstance(stock, Mapping) else {}


def wheel_started_event_from_assignment(
    terminal_event: Any,
    source_put_lot: Mapping[str, Any],
    *,
    recorded_at_ms: int,
) -> dict[str, Any] | None:
    event = _trade_event_fact(terminal_event)
    if _event_type(event) != "assignment":
        return None
    fields = _lot_fields(source_put_lot)
    if (
        str(fields.get("option_type") or "").strip().lower() != "put"
        or str(fields.get("side") or fields.get("position_side") or "").strip().lower()
        != "short"
    ):
        return None
    event_id = _required_text(event.get("event_id"), "source_trade_event_id")
    account = _required_text(
        event.get("account") or fields.get("account"),
        "account",
    ).lower()
    stock = _stock_settlement(event)
    if str(stock.get("side") or "").strip().lower() != "buy":
        raise ValueError("Wheel start requires buy-side Short Put assignment settlement")
    contracts = _positive_int(event.get("contracts"), "assignment contracts")
    try:
        multiplier = int(float(event.get("multiplier") or fields.get("multiplier") or 0))
        shares = int(stock.get("shares") or stock.get("stock_qty") or 0)
        price = float(stock.get("price") if stock.get("price") is not None else stock.get("stock_price"))
    except (TypeError, ValueError):
        raise ValueError("Wheel start assignment settlement is incomplete") from None
    if multiplier <= 0 or shares != contracts * multiplier or price < 0:
        raise ValueError("Wheel start assignment settlement quantity or price is invalid")
    occurred_at_ms = _positive_int(
        stock.get("event_time_ms") or event.get("event_time_ms"),
        "assignment occurred_at_ms",
    )
    stock_lot_id = f"assigned-stock-{event_id}"
    return build_wheel_event(
        event_id=f"wheel-started:{event_id}",
        account=account,
        stock_lot_id=stock_lot_id,
        event_type="wheel_started",
        occurred_at_ms=occurred_at_ms,
        recorded_at_ms=recorded_at_ms,
        source_trade_event_id=event_id,
        payload={
            "schema_version": "wheel_started.v1",
            "source_option_lot_id": str(event.get("target_lot_id") or "").strip(),
            "shares": shares,
            "assignment_price": price,
            "currency": str(stock.get("currency") or event.get("currency") or "").strip().upper(),
        },
    )


def wheel_called_away_event_from_call_assignment(
    terminal_event: Any,
    source_call_lot: Mapping[str, Any],
    stock_lot_before: Mapping[str, Any] | None,
    stock_lot_after: Mapping[str, Any] | None,
    *,
    recorded_at_ms: int,
) -> dict[str, Any] | None:
    event = _trade_event_fact(terminal_event)
    if _event_type(event) != "assignment":
        return None
    fields = _lot_fields(source_call_lot)
    strategy = str(fields.get("strategy") or "").strip().lower()
    leg_role = str(fields.get("leg_role") or "").strip().lower()
    stock_lot_id = str(fields.get("source_stock_lot_id") or "").strip()
    if strategy != "wheel" and leg_role != "wheel_call" and not stock_lot_id:
        return None
    if (
        strategy != "wheel"
        or leg_role != "wheel_call"
        or not stock_lot_id
        or str(fields.get("strategy_group_id") or "").strip()
        or str(fields.get("option_type") or "").strip().lower() != "call"
        or str(fields.get("side") or fields.get("position_side") or "").strip().lower()
        != "short"
    ):
        raise ValueError("Wheel Call assignment has incomplete or conflicting linkage")
    stock = _stock_settlement(event)
    if str(stock.get("side") or "").strip().lower() != "sell":
        raise ValueError("Wheel Call assignment requires sell-side stock settlement")
    contracts = _positive_int(event.get("contracts"), "assignment contracts")
    try:
        multiplier = int(float(event.get("multiplier") or fields.get("multiplier") or 0))
        shares = int(stock.get("shares") or stock.get("stock_qty") or 0)
        before = int((stock_lot_before or {}).get("shares_remaining"))
        after = int((stock_lot_after or {}).get("shares_remaining"))
    except (TypeError, ValueError):
        raise ValueError("Wheel Call assignment stock-lot evidence is incomplete") from None
    if multiplier <= 0 or shares != contracts * multiplier:
        raise ValueError("Wheel Call assignment settlement quantity is invalid")
    if (
        str((stock_lot_before or {}).get("stock_lot_id") or "") != stock_lot_id
        or str((stock_lot_after or {}).get("stock_lot_id") or "") != stock_lot_id
        or before - after != shares
        or after < 0
    ):
        raise ValueError("Wheel Call assignment did not exactly reduce its stock batch")
    if after > 0:
        return None
    source_event_id = _required_text(event.get("event_id"), "source_trade_event_id")
    account = _required_text(
        event.get("account") or fields.get("account"),
        "account",
    ).lower()
    occurred_at_ms = _positive_int(
        stock.get("event_time_ms") or event.get("event_time_ms"),
        "assignment occurred_at_ms",
    )
    return build_wheel_event(
        event_id=f"wheel-called-away:{source_event_id}:{stock_lot_id}",
        account=account,
        stock_lot_id=stock_lot_id,
        event_type="wheel_called_away",
        occurred_at_ms=occurred_at_ms,
        recorded_at_ms=recorded_at_ms,
        source_trade_event_id=source_event_id,
        payload={
            "schema_version": "wheel_called_away.v1",
            "source_call_lot_id": str(event.get("target_lot_id") or "").strip(),
            "shares": shares,
        },
    )


def plan_wheel_manual_end(
    wheel_batch: Mapping[str, Any],
    request_id: str,
    actor: str,
    *,
    occurred_at_ms: int,
    recorded_at_ms: int,
    account: str,
) -> dict[str, Any]:
    stock_lot_id = _required_text(wheel_batch.get("stock_lot_id"), "stock_lot_id")
    if wheel_batch.get("lifecycle_status") != "active":
        raise ValueError("Wheel lifecycle is not active")
    if wheel_batch.get("integrity_status") != "trusted":
        raise ValueError("Wheel lifecycle integrity is not trusted")
    if wheel_batch.get("active_call_lot_ids"):
        raise ValueError("Wheel lifecycle has an active Call")
    if wheel_batch.get("active_intent_ids"):
        raise ValueError("Wheel lifecycle has an active Call intent")
    request = _required_text(request_id, "request_id")
    actor_value = _required_text(actor, "actor")
    account_value = _required_text(account, "account").lower()
    event_digest = canonical_sha256(
        {
            "account": account_value,
            "stock_lot_id": stock_lot_id,
            "request_id": request,
        }
    )[:24]
    return build_wheel_event(
        event_id=f"wheel-manual-ended:{event_digest}",
        account=account_value,
        stock_lot_id=stock_lot_id,
        event_type="wheel_manual_ended",
        occurred_at_ms=occurred_at_ms,
        recorded_at_ms=recorded_at_ms,
        payload={
            "schema_version": "wheel_manual_ended.v1",
            "request_id": request,
            "actor": actor_value,
            "batch_generation_hash": str(
                wheel_batch.get("batch_generation_hash") or ""
            ),
        },
    )


def _lot_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    fields = row.get("fields")
    return dict(fields) if isinstance(fields, Mapping) else dict(row)


def _event_time(row: Mapping[str, Any]) -> int:
    try:
        return int(row.get("event_time_ms") or row.get("trade_time_ms") or 0)
    except (TypeError, ValueError):
        return 0


def _event_type(row: Mapping[str, Any]) -> str:
    payload = row.get("raw_payload")
    payload = payload if isinstance(payload, Mapping) else {}
    return str(
        row.get("event_type") or payload.get("close_type") or ""
    ).strip().lower()


def _trade_account(row: Mapping[str, Any]) -> str:
    key = row.get("contract_key")
    key = key if isinstance(key, Mapping) else {}
    return str(row.get("account") or key.get("account") or "").strip().lower()


def _trade_symbol(row: Mapping[str, Any]) -> str:
    key = row.get("contract_key")
    key = key if isinstance(key, Mapping) else {}
    return str(
        row.get("symbol") or key.get("underlying_symbol") or key.get("symbol") or ""
    ).strip().upper()


def _trade_option_type(row: Mapping[str, Any]) -> str:
    key = row.get("contract_key")
    key = key if isinstance(key, Mapping) else {}
    return str(row.get("option_type") or key.get("option_type") or "").strip().lower()


def _trade_position_side(row: Mapping[str, Any]) -> str:
    key = row.get("contract_key")
    key = key if isinstance(key, Mapping) else {}
    explicit = str(row.get("position_side") or key.get("position_side") or "").strip().lower()
    if explicit:
        return explicit
    side = str(row.get("side") or "").strip().lower()
    effect = str(row.get("position_effect") or "").strip().lower()
    if effect == "open":
        return "short" if side == "sell" else "long" if side == "buy" else ""
    if effect == "close":
        return "short" if side == "buy" else "long" if side == "sell" else ""
    return ""


def _active_trade_events(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    voided = {
        str(row.get("target_event_id") or "").strip()
        for row in rows
        if _event_type(row) == "void" and str(row.get("target_event_id") or "").strip()
    }
    return [
        dict(row)
        for row in rows
        if _event_type(row) != "void"
        and str(row.get("event_id") or "").strip() not in voided
    ]


def _contracts_open(fields: Mapping[str, Any]) -> int:
    try:
        if str(fields.get("status") or "").strip().lower() == "close":
            return 0
        return max(0, int(fields.get("contracts_open", fields.get("contracts", 0)) or 0))
    except (TypeError, ValueError):
        return 0


def _stable_stock_fact(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    fields = (
        "stock_lot_id",
        "source_assignment_event_id",
        "account",
        "broker",
        "symbol",
        "currency",
        "assigned_at_ms",
        "shares_opened",
        "shares_remaining",
        "shares_sold",
        "assignment_price",
        "assignment_fees",
        "stock_cost_basis_total",
        "stock_principal_basis_total",
        "stock_sale_cash_in_net",
        "stock_sale_cash_in_gross",
        "stock_sale_fees",
        "assigned_stock_realized_pnl",
        "sale_event_ids",
    )
    return {field: row.get(field) for field in fields}


def _intent_contracts(payload: Mapping[str, Any]) -> int | None:
    for field in ("contracts", "granted_contracts", "quantity"):
        value = payload.get(field)
        if value in (None, ""):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None
    return None


def _intent_state(
    events: Sequence[Mapping[str, Any]],
    *,
    as_of_ms: int,
    known_trade_event_ids: set[str],
) -> tuple[list[str], list[str]]:
    by_intent: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    reasons: list[str] = []
    for event in events:
        event_type = str(event.get("event_type") or "")
        if not event_type.startswith("wheel_call_intent_"):
            continue
        intent_id = str(event.get("intent_id") or "").strip()
        if not intent_id:
            reasons.append("intent_id_missing")
            continue
        by_intent[intent_id].append(event)
    active: list[str] = []
    for intent_id in sorted(by_intent):
        intent_events = by_intent[intent_id]
        created = [item for item in intent_events if item["event_type"] == "wheel_call_intent_created"]
        cancelled = [item for item in intent_events if item["event_type"] == "wheel_call_intent_cancelled"]
        consumed = [item for item in intent_events if item["event_type"] == "wheel_call_intent_consumed"]
        if len(created) != 1:
            reasons.append("intent_creation_conflict")
            continue
        creation = created[0]
        created_contracts = _intent_contracts(creation["payload"])
        try:
            expires_at_ms = int(creation["payload"].get("expires_at_ms") or 0)
        except (TypeError, ValueError):
            expires_at_ms = 0
        if created_contracts is None or expires_at_ms <= int(creation["occurred_at_ms"]):
            reasons.append("intent_contract_invalid")
            continue
        if len(cancelled) > 1:
            reasons.append("intent_cancellation_conflict")
            continue
        cancel_at = int(cancelled[0]["occurred_at_ms"]) if cancelled else None
        if cancel_at is not None and cancel_at < int(creation["occurred_at_ms"]):
            reasons.append("intent_causality_conflict")
            continue
        consumed_contracts = 0
        intent_conflict = False
        for item in consumed:
            quantity = _intent_contracts(item["payload"])
            source_id = str(item.get("source_trade_event_id") or "").strip()
            occurred_at_ms = int(item["occurred_at_ms"])
            if (
                quantity is None
                or not source_id
                or source_id not in known_trade_event_ids
                or occurred_at_ms < int(creation["occurred_at_ms"])
                or occurred_at_ms > expires_at_ms
                or (cancel_at is not None and occurred_at_ms >= cancel_at)
            ):
                intent_conflict = True
                break
            consumed_contracts += quantity
        if intent_conflict or consumed_contracts > created_contracts:
            reasons.append("intent_consumption_conflict")
            continue
        if consumed_contracts < created_contracts and cancel_at is None and as_of_ms <= expires_at_ms:
            active.append(intent_id)
    return active, reasons


def project_wheel_lifecycles(
    wheel_events: Sequence[Mapping[str, Any]],
    trade_events: Sequence[Mapping[str, Any]],
    position_lots: Sequence[Mapping[str, Any]],
    assigned_stock_projection: Mapping[str, Any],
    as_of_ms: int,
) -> list[dict[str, Any]]:
    """Rebuild Wheel batches from immutable facts; never guesses a missing link."""

    instant = _positive_int(as_of_ms, "as_of_ms")
    events_by_id: dict[str, dict[str, Any]] = {}
    invalid_by_group: dict[tuple[str, str], set[str]] = defaultdict(set)
    for raw in wheel_events:
        group = (
            str(raw.get("account") or "").strip().lower(),
            str(raw.get("stock_lot_id") or "").strip(),
        )
        try:
            event = normalize_wheel_event(raw)
        except (TypeError, ValueError):
            if all(group):
                invalid_by_group[group].add("invalid_wheel_event")
            continue
        previous = events_by_id.get(event["event_id"])
        if previous is not None and previous["payload_hash"] != event["payload_hash"]:
            invalid_by_group[(event["account"], event["stock_lot_id"])].add(
                "wheel_event_id_conflict"
            )
            invalid_by_group[(previous["account"], previous["stock_lot_id"])].add(
                "wheel_event_id_conflict"
            )
            continue
        if previous is None or event["recorded_at_ms"] < previous["recorded_at_ms"]:
            events_by_id[event["event_id"]] = event

    voided_ids: set[str] = set()
    valid_void_ids: set[str] = set()
    for event in events_by_id.values():
        if event["event_type"] != "wheel_event_voided":
            continue
        group = (event["account"], event["stock_lot_id"])
        target_id = str(event["payload"].get("target_wheel_event_id") or "").strip()
        target = events_by_id.get(target_id)
        if (
            target is None
            or target["event_type"] == "wheel_event_voided"
            or (target["account"], target["stock_lot_id"]) != group
        ):
            invalid_by_group[group].add("wheel_void_target_invalid")
            continue
        voided_ids.add(target_id)
        valid_void_ids.add(event["event_id"])

    effective_events = [
        event
        for event in events_by_id.values()
        if event["event_id"] not in voided_ids
        and (
            event["event_type"] != "wheel_event_voided"
            or event["event_id"] in valid_void_ids
        )
    ]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in effective_events:
        grouped[(event["account"], event["stock_lot_id"])].append(event)

    active_trade_events = _active_trade_events(trade_events)
    trade_by_id = {
        str(row.get("event_id") or "").strip(): row
        for row in active_trade_events
        if str(row.get("event_id") or "").strip()
    }
    all_stock_rows = assigned_stock_projection.get("_all_assigned_stock_lots")
    if not isinstance(all_stock_rows, Sequence):
        all_stock_rows = assigned_stock_projection.get("assigned_stock_lots") or []
    stock_rows_by_id: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in all_stock_rows:
        if isinstance(row, Mapping) and str(row.get("stock_lot_id") or "").strip():
            stock_rows_by_id[str(row["stock_lot_id"]).strip()].append(row)
    review_rows = [
        row
        for row in assigned_stock_projection.get("assigned_stock_review_rows") or []
        if isinstance(row, Mapping)
    ]

    lots = [(str(row.get("record_id") or "").strip(), _lot_fields(row)) for row in position_lots]
    results: list[dict[str, Any]] = []
    for group in sorted(grouped):
        account, stock_lot_id = group
        batch_events = sorted(
            grouped[group],
            key=lambda item: (int(item["occurred_at_ms"]), str(item["event_id"])),
        )
        starts = [item for item in batch_events if item["event_type"] == "wheel_started"]
        if not starts:
            continue
        reasons = set(invalid_by_group.get(group, set()))
        if len(starts) != 1:
            reasons.add("wheel_start_conflict")
        start = starts[0]
        terminals = [
            item
            for item in batch_events
            if item["event_type"] in {"wheel_called_away", "wheel_manual_ended"}
        ]
        if len(terminals) > 1:
            reasons.add("wheel_terminal_conflict")

        stock_matches = stock_rows_by_id.get(stock_lot_id, [])
        stock_row = stock_matches[0] if len(stock_matches) == 1 else None
        if len(stock_matches) > 1:
            reasons.add("assigned_stock_lot_conflict")
        start_trade_id = str(start.get("source_trade_event_id") or "").strip()
        start_trade = trade_by_id.get(start_trade_id)
        if (
            not start_trade_id
            or start_trade is None
            or _event_type(start_trade) != "assignment"
            or _trade_account(start_trade) != account
            or _trade_option_type(start_trade) != "put"
            or _trade_position_side(start_trade) != "short"
        ):
            reasons.add("wheel_start_source_invalid")
        if stock_row is not None and str(stock_row.get("source_assignment_event_id") or "") != start_trade_id:
            reasons.add("wheel_start_stock_lot_mismatch")

        linked_lots: list[tuple[str, dict[str, Any]]] = []
        for record_id, fields in lots:
            if str(fields.get("account") or "").strip().lower() != account:
                continue
            if str(fields.get("source_stock_lot_id") or "").strip() != stock_lot_id:
                continue
            if (
                str(fields.get("strategy") or "").strip().lower() != "wheel"
                or str(fields.get("leg_role") or "").strip().lower() != "wheel_call"
                or str(fields.get("strategy_group_id") or "").strip()
                or str(fields.get("option_type") or "").strip().lower() != "call"
                or str(fields.get("side") or "").strip().lower() != "short"
            ):
                reasons.add("wheel_call_linkage_conflict")
                continue
            linked_lots.append((record_id, fields))
        active_call_lot_ids = sorted(
            record_id for record_id, fields in linked_lots if _contracts_open(fields) > 0
        )

        assignment_ids = {
            str(row.get("event_id") or "").strip()
            for row in active_trade_events
            if _event_type(row) == "assignment"
            and str(row.get("target_lot_id") or "").strip()
            in {record_id for record_id, _fields in linked_lots}
        }
        called_events = [item for item in terminals if item["event_type"] == "wheel_called_away"]
        manual_events = [item for item in terminals if item["event_type"] == "wheel_manual_ended"]
        if called_events:
            source_id = str(called_events[0].get("source_trade_event_id") or "").strip()
            if source_id not in assignment_ids:
                reasons.add("wheel_called_away_source_invalid")

        active_intent_ids, intent_reasons = _intent_state(
            batch_events,
            as_of_ms=instant,
            known_trade_event_ids=set(trade_by_id),
        )
        reasons.update(intent_reasons)

        shares_remaining: int | None = None
        if stock_row is not None:
            try:
                shares_remaining = int(stock_row.get("shares_remaining"))
            except (TypeError, ValueError):
                reasons.add("assigned_stock_shares_unavailable")
            if shares_remaining is not None and shares_remaining < 0:
                reasons.add("assigned_stock_shares_conflict")
        multiplier = None
        if start_trade is not None:
            try:
                multiplier = int(float(start_trade.get("multiplier") or 0))
            except (TypeError, ValueError):
                multiplier = None
        if multiplier is None or multiplier <= 0:
            reasons.add("contract_multiplier_unavailable")

        locked_shares = 0
        for _record_id, fields in linked_lots:
            if _contracts_open(fields) <= 0:
                continue
            try:
                locked_shares += _contracts_open(fields) * int(float(fields.get("multiplier") or 0))
            except (TypeError, ValueError):
                reasons.add("wheel_call_multiplier_invalid")
        if shares_remaining is not None and locked_shares > shares_remaining:
            reasons.add("wheel_call_overcovers_batch")
        if manual_events and (active_call_lot_ids or active_intent_ids):
            reasons.add("manual_end_has_active_call_or_intent")
        if called_events and shares_remaining != 0:
            reasons.add("called_away_stock_not_zero")
        if shares_remaining == 0 and assignment_ids and not called_events:
            reasons.add("called_away_event_missing")

        for review in review_rows:
            if str(review.get("stock_lot_id") or "").strip() != stock_lot_id:
                continue
            if str(review.get("status") or "") in {
                "source_conflict",
                "incomplete_inventory_basis",
                "manual_review_required",
                "missing_stock_settlement",
            }:
                reasons.add("assigned_stock_projection_conflict")

        conflict_codes = {
            reason
            for reason in reasons
            if reason.endswith("conflict")
            or reason.endswith("_invalid")
            or reason in {
                "invalid_wheel_event",
                "wheel_start_stock_lot_mismatch",
                "manual_end_has_active_call_or_intent",
                "called_away_stock_not_zero",
                "called_away_event_missing",
            }
        }
        integrity_status = "conflict" if conflict_codes else "trusted"
        terminal = terminals[0] if len(terminals) == 1 else None
        lifecycle_status = (
            "called_away"
            if terminal is not None and terminal["event_type"] == "wheel_called_away"
            else "manual_ended"
            if terminal is not None and terminal["event_type"] == "wheel_manual_ended"
            else "active"
        )
        if integrity_status == "conflict" or lifecycle_status != "active":
            phase = None
        elif active_call_lot_ids:
            phase = "call_open"
        elif active_intent_ids:
            phase = "call_pending"
        elif shares_remaining is not None and multiplier is not None and shares_remaining < multiplier:
            phase = "residual_stock"
        elif stock_row is None or shares_remaining is None or multiplier is None:
            phase = "data_unavailable"
        else:
            phase = "ready"

        related_lot_ids = {record_id for record_id, _fields in linked_lots}
        related_trade_ids = {
            start_trade_id,
            *assignment_ids,
            *{
                str(fields.get("source_event_id") or "").strip()
                for _record_id, fields in linked_lots
            },
            *{
                str(item.get("source_trade_event_id") or "").strip()
                for item in batch_events
            },
        }
        related_trades = [
            row
            for row in active_trade_events
            if str(row.get("event_id") or "").strip() in related_trade_ids
            or str(row.get("target_lot_id") or "").strip() in related_lot_ids
        ]
        generation_payload = {
            "schema_version": WHEEL_PROJECTION_SCHEMA,
            "account": account,
            "stock_lot_id": stock_lot_id,
            "wheel_events": [
                {
                    key: event.get(key)
                    for key in (
                        "event_id",
                        "account",
                        "stock_lot_id",
                        "event_type",
                        "occurred_at_ms",
                        "intent_id",
                        "source_trade_event_id",
                        "payload",
                        "payload_hash",
                    )
                }
                for event in batch_events
            ],
            "position_lots": [
                {"record_id": record_id, "fields": fields}
                for record_id, fields in linked_lots
            ],
            "trade_events": related_trades,
            "assigned_stock": _stable_stock_fact(stock_row),
        }
        batch_generation_hash = canonical_sha256(generation_payload)
        result = {
            "stock_lot_id": stock_lot_id,
            "lifecycle_status": lifecycle_status,
            "phase": phase,
            "integrity_status": integrity_status,
            "reason_codes": sorted(reasons),
            "shares_remaining": shares_remaining,
            "batch_generation_hash": batch_generation_hash,
            "start_event_id": start["event_id"],
            "terminal_event_id": terminal["event_id"] if terminal is not None else None,
            "active_call_lot_ids": active_call_lot_ids,
            "active_intent_ids": active_intent_ids,
            "candidate": None,
        }
        result["projection_hash"] = canonical_sha256(
            {
                "schema_version": WHEEL_PROJECTION_SCHEMA,
                "batch_generation_hash": batch_generation_hash,
                "as_of_ms": instant,
                "derived": result,
            }
        )
        results.append(result)
    return results


__all__ = [
    "WHEEL_EVENT_SCHEMA",
    "WHEEL_EVENT_TYPES",
    "WHEEL_PROJECTION_SCHEMA",
    "build_wheel_event",
    "normalize_wheel_event",
    "plan_wheel_manual_end",
    "project_wheel_lifecycles",
    "wheel_called_away_event_from_call_assignment",
    "wheel_event_payload_hash",
    "wheel_started_event_from_assignment",
]
