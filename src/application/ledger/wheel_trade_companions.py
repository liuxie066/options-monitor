from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping, Sequence

from domain.domain.wheel import (
    plan_wheel_call_intent_consume,
    project_wheel_call_intents,
    project_wheel_lifecycles,
    wheel_called_away_event_from_call_assignment,
    wheel_started_event_from_assignment,
)
from domain.domain.risk_capacity import revalidate_opening_share_coverage
from src.application.ledger.assigned_stock_projection import (
    project_assigned_stock_lifecycle_from_rows,
)


def _wheel_batches_from_rows(
    rows: Mapping[str, Any],
    *,
    account: str,
    as_of_ms: int,
) -> list[dict[str, Any]]:
    assigned_stock = project_assigned_stock_lifecycle_from_rows(
        rows,
        account=account,
        as_of_ms=as_of_ms,
    )
    return project_wheel_lifecycles(
        rows.get("account_wheel_events") or [],
        rows.get("trade_events") or [],
        rows.get("account_position_lots") or [],
        assigned_stock,
        as_of_ms,
    )


def _event_account(event: Any) -> str:
    key = getattr(event, "contract_key", None)
    return str(getattr(key, "account", "") or "").strip().lower()


def _event_time_ms(event: Any) -> int:
    return int(getattr(event, "event_time_ms", 0) or 0)


def _stock_lot(report: Mapping[str, Any], stock_lot_id: str) -> dict[str, Any] | None:
    rows = report.get("_all_assigned_stock_lots") or report.get("assigned_stock_lots") or []
    matches = [
        dict(item)
        for item in rows
        if isinstance(item, Mapping)
        and str(item.get("stock_lot_id") or "").strip() == stock_lot_id
    ]
    if len(matches) > 1:
        raise ValueError(f"assigned stock lot is not unique: {stock_lot_id}")
    return matches[0] if matches else None


def capture_wheel_trade_companion_context(
    repo: Any,
    *,
    conn: Any,
    events: Sequence[Any],
    wheel_start_enabled: bool,
) -> dict[str, Any]:
    source_fields: dict[str, dict[str, Any]] = {}
    call_accounts: set[str] = set()
    for event in events:
        if str(getattr(event, "event_type", "") or "").strip().lower() != "assignment":
            continue
        event_id = str(getattr(event, "event_id", "") or "").strip()
        target_lot_id = str(getattr(event, "target_lot_id", "") or "").strip()
        if not event_id or not target_lot_id:
            continue
        fields = repo.get_position_lot_fields(target_lot_id, conn=conn)
        source_fields[event_id] = fields
        strategy = str(fields.get("strategy") or "").strip().lower()
        leg_role = str(fields.get("leg_role") or "").strip().lower()
        source_stock_lot_id = str(fields.get("source_stock_lot_id") or "").strip()
        if strategy == "wheel" or leg_role == "wheel_call" or source_stock_lot_id:
            call_accounts.add(_event_account(event))
    before_rows = {
        account: repo.read_lifecycle_account_rows(account=account, conn=conn)
        for account in sorted(call_accounts)
        if account
    }
    return {
        "wheel_start_enabled": bool(wheel_start_enabled),
        "source_fields": source_fields,
        "before_rows": before_rows,
    }


def append_wheel_trade_companions(
    repo: Any,
    *,
    conn: Any,
    events: Sequence[Any],
    created_flags: Sequence[bool],
    context: Mapping[str, Any],
    recorded_at_ms: int,
) -> dict[str, str]:
    source_fields = dict(context.get("source_fields") or {})
    before_rows = dict(context.get("before_rows") or {})
    new_assignments = [
        event
        for event, created in zip(events, created_flags, strict=True)
        if created
        and str(getattr(event, "event_type", "") or "").strip().lower() == "assignment"
    ]
    if not new_assignments:
        return {}
    accounts = sorted({_event_account(event) for event in new_assignments if _event_account(event)})
    after_rows = {
        account: repo.read_lifecycle_account_rows(account=account, conn=conn)
        for account in accounts
    }
    before_assigned: dict[tuple[str, int], dict[str, Any]] = {}
    after_assigned: dict[tuple[str, int], dict[str, Any]] = {}
    companion_by_trade: dict[str, str] = {}
    for event in sorted(
        new_assignments,
        key=lambda item: (_event_time_ms(item), str(getattr(item, "event_id", ""))),
    ):
        event_id = str(getattr(event, "event_id", "") or "").strip()
        account = _event_account(event)
        fields = source_fields.get(event_id)
        if fields is None:
            raise ValueError(f"assignment source lot was not captured: {event_id}")
        companion = None
        if bool(context.get("wheel_start_enabled")):
            companion = wheel_started_event_from_assignment(
                event,
                fields,
                recorded_at_ms=recorded_at_ms,
            )
        strategy = str(fields.get("strategy") or "").strip().lower()
        leg_role = str(fields.get("leg_role") or "").strip().lower()
        stock_lot_id = str(fields.get("source_stock_lot_id") or "").strip()
        if strategy == "wheel" or leg_role == "wheel_call" or stock_lot_id:
            instant = _event_time_ms(event)
            key = (account, instant)
            if key not in before_assigned:
                before_assigned[key] = project_assigned_stock_lifecycle_from_rows(
                    before_rows[account],
                    account=account,
                    as_of_ms=instant,
                )
                after_assigned[key] = project_assigned_stock_lifecycle_from_rows(
                    after_rows[account],
                    account=account,
                    as_of_ms=instant,
                )
            companion = wheel_called_away_event_from_call_assignment(
                event,
                fields,
                _stock_lot(before_assigned[key], stock_lot_id),
                _stock_lot(after_assigned[key], stock_lot_id),
                recorded_at_ms=recorded_at_ms,
            )
        if companion is None:
            continue
        repo.append_wheel_event_once(companion, conn=conn)
        companion_by_trade[event_id] = companion["event_id"]

    if companion_by_trade:
        verification_rows = {
            account: repo.read_lifecycle_account_rows(account=account, conn=conn)
            for account in accounts
        }
        for event in new_assignments:
            event_id = str(getattr(event, "event_id", "") or "").strip()
            companion_id = companion_by_trade.get(event_id)
            if not companion_id:
                continue
            account = _event_account(event)
            batches = _wheel_batches_from_rows(
                verification_rows[account],
                account=account,
                as_of_ms=max(_event_time_ms(event), 1),
            )
            matches = [
                batch
                for batch in batches
                if batch["start_event_id"] == companion_id
                or batch["terminal_event_id"] == companion_id
            ]
            if len(matches) != 1 or matches[0]["integrity_status"] != "trusted":
                raise ValueError("Wheel companion event projection verification failed")
    return companion_by_trade


