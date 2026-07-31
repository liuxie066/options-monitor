from __future__ import annotations

import uuid
from typing import Any, Callable

from src.application.ledger.api import (
    build_notification_intent,
    canonical_payload_hash,
    with_sqlite_repo_transaction,
)
from src.application.cash_conversion import utc_now_ms


OUTBOX_STATUSES = frozenset(
    {
        "pending",
        "claimed",
        "send_started",
        "confirmed",
        "accepted",
        "explicit_failed",
        "unknown",
        "suppressed",
    }
)
CLAIM_LEASE_MS = 5 * 60 * 1000
MAX_ATTEMPTS = 3
RETRY_BACKOFF_MS = (30 * 1000, 5 * 60 * 1000)


def enqueue_notification_intent(
    repo: Any,
    intent: dict[str, Any],
) -> dict[str, Any]:
    created = bool(
        repo.insert_trade_lifecycle_notification_once(dict(intent or {}))
    )
    row = repo.get_trade_lifecycle_notification(intent["outbox_id"])
    if not isinstance(row, dict):
        raise RuntimeError("notification outbox readback failed")
    return {"created": created, "outbox": row}


def recover_stale_notifications(
    repo: Any,
    *,
    now_ms: int,
) -> dict[str, int]:
    current = int(now_ms)
    stale_before = current - CLAIM_LEASE_MS

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, int]:
        if conn is None:
            raise TypeError("notification recovery requires SQLite authority")
        reclaimed = 0
        frozen_unknown = 0
        for row in sqlite_repo.list_trade_lifecycle_notifications(conn=conn):
            status = str(row.get("status") or "")
            claimed_at = int(row.get("claimed_at_ms") or 0)
            started_at = int(row.get("send_started_at_ms") or 0)
            if (
                status == "claimed"
                and claimed_at > 0
                and claimed_at <= stale_before
                and started_at <= 0
            ):
                changed = sqlite_repo.compare_and_set_trade_lifecycle_notification(
                    outbox_id=row["outbox_id"],
                    expected_status="claimed",
                    new_status="pending",
                    expected_claim_id=str(row.get("claim_id") or ""),
                    fields={
                        "claim_id": None,
                        "claimed_at_ms": None,
                        "next_attempt_at_ms": current,
                        "last_error": "stale_claim_recovered_before_send",
                    },
                    conn=conn,
                )
                reclaimed += int(changed)
            elif (
                status == "send_started"
                and started_at > 0
                and started_at <= stale_before
            ):
                changed = sqlite_repo.compare_and_set_trade_lifecycle_notification(
                    outbox_id=row["outbox_id"],
                    expected_status="send_started",
                    new_status="unknown",
                    expected_claim_id=str(row.get("claim_id") or ""),
                    fields={
                        "next_attempt_at_ms": None,
                        "last_error": "stale_send_started_delivery_unknown",
                    },
                    conn=conn,
                )
                frozen_unknown += int(changed)
        return {
            "reclaimed_claimed_count": reclaimed,
            "frozen_unknown_count": frozen_unknown,
        }

    return with_sqlite_repo_transaction(repo, _run)


def claim_next_notification(
    repo: Any,
    *,
    now_ms: int,
    claim_id: str | None = None,
    account: str | None = None,
) -> dict[str, Any] | None:
    current = int(now_ms)
    claim_value = str(claim_id or uuid.uuid4().hex)
    account_value = str(account or "").strip().lower()

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, Any] | None:
        if conn is None:
            raise TypeError("notification claim requires SQLite authority")
        candidates = [
            row
            for row in sqlite_repo.list_trade_lifecycle_notifications(conn=conn)
            if str(row.get("status") or "") in {"pending", "explicit_failed"}
            and (
                not account_value
                or str(
                    (row.get("payload") or {}).get("account") or ""
                ).strip().lower()
                == account_value
            )
            and (
                row.get("next_attempt_at_ms") is None
                or int(row.get("next_attempt_at_ms") or 0) <= current
            )
            and int(row.get("attempt_count") or 0) < MAX_ATTEMPTS
        ]
        if not candidates:
            return None
        row = candidates[0]
        expected_status = str(row["status"])
        attempts = int(row.get("attempt_count") or 0) + 1
        changed = sqlite_repo.compare_and_set_trade_lifecycle_notification(
            outbox_id=row["outbox_id"],
            expected_status=expected_status,
            new_status="claimed",
            claim_id=claim_value,
            fields={
                "claimed_at_ms": current,
                "send_started_at_ms": None,
                "attempt_count": attempts,
                "next_attempt_at_ms": None,
                "last_error": None,
            },
            conn=conn,
        )
        if not changed:
            return None
        return sqlite_repo.get_trade_lifecycle_notification(
            row["outbox_id"],
            conn=conn,
        )

    return with_sqlite_repo_transaction(repo, _run)


