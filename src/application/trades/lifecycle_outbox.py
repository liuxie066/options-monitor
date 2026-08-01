from __future__ import annotations

import uuid
from typing import Any, Callable, Iterable

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
        "batched",
    }
)
CLAIM_LEASE_MS = 5 * 60 * 1000
MAX_ATTEMPTS = 3
RETRY_BACKOFF_MS = (30 * 1000, 5 * 60 * 1000)
QUIET_WINDOW_MS = 10 * 1000
MAX_BATCH_WAIT_MS = 60 * 1000
TARGET_SEND_INTERVAL_MS = 60 * 1000
BATCH_RETRY_BACKOFF_MS = (60 * 1000, 5 * 60 * 1000)
BATCH_RENDERER_VERSION = "trade_lifecycle_batch.v1"


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
            and not str(row.get("delivery_batch_id") or "").strip()
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


def build_notification_batch_route(
    *,
    provider: str,
    channel: str,
    target: str,
) -> dict[str, str]:
    provider_value = str(provider or "").strip().lower()
    channel_value = str(channel or "").strip().lower()
    target_value = str(target or "").strip()
    if not provider_value or not channel_value or not target_value:
        raise ValueError("notification batch route is incomplete")
    target_fingerprint = canonical_payload_hash(
        {"target": target_value}
    )
    route_fingerprint = canonical_payload_hash(
        {
            "provider": provider_value,
            "channel": channel_value,
            "target_fingerprint": target_fingerprint,
        }
    )
    return {
        "provider": provider_value,
        "channel": channel_value,
        "target": target_value,
        "target_fingerprint": target_fingerprint,
        "route_fingerprint": route_fingerprint,
    }


def _normalized_accounts(values: Iterable[str] | None) -> set[str] | None:
    if values is None:
        return None
    return {
        str(value or "").strip().lower()
        for value in values
        if str(value or "").strip()
    }


def _validated_batch_route(route: dict[str, Any]) -> dict[str, str]:
    snapshot = build_notification_batch_route(
        provider=str(route.get("provider") or ""),
        channel=str(route.get("channel") or ""),
        target=str(route.get("target") or ""),
    )
    for key in ("target_fingerprint", "route_fingerprint"):
        supplied = str(route.get(key) or "").strip()
        if supplied and supplied != snapshot[key]:
            raise ValueError("notification batch route fingerprint mismatch")
    return snapshot


def _batch_is_active(batch: dict[str, Any]) -> bool:
    status = str(batch.get("status") or "").strip().lower()
    return status in {"pending", "claimed", "send_started"} or (
        status == "explicit_failed"
        and int(batch.get("attempt_count") or 0) < MAX_ATTEMPTS
    )


def _batch_member_envelope(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "outbox_id": str(row["outbox_id"]),
        "case_id": str(row["case_id"]),
        "transition_type": str(row["transition_type"]),
        "resolution_revision": int(row["resolution_revision"]),
        "delivery_revision": int(row.get("delivery_revision") or 0),
        "transition_key": str(row["transition_key"]),
        "state_fingerprint": str(row["state_fingerprint"]),
        "payload_hash": str(row["payload_hash"]),
        "created_at_ms": int(row["created_at_ms"]),
        "payload": dict(row.get("payload") or {}),
    }


def _eligible_unbound_notifications(
    sqlite_repo: Any,
    *,
    conn: Any,
    now_ms: int,
    allowed_accounts: set[str] | None,
) -> list[dict[str, Any]]:
    current = int(now_ms)
    rows = []
    for row in sqlite_repo.list_trade_lifecycle_notifications(conn=conn):
        status = str(row.get("status") or "").strip().lower()
        account = str(
            (row.get("payload") or {}).get("account") or ""
        ).strip().lower()
        if (
            str(row.get("delivery_batch_id") or "").strip()
            or status not in {"pending", "explicit_failed"}
            or int(row.get("attempt_count") or 0) >= MAX_ATTEMPTS
            or (
                row.get("next_attempt_at_ms") is not None
                and int(row.get("next_attempt_at_ms") or 0) > current
            )
            or (
                allowed_accounts is not None
                and account not in allowed_accounts
            )
        ):
            continue
        rows.append(dict(row))
    rows.sort(
        key=lambda item: (
            int(item.get("created_at_ms") or 0),
            str(item.get("outbox_id") or ""),
        )
    )
    return rows


