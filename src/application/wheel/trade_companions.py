from __future__ import annotations

from typing import Any, Mapping, Sequence

from domain.domain.wheel import (
    wheel_called_away_event_from_call_assignment,
    wheel_started_event_from_assignment,
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
    from src.application.wheel.read_model import (
        build_assigned_stock_projection_from_rows,
        build_wheel_read_model_from_rows,
    )

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
                before_assigned[key] = build_assigned_stock_projection_from_rows(
                    before_rows[account],
                    account=account,
                    as_of_ms=instant,
                )
                after_assigned[key] = build_assigned_stock_projection_from_rows(
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
            model = build_wheel_read_model_from_rows(
                verification_rows[account],
                account=account,
                as_of_ms=max(_event_time_ms(event), 1),
            )
            matches = [
                batch
                for batch in model["batches"]
                if batch["start_event_id"] == companion_id
                or batch["terminal_event_id"] == companion_id
            ]
            if len(matches) != 1 or matches[0]["integrity_status"] != "trusted":
                raise ValueError("Wheel companion event projection verification failed")
    return companion_by_trade


__all__ = [
    "append_wheel_trade_companions",
    "capture_wheel_trade_companion_context",
]
