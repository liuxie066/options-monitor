from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.application.ledger.notification_outbox import (
    build_notification_intent,
    canonical_payload_hash,
)
from src.application.ledger.repository import (
    SQLiteOptionPositionsRepository,
)
from src.application.trades.lifecycle_outbox import (
    BATCH_RETRY_BACKOFF_MS,
    CLAIM_LEASE_MS,
    MAX_BATCH_WAIT_MS,
    QUIET_WINDOW_MS,
    TARGET_SEND_INTERVAL_MS,
    build_notification_batch_route,
    claim_next_notification,
    claim_next_notification_batch,
    complete_notification_batch_attempt,
    dispatch_notification_batch_once,
    enqueue_notification_intent,
    mark_notification_batch_send_started,
    plan_notification_batch,
    reconcile_notification_batch,
    recover_stale_notification_batches,
)


def _repo(tmp_path: Path) -> SQLiteOptionPositionsRepository:
    return SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")


def _route(*, target: str = "ou_test_target") -> dict[str, str]:
    return build_notification_batch_route(
        provider="feishu_app",
        channel="bot",
        target=target,
    )


def _enqueue(
    repo: SQLiteOptionPositionsRepository,
    *,
    suffix: str,
    account: str = "lx",
    transition_type: str = "needs_review",
) -> dict:
    intent = build_notification_intent(
        case_id=f"case-{suffix}",
        transition_type=transition_type,
        resolution_revision=1,
        transition_key=f"lifecycle:case-{suffix}:{transition_type}",
        state_fingerprint=f"state-{suffix}",
        payload={
            "account": account,
            "case_id": f"case-{suffix}",
            "transition_type": transition_type,
            "symbol": f"SYM{suffix}",
        },
    )
    enqueue_notification_intent(repo, intent)
    row = repo.get_trade_lifecycle_notification(intent["outbox_id"])
    assert row is not None
    return row


def _quiet_now(rows: list[dict]) -> int:
    return max(int(row["created_at_ms"]) for row in rows) + QUIET_WINDOW_MS