def plan_notification_batch(
    repo: Any,
    *,
    route: dict[str, Any],
    now_ms: int,
    allowed_accounts: Iterable[str] | None = None,
    apply_changes: bool = True,
) -> dict[str, Any]:
    current = int(now_ms)
    route_snapshot = _validated_batch_route(route)
    account_values = _normalized_accounts(allowed_accounts)

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError(
                "notification batch planning requires SQLite authority"
            )
        route_batches = (
            sqlite_repo.list_trade_lifecycle_notification_batches(
                route_fingerprint=route_snapshot["route_fingerprint"],
                conn=conn,
            )
        )
        active = next(
            (batch for batch in route_batches if _batch_is_active(batch)),
            None,
        )
        if active is not None:
            return {
                "status": "active_batch",
                "reason": "route_has_active_batch",
                "batch": active,
            }
        rows = _eligible_unbound_notifications(
            sqlite_repo,
            conn=conn,
            now_ms=current,
            allowed_accounts=account_values,
        )
        if not rows:
            return {
                "status": "idle",
                "reason": "no_eligible_unbound_intents",
                "batch": None,
                "candidate_count": 0,
            }
        latest_send_started = max(
            (
                int(batch.get("send_started_at_ms") or 0)
                for batch in route_batches
            ),
            default=0,
        )
        if (
            latest_send_started > 0
            and current - latest_send_started
            < TARGET_SEND_INTERVAL_MS
        ):
            return {
                "status": "rate_limited",
                "reason": "route_send_interval",
                "batch": None,
                "candidate_count": len(rows),
                "next_attempt_at_ms": (
                    latest_send_started + TARGET_SEND_INTERVAL_MS
                ),
            }
        oldest_created = int(rows[0]["created_at_ms"])
        newest_created = int(rows[-1]["created_at_ms"])
        quiet_ready = current - newest_created >= QUIET_WINDOW_MS
        max_wait_ready = current - oldest_created >= MAX_BATCH_WAIT_MS
        if not quiet_ready and not max_wait_ready:
            return {
                "status": "waiting",
                "reason": "aggregation_window_open",
                "batch": None,
                "candidate_count": len(rows),
                "quiet_ready_at_ms": newest_created + QUIET_WINDOW_MS,
                "max_wait_at_ms": oldest_created + MAX_BATCH_WAIT_MS,
            }
        members = [_batch_member_envelope(row) for row in rows]
        member_ids = [str(member["outbox_id"]) for member in members]
        identity_hash = canonical_payload_hash(
            {
                "renderer_version": BATCH_RENDERER_VERSION,
                "route_fingerprint": route_snapshot[
                    "route_fingerprint"
                ],
                "member_outbox_ids": sorted(member_ids),
            }
        )
        batch_id = "tlb_" + identity_hash[:32]
        payload = {
            "schema_version": BATCH_RENDERER_VERSION,
            "batch_id": batch_id,
            "route": {
                "provider": route_snapshot["provider"],
                "channel": route_snapshot["channel"],
                "target_fingerprint": route_snapshot[
                    "target_fingerprint"
                ],
                "route_fingerprint": route_snapshot[
                    "route_fingerprint"
                ],
            },
            "members": members,
        }
        batch = {
            "batch_id": batch_id,
            "route_fingerprint": route_snapshot["route_fingerprint"],
            "provider": route_snapshot["provider"],
            "channel": route_snapshot["channel"],
            "target_fingerprint": route_snapshot[
                "target_fingerprint"
            ],
            "renderer_version": BATCH_RENDERER_VERSION,
            "status": "pending",
            "payload": payload,
            "payload_hash": canonical_payload_hash(payload),
            "member_count": len(members),
            "first_intent_created_at_ms": oldest_created,
            "last_intent_created_at_ms": newest_created,
            "attempt_count": max(
                int(row.get("attempt_count") or 0) for row in rows
            ),
            "next_attempt_at_ms": current,
            "created_at_ms": current,
        }
        if not apply_changes:
            return {
                "status": "ready",
                "reason": (
                    "max_wait_elapsed"
                    if max_wait_ready and not quiet_ready
                    else "quiet_window_elapsed"
                ),
                "batch": batch,
                "candidate_count": len(rows),
            }
        created = sqlite_repo.insert_trade_lifecycle_notification_batch_once(
            batch,
            member_outbox_ids=member_ids,
            conn=conn,
        )
        stored = sqlite_repo.get_trade_lifecycle_notification_batch(
            batch_id,
            conn=conn,
        )
        if not isinstance(stored, dict):
            raise RuntimeError(
                "notification delivery batch readback failed"
            )
        return {
            "status": "created" if created else "existing",
            "reason": (
                "max_wait_elapsed"
                if max_wait_ready and not quiet_ready
                else "quiet_window_elapsed"
            ),
            "batch": stored,
            "candidate_count": len(rows),
        }

    return with_sqlite_repo_transaction(repo, _run)


