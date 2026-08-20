from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from domain.domain.wheel import plan_wheel_manual_end
from src.application.ledger.api import with_sqlite_repo_transaction
from src.application.wheel.read_model import build_wheel_read_model_from_rows
from src.application.write_contract import attach_write_contract


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _wheel_batch(
    rows: Mapping[str, Any],
    *,
    account: str,
    stock_lot_id: str,
    as_of_ms: int,
) -> dict[str, Any]:
    matches = [
        batch
        for batch in build_wheel_read_model_from_rows(
            rows,
            account=account,
            as_of_ms=as_of_ms,
        )["batches"]
        if batch["stock_lot_id"] == stock_lot_id
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Wheel batch must resolve uniquely: stock_lot_id={stock_lot_id}"
        )
    return matches[0]


def end_wheel_lifecycle(
    repo: Any,
    *,
    account: str,
    stock_lot_id: str,
    expected_batch_generation_hash: str,
    request_id: str,
    actor: str,
    apply_changes: bool = False,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    stock_lot_value = str(stock_lot_id or "").strip()
    expected_generation = str(expected_batch_generation_hash or "").strip()
    request_value = str(request_id or "").strip()
    actor_value = str(actor or "").strip()
    instant = int(as_of_ms or _now_ms())
    if not all(
        (account_value, stock_lot_value, expected_generation, request_value, actor_value)
    ) or instant <= 0:
        raise ValueError("Wheel manual end requires complete request fields")

    def _run(sqlite_repo: Any, conn: Any) -> dict[str, Any]:
        rows = sqlite_repo.read_lifecycle_account_rows(
            account=account_value,
            conn=conn,
        )
        existing = [
            event
            for event in rows.get("account_wheel_events") or []
            if str(event.get("stock_lot_id") or "").strip() == stock_lot_value
            and str(event.get("event_type") or "").strip() == "wheel_manual_ended"
            and str((event.get("payload") or {}).get("request_id") or "").strip()
            == request_value
        ]
        if len(existing) > 1:
            raise ValueError("Wheel manual end request identity is not unique")
        if existing:
            event = existing[0]
            payload = event.get("payload") or {}
            if (
                str(payload.get("actor") or "").strip() != actor_value
                or str(payload.get("batch_generation_hash") or "").strip()
                != expected_generation
            ):
                raise ValueError("Wheel manual end request identity conflicts")
            batch = _wheel_batch(
                rows,
                account=account_value,
                stock_lot_id=stock_lot_value,
                as_of_ms=max(instant, int(event.get("occurred_at_ms") or 0)),
            )
            if (
                batch["lifecycle_status"] != "manual_ended"
                or batch["phase"] is not None
                or batch["terminal_event_id"] != event["event_id"]
            ):
                raise ValueError("Wheel manual end idempotency verification failed")
            return _result(
                event=event,
                generation=expected_generation,
                status_before="active",
                status_after="manual_ended",
                dry_run=not apply_changes,
                write_applied=False,
                idempotent=True,
            )

        batch = _wheel_batch(
            rows,
            account=account_value,
            stock_lot_id=stock_lot_value,
            as_of_ms=instant,
        )
        if batch["batch_generation_hash"] != expected_generation:
            raise ValueError("Wheel batch generation changed; refresh before confirming")
        event = plan_wheel_manual_end(
            batch,
            request_value,
            actor_value,
            occurred_at_ms=instant,
            recorded_at_ms=instant,
            account=account_value,
        )
        if not apply_changes:
            return _result(
                event=event,
                generation=expected_generation,
                status_before=batch["lifecycle_status"],
                status_after="manual_ended",
                dry_run=True,
                write_applied=False,
                idempotent=False,
            )

        created = sqlite_repo.append_wheel_event_once(event, conn=conn)
        projected = _wheel_batch(
            sqlite_repo.read_lifecycle_account_rows(
                account=account_value,
                conn=conn,
            ),
            account=account_value,
            stock_lot_id=stock_lot_value,
            as_of_ms=instant,
        )
        if (
            not created
            or projected["lifecycle_status"] != "manual_ended"
            or projected["phase"] is not None
            or projected["terminal_event_id"] != event["event_id"]
        ):
            raise ValueError("Wheel manual end projection verification failed")
        return _result(
            event=event,
            generation=expected_generation,
            status_before=batch["lifecycle_status"],
            status_after=projected["lifecycle_status"],
            dry_run=False,
            write_applied=True,
            idempotent=False,
        )

    return with_sqlite_repo_transaction(repo, _run)


def _result(
    *,
    event: Mapping[str, Any],
    generation: str,
    status_before: str,
    status_after: str,
    dry_run: bool,
    write_applied: bool,
    idempotent: bool,
) -> dict[str, Any]:
    return attach_write_contract(
        {
            "schema_version": "wheel_end_result.v1",
            "stock_lot_id": event["stock_lot_id"],
            "event_id": event["event_id"],
            "request_id": str((event.get("payload") or {}).get("request_id") or ""),
            "batch_generation_hash": generation,
            "lifecycle_status_before": status_before,
            "lifecycle_status_after": status_after,
            "idempotent": idempotent,
        },
        dry_run=dry_run,
        write_applied=write_applied,
        audit_id=event["event_id"],
        rollback_hint=(
            "use the controlled wheel_event_voided repair path for an incorrect fact"
            if write_applied
            else None
        ),
    )


__all__ = ["end_wheel_lifecycle"]
