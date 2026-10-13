from __future__ import annotations

from typing import Any, Callable, Iterable

from src.application.ledger.api import (
    BATCH_RENDERER_VERSION,
    BATCH_RETRY_BACKOFF_MS,
    CLAIM_LEASE_MS,
    MAX_BATCH_WAIT_MS,
    MAX_ATTEMPTS,
    OUTBOX_STATUSES,
    RETRY_BACKOFF_MS,
    QUIET_WINDOW_MS,
    TARGET_SEND_INTERVAL_MS,
    build_notification_batch_route,
    build_notification_intent,
    canonical_payload_hash,
    claim_next_notification_batch,
    claim_next_notification,
    complete_notification_batch_attempt,
    complete_notification_attempt,
    enqueue_notification_intent,
    mark_notification_batch_send_started as _ledger_mark_notification_batch_send_started,
    mark_notification_send_started as _ledger_mark_notification_send_started,
    plan_notification_batch as _ledger_plan_notification_batch,
    reconcile_notification_batch,
    reconcile_unknown_notification,
    recover_stale_notification_batches,
    recover_stale_notifications,
    validate_notification_batch_route,
    validate_combo_pair_success,
)


def mark_notification_send_started(
    repo: Any,
    *,
    outbox_id: str,
    claim_id: str,
    now_ms: int,
) -> dict[str, Any]:
    from src.application.trades.receipt import build_trade_lifecycle_notification_message

    return _ledger_mark_notification_send_started(
        repo,
        outbox_id=outbox_id,
        claim_id=claim_id,
        now_ms=now_ms,
        render_message=build_trade_lifecycle_notification_message,
    )


def mark_notification_batch_send_started(
    repo: Any,
    *,
    batch_id: str,
    claim_id: str,
    now_ms: int,
    route: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from src.application.trades.receipt import build_trade_lifecycle_notification_batch_message

    return _ledger_mark_notification_batch_send_started(
        repo,
        batch_id=batch_id,
        claim_id=claim_id,
        now_ms=now_ms,
        render_message=build_trade_lifecycle_notification_batch_message,
        route=route,
        validate_combo_pair=validate_combo_pair_success,
    )


def plan_notification_batch(
    repo: Any, *, route: dict[str, Any], now_ms: int,
    allowed_accounts: Iterable[str] | None = None, apply_changes: bool = True,
) -> dict[str, Any]:
    return _ledger_plan_notification_batch(repo, route=route, now_ms=now_ms,
        allowed_accounts=allowed_accounts, apply_changes=apply_changes,
        validate_combo_pair=validate_combo_pair_success)


def dispatch_notification_batch_once(
    repo: Any,
    *,
    route: dict[str, Any],
    send_fn: Callable[[dict[str, Any]], dict[str, Any]],
    now_ms: int,
    allowed_accounts: Iterable[str] | None = None,
) -> dict[str, Any]:
    allowed_accounts = tuple(allowed_accounts) if allowed_accounts is not None else None
    recovery = recover_stale_notification_batches(repo, now_ms=now_ms)
    planned = plan_notification_batch(
        repo,
        route=route,
        now_ms=now_ms,
        allowed_accounts=allowed_accounts,
        apply_changes=True,
    )
    route_snapshot = validate_notification_batch_route(route)
    claimed = claim_next_notification_batch(
        repo,
        now_ms=now_ms,
        route_fingerprint=route_snapshot["route_fingerprint"],
        allowed_accounts=allowed_accounts,
        route=route_snapshot,
    )
    if claimed is None:
        return {
            "status": "idle",
            "reason": planned.get("reason"),
            "planning": planned,
            "recovery": recovery,
            "batch": None,
        }
    batch_id = str(claimed["batch_id"])
    claim_id = str(claimed["claim_id"])
    started = mark_notification_batch_send_started(
        repo,
        batch_id=batch_id,
        claim_id=claim_id,
        now_ms=now_ms,
        route=route_snapshot,
    )
    if started["status"] == "suppressed":
        return {"status": "suppressed", "planning": planned, "recovery": recovery, "batch": started}
    try:
        receipt = send_fn(dict(started["payload"]))
    except Exception as exc:
        completed = complete_notification_batch_attempt(
            repo,
            batch_id=batch_id,
            claim_id=claim_id,
            outcome="unknown",
            now_ms=now_ms,
            error=f"{type(exc).__name__}: {exc}",
        )
        return {
            "status": "unknown",
            "planning": planned,
            "recovery": recovery,
            "batch": completed,
        }
    provider_status = str(
        receipt.get("status") or ""
    ).strip().lower()
    if bool(receipt.get("delivery_confirmed")) or (
        provider_status == "confirmed"
    ):
        outcome = "confirmed"
    elif provider_status == "accepted":
        outcome = "accepted"
    elif bool(receipt.get("explicit_pre_acceptance_failure")):
        outcome = "explicit_failed"
    else:
        outcome = "unknown"
    completed = complete_notification_batch_attempt(
        repo,
        batch_id=batch_id,
        claim_id=claim_id,
        outcome=outcome,
        now_ms=now_ms,
        provider_message_id=receipt.get("message_id"),
        provider_receipt=receipt,
        error=receipt.get("send_message") or receipt.get("error"),
    )
    return {
        "status": outcome,
        "planning": planned,
        "recovery": recovery,
        "batch": completed,
    }



__all__ = [
    "BATCH_RENDERER_VERSION",
    "BATCH_RETRY_BACKOFF_MS",
    "CLAIM_LEASE_MS",
    "MAX_BATCH_WAIT_MS",
    "MAX_ATTEMPTS",
    "OUTBOX_STATUSES",
    "QUIET_WINDOW_MS",
    "TARGET_SEND_INTERVAL_MS",
    "build_notification_batch_route",
    "build_notification_intent",
    "canonical_payload_hash",
    "claim_next_notification_batch",
    "claim_next_notification",
    "complete_notification_batch_attempt",
    "complete_notification_attempt",
    "dispatch_notification_batch_once",
    "enqueue_notification_intent",
    "mark_notification_batch_send_started",
    "mark_notification_send_started",
    "plan_notification_batch",
    "reconcile_notification_batch",
    "reconcile_unknown_notification",
    "recover_stale_notification_batches",
    "recover_stale_notifications",
]