def mark_notification_send_started(
    repo: Any,
    *,
    outbox_id: str,
    claim_id: str,
    now_ms: int,
) -> dict[str, Any]:
    changed = repo.compare_and_set_trade_lifecycle_notification(
        outbox_id=outbox_id,
        expected_status="claimed",
        new_status="send_started",
        expected_claim_id=claim_id,
        fields={"send_started_at_ms": int(now_ms)},
    )
    if not changed:
        raise ValueError("notification claim lost before send_started")
    row = repo.get_trade_lifecycle_notification(outbox_id)
    if not isinstance(row, dict):
        raise RuntimeError("notification outbox readback failed")
    return row


def complete_notification_attempt(
    repo: Any,
    *,
    outbox_id: str,
    claim_id: str,
    outcome: str,
    now_ms: int,
    provider_message_id: str | None = None,
    provider_receipt: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    outcome_value = str(outcome or "").strip().lower()
    if outcome_value not in {
        "confirmed",
        "accepted",
        "explicit_failed",
        "unknown",
    }:
        raise ValueError("unsupported notification delivery outcome")
    row = repo.get_trade_lifecycle_notification(outbox_id)
    if not isinstance(row, dict):
        raise ValueError("notification outbox row not found")
    attempts = int(row.get("attempt_count") or 0)
    next_attempt_at_ms = None
    if outcome_value == "explicit_failed" and attempts < MAX_ATTEMPTS:
        backoff_index = min(max(attempts - 1, 0), len(RETRY_BACKOFF_MS) - 1)
        next_attempt_at_ms = int(now_ms) + RETRY_BACKOFF_MS[backoff_index]
    fields = {
        "provider_message_id": (
            str(provider_message_id or "").strip() or None
        ),
        "provider_receipt_json": dict(provider_receipt or {}),
        "next_attempt_at_ms": next_attempt_at_ms,
        "last_error": str(error or "").strip() or None,
        "confirmed_at_ms": (
            int(now_ms) if outcome_value == "confirmed" else None
        ),
    }
    changed = repo.compare_and_set_trade_lifecycle_notification(
        outbox_id=outbox_id,
        expected_status="send_started",
        new_status=outcome_value,
        expected_claim_id=claim_id,
        fields=fields,
    )
    if not changed:
        raise ValueError("notification completion compare-and-set failed")
    completed = repo.get_trade_lifecycle_notification(outbox_id)
    if not isinstance(completed, dict):
        raise RuntimeError("notification outbox readback failed")
    return completed


def dispatch_notifications_once(
    repo: Any,
    *,
    send_fn: Callable[[dict[str, Any]], dict[str, Any]],
    now_ms: int,
    account: str | None = None,
) -> dict[str, Any]:
    recovery = recover_stale_notifications(repo, now_ms=now_ms)
    claimed = claim_next_notification(
        repo,
        now_ms=now_ms,
        account=account,
    )
    if claimed is None:
        return {
            "status": "idle",
            "recovery": recovery,
            "outbox": None,
        }
    outbox_id = str(claimed["outbox_id"])
    claim_id = str(claimed["claim_id"])
    started = mark_notification_send_started(
        repo,
        outbox_id=outbox_id,
        claim_id=claim_id,
        now_ms=now_ms,
    )
    try:
        receipt = send_fn(dict(started["payload"]))
    except Exception as exc:
        completed = complete_notification_attempt(
            repo,
            outbox_id=outbox_id,
            claim_id=claim_id,
            outcome="unknown",
            now_ms=now_ms,
            error=f"{type(exc).__name__}: {exc}",
        )
        return {
            "status": "unknown",
            "recovery": recovery,
            "outbox": completed,
        }
    provider_status = str(receipt.get("status") or "").strip().lower()
    confirmed = bool(receipt.get("delivery_confirmed"))
    explicit_failure = bool(receipt.get("explicit_pre_acceptance_failure"))
    if confirmed or provider_status == "confirmed":
        outcome = "confirmed"
    elif provider_status == "accepted":
        outcome = "accepted"
    elif explicit_failure:
        outcome = "explicit_failed"
    else:
        outcome = "unknown"
    completed = complete_notification_attempt(
        repo,
        outbox_id=outbox_id,
        claim_id=claim_id,
        outcome=outcome,
        now_ms=now_ms,
        provider_message_id=receipt.get("message_id"),
        provider_receipt=receipt,
        error=receipt.get("send_message") or receipt.get("error"),
    )
    return {
        "status": outcome,
        "recovery": recovery,
        "outbox": completed,
    }


def reconcile_unknown_notification(
    repo: Any,
    *,
    outbox_id: str,
    action: str,
    broker_ref: str,
    note: str,
    apply_changes: bool,
    now_ms: int | None = None,
) -> dict[str, Any]:
    row = repo.get_trade_lifecycle_notification(outbox_id)
    if not isinstance(row, dict):
        raise ValueError("notification outbox row not found")
    current_status = str(row.get("status") or "")
    if current_status not in {"accepted", "unknown"}:
        raise ValueError(
            "only accepted or unknown notification can be reconciled"
        )
    reference = str(broker_ref or "").strip()
    explanation = str(note or "").strip()
    if not reference or not explanation:
        raise ValueError("broker_ref and note are required")
    normalized_action = str(action or "").strip().lower()
    allowed_actions = (
        {"confirmed", "unknown"}
        if current_status == "accepted"
        else {"confirmed", "resend"}
    )
    if normalized_action not in allowed_actions:
        raise ValueError("notification reconciliation action is invalid")
    preview = {
        "outbox_id": outbox_id,
        "action": normalized_action,
        "current_status": current_status,
        "broker_ref": reference,
        "note": explanation,
        "apply_changes": bool(apply_changes),
    }
    if not apply_changes:
        return preview
    resolved_at_ms = int(
        now_ms if now_ms is not None else utc_now_ms()
    )

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError(
                "notification reconciliation requires SQLite authority"
            )
        current = sqlite_repo.get_trade_lifecycle_notification(
            outbox_id,
            conn=conn,
        )
        if not isinstance(current, dict):
            raise ValueError("notification outbox row not found")
        if str(current.get("status") or "") != current_status:
            raise ValueError(
                "notification status changed during reconcile"
            )
        manual_receipt = {
            "schema_version": "manual_notification_resolution.v1",
            "action": normalized_action,
            "broker_ref": reference,
            "note": explanation,
            "resolved_at_ms": resolved_at_ms,
            "original_provider_receipt": current.get(
                "provider_receipt"
            ),
        }
        if normalized_action in {"confirmed", "unknown"}:
            new_status = normalized_action
            changed = (
                sqlite_repo.compare_and_set_trade_lifecycle_notification(
                    outbox_id=outbox_id,
                    expected_status=current_status,
                    new_status=new_status,
                    fields={
                        "confirmed_at_ms": (
                            resolved_at_ms
                            if new_status == "confirmed"
                            else None
                        ),
                        "provider_receipt_json": manual_receipt,
                    },
                    conn=conn,
                )
            )
            if not changed:
                raise ValueError(
                    "notification status changed during reconcile"
                )
            return {
                **preview,
                "outbox": (
                    sqlite_repo.get_trade_lifecycle_notification(
                        outbox_id,
                        conn=conn,
                    )
                ),
            }
        transition_key = str(
            current.get("transition_key") or ""
        ).strip()
        sibling_rows = [
            item
            for item in sqlite_repo.list_trade_lifecycle_notifications(
                case_id=str(current["case_id"]),
                conn=conn,
            )
            if str(item.get("transition_key") or "").strip()
            == transition_key
        ]
        next_delivery_revision = (
            max(
                int(item.get("delivery_revision") or 0)
                for item in sibling_rows
            )
            + 1
        )
        intent = build_notification_intent(
            case_id=str(current["case_id"]),
            transition_type=str(current["transition_type"]),
            resolution_revision=int(current["resolution_revision"]),
            delivery_revision=next_delivery_revision,
            transition_key=transition_key,
            state_fingerprint=str(current["state_fingerprint"]),
            payload={
                **dict(current["payload"]),
                "compensates_outbox_id": outbox_id,
                "delivery_revision": next_delivery_revision,
                "manual_resolution": {
                    "broker_ref": reference,
                    "note": explanation,
                    "resolved_at_ms": resolved_at_ms,
                },
            },
        )
        created = (
            sqlite_repo.insert_trade_lifecycle_notification_once(
                intent,
                conn=conn,
            )
        )
        return {
            **preview,
            "compensating_intent_created": bool(created),
            "compensating_outbox": (
                sqlite_repo.get_trade_lifecycle_notification(
                    intent["outbox_id"],
                    conn=conn,
                )
            ),
        }

    return with_sqlite_repo_transaction(repo, _run)


__all__ = [
    "CLAIM_LEASE_MS",
    "MAX_ATTEMPTS",
    "OUTBOX_STATUSES",
    "build_notification_intent",
    "canonical_payload_hash",
    "claim_next_notification",
    "complete_notification_attempt",
    "dispatch_notifications_once",
    "enqueue_notification_intent",
    "mark_notification_send_started",
    "reconcile_unknown_notification",
    "recover_stale_notifications",
]
