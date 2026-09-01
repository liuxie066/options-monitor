from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterator

from domain.domain.ledger.position_fields import normalize_trade_price
from domain.domain.strategy_vocab import (
    STRATEGY_COVERED_CALL,
    STRATEGY_SELL_PUT,
    strategy_action_label,
)
from src.application.notification_delivery_adapter import (
    build_notification_transport_key,
    normalize_notification_delivery_result,
    select_notification_delivery_adapter,
)
from src.application.notification_delivery_route import (
    resolve_notification_delivery_route,
)
from src.application.notification_shells import render_receipt
from src.application.trade_time_format import format_trade_time_beijing
from src.application.trades.deal_identity import (
    active_ledger_events,
    structured_deal_keys_from_ledger_event,
)
from src.application.trades.lifecycle_outbox import (
    build_notification_batch_route,
)
from src.application.trades.receipt import (
    classify_trade_lifecycle_delivery_result,
)
from src.application.trades.state import (
    append_trade_intake_audit,
    load_trade_intake_state,
    lookup_deal_state_entry,
)
from src.infrastructure.io_utils import atomic_write_json, utc_now


COMPENSATION_SCHEMA_VERSION = "trade_intake_receipt_compensation.v1"
LEGACY_FALSE_OUTBOX_REASON = "legacy_false_outbox_marker"
SKIPPED_NO_ROUTE_REASON = "skipped_no_route"
SUPPORTED_COMPENSATION_REASONS = {
    LEGACY_FALSE_OUTBOX_REASON,
    SKIPPED_NO_ROUTE_REASON,
}


def compensate_trade_intake_receipts(
    *,
    base: Path,
    config: dict[str, Any],
    sources: list[dict[str, Any]],
    repo: Any,
    account: str,
    deal_ids: list[str],
    apply_changes: bool,
    expected_payload_hash: str | None = None,
    reason: str = LEGACY_FALSE_OUTBOX_REASON,
    send_fn: Callable[..., Any] | None = None,
    normalize_fn: Callable[..., dict[str, Any]] | None = None,
    route_resolver: Callable[..., dict[str, Any]] = (
        resolve_notification_delivery_route
    ),
    adapter_selector: Callable[[Any], Any] = (
        select_notification_delivery_adapter
    ),
    now_fn: Callable[[], str] = utc_now,
) -> dict[str, Any]:
    """Preview or send one guarded receipt for already-recorded open trades."""

    source = _select_source(sources, account=account)
    if not apply_changes:
        plan = _build_plan(
            config=config,
            source=source,
            repo=repo,
            account=account,
            deal_ids=deal_ids,
            reason=reason,
            route_resolver=route_resolver,
        )
        record_path = Path(plan["record_path"])
        if record_path.exists():
            return {
                **_existing_record_result(plan, record_path=record_path),
                "dry_run": True,
            }
        return _preview_payload(plan)

    lock_path = Path(source["state_path"]).parent / "receipt_compensations.lock"
    with _exclusive_lock(lock_path):
        plan = _build_plan(
            config=config,
            source=source,
            repo=repo,
            account=account,
            deal_ids=deal_ids,
            reason=reason,
            route_resolver=route_resolver,
        )
        _validate_expected_payload_hash(
            plan,
            expected_payload_hash=expected_payload_hash,
        )
        return _execute_plan(
            base=base,
            plan=plan,
            send_fn=send_fn,
            normalize_fn=normalize_fn,
            adapter_selector=adapter_selector,
            now_fn=now_fn,
        )