def prepare_wheel_intent_open_event(
    rows: Mapping[str, Any],
    event: Any,
    coverage_fact: Mapping[str, Any],
    *,
    recorded_at_ms: int,
) -> tuple[Any, dict[str, Any] | None, str]:
    if (
        str(getattr(event, "event_type", "") or "").strip().lower() != "open"
        or str(getattr(getattr(event, "contract_key", None), "option_type", ""))
        != "call"
        or str(getattr(getattr(event, "contract_key", None), "position_side", ""))
        != "short"
    ):
        return event, None, "not_short_call_open"
    account = _event_account(event)
    instant = _event_time_ms(event)
    batches = _wheel_batches_from_rows(
        rows,
        account=account,
        as_of_ms=instant,
    )
    contract_key = getattr(event, "contract_key", None)
    current_coverage = revalidate_opening_share_coverage(
        coverage_fact,
        list(rows.get("account_position_lots") or []),
        batches,
        account=account,
        symbol=str(getattr(contract_key, "underlying_symbol", "") or ""),
    )
    known_trade_ids = {
        str(item.get("event_id") or "").strip()
        for item in rows.get("trade_events") or []
        if str(item.get("event_id") or "").strip()
    }
    plans: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for batch in batches:
        if batch["lifecycle_status"] != "active" or batch["integrity_status"] != "trusted":
            continue
        summaries = project_wheel_call_intents(
            rows.get("account_wheel_events") or [],
            account=account,
            stock_lot_id=batch["stock_lot_id"],
            as_of_ms=instant,
            known_trade_event_ids=known_trade_ids,
        )
        for intent in summaries:
            if intent.get("status") != "active":
                continue
            intent_payload = intent.get("payload")
            intent_payload = (
                intent_payload if isinstance(intent_payload, Mapping) else intent
            )
            intent_coverage = {
                **current_coverage,
                "shares_available_for_cover": int(
                    current_coverage.get("shares_available_for_cover") or 0
                )
                + int(intent.get("remaining_contracts") or 0)
                * int(intent_payload.get("multiplier") or 0),
            }
            try:
                plan = plan_wheel_call_intent_consume(
                    batch,
                    intent,
                    event,
                    intent_coverage,
                    recorded_at_ms=recorded_at_ms,
                )
            except ValueError:
                continue
            plans.append((batch, plan))
    if not plans:
        return event, None, "no_matching_intent"
    if len(plans) != 1:
        return event, None, "ambiguous_matching_intent"
    batch, plan = plans[0]
    raw_payload = {
        **dict(getattr(event, "raw_payload", None) or {}),
        "strategy": "wheel",
        "leg_role": "wheel_call",
        "source_stock_lot_id": batch["stock_lot_id"],
        "wheel_call_intent_id": plan["intent_id"],
    }
    return (
        replace(
            event,
            lot_id=str(getattr(event, "lot_id", "") or f"lot_{event.event_id}"),
            raw_payload=raw_payload,
        ),
        plan,
        "matched_intent",
    )


def append_and_verify_wheel_intent_consumption(
    repo: Any,
    *,
    conn: Any,
    linked_event: Any,
    intent_event: Mapping[str, Any],
) -> None:
    if not repo.append_wheel_event_once(intent_event, conn=conn):
        raise ValueError("Wheel Call intent consumption unexpectedly replayed")
    lot_id = str(
        getattr(linked_event, "lot_id", "")
        or getattr(linked_event, "target_lot_id", "")
        or ""
    ).strip()
    fields = repo.get_position_lot_fields(lot_id, conn=conn)
    if (
        str(fields.get("strategy") or "") != "wheel"
        or str(fields.get("leg_role") or "") != "wheel_call"
        or str(fields.get("source_stock_lot_id") or "")
        != str(intent_event.get("stock_lot_id") or "")
    ):
        raise ValueError("Wheel Call intent linkage verification failed")
    account = _event_account(linked_event)
    batches = _wheel_batches_from_rows(
        repo.read_lifecycle_account_rows(account=account, conn=conn),
        account=account,
        as_of_ms=max(_event_time_ms(linked_event), 1),
    )
    matches = [
        batch
        for batch in batches
        if batch["stock_lot_id"] == intent_event["stock_lot_id"]
    ]
    if (
        len(matches) != 1
        or matches[0]["integrity_status"] != "trusted"
        or lot_id not in matches[0]["active_call_lot_ids"]
        or intent_event["intent_id"] in matches[0]["active_intent_ids"]
    ):
        raise ValueError("Wheel Call intent projection verification failed")


__all__ = [
    "append_and_verify_wheel_intent_consumption",
    "append_wheel_trade_companions",
    "capture_wheel_trade_companion_context",
    "prepare_wheel_intent_open_event",
]