def recover_stale_notification_batches(
    repo: Any,
    *,
    now_ms: int,
) -> dict[str, int]:
    current = int(now_ms)
    stale_before = current - CLAIM_LEASE_MS

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, int]:
        if conn is None:
            raise TypeError(
                "notification batch recovery requires SQLite authority"
            )
        reclaimed = 0
        frozen = 0
        for batch in (
            sqlite_repo.list_trade_lifecycle_notification_batches(
                conn=conn
            )
        ):
            status = str(batch.get("status") or "")
            claimed_at = int(batch.get("claimed_at_ms") or 0)
            started_at = int(batch.get("send_started_at_ms") or 0)
            if (
                status == "claimed"
                and claimed_at > 0
                and claimed_at <= stale_before
                and started_at <= 0
            ):
                changed = (
                    sqlite_repo.compare_and_set_trade_lifecycle_notification_batch(
                        batch_id=batch["batch_id"],
                        expected_status="claimed",
                        new_status="pending",
                        expected_claim_id=str(
                            batch.get("claim_id") or ""
                        ),
                        fields={
                            "claim_id": None,
                            "claimed_at_ms": None,
                            "next_attempt_at_ms": current,
                            "last_error": (
                                "stale_batch_claim_recovered_before_send"
                            ),
                        },
                        conn=conn,
                    )
                )
                reclaimed += int(changed)
            elif (
                status == "send_started"
                and started_at > 0
                and started_at <= stale_before
            ):
                changed = (
                    sqlite_repo.compare_and_set_trade_lifecycle_notification_batch(
                        batch_id=batch["batch_id"],
                        expected_status="send_started",
                        new_status="unknown",
                        expected_claim_id=str(
                            batch.get("claim_id") or ""
                        ),
                        fields={
                            "next_attempt_at_ms": None,
                            "last_error": (
                                "stale_batch_send_started_delivery_unknown"
                            ),
                        },
                        conn=conn,
                    )
                )
                if changed:
                    settled = (
                        sqlite_repo.update_trade_lifecycle_notification_batch_members(
                            batch_id=batch["batch_id"],
                            expected_statuses=("batched",),
                            new_status="unknown",
                            fields={
                                "attempt_count": int(
                                    batch.get("attempt_count") or 0
                                ),
                                "next_attempt_at_ms": None,
                                "last_error": (
                                    "stale_batch_send_started_delivery_unknown"
                                ),
                            },
                            conn=conn,
                        )
                    )
                    if settled != int(batch["member_count"]):
                        raise ValueError(
                            "notification batch stale recovery member mismatch"
                        )
                frozen += int(changed)
        return {
            "reclaimed_claimed_count": reclaimed,
            "frozen_unknown_count": frozen,
        }

    return with_sqlite_repo_transaction(repo, _run)