def test_twenty_four_intents_use_one_batch_and_one_receipt(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    rows = [
        _enqueue(
            repo,
            suffix=f"{index:02d}",
            account="lx" if index < 15 else "sy",
        )
        for index in range(24)
    ]
    calls: list[dict] = []

    def _send(payload: dict) -> dict:
        calls.append(payload)
        return {
            "status": "confirmed",
            "delivery_confirmed": True,
            "message_id": "provider-message-1",
        }

    result = dispatch_notification_batch_once(
        repo,
        route=_route(),
        send_fn=_send,
        now_ms=_quiet_now(rows),
        allowed_accounts={"lx", "sy"},
    )

    assert result["status"] == "confirmed"
    assert len(calls) == 1
    assert len(calls[0]["members"]) == 24
    batch = result["batch"]
    assert batch["member_count"] == 24
    assert batch["status"] == "confirmed"
    assert batch["provider_message_id"] == "provider-message-1"
    assert batch["batch_id"] == calls[0]["batch_id"]
    assert "ou_test_target" not in str(batch["payload"])
    settled = repo.list_trade_lifecycle_notification_batch_members(
        batch["batch_id"]
    )
    assert len(settled) == 24
    assert {row["status"] for row in settled} == {"confirmed"}
    assert {
        row["delivery_batch_id"] for row in settled
    } == {batch["batch_id"]}


def test_quiet_window_and_oldest_max_wait_are_both_enforced(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    first = _enqueue(repo, suffix="old")
    second = _enqueue(repo, suffix="new")
    base = min(int(first["created_at_ms"]), int(second["created_at_ms"]))
    with repo._connect() as conn:
        conn.execute(
            """
            UPDATE trade_lifecycle_notification_outbox
            SET created_at_ms = ?, updated_at_ms = ?
            WHERE outbox_id = ?
            """,
            (base, base, first["outbox_id"]),
        )
        conn.execute(
            """
            UPDATE trade_lifecycle_notification_outbox
            SET created_at_ms = ?, updated_at_ms = ?
            WHERE outbox_id = ?
            """,
            (
                base + MAX_BATCH_WAIT_MS - 5_000,
                base + MAX_BATCH_WAIT_MS - 5_000,
                second["outbox_id"],
            ),
        )

    waiting = plan_notification_batch(
        repo,
        route=_route(),
        now_ms=base + MAX_BATCH_WAIT_MS - 1,
        apply_changes=False,
    )
    forced = plan_notification_batch(
        repo,
        route=_route(),
        now_ms=base + MAX_BATCH_WAIT_MS,
        apply_changes=False,
    )

    assert waiting["status"] == "waiting"
    assert forced["status"] == "ready"
    assert forced["reason"] == "max_wait_elapsed"
    assert forced["candidate_count"] == 2


def test_batch_binding_is_atomic_and_old_claim_predicate_fails_closed(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    rows = [_enqueue(repo, suffix=str(index)) for index in range(3)]
    planned = plan_notification_batch(
        repo,
        route=_route(),
        now_ms=_quiet_now(rows),
    )

    assert planned["status"] == "created"
    members = repo.list_trade_lifecycle_notification_batch_members(
        planned["batch"]["batch_id"]
    )
    assert len(members) == 3
    assert {row["status"] for row in members} == {"batched"}
    assert claim_next_notification(
        repo,
        now_ms=_quiet_now(rows),
    ) is None


def test_concurrent_planners_create_exactly_one_batch(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    rows = [_enqueue(repo, suffix=str(index)) for index in range(5)]
    current = _quiet_now(rows)

    def _plan() -> dict:
        return plan_notification_batch(
            repo,
            route=_route(),
            now_ms=current,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _value: _plan(), range(2)))

    assert {result["status"] for result in results} == {
        "created",
        "active_batch",
    }
    batches = repo.list_trade_lifecycle_notification_batches()
    assert len(batches) == 1
    assert batches[0]["member_count"] == 5


def test_explicit_failure_retries_same_batch_and_stops_at_three(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    row = _enqueue(repo, suffix="retry")
    first_at = _quiet_now([row])
    seen_batch_ids: list[str] = []

    def _reject(payload: dict) -> dict:
        seen_batch_ids.append(str(payload["batch_id"]))
        return {
            "status": "explicit_failed",
            "explicit_pre_acceptance_failure": True,
            "error": "provider rejected before acceptance",
        }

    first = dispatch_notification_batch_once(
        repo,
        route=_route(),
        send_fn=_reject,
        now_ms=first_at,
    )
    first_batch = first["batch"]
    assert first_batch["attempt_count"] == 1
    assert first_batch["status"] == "explicit_failed"
    assert repo.get_trade_lifecycle_notification(
        row["outbox_id"]
    )["status"] == "batched"

    second_at = int(first_batch["next_attempt_at_ms"])
    second = dispatch_notification_batch_once(
        repo,
        route=_route(),
        send_fn=_reject,
        now_ms=second_at,
    )
    assert second["batch"]["attempt_count"] == 2
    assert (
        int(second["batch"]["next_attempt_at_ms"])
        == second_at + BATCH_RETRY_BACKOFF_MS[1]
    )

    third_at = int(second["batch"]["next_attempt_at_ms"])
    third = dispatch_notification_batch_once(
        repo,
        route=_route(),
        send_fn=_reject,
        now_ms=third_at,
    )
    assert third["batch"]["attempt_count"] == 3
    assert third["batch"]["next_attempt_at_ms"] is None
    terminal_member = repo.get_trade_lifecycle_notification(
        row["outbox_id"]
    )
    assert terminal_member["status"] == "explicit_failed"
    assert terminal_member["attempt_count"] == 3

    fourth = dispatch_notification_batch_once(
        repo,
        route=_route(),
        send_fn=_reject,
        now_ms=third_at + TARGET_SEND_INTERVAL_MS,
    )
    assert fourth["status"] == "idle"
    assert len(seen_batch_ids) == 3
    assert len(set(seen_batch_ids)) == 1


def test_route_budget_holds_new_intent_for_sixty_seconds(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    first_row = _enqueue(repo, suffix="first")
    first_at = _quiet_now([first_row])
    calls: list[str] = []

    def _confirm(payload: dict) -> dict:
        calls.append(str(payload["batch_id"]))
        return {
            "status": "confirmed",
            "delivery_confirmed": True,
            "message_id": f"message-{len(calls)}",
        }

    first = dispatch_notification_batch_once(
        repo,
        route=_route(),
        send_fn=_confirm,
        now_ms=first_at,
    )
    assert first["status"] == "confirmed"
    second_row = _enqueue(repo, suffix="second")
    quiet_ready = max(
        first_at + QUIET_WINDOW_MS,
        int(second_row["created_at_ms"]) + QUIET_WINDOW_MS,
    )
    held_at = min(
        quiet_ready,
        first_at + TARGET_SEND_INTERVAL_MS - 1,
    )
    held = dispatch_notification_batch_once(
        repo,
        route=_route(),
        send_fn=_confirm,
        now_ms=held_at,
    )
    assert held["status"] == "idle"
    released_at = max(
        first_at + TARGET_SEND_INTERVAL_MS,
        int(second_row["created_at_ms"]) + QUIET_WINDOW_MS,
    )
    released = dispatch_notification_batch_once(
        repo,
        route=_route(),
        send_fn=_confirm,
        now_ms=released_at,
    )
    assert released["status"] == "confirmed"
    assert len(calls) == 2


def test_stale_claim_recovers_but_stale_send_freezes_whole_batch(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    rows = [_enqueue(repo, suffix=str(index)) for index in range(2)]
    planned_at = _quiet_now(rows)
    planned = plan_notification_batch(
        repo,
        route=_route(),
        now_ms=planned_at,
    )
    batch_id = planned["batch"]["batch_id"]
    first_claim = claim_next_notification_batch(
        repo,
        now_ms=planned_at,
        claim_id="claim-1",
    )
    assert first_claim is not None
    recovered_at = planned_at + CLAIM_LEASE_MS + 1
    recovered = recover_stale_notification_batches(
        repo,
        now_ms=recovered_at,
    )
    assert recovered["reclaimed_claimed_count"] == 1
    assert repo.get_trade_lifecycle_notification_batch(
        batch_id
    )["status"] == "pending"
    assert {
        row["status"]
        for row in repo.list_trade_lifecycle_notification_batch_members(
            batch_id
        )
    } == {"batched"}

    second_claim = claim_next_notification_batch(
        repo,
        now_ms=recovered_at,
        claim_id="claim-2",
    )
    assert second_claim is not None
    mark_notification_batch_send_started(
        repo,
        batch_id=batch_id,
        claim_id="claim-2",
        now_ms=recovered_at,
    )
    frozen = recover_stale_notification_batches(
        repo,
        now_ms=recovered_at + CLAIM_LEASE_MS + 1,
    )
    assert frozen["frozen_unknown_count"] == 1
    assert repo.get_trade_lifecycle_notification_batch(
        batch_id
    )["status"] == "unknown"
    assert {
        row["status"]
        for row in repo.list_trade_lifecycle_notification_batch_members(
            batch_id
        )
    } == {"unknown"}


def test_stale_claims_do_not_consume_provider_attempt_budget(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    row = _enqueue(repo, suffix="claim-recovery")
    current = _quiet_now([row])
    batch_id = plan_notification_batch(
        repo,
        route=_route(),
        now_ms=current,
    )["batch"]["batch_id"]

    for index in range(3):
        claimed = claim_next_notification_batch(
            repo,
            now_ms=current,
            claim_id=f"claim-{index}",
        )
        assert claimed is not None
        assert claimed["attempt_count"] == 0
        current += CLAIM_LEASE_MS + 1
        recovered = recover_stale_notification_batches(
            repo,
            now_ms=current,
        )
        assert recovered["reclaimed_claimed_count"] == 1
        assert repo.get_trade_lifecycle_notification_batch(
            batch_id
        )["attempt_count"] == 0

    final_claim = claim_next_notification_batch(
        repo,
        now_ms=current,
        claim_id="claim-final",
    )
    assert final_claim is not None
    started = mark_notification_batch_send_started(
        repo,
        batch_id=batch_id,
        claim_id="claim-final",
        now_ms=current,
    )
    assert started["attempt_count"] == 1


def test_unknown_batch_resend_creates_one_compensating_intent_per_member(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    rows = [_enqueue(repo, suffix=str(index)) for index in range(2)]
    result = dispatch_notification_batch_once(
        repo,
        route=_route(),
        send_fn=lambda _payload: {"status": "unknown"},
        now_ms=_quiet_now(rows),
    )
    batch_id = result["batch"]["batch_id"]

    resend = reconcile_notification_batch(
        repo,
        batch_id=batch_id,
        action="resend",
        broker_ref="provider-check-1",
        note="provider acceptance could not be proven",
        apply_changes=True,
        now_ms=_quiet_now(rows) + 1,
    )

    assert resend["compensating_intent_count"] == 2
    assert repo.get_trade_lifecycle_notification_batch(
        batch_id
    )["status"] == "unknown"
    compensating = resend["compensating_intents"]
    assert {row["status"] for row in compensating} == {"pending"}
    assert {
        row["delivery_batch_id"] for row in compensating
    } == {None}
    assert {
        row["payload"]["compensates_batch_id"]
        for row in compensating
    } == {batch_id}


def test_suppressed_and_confirmed_legacy_rows_are_not_reactivated(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    suppressed = build_notification_intent(
        case_id="case-suppressed",
        transition_type="needs_review",
        resolution_revision=1,
        transition_key="lifecycle:case-suppressed:needs_review",
        state_fingerprint="state-suppressed",
        payload={"account": "lx", "case_id": "case-suppressed"},
        status="suppressed",
    )
    assert repo.insert_trade_lifecycle_notification_once(suppressed)
    confirmed = _enqueue(repo, suffix="confirmed")
    assert repo.compare_and_set_trade_lifecycle_notification(
        outbox_id=confirmed["outbox_id"],
        expected_status="pending",
        new_status="confirmed",
        fields={"confirmed_at_ms": int(confirmed["created_at_ms"])},
    )

    preview = plan_notification_batch(
        repo,
        route=_route(),
        now_ms=max(
            int(confirmed["created_at_ms"]),
            int(
                repo.get_trade_lifecycle_notification(
                    suppressed["outbox_id"]
                )["created_at_ms"]
            ),
        )
        + MAX_BATCH_WAIT_MS,
        apply_changes=False,
    )

    assert preview["status"] == "idle"
    for outbox_id in (suppressed["outbox_id"], confirmed["outbox_id"]):
        assert repo.get_trade_lifecycle_notification(
            outbox_id
        )["delivery_batch_id"] is None


def test_terminal_completion_rolls_back_when_member_cardinality_changes(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    row = _enqueue(repo, suffix="cardinality")
    current = _quiet_now([row])
    batch = plan_notification_batch(
        repo,
        route=_route(),
        now_ms=current,
    )["batch"]
    claimed = claim_next_notification_batch(
        repo,
        now_ms=current,
        claim_id="claim-cardinality",
    )
    assert claimed is not None
    mark_notification_batch_send_started(
        repo,
        batch_id=batch["batch_id"],
        claim_id="claim-cardinality",
        now_ms=current,
    )
    with repo._connect() as conn:
        conn.execute(
            """
            UPDATE trade_lifecycle_notification_outbox
            SET status = 'unknown'
            WHERE outbox_id = ?
            """,
            (row["outbox_id"],),
        )

    try:
        complete_notification_batch_attempt(
            repo,
            batch_id=batch["batch_id"],
            claim_id="claim-cardinality",
            outcome="confirmed",
            now_ms=current + 1,
        )
    except ValueError as exc:
        assert "member mismatch" in str(exc)
    else:
        raise AssertionError("expected atomic member mismatch failure")
    assert repo.get_trade_lifecycle_notification_batch(
        batch["batch_id"]
    )["status"] == "send_started"


@pytest.mark.parametrize(
    "tamper",
    ("member_id", "payload_hash", "payload"),
)
def test_repository_rejects_frozen_member_mismatch(
    tmp_path: Path,
    tamper: str,
) -> None:
    repo = _repo(tmp_path)
    rows = [
        _enqueue(repo, suffix="tamper-a"),
        _enqueue(repo, suffix="tamper-b"),
    ]
    preview = plan_notification_batch(
        repo,
        route=_route(),
        now_ms=_quiet_now(rows),
        apply_changes=False,
    )
    batch = copy.deepcopy(preview["batch"])
    member_ids = [row["outbox_id"] for row in rows]
    if tamper == "member_id":
        batch["payload"]["members"][0]["outbox_id"] = "outbox_wrong"
    elif tamper == "payload_hash":
        batch["payload"]["members"][0]["payload_hash"] = "wrong"
    else:
        batch["payload"]["members"][0]["payload"]["symbol"] = "WRONG"
    batch["payload_hash"] = canonical_payload_hash(batch["payload"])

    with pytest.raises(ValueError):
        repo.insert_trade_lifecycle_notification_batch_once(
            batch,
            member_outbox_ids=member_ids,
        )

    assert repo.list_trade_lifecycle_notification_batches() == []
    for row in rows:
        stored = repo.get_trade_lifecycle_notification(row["outbox_id"])
        assert stored["status"] == "pending"
        assert stored["delivery_batch_id"] is None