def _select_source(
    sources: list[dict[str, Any]],
    *,
    account: str,
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    if not account_value or account_value != str(account or "").strip():
        raise ValueError("receipt compensation account must be a lowercase label")
    matches = [
        dict(source)
        for source in sources
        if str(source.get("account") or "").strip().lower() == account_value
    ]
    if len(matches) != 1:
        raise ValueError(
            "receipt compensation requires exactly one configured intake source "
            f"for account={account_value}; matched={len(matches)}"
        )
    source = matches[0]
    receipt_config = (
        dict(source.get("receipt") or {})
        if isinstance(source.get("receipt"), dict)
        else {}
    )
    if receipt_config.get("enabled", True) is False:
        raise ValueError(
            f"trade receipt delivery is disabled for account={account_value}"
        )
    for key in ("state_path", "audit_path"):
        if not str(source.get(key) or "").strip():
            raise ValueError(f"intake source is missing {key}")
    return source


def _build_plan(
    *,
    config: dict[str, Any],
    source: dict[str, Any],
    repo: Any,
    account: str,
    deal_ids: list[str],
    reason: str,
    route_resolver: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    account_value = str(account).strip().lower()
    canonical_ids = _canonical_deal_ids(deal_ids, account=account_value)
    reason_value = str(reason or "").strip()
    if reason_value not in SUPPORTED_COMPENSATION_REASONS:
        raise ValueError(
            "unsupported receipt compensation reason; expected one of "
            + ", ".join(sorted(SUPPORTED_COMPENSATION_REASONS))
        )

    state_path = Path(source["state_path"])
    audit_path = Path(source["audit_path"])
    if not state_path.is_file():
        raise ValueError(f"trade intake state does not exist: {state_path}")
    state = load_trade_intake_state(state_path)
    state_rows = {
        deal_id: _validated_compensable_state_row(
            state,
            deal_id=deal_id,
            account=account_value,
            reason=reason_value,
        )
        for deal_id in canonical_ids
    }

    events = active_ledger_events(_list_trade_events(repo))
    members = [
        _build_member(
            events,
            deal_id=deal_id,
            state_row=state_rows[deal_id],
            account=account_value,
        )
        for deal_id in canonical_ids
    ]
    _validate_uniform_open_contract(members)
    members.sort(
        key=lambda item: (
            int(item.get("trade_time_ms") or 0),
            str(item.get("canonical_deal_id") or ""),
        )
    )
    message = _build_message(account=account_value, members=members)

    route = route_resolver(config=config)
    route = dict(route) if isinstance(route, dict) else {}
    route_snapshot = build_notification_batch_route(
        provider=str(route.get("provider") or ""),
        channel=str(route.get("channel") or ""),
        target=str(route.get("target") or ""),
    )
    compensation_id = _compensation_id(
        account=account_value,
        deal_ids=canonical_ids,
    )
    transport_key = build_notification_transport_key(compensation_id)
    public_route = {
        key: route_snapshot[key]
        for key in (
            "provider",
            "channel",
            "target_fingerprint",
            "route_fingerprint",
        )
    }
    payload_hash = _payload_hash(
        {
            "schema_version": COMPENSATION_SCHEMA_VERSION,
            "compensation_id": compensation_id,
            "reason": reason_value,
            "account": account_value,
            "deal_ids": canonical_ids,
            "members": members,
            "message": message,
            "route": public_route,
            "transport_idempotency_key": transport_key,
        }
    )
    record_path = (
        state_path.parent
        / "receipt_compensations"
        / f"{compensation_id}.json"
    )
    return {
        "schema_version": COMPENSATION_SCHEMA_VERSION,
        "compensation_id": compensation_id,
        "reason": reason_value,
        "account": account_value,
        "deal_ids": canonical_ids,
        "members": members,
        "message": message,
        "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
        "payload_hash": payload_hash,
        "route": public_route,
        "transport_idempotency_key": transport_key,
        "state_path": str(state_path),
        "audit_path": str(audit_path),
        "record_path": str(record_path),
        "source_id": str(source.get("id") or account_value),
        "_delivery_route": route_snapshot,
        "_notifications": (
            dict(route.get("notifications") or {})
            if isinstance(route.get("notifications"), dict)
            else {}
        ),
    }


def _canonical_deal_ids(
    deal_ids: list[str],
    *,
    account: str,
) -> list[str]:
    values = [str(value or "").strip() for value in list(deal_ids or [])]
    if not values or any(not value for value in values):
        raise ValueError("at least one canonical --deal-id is required")
    if len(set(values)) != len(values):
        raise ValueError("receipt compensation deal IDs must be unique")
    for value in values:
        parts = value.split(":", 3)
        if (
            len(parts) != 4
            or parts[0] != "futu"
            or parts[1] != account
            or not parts[2].isdigit()
            or not parts[3].isdigit()
        ):
            raise ValueError(
                "receipt compensation requires canonical IDs in the form "
                f"futu:{account}:<futu_account_id>:<deal_id>"
            )
    return sorted(values)


def _validated_compensable_state_row(
    state: dict[str, Any],
    *,
    deal_id: str,
    account: str,
    reason: str,
) -> dict[str, Any]:
    entry = lookup_deal_state_entry(state, deal_id)
    if entry is None:
        raise ValueError(f"trade intake state is missing deal_id={deal_id}")
    bucket, row = entry
    if bucket != "processed_deal_ids":
        raise ValueError(
            f"receipt compensation requires processed state: {deal_id} bucket={bucket}"
        )
    if str(row.get("status") or "").strip().lower() != "applied":
        raise ValueError(f"receipt compensation requires applied trade: {deal_id}")
    if str(row.get("action") or "").strip().lower() != "open":
        raise ValueError(f"receipt compensation only supports open trades: {deal_id}")
    if str(row.get("reason") or "").strip().lower() != "applied_open":
        raise ValueError(
            f"receipt compensation requires applied_open state: {deal_id}"
        )
    if str(row.get("account") or "").strip().lower() != account:
        raise ValueError(f"receipt compensation account mismatch: {deal_id}")

    parts = deal_id.split(":", 3)
    if str(row.get("futu_account_id") or "").strip() != parts[2]:
        raise ValueError(f"receipt compensation Futu account mismatch: {deal_id}")
    if str(row.get("source_deal_id") or "").strip() != parts[3]:
        raise ValueError(f"receipt compensation source deal mismatch: {deal_id}")

    receipt = row.get("receipt")
    if not isinstance(receipt, dict):
        raise ValueError(f"receipt compensation requires stored receipt evidence: {deal_id}")
    if bool(receipt.get("delivery_confirmed")) or str(
        receipt.get("message_id") or ""
    ).strip():
        raise ValueError(f"receipt is already delivery-confirmed: {deal_id}")
    receipt_status = str(receipt.get("status") or "").strip().lower()
    receipt_reason = str(receipt.get("reason") or "").strip().lower()
    if reason == LEGACY_FALSE_OUTBOX_REASON:
        if (
            receipt_status != "outbox_managed"
            or receipt_reason != "transactional_outbox"
        ):
            raise ValueError(
                f"receipt is not the legacy false outbox marker: {deal_id}"
            )
    elif reason == SKIPPED_NO_ROUTE_REASON:
        if (
            receipt_status != "skipped"
            or receipt_reason != SKIPPED_NO_ROUTE_REASON
            or receipt.get("target_set") is not False
        ):
            raise ValueError(
                f"receipt is not an unsent no-route marker: {deal_id}"
            )
    claimed_outbox_ids = [
        value
        for value in [
            receipt.get("outbox_id"),
            *(
                list(receipt.get("outbox_ids") or [])
                if isinstance(receipt.get("outbox_ids"), list)
                else []
            ),
        ]
        if str(value or "").strip()
    ]
    if claimed_outbox_ids or bool(receipt.get("outbox_readback_confirmed")):
        raise ValueError(
            f"receipt has durable outbox evidence and must not be compensated: {deal_id}"
        )
    return dict(row)


def _list_trade_events(repo: Any) -> list[dict[str, Any]]:
    candidate = getattr(repo, "primary_repo", repo)
    list_fn = getattr(candidate, "list_trade_events", None)
    if not callable(list_fn):
        raise TypeError("option_positions repo does not expose list_trade_events")
    rows = list_fn()
    if not isinstance(rows, list):
        raise TypeError("option_positions repo returned non-list trade_events")
    return [dict(item) for item in rows if isinstance(item, dict)]


def _build_member(
    events: list[dict[str, Any]],
    *,
    deal_id: str,
    state_row: dict[str, Any],
    account: str,
) -> dict[str, Any]:
    matched = [
        event
        for event in events
        if str(event.get("event_id") or "").strip() == deal_id
        or deal_id in structured_deal_keys_from_ledger_event(event)
    ]
    if not matched:
        raise ValueError(f"canonical ledger event is missing for deal_id={deal_id}")
    if any(
        str(event.get("account") or "").strip().lower() != account
        for event in matched
    ):
        raise ValueError(f"ledger event account mismatch for deal_id={deal_id}")

    contract_fields = (
        "symbol",
        "option_type",
        "side",
        "position_effect",
        "expiration_ymd",
        "strike",
        "currency",
        "multiplier",
    )
    normalized = {
        field: {_normalized_scalar(event.get(field)) for event in matched}
        for field in contract_fields
    }
    inconsistent = [field for field, values in normalized.items() if len(values) != 1]
    if inconsistent:
        raise ValueError(
            f"ledger events disagree for deal_id={deal_id}: {','.join(inconsistent)}"
        )
    first = matched[0]
    contracts = sum(_positive_int(event.get("contracts"), field="contracts") for event in matched)
    prices = {
        Decimal(
            str(
                normalize_trade_price(
                    event.get("price"),
                    field_name="price",
                )
            )
        )
        for event in matched
    }
    if len(prices) != 1:
        raise ValueError(f"ledger event prices disagree for deal_id={deal_id}")
    price = next(iter(prices))
    if price <= 0:
        raise ValueError(f"ledger event price must be positive: {deal_id}")
    multiplier = _positive_int(first.get("multiplier"), field="multiplier")
    strike = _decimal(first.get("strike"), field="strike")
    if strike <= 0:
        raise ValueError(f"ledger event strike must be positive: {deal_id}")
    trade_times = [
        _positive_int(event.get("trade_time_ms"), field="trade_time_ms")
        for event in matched
    ]
    trade_time_ms = min(trade_times)
    premium_amount = price * Decimal(contracts) * Decimal(multiplier)
    return {
        "canonical_deal_id": deal_id,
        "source_deal_id": str(state_row.get("source_deal_id") or ""),
        "event_ids": sorted(
            str(event.get("event_id") or "").strip() for event in matched
        ),
        "account": account,
        "symbol": str(first.get("symbol") or "").strip().upper(),
        "option_type": str(first.get("option_type") or "").strip().lower(),
        "side": str(first.get("side") or "").strip().lower(),
        "position_effect": str(first.get("position_effect") or "").strip().lower(),
        "expiration_ymd": str(first.get("expiration_ymd") or "").strip(),
        "strike": float(strike),
        "currency": str(first.get("currency") or "").strip().upper(),
        "multiplier": int(multiplier),
        "contracts": contracts,
        "price": float(price),
        "premium_amount": float(premium_amount),
        "trade_time_ms": trade_time_ms,
        "trade_time_beijing": format_trade_time_beijing(trade_time_ms),
        "prior_receipt": dict(state_row.get("receipt") or {}),
    }


def _validate_uniform_open_contract(members: list[dict[str, Any]]) -> None:
    if not members:
        raise ValueError("receipt compensation has no ledger members")
    common_fields = (
        "account",
        "symbol",
        "option_type",
        "side",
        "position_effect",
        "expiration_ymd",
        "strike",
        "currency",
        "multiplier",
    )
    inconsistent = [
        field
        for field in common_fields
        if len({_normalized_scalar(item.get(field)) for item in members}) != 1
    ]
    if inconsistent:
        raise ValueError(
            "combined receipt requires one uniform option contract: "
            + ",".join(inconsistent)
        )
    first = members[0]
    missing = [
        field
        for field in ("symbol", "expiration_ymd", "currency")
        if not str(first.get(field) or "").strip()
    ]
    if missing:
        raise ValueError(
            "receipt compensation ledger contract is incomplete: "
            + ",".join(missing)
        )
    if (
        first.get("side") != "sell"
        or first.get("position_effect") != "open"
        or first.get("option_type") not in {"put", "call"}
    ):
        raise ValueError(
            "receipt compensation only supports already-recorded CSP/CC opens"
        )


def _build_message(
    *,
    account: str,
    members: list[dict[str, Any]],
) -> str:
    first = members[0]
    option_type = str(first["option_type"])
    option_label = "Put" if option_type == "put" else "Call"
    action_label = strategy_action_label(
        STRATEGY_SELL_PUT if option_type == "put" else STRATEGY_COVERED_CALL
    )
    total_contracts = sum(int(item["contracts"]) for item in members)
    total_premium = sum(
        (_decimal(item["premium_amount"], field="premium_amount") for item in members),
        Decimal("0"),
    )
    currency = str(first["currency"])
    prices = {_decimal(item["price"], field="price") for item in members}
    price_text = (
        f"{_format_decimal(next(iter(prices)))} {currency}"
        if len(prices) == 1
        else "见成交明细"
    )
    fields: list[tuple[str, object]] = [
        ("动作", f"{action_label} 开仓"),
        ("标的", first["symbol"]),
        (
            "合约",
            f"{first['expiration_ymd']} {_format_decimal(_decimal(first['strike'], field='strike'))} {option_label}",
        ),
        ("数量", f"{len(members)} 笔 · {total_contracts} 张"),
        ("成交", price_text),
        ("资金", f"权利金毛流入 {currency} {_format_decimal(total_premium, places=2)}"),
        ("说明", "原交易已入账；本消息仅补充历史回执，不会重复记账。"),
    ]
    rows = [
        "｜".join(
            [
                str(item.get("trade_time_beijing") or "-"),
                f"{item['contracts']} 张",
                f"{_format_decimal(_decimal(item['price'], field='price'))} {currency}",
                f"`{item['source_deal_id']}`",
            ]
        )
        for item in members
    ]
    return render_receipt(
        account=account,
        receipt_type="历史成交补充",
        status="✅ 已入账",
        fields=fields,
        sections=(("成交明细（原交易）", rows),),
    )


def _preview_payload(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "ready",
        "dry_run": True,
        "write_applied": False,
        **_public_plan(plan),
    }


def _validate_expected_payload_hash(
    plan: dict[str, Any],
    *,
    expected_payload_hash: str | None,
) -> None:
    expected = str(expected_payload_hash or "").strip().lower()
    if not expected:
        raise ValueError(
            "receipt compensation apply requires the payload_hash from dry-run"
        )
    actual = str(plan.get("payload_hash") or "").strip().lower()
    if expected != actual:
        raise ValueError(
            "receipt compensation payload changed after dry-run; preview again"
        )


def _execute_plan(
    *,
    base: Path,
    plan: dict[str, Any],
    send_fn: Callable[..., Any] | None,
    normalize_fn: Callable[..., dict[str, Any]] | None,
    adapter_selector: Callable[[Any], Any],
    now_fn: Callable[[], str],
) -> dict[str, Any]:
    record_path = Path(plan["record_path"])
    if record_path.exists():
        return _existing_record_result(plan, record_path=record_path)

    route = dict(plan["_delivery_route"])
    if send_fn is None or normalize_fn is None:
        adapter = adapter_selector(route["provider"])
        resolved_send_fn = send_fn or adapter.send_fn
        resolved_normalize_fn = normalize_fn or adapter.normalize_fn
    else:
        resolved_send_fn = send_fn
        resolved_normalize_fn = normalize_fn

    created_at = now_fn()
    prepared_record = {
        **_public_plan(plan),
        "status": "prepared",
        "delivery_confirmed": False,
        "message_id": None,
        "attempt_count": 0,
        "created_at": created_at,
        "updated_at": created_at,
    }
    _write_json_exclusive(record_path, prepared_record)
    _append_audit(
        plan,
        phase="receipt_compensation_prepared",
        outcome={
            "status": "prepared",
            "delivery_confirmed": False,
            "message_id": None,
        },
    )

    send_started_at = now_fn()
    send_started_record = {
        **prepared_record,
        "status": "send_started",
        "attempt_count": 1,
        "send_started_at": send_started_at,
        "updated_at": send_started_at,
    }
    atomic_write_json(record_path, send_started_record, sort_keys=True)

    try:
        send_result = resolved_send_fn(
            base=base,
            channel=str(route["channel"]),
            target=str(route["target"]),
            message=str(plan["message"]),
            notifications=dict(plan["_notifications"]),
            idempotency_key=str(plan["transport_idempotency_key"]),
        )
        normalized = normalize_notification_delivery_result(
            send_result,
            normalize_fn=resolved_normalize_fn,
        )
        classification = classify_trade_lifecycle_delivery_result(normalized)
        outcome = str(classification["outcome"])
        receipt = _receipt_evidence(normalized)
    except Exception as exc:
        outcome = "unknown"
        classification = {
            "outcome": "unknown",
            "delivery_confirmed": False,
            "command_ok": False,
            "explicit_pre_acceptance_failure": False,
            "classification_evidence": {
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
            },
        }
        receipt = {
            "delivery_confirmed": False,
            "message_id": None,
            "command_ok": False,
            "error_code": "SEND_EXCEPTION_UNKNOWN",
            "send_message": f"{type(exc).__name__}: {exc}",
        }

    completed_at = now_fn()
    delivery_confirmed = bool(classification["delivery_confirmed"])
    message_id = str(receipt.get("message_id") or "").strip() or None
    final_record = {
        **send_started_record,
        "status": outcome,
        "delivery_confirmed": delivery_confirmed,
        "message_id": message_id,
        "explicit_pre_acceptance_failure": bool(
            classification["explicit_pre_acceptance_failure"]
        ),
        "classification_evidence": dict(
            classification.get("classification_evidence") or {}
        ),
        "receipt": receipt,
        "completed_at": completed_at,
        "updated_at": completed_at,
    }
    atomic_write_json(record_path, final_record, sort_keys=True)
    _append_audit(
        plan,
        phase=f"receipt_compensation_{outcome}",
        outcome={
            "status": outcome,
            "delivery_confirmed": delivery_confirmed,
            "message_id": message_id,
            "explicit_pre_acceptance_failure": bool(
                classification["explicit_pre_acceptance_failure"]
            ),
            "classification_evidence": dict(
                classification.get("classification_evidence") or {}
            ),
        },
    )
    return {
        "ok": delivery_confirmed,
        "status": outcome,
        "dry_run": False,
        "write_applied": True,
        "delivery_confirmed": delivery_confirmed,
        "message_id": message_id,
        "receipt": receipt,
        **_public_plan(plan),
    }


def _existing_record_result(
    plan: dict[str, Any],
    *,
    record_path: Path,
) -> dict[str, Any]:
    try:
        existing = json.loads(record_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(
            f"receipt compensation record is unreadable: {record_path}: {exc}"
        ) from exc
    if not isinstance(existing, dict):
        raise ValueError(f"receipt compensation record is invalid: {record_path}")
    for key in ("compensation_id", "payload_hash"):
        if str(existing.get(key) or "") != str(plan.get(key) or ""):
            raise ValueError(
                f"receipt compensation record identity mismatch: {record_path}"
            )
    prior_status = str(existing.get("status") or "unknown").strip().lower()
    confirmed = bool(existing.get("delivery_confirmed")) and bool(
        str(existing.get("message_id") or "").strip()
    )
    return {
        "ok": confirmed,
        "status": "duplicate_suppressed",
        "prior_status": prior_status,
        "dry_run": False,
        "write_applied": False,
        "delivery_confirmed": confirmed,
        "message_id": str(existing.get("message_id") or "").strip() or None,
        "suppression_reason": (
            "already_confirmed"
            if confirmed
            else "existing_nonterminal_or_unconfirmed_compensation"
        ),
        **_public_plan(plan),
    }


def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in plan.items()
        if not str(key).startswith("_")
    }


def _receipt_evidence(normalized: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "delivery_confirmed",
        "message_id",
        "command_ok",
        "error_code",
        "send_message",
        "message",
        "http_status",
        "provider_response_code",
        "ambiguous_send",
        "duplicate_risk",
        "idempotency_key",
        "effective_idempotency_key",
        "retry_attempt_count",
        "fallback_used",
        "local_error_code",
    )
    return {
        key: normalized.get(key)
        for key in keys
        if normalized.get(key) is not None
    }


def _append_audit(
    plan: dict[str, Any],
    *,
    phase: str,
    outcome: dict[str, Any],
) -> None:
    append_trade_intake_audit(
        plan["audit_path"],
        {
            "phase": phase,
            "source": "operator_receipt_compensation",
            "schema_version": COMPENSATION_SCHEMA_VERSION,
            "compensation_id": plan["compensation_id"],
            "reason": plan["reason"],
            "account": plan["account"],
            "deal_ids": list(plan["deal_ids"]),
            "message_sha256": plan["message_sha256"],
            "payload_hash": plan["payload_hash"],
            "route": dict(plan["route"]),
            "transport_idempotency_key": plan[
                "transport_idempotency_key"
            ],
            "outcome": dict(outcome),
            "record_path": plan["record_path"],
            "updated_at": utc_now(),
        },
    )


def _compensation_id(*, account: str, deal_ids: list[str]) -> str:
    raw = "\n".join(
        [COMPENSATION_SCHEMA_VERSION, account, *sorted(deal_ids)]
    )
    return "trade-receipt-comp-" + hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:32]


def _payload_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _write_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _normalized_scalar(value: Any) -> str:
    if isinstance(value, float):
        return _format_decimal(_decimal(value, field="value"))
    return str(value if value is not None else "").strip().lower()


def _positive_int(value: Any, *, field: str) -> int:
    numeric = _decimal(value, field=field)
    if numeric != numeric.to_integral_value():
        raise ValueError(f"ledger event {field} must be an integer")
    normalized = int(numeric)
    if normalized <= 0:
        raise ValueError(f"ledger event {field} must be positive")
    return normalized


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"ledger event {field} must be numeric") from exc
    if not normalized.is_finite():
        raise ValueError(f"ledger event {field} must be finite")
    return normalized


def _format_decimal(value: Decimal, *, places: int | None = None) -> str:
    if places is not None:
        return f"{value:,.{places}f}"
    normalized = value.normalize()
    text = format(normalized, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


__all__ = [
    "COMPENSATION_SCHEMA_VERSION",
    "LEGACY_FALSE_OUTBOX_REASON",
    "compensate_trade_intake_receipts",
]