def claim_next_notification_batch(
    repo: Any,
    *,
    now_ms: int,
    claim_id: str | None = None,
    route_fingerprint: str | None = None,
) -> dict[str, Any] | None:
    current = int(now_ms)
    claim_value = str(claim_id or uuid.uuid4().hex)
    route_value = str(route_fingerprint or "").strip()

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, Any] | None:
        if conn is None:
            raise TypeError(
                "notification batch claim requires SQLite authority"
            )
        batches = (
            sqlite_repo.list_trade_lifecycle_notification_batches(
                route_fingerprint=route_value or None,
                conn=conn,
            )
        )
        candidates = [
            batch
            for batch in batches
            if str(batch.get("status") or "")
            in {"pending", "explicit_failed"}
            and int(batch.get("attempt_count") or 0) < MAX_ATTEMPTS
            and (
                batch.get("next_attempt_at_ms") is None
                or int(batch.get("next_attempt_at_ms") or 0) <= current
            )
        ]
        for batch in candidates:
            route_batches = (
                batches
                if route_value
                else sqlite_repo.list_trade_lifecycle_notification_batches(
                    route_fingerprint=str(
                        batch.get("route_fingerprint") or ""
                    ),
                    conn=conn,
                )
            )
            latest_started = max(
                (
                    int(item.get("send_started_at_ms") or 0)
                    for item in route_batches
                ),
                default=0,
            )
            if (
                latest_started > 0
                and current - latest_started
                < TARGET_SEND_INTERVAL_MS
            ):
                continue
            changed = (
                sqlite_repo.compare_and_set_trade_lifecycle_notification_batch(
                    batch_id=batch["batch_id"],
                    expected_status=str(batch["status"]),
                    new_status="claimed",
                    claim_id=claim_value,
                    fields={
                        "claimed_at_ms": current,
                        "send_started_at_ms": None,
                        "next_attempt_at_ms": None,
                        "last_error": None,
                    },
                    conn=conn,
                )
            )
            if not changed:
                continue
            return sqlite_repo.get_trade_lifecycle_notification_batch(
                batch["batch_id"],
                conn=conn,
            )
        return None

    return with_sqlite_repo_transaction(repo, _run)


def mark_notification_batch_send_started(
    repo: Any,
    *,
    batch_id: str,
    claim_id: str,
    now_ms: int,
) -> dict[str, Any]:
    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError(
                "notification batch send-start requires SQLite authority"
            )
        current = sqlite_repo.get_trade_lifecycle_notification_batch(
            batch_id,
            conn=conn,
        )
        if not isinstance(current, dict):
            raise ValueError("notification delivery batch not found")
        attempts = int(current.get("attempt_count") or 0) + 1
        if attempts > MAX_ATTEMPTS:
            raise ValueError(
                "notification batch attempt budget exhausted"
            )
        changed = (
            sqlite_repo.compare_and_set_trade_lifecycle_notification_batch(
                batch_id=batch_id,
                expected_status="claimed",
                new_status="send_started",
                expected_claim_id=claim_id,
                fields={
                    "send_started_at_ms": int(now_ms),
                    "attempt_count": attempts,
                },
                conn=conn,
            )
        )
        if not changed:
            raise ValueError(
                "notification batch claim lost before send_started"
            )
        row = sqlite_repo.get_trade_lifecycle_notification_batch(
            batch_id,
            conn=conn,
        )
        if not isinstance(row, dict):
            raise RuntimeError(
                "notification delivery batch readback failed"
            )
        return row

    return with_sqlite_repo_transaction(repo, _run)


def complete_notification_batch_attempt(
    repo: Any,
    *,
    batch_id: str,
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
        raise ValueError(
            "unsupported notification batch delivery outcome"
        )
    current = int(now_ms)

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError(
                "notification batch completion requires SQLite authority"
            )
        batch = sqlite_repo.get_trade_lifecycle_notification_batch(
            batch_id,
            conn=conn,
        )
        if not isinstance(batch, dict):
            raise ValueError("notification delivery batch not found")
        if (
            str(batch.get("status") or "") != "send_started"
            or str(batch.get("claim_id") or "") != str(claim_id)
        ):
            raise ValueError(
                "notification batch completion compare-and-set failed"
            )
        attempts = int(batch.get("attempt_count") or 0)
        retryable_failure = (
            outcome_value == "explicit_failed"
            and attempts < MAX_ATTEMPTS
        )
        next_attempt_at_ms = None
        if retryable_failure:
            backoff_index = min(
                max(attempts - 1, 0),
                len(BATCH_RETRY_BACKOFF_MS) - 1,
            )
            next_attempt_at_ms = (
                current + BATCH_RETRY_BACKOFF_MS[backoff_index]
            )
        error_value = str(error or "").strip() or None
        fields = {
            "provider_message_id": (
                str(provider_message_id or "").strip() or None
            ),
            "provider_receipt_json": dict(provider_receipt or {}),
            "next_attempt_at_ms": next_attempt_at_ms,
            "last_error": error_value,
            "confirmed_at_ms": (
                current if outcome_value == "confirmed" else None
            ),
        }
        changed = (
            sqlite_repo.compare_and_set_trade_lifecycle_notification_batch(
                batch_id=batch_id,
                expected_status="send_started",
                new_status=outcome_value,
                expected_claim_id=claim_id,
                fields=fields,
                conn=conn,
            )
        )
        if not changed:
            raise ValueError(
                "notification batch completion compare-and-set failed"
            )
        if not retryable_failure:
            settled = (
                sqlite_repo.update_trade_lifecycle_notification_batch_members(
                    batch_id=batch_id,
                    expected_statuses=("batched",),
                    new_status=outcome_value,
                    fields={
                        "attempt_count": attempts,
                        "next_attempt_at_ms": None,
                        "last_error": error_value,
                        "confirmed_at_ms": (
                            current
                            if outcome_value == "confirmed"
                            else None
                        ),
                    },
                    conn=conn,
                )
            )
            if settled != int(batch["member_count"]):
                raise ValueError(
                    "notification batch completion member mismatch"
                )
        completed = sqlite_repo.get_trade_lifecycle_notification_batch(
            batch_id,
            conn=conn,
        )
        if not isinstance(completed, dict):
            raise RuntimeError(
                "notification delivery batch readback failed"
            )
        return completed

    return with_sqlite_repo_transaction(repo, _run)


def dispatch_notification_batch_once(
    repo: Any,
    *,
    route: dict[str, Any],
    send_fn: Callable[[dict[str, Any]], dict[str, Any]],
    now_ms: int,
    allowed_accounts: Iterable[str] | None = None,
) -> dict[str, Any]:
    recovery = recover_stale_notification_batches(repo, now_ms=now_ms)
    planned = plan_notification_batch(
        repo,
        route=route,
        now_ms=now_ms,
        allowed_accounts=allowed_accounts,
        apply_changes=True,
    )
    route_snapshot = _validated_batch_route(route)
    claimed = claim_next_notification_batch(
        repo,
        now_ms=now_ms,
        route_fingerprint=route_snapshot["route_fingerprint"],
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
    )
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


def reconcile_notification_batch(
    repo: Any,
    *,
    batch_id: str,
    action: str,
    broker_ref: str,
    note: str,
    apply_changes: bool,
    now_ms: int | None = None,
) -> dict[str, Any]:
    batch = repo.get_trade_lifecycle_notification_batch(batch_id)
    if not isinstance(batch, dict):
        raise ValueError("notification delivery batch not found")
    current_status = str(batch.get("status") or "")
    if current_status not in {"accepted", "unknown"}:
        raise ValueError(
            "only accepted or unknown notification batch can be reconciled"
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
        raise ValueError(
            "notification batch reconciliation action is invalid"
        )
    preview = {
        "batch_id": str(batch_id),
        "action": normalized_action,
        "current_status": current_status,
        "member_count": int(batch.get("member_count") or 0),
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
                "notification batch reconcile requires SQLite authority"
            )
        current = sqlite_repo.get_trade_lifecycle_notification_batch(
            batch_id,
            conn=conn,
        )
        if not isinstance(current, dict):
            raise ValueError("notification delivery batch not found")
        if str(current.get("status") or "") != current_status:
            raise ValueError(
                "notification batch status changed during reconcile"
            )
        manual_receipt = {
            "schema_version": "manual_notification_batch_resolution.v1",
            "action": normalized_action,
            "broker_ref": reference,
            "note": explanation,
            "resolved_at_ms": resolved_at_ms,
            "original_provider_receipt": current.get(
                "provider_receipt"
            ),
        }
        if normalized_action in {"confirmed", "unknown"}:
            changed = (
                sqlite_repo.compare_and_set_trade_lifecycle_notification_batch(
                    batch_id=batch_id,
                    expected_status=current_status,
                    new_status=normalized_action,
                    fields={
                        "confirmed_at_ms": (
                            resolved_at_ms
                            if normalized_action == "confirmed"
                            else None
                        ),
                        "provider_receipt_json": manual_receipt,
                    },
                    conn=conn,
                )
            )
            if not changed:
                raise ValueError(
                    "notification batch status changed during reconcile"
                )
            settled = (
                sqlite_repo.update_trade_lifecycle_notification_batch_members(
                    batch_id=batch_id,
                    expected_statuses=(current_status,),
                    new_status=normalized_action,
                    fields={
                        "confirmed_at_ms": (
                            resolved_at_ms
                            if normalized_action == "confirmed"
                            else None
                        )
                    },
                    conn=conn,
                )
            )
            if settled != int(current["member_count"]):
                raise ValueError(
                    "notification batch reconcile member mismatch"
                )
            return {
                **preview,
                "batch": (
                    sqlite_repo.get_trade_lifecycle_notification_batch(
                        batch_id,
                        conn=conn,
                    )
                ),
            }
        members = (
            sqlite_repo.list_trade_lifecycle_notification_batch_members(
                batch_id,
                conn=conn,
            )
        )
        compensating: list[dict[str, Any]] = []
        seen_transition_keys: set[str] = set()
        for member in members:
            transition_key = str(
                member.get("transition_key") or ""
            ).strip()
            if transition_key in seen_transition_keys:
                raise ValueError(
                    "notification batch resend has duplicate transition key"
                )
            seen_transition_keys.add(transition_key)
            siblings = [
                item
                for item in sqlite_repo.list_trade_lifecycle_notifications(
                    case_id=str(member["case_id"]),
                    conn=conn,
                )
                if str(item.get("transition_key") or "").strip()
                == transition_key
            ]
            next_delivery_revision = max(
                int(item.get("delivery_revision") or 0)
                for item in siblings
            ) + 1
            intent = build_notification_intent(
                case_id=str(member["case_id"]),
                transition_type=str(member["transition_type"]),
                resolution_revision=int(member["resolution_revision"]),
                delivery_revision=next_delivery_revision,
                transition_key=transition_key,
                state_fingerprint=str(member["state_fingerprint"]),
                payload={
                    **dict(member["payload"]),
                    "compensates_outbox_id": member["outbox_id"],
                    "compensates_batch_id": batch_id,
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
            if not created:
                raise ValueError(
                    "notification batch compensating intent already exists"
                )
            stored = sqlite_repo.get_trade_lifecycle_notification(
                intent["outbox_id"],
                conn=conn,
            )
            if not isinstance(stored, dict):
                raise RuntimeError(
                    "notification compensating intent readback failed"
                )
            compensating.append(stored)
        return {
            **preview,
            "compensating_intents": compensating,
            "compensating_intent_count": len(compensating),
        }

    return with_sqlite_repo_transaction(repo, _run)


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
    "dispatch_notifications_once",
    "enqueue_notification_intent",
    "mark_notification_batch_send_started",
    "mark_notification_send_started",
    "plan_notification_batch",
    "reconcile_notification_batch",
    "reconcile_unknown_notification",
    "recover_stale_notification_batches",
    "recover_stale_notifications",
]
