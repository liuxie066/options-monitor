from __future__ import annotations

from pathlib import Path
import sqlite3
import uuid

import pytest

from src.application.ledger.api import (
    build_lifecycle_attempt_audit_envelope,
    lifecycle_attempt_diagnostic_sha256,
    record_lifecycle_attempt_audit_atomically,
)
from src.application.ledger.repository import (
    SQLiteOptionPositionsRepository,
)
from src.application.trades.inbox import (
    SettlementAttemptClaimOwnershipLost,
    claim_settlement_attempt,
    claim_settlement_provider_batch,
    complete_settlement_attempt,
    finish_settlement_attempt_provider_invocation,
    get_settlement_attempt_state,
    list_settlement_attempt_states,
    mark_settlement_attempt_provider_started,
    reconcile_settlement_attempt_invocation,
    replace_finished_settlement_attempt_provider_invocation,
    renew_settlement_attempt_claim,
    renew_settlement_provider_batch_claim,
    release_settlement_provider_batch_claim,
    reserve_settlement_attempt_invocation,
    settlement_attempt_summary,
    upsert_settlement_attempt_state,
)
from src.application.trades.settlement_attempts import (
    SettlementAttemptOutcome,
    SettlementCapabilitySnapshot,
    SettlementCollectorContract,
    backoff_delay_ms,
    classify_exception_outcome,
    classify_observation_outcome,
    prepare_provider_required_state,
    provider_input_scope_fingerprint,
    settlement_attempt_updates_after_outcome,
)


def _state(*, now_ms: int = 1_000) -> dict:
    return prepare_provider_required_state(
        None,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint_value="case-scope-1",
        provider_input_scope_fingerprint_value="provider-scope-1",
        contract_version="collector.v1",
        capability_fingerprint="capability-1",
        now_ms=now_ms,
    )


def _outcome(kind: str) -> SettlementAttemptOutcome:
    return SettlementAttemptOutcome(
        kind=kind,
        source_id="lx",
        account="lx",
        case_id="case-1",
        contract_version="collector.v1",
        capability_fingerprint="capability-1",
        reason_code=f"reason:{kind}",
        error_class="unknown",
    )


def _reserved(path: Path) -> dict:
    upsert_settlement_attempt_state(path, state=_state())
    reserved = reserve_settlement_attempt_invocation(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint="case-scope-1",
        claim_id="claim-1",
        now_ms=1_000,
        lease_ms=1,
    )
    assert reserved is not None
    return reserved


def _reserve_started(path: Path) -> dict:
    reserved = _reserved(path)
    return mark_settlement_attempt_provider_started(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        claim_id="claim-1",
        invocation_id=str(reserved["invocation_id"]),
        attempted_at_ms=1_500,
    )


def _failure_finished(path: Path) -> dict:
    started = _reserve_started(path)
    outcome = _outcome("unknown_error")
    diagnostic = lifecycle_attempt_diagnostic_sha256(
        reason_code=outcome.reason_code,
        provider_code=outcome.provider_code,
        error_class=outcome.error_class,
    )
    return finish_settlement_attempt_provider_invocation(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        claim_id="claim-1",
        invocation_id=str(started["invocation_id"]),
        outcome=outcome,
        outcome_code=7,
        semantic_fingerprint=None,
        receipt_sha256=None,
        diagnostic_sha256=diagnostic,
        control_now_ms=2_000,
    )


def _failure_audit(
    state: dict,
    *,
    ordinal: int = 1,
    last_ordinal: int = 1,
) -> dict:
    invocation = uuid.UUID(str(state["invocation_id"])).bytes
    return {
        "case_id": "case-1",
        "invocation_id": invocation,
        "attempted_at_ms": 1_500,
        "outcome_code": 7,
        "semantic_fingerprint": None,
        "receipt_sha256": None,
        "diagnostic_sha256": state["pending_diagnostic_sha256"],
        "span_ordinal": None,
        "ordinal": ordinal,
        "last_ordinal": last_ordinal,
        "last_invocation_id": invocation,
        "chain_sha256": b"c" * 32,
    }


def _execute_base_14d06ca1_writer_sql(
    conn: sqlite3.Connection,
    operation: str,
) -> None:
    if operation == "claim":
        conn.execute(
            """
            UPDATE lifecycle_settlement_attempt_state
            SET claim_id = ?, claim_until_ms = ?, updated_at_ms = ?
            WHERE source_id = ? AND account = ? AND case_id = ?
              AND case_scope_fingerprint = ?
              AND classification = 'provider_required'
              AND (next_attempt_at_ms IS NULL OR next_attempt_at_ms <= ?)
              AND (
                claim_id IS NULL OR claim_id = ''
                OR claim_until_ms IS NULL OR claim_until_ms <= ?
                OR claim_id = ?
              )
            """,
            (
                "base-worker",
                122_000,
                2_000,
                "lx",
                "lx",
                "case-1",
                "case-scope-1",
                2_000,
                2_000,
                "base-worker",
            ),
        )
        return
    if operation == "complete":
        conn.execute(
            """
            UPDATE lifecycle_settlement_attempt_state
            SET case_scope_fingerprint = ?,
                provider_input_scope_fingerprint = ?,
                collector_contract_version = ?,
                capability_fingerprint = ?,
                classification = ?,
                outcome_kind = ?,
                reason_code = ?,
                provider_code = ?,
                error_class = ?,
                attempt_count = ?,
                no_progress_count = ?,
                next_attempt_at_ms = ?,
                last_attempt_at_ms = ?,
                last_semantic_fingerprint = ?,
                claim_id = ?,
                claim_until_ms = ?,
                updated_at_ms = ?
            WHERE source_id = ? AND account = ? AND case_id = ?
              AND claim_id = ?
            """,
            (
                "base-complete-scope",
                "base-provider-scope",
                "collector.v1",
                "capability-1",
                "provider_required",
                "base_complete",
                "base_complete_reason",
                None,
                None,
                1,
                0,
                0,
                2_000,
                None,
                None,
                None,
                2_000,
                "lx",
                "lx",
                "case-1",
                "claim-1",
            ),
        )
        return
    if operation == "upsert":
        conn.execute(
            """
            INSERT INTO lifecycle_settlement_attempt_state (
              source_id, account, case_id, case_scope_fingerprint,
              provider_input_scope_fingerprint,
              collector_contract_version, capability_fingerprint,
              classification, outcome_kind, reason_code, provider_code,
              error_class, attempt_count, no_progress_count,
              next_attempt_at_ms, last_attempt_at_ms,
              last_semantic_fingerprint, claim_id, claim_until_ms,
              updated_at_ms
            ) VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(source_id, account, case_id) DO UPDATE SET
              case_scope_fingerprint = excluded.case_scope_fingerprint,
              provider_input_scope_fingerprint = excluded.provider_input_scope_fingerprint,
              collector_contract_version = excluded.collector_contract_version,
              capability_fingerprint = excluded.capability_fingerprint,
              classification = excluded.classification,
              outcome_kind = excluded.outcome_kind,
              reason_code = excluded.reason_code,
              provider_code = excluded.provider_code,
              error_class = excluded.error_class,
              attempt_count = excluded.attempt_count,
              no_progress_count = excluded.no_progress_count,
              next_attempt_at_ms = excluded.next_attempt_at_ms,
              last_attempt_at_ms = excluded.last_attempt_at_ms,
              last_semantic_fingerprint = excluded.last_semantic_fingerprint,
              claim_id = excluded.claim_id,
              claim_until_ms = excluded.claim_until_ms,
              updated_at_ms = excluded.updated_at_ms
            WHERE lifecycle_settlement_attempt_state.claim_id IS NULL
               OR lifecycle_settlement_attempt_state.claim_id = ''
               OR lifecycle_settlement_attempt_state.claim_until_ms IS NULL
               OR lifecycle_settlement_attempt_state.claim_until_ms <= excluded.updated_at_ms
               OR lifecycle_settlement_attempt_state.claim_id = excluded.claim_id
            """,
            (
                "lx",
                "lx",
                "case-1",
                "base-upsert-scope",
                "base-provider-scope",
                "collector.v1",
                "capability-1",
                "provider_required",
                "base_upsert",
                "base_upsert_reason",
                None,
                None,
                1,
                0,
                0,
                2_000,
                None,
                "claim-1",
                0,
                2_000,
            ),
        )
        return
    raise AssertionError(f"unknown base writer operation: {operation}")


def test_attempt_backoff_schedules_are_bounded() -> None:
    assert [
        backoff_delay_ms("retryable_error", attempt_count=count, no_progress_count=0)
        for count in range(6)
    ] == [60_000, 300_000, 900_000, 3_600_000, 3_600_000, 3_600_000]
    assert [
        backoff_delay_ms("unknown_error", attempt_count=count, no_progress_count=0)
        for count in range(6)
    ] == [300_000, 900_000, 3_600_000, 21_600_000, 21_600_000, 21_600_000]
    assert [
        backoff_delay_ms("observed_incomplete", attempt_count=0, no_progress_count=count)
        for count in range(6)
    ] == [300_000, 900_000, 3_600_000, 21_600_000, 21_600_000, 21_600_000]
    assert backoff_delay_ms(
        "blocked_account_explicit",
        attempt_count=0,
        no_progress_count=0,
    ) == 86_400_000
    assert backoff_delay_ms(
        "blocked_static",
        attempt_count=0,
        no_progress_count=0,
    ) is None


def test_attempt_updates_sanitize_malformed_persisted_counters() -> None:
    state = {
        **_state(),
        "attempt_count": "malformed",
        "no_progress_count": -5,
    }

    updates = settlement_attempt_updates_after_outcome(
        state,
        outcome=_outcome("unknown_error"),
        now_ms=1_000,
        case_scope_fingerprint_value="case-scope-1",
        provider_input_scope_fingerprint_value="provider-scope-1",
        provider_attempted=True,
    )

    assert updates["attempt_count"] == 1
    assert updates["no_progress_count"] == 0
    assert updates["next_attempt_at_ms"] == 301_000


@pytest.mark.parametrize(
    ("outcome_kind", "expected_calls"),
    [
        ("retryable_error", 27),
        ("unknown_error", 7),
        ("blocked_account_explicit", 2),
        ("observed_incomplete", 7),
    ],
)
def test_minute_ticks_have_exact_bounded_calls_through_24_hours(
    outcome_kind: str,
    expected_calls: int,
) -> None:
    state = _state(now_ms=1)
    call_count = 0
    for now_ms in range(0, 86_400_001, 60_000):
        next_attempt = state.get("next_attempt_at_ms")
        if next_attempt is not None and int(next_attempt) > now_ms:
            continue
        call_count += 1
        updates = settlement_attempt_updates_after_outcome(
            state,
            outcome=_outcome(outcome_kind),
            now_ms=now_ms,
            case_scope_fingerprint_value="case-scope-1",
            provider_input_scope_fingerprint_value=(
                "provider-scope-1"
            ),
            semantic_fingerprint=(
                "semantic-1"
                if outcome_kind == "observed_incomplete"
                else None
            ),
            provider_attempted=True,
        )
        state = {**state, **updates}

    assert call_count == expected_calls


def test_case_scope_change_preserves_backoff_when_provider_scope_is_stable() -> None:
    prior = {
        **_state(),
        "outcome_kind": "unknown_error",
        "attempt_count": 3,
        "next_attempt_at_ms": 99_000,
        "last_attempt_at_ms": 2_000,
    }

    changed_case = prepare_provider_required_state(
        prior,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint_value="case-scope-2",
        provider_input_scope_fingerprint_value="provider-scope-1",
        contract_version="collector.v1",
        capability_fingerprint="capability-1",
        now_ms=3_000,
    )
    changed_capability = prepare_provider_required_state(
        prior,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint_value="case-scope-2",
        provider_input_scope_fingerprint_value="provider-scope-1",
        contract_version="collector.v1",
        capability_fingerprint="capability-2",
        now_ms=3_000,
    )

    assert changed_case["attempt_count"] == 3
    assert changed_case["next_attempt_at_ms"] == 99_000
    assert changed_capability["attempt_count"] == 0
    assert changed_capability["next_attempt_at_ms"] is None


def test_effective_anchor_identity_resets_provider_backoff_only_when_changed() -> None:
    lifecycle_case = {
        "case_id": "case-1",
        "account": "lx",
        "futu_account_id": "1001",
        "contract_key": "contract-1",
        "target_contracts_by_lot": {"lot-1": 1},
        "observation_start_ms": 100,
    }
    read_model = {
        "pending_until_ms": 200,
        "pairing_until_ms": 150,
        "first_option_close_received_at_ms": 120,
        "remaining_contracts_by_lot": {"lot-1": 1},
        "reserved_contracts_by_lot": {"lot-1": 1},
        "terminal_event_ids": [],
        "reservation_evidence_ids": ["anchor-evidence-1"],
        "timing_policy_hash": "timing-1",
    }
    scope_a = provider_input_scope_fingerprint(
        lifecycle_case=lifecycle_case,
        read_model=read_model,
    )
    scope_with_unrelated_context = provider_input_scope_fingerprint(
        lifecycle_case=lifecycle_case,
        read_model={
            **read_model,
            "_settlement_observation_context": {
                "unrelated_evidence_id": "diagnostic-1"
            },
        },
    )
    scope_b = provider_input_scope_fingerprint(
        lifecycle_case=lifecycle_case,
        read_model={
            **read_model,
            "reservation_evidence_ids": ["anchor-evidence-2"],
        },
    )
    prior = prepare_provider_required_state(
        None,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint_value="case-scope-1",
        provider_input_scope_fingerprint_value=scope_a,
        contract_version="collector.v1",
        capability_fingerprint="capability-1",
        now_ms=1_000,
    )
    prior = {
        **prior,
        **settlement_attempt_updates_after_outcome(
            prior,
            outcome=_outcome("unknown_error"),
            now_ms=1_000,
            case_scope_fingerprint_value="case-scope-1",
            provider_input_scope_fingerprint_value=scope_a,
            provider_attempted=True,
        ),
    }

    unchanged = prepare_provider_required_state(
        prior,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint_value="case-scope-2",
        provider_input_scope_fingerprint_value=(
            scope_with_unrelated_context
        ),
        contract_version="collector.v1",
        capability_fingerprint="capability-1",
        now_ms=2_000,
    )
    changed_anchor = prepare_provider_required_state(
        prior,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint_value="case-scope-2",
        provider_input_scope_fingerprint_value=scope_b,
        contract_version="collector.v1",
        capability_fingerprint="capability-1",
        now_ms=2_000,
    )

    assert scope_with_unrelated_context == scope_a
    assert scope_b != scope_a
    assert unchanged["attempt_count"] == 1
    assert unchanged["next_attempt_at_ms"] == 301_000
    assert changed_anchor["attempt_count"] == 0
    assert changed_anchor["next_attempt_at_ms"] is None


def test_legacy_semantic_block_rechecks_after_evidence_scope_changes() -> None:
    prior = {
        **_state(),
        "outcome_kind": "legacy_semantic_unavailable",
        "attempt_count": 1,
        "last_attempt_at_ms": 2_000,
    }

    unchanged = prepare_provider_required_state(
        prior,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint_value="case-scope-1",
        provider_input_scope_fingerprint_value="provider-scope-1",
        contract_version="collector.v1",
        capability_fingerprint="capability-1",
        now_ms=3_000,
    )
    repaired_evidence = prepare_provider_required_state(
        prior,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint_value="case-scope-2",
        provider_input_scope_fingerprint_value="provider-scope-1",
        contract_version="collector.v1",
        capability_fingerprint="capability-1",
        now_ms=3_000,
    )

    assert unchanged["outcome_kind"] == "legacy_semantic_unavailable"
    assert repaired_evidence["outcome_kind"] is None
    assert repaired_evidence["attempt_count"] == 0


def test_unknown_errors_never_promote_to_permanent_block() -> None:
    state = _state()
    now_ms = 1_000
    for _ in range(10):
        updates = settlement_attempt_updates_after_outcome(
            state,
            outcome=_outcome("unknown_error"),
            now_ms=now_ms,
            case_scope_fingerprint_value="case-scope-1",
            provider_input_scope_fingerprint_value="provider-scope-1",
        )
        state = {**state, **updates}
        now_ms = int(state["next_attempt_at_ms"])

    assert state["outcome_kind"] == "unknown_error"
    assert int(state["next_attempt_at_ms"]) - int(state["last_attempt_at_ms"]) == 21_600_000


def test_stale_revalidation_does_not_count_as_provider_attempt() -> None:
    state = _state()

    before_call = settlement_attempt_updates_after_outcome(
        state,
        outcome=_outcome("stale_generation"),
        now_ms=2_000,
        case_scope_fingerprint_value="case-scope-2",
        provider_input_scope_fingerprint_value="provider-scope-1",
        provider_attempted=False,
    )
    after_call = settlement_attempt_updates_after_outcome(
        state,
        outcome=_outcome("stale_generation"),
        now_ms=2_000,
        case_scope_fingerprint_value="case-scope-2",
        provider_input_scope_fingerprint_value="provider-scope-1",
        provider_attempted=True,
    )

    assert before_call["attempt_count"] == 0
    assert before_call["last_attempt_at_ms"] is None
    assert before_call["classification"] == "unclassified"
    assert after_call["attempt_count"] == 1
    assert after_call["last_attempt_at_ms"] == 2_000
    assert after_call["classification"] == "unclassified"


def test_claim_completion_is_atomic_and_stale_owner_cannot_overwrite(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    upsert_settlement_attempt_state(path, state=_state())
    assert claim_settlement_attempt(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint="case-scope-1",
        claim_id="claim-1",
        now_ms=1_000,
        lease_ms=1,
    )

    attempted_overwrite = upsert_settlement_attempt_state(
        path,
        state={
            **_state(now_ms=2_000),
            "outcome_kind": "unknown_error",
        },
    )
    assert attempted_overwrite["claim_id"] == "claim-1"
    assert attempted_overwrite["outcome_kind"] is None

    with pytest.raises(
        SettlementAttemptClaimOwnershipLost,
        match="claim ownership changed",
    ):
        complete_settlement_attempt(
            path,
            source_id="lx",
            account="lx",
            case_id="case-1",
            claim_id="stale-claim",
            updates={"outcome_kind": "unknown_error"},
        )

    completed = complete_settlement_attempt(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        claim_id="claim-1",
        updates={
            "outcome_kind": "unknown_error",
            "next_attempt_at_ms": 301_000,
            "updated_at_ms": 2_000,
        },
    )
    assert completed["claim_id"] is None
    assert completed["outcome_kind"] == "unknown_error"
    assert get_settlement_attempt_state(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
    ) == completed


def test_invocation_columns_upgrade_additively_and_legacy_rows_remain_valid(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-inbox.sqlite3"
    state = _state()
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE lifecycle_settlement_attempt_state (
              source_id TEXT NOT NULL,
              account TEXT NOT NULL,
              case_id TEXT NOT NULL,
              case_scope_fingerprint TEXT NOT NULL,
              provider_input_scope_fingerprint TEXT,
              collector_contract_version TEXT NOT NULL,
              capability_fingerprint TEXT NOT NULL,
              classification TEXT NOT NULL,
              outcome_kind TEXT,
              reason_code TEXT,
              provider_code TEXT,
              error_class TEXT,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              no_progress_count INTEGER NOT NULL DEFAULT 0,
              next_attempt_at_ms INTEGER,
              last_attempt_at_ms INTEGER,
              last_semantic_fingerprint TEXT,
              claim_id TEXT,
              claim_until_ms INTEGER,
              updated_at_ms INTEGER NOT NULL,
              PRIMARY KEY(source_id, account, case_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO lifecycle_settlement_attempt_state (
              source_id, account, case_id, case_scope_fingerprint,
              provider_input_scope_fingerprint,
              collector_contract_version, capability_fingerprint,
              classification, attempt_count, no_progress_count, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state["source_id"],
                state["account"],
                state["case_id"],
                state["case_scope_fingerprint"],
                state["provider_input_scope_fingerprint"],
                state["collector_contract_version"],
                state["capability_fingerprint"],
                state["classification"],
                state["attempt_count"],
                state["no_progress_count"],
                state["updated_at_ms"],
            ),
        )

    stored = get_settlement_attempt_state(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
    )
    assert stored is not None
    assert stored["invocation_state"] is None
    assert stored["invocation_writer_epoch"] == 0
    assert all(
        stored[field] is None
        for field in (
            "invocation_id",
            "invocation_attempted_at_ms",
            "pending_outcome_code",
            "pending_semantic_fingerprint",
            "pending_receipt_sha256",
            "pending_diagnostic_sha256",
            "pending_outcome_kind",
            "pending_reason_code",
            "pending_provider_code",
            "pending_error_class",
            "pending_retry_after_ms",
            "pending_control_now_ms",
            "committed_audit_ordinal",
            "committed_chain_sha256",
        )
    )
    assert get_settlement_attempt_state(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
    ) == stored
    with sqlite3.connect(path) as conn:
        columns = [
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(lifecycle_settlement_attempt_state)"
            )
        ]
        trigger_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM sqlite_master
            WHERE type = 'trigger'
              AND name =
                'trg_lifecycle_settlement_attempt_invocation_writer_fence'
            """
        ).fetchone()[0]
    assert columns.count("invocation_writer_epoch") == 1
    assert trigger_count == 1


@pytest.mark.parametrize("operation", ["claim", "complete", "upsert"])
@pytest.mark.parametrize(
    "invocation_state",
    [
        None,
        "reserved",
        "provider_started",
        "provider_finished",
        "ambiguous_provider_result",
        "ledger_committed",
    ],
)
def test_base_writer_sql_updates_only_legacy_null_invocation_rows(
    tmp_path: Path,
    operation: str,
    invocation_state: str | None,
) -> None:
    state_label = invocation_state or "legacy-null"
    path = tmp_path / f"{operation}-{state_label}.sqlite3"
    upsert_settlement_attempt_state(path, state=_state())
    invocation_id = str(uuid.uuid4()) if invocation_state is not None else None
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            UPDATE lifecycle_settlement_attempt_state
            SET claim_id = 'claim-1', claim_until_ms = 0,
                next_attempt_at_ms = 0,
                invocation_id = ?, invocation_state = ?,
                invocation_attempted_at_ms = CASE
                  WHEN ? IN ('provider_started', 'provider_finished',
                             'ambiguous_provider_result', 'ledger_committed')
                  THEN 1500
                  ELSE NULL
                END,
                invocation_writer_epoch = invocation_writer_epoch + CASE
                  WHEN ? IS NULL THEN 0 ELSE 1
                END
            WHERE source_id = 'lx' AND account = 'lx' AND case_id = 'case-1'
            """,
            (
                invocation_id,
                invocation_state,
                invocation_state,
                invocation_state,
            ),
        )

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        before = dict(
            conn.execute(
                "SELECT * FROM lifecycle_settlement_attempt_state"
            ).fetchone()
        )
        if invocation_state is None:
            _execute_base_14d06ca1_writer_sql(conn, operation)
            conn.commit()
            after = dict(
                conn.execute(
                    "SELECT * FROM lifecycle_settlement_attempt_state"
                ).fetchone()
            )
            assert after["updated_at_ms"] == 2_000
            assert after["invocation_writer_epoch"] == 0
            assert after != before
        else:
            with pytest.raises(
                sqlite3.IntegrityError,
                match="requires current writer",
            ):
                _execute_base_14d06ca1_writer_sql(conn, operation)
            conn.rollback()
            after = dict(
                conn.execute(
                    "SELECT * FROM lifecycle_settlement_attempt_state"
                ).fetchone()
            )
            assert after == before


def test_reservation_reuses_uuid_and_blocks_legacy_claim_path(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    upsert_settlement_attempt_state(path, state=_state())

    reserved = reserve_settlement_attempt_invocation(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint="case-scope-1",
        claim_id="claim-1",
        now_ms=1_000,
        lease_ms=1,
    )
    assert reserved is not None
    parsed = uuid.UUID(str(reserved["invocation_id"]))
    assert parsed.version == 4
    assert str(parsed) == reserved["invocation_id"]
    assert reserved["invocation_state"] == "reserved"
    assert reserved["invocation_attempted_at_ms"] is None

    same_owner = reserve_settlement_attempt_invocation(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint="case-scope-1",
        claim_id="claim-1",
        now_ms=2_000,
        lease_ms=1,
    )
    assert same_owner is not None
    assert same_owner["invocation_id"] == reserved["invocation_id"]
    assert reserve_settlement_attempt_invocation(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint="case-scope-1",
        claim_id="claim-2",
        now_ms=120_999,
        lease_ms=1,
    ) is None

    reused = reserve_settlement_attempt_invocation(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint="case-scope-1",
        claim_id="claim-2",
        now_ms=122_000,
        lease_ms=1,
    )
    assert reused is not None
    assert reused["invocation_id"] == reserved["invocation_id"]
    assert not claim_settlement_attempt(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint="case-scope-1",
        claim_id="legacy-worker",
        now_ms=242_000,
        lease_ms=1,
    )
    summary = settlement_attempt_summary(
        path,
        source_id="lx",
        account="lx",
        case_ids=("case-1",),
        now_ms=242_000,
    )
    assert summary["ambiguous_provider_result_count"] == 0
    assert summary["eligible_count"] == 1


def test_reserved_pre_call_completion_clears_invocation_without_attempt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    upsert_settlement_attempt_state(path, state=_state())
    reserved = reserve_settlement_attempt_invocation(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint="case-scope-1",
        claim_id="claim-1",
        now_ms=1_000,
        lease_ms=1,
    )
    assert reserved is not None
    outcome = _outcome("blocked_static")
    updates = settlement_attempt_updates_after_outcome(
        reserved,
        outcome=outcome,
        now_ms=2_000,
        case_scope_fingerprint_value="case-scope-1",
        provider_input_scope_fingerprint_value="provider-scope-1",
        provider_attempted=False,
    )

    completed = complete_settlement_attempt(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        claim_id="claim-1",
        updates=updates,
    )

    assert completed["attempt_count"] == 0
    assert completed["invocation_state"] is None
    assert completed["invocation_id"] is None
    assert completed["invocation_attempted_at_ms"] is None
    assert all(
        completed[field] is None
        for field in (
            "pending_outcome_code",
            "pending_semantic_fingerprint",
            "pending_receipt_sha256",
            "pending_diagnostic_sha256",
            "pending_outcome_kind",
            "pending_reason_code",
            "pending_provider_code",
            "pending_error_class",
            "pending_retry_after_ms",
            "pending_control_now_ms",
            "committed_audit_ordinal",
            "committed_chain_sha256",
        )
    )


def test_legacy_completion_rejects_post_start_invocation_states(
    tmp_path: Path,
) -> None:
    started_path = tmp_path / "started.sqlite3"
    started = _reserve_started(started_path)
    with pytest.raises(
        SettlementAttemptClaimOwnershipLost,
        match="requires exact audit reconciliation",
    ):
        complete_settlement_attempt(
            started_path,
            source_id="lx",
            account="lx",
            case_id="case-1",
            claim_id="claim-1",
            updates={},
        )

    finished_path = tmp_path / "finished.sqlite3"
    _failure_finished(finished_path)
    with pytest.raises(
        SettlementAttemptClaimOwnershipLost,
        match="requires exact audit reconciliation",
    ):
        complete_settlement_attempt(
            finished_path,
            source_id="lx",
            account="lx",
            case_id="case-1",
            claim_id="claim-1",
            updates={},
        )


def test_provider_finished_reconciles_exact_current_audit_and_control(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    finished = _failure_finished(path)

    assert finished["invocation_state"] == "provider_finished"
    assert finished["attempt_count"] == 0
    assert finished["no_progress_count"] == 0
    assert finished["last_semantic_fingerprint"] is None
    assert finished["outcome_kind"] == "unknown_error"
    assert finished["pending_outcome_kind"] == "unknown_error"
    assert finished["pending_outcome_code"] == 7
    assert finished["last_attempt_at_ms"] == 2_000
    assert finished["next_attempt_at_ms"] == 302_000

    audit = _failure_audit(finished)
    committed = reconcile_settlement_attempt_invocation(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        invocation_id=str(finished["invocation_id"]),
        audit=audit,
    )
    assert committed["invocation_state"] == "ledger_committed"
    assert committed["claim_id"] is None
    assert committed["attempt_count"] == 1
    assert committed["committed_audit_ordinal"] == 1
    assert committed["committed_chain_sha256"] == b"c" * 32
    assert reconcile_settlement_attempt_invocation(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        invocation_id=str(finished["invocation_id"]),
        audit=audit,
    ) == committed

    replanned = upsert_settlement_attempt_state(
        path,
        state={
            **_state(now_ms=3_000),
            "classification": "local",
        },
    )
    assert replanned["classification"] == "local"
    assert replanned["invocation_state"] is None
    assert all(
        replanned[field] is None
        for field in (
            "invocation_id",
            "invocation_attempted_at_ms",
            "pending_outcome_code",
            "pending_semantic_fingerprint",
            "pending_receipt_sha256",
            "pending_diagnostic_sha256",
            "pending_outcome_kind",
            "pending_reason_code",
            "pending_provider_code",
            "pending_error_class",
            "pending_retry_after_ms",
            "pending_control_now_ms",
            "committed_audit_ordinal",
            "committed_chain_sha256",
        )
    )


def test_real_ledger_lookup_reconciles_without_case_id_splicing(
    tmp_path: Path,
) -> None:
    inbox_path = tmp_path / "inbox.sqlite3"
    finished = _failure_finished(inbox_path)
    outcome = _outcome("unknown_error")
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    repo.upsert_trade_lifecycle_case(
        {
            "case_id": "case-1",
            "case_key": "case-1",
            "account": "lx",
            "symbol": "NVDA",
            "status": "waiting_settlement_evidence",
        }
    )
    envelope = build_lifecycle_attempt_audit_envelope(
        case_id="case-1",
        invocation_id=str(finished["invocation_id"]),
        attempted_at_ms=1_500,
        outcome_kind="processing_failure_after_call",
        reason_code=outcome.reason_code,
        provider_code=outcome.provider_code,
        error_class=outcome.error_class,
    )
    written = record_lifecycle_attempt_audit_atomically(
        repo,
        attempt_audit=envelope,
    )
    audit = repo.get_trade_lifecycle_attempt_audit_by_invocation(
        case_id="case-1",
        invocation_id=str(finished["invocation_id"]),
    )

    assert audit is not None
    assert audit["case_id"] == "case-1"
    committed = reconcile_settlement_attempt_invocation(
        inbox_path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        invocation_id=str(finished["invocation_id"]),
        audit=audit,
    )
    assert committed["invocation_state"] == "ledger_committed"
    assert committed["committed_audit_ordinal"] == written["audit_ordinal"]
    assert committed["committed_chain_sha256"].hex() == written[
        "audit_chain_sha256"
    ]


def test_provider_finish_replay_is_zero_write_and_mismatch_fails_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    finished = _failure_finished(path)
    outcome = _outcome("unknown_error")
    diagnostic = lifecycle_attempt_diagnostic_sha256(
        reason_code=outcome.reason_code,
        provider_code=outcome.provider_code,
        error_class=outcome.error_class,
    )

    replay = finish_settlement_attempt_provider_invocation(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        claim_id="claim-1",
        invocation_id=str(finished["invocation_id"]),
        outcome=outcome,
        outcome_code=7,
        semantic_fingerprint=None,
        receipt_sha256=None,
        diagnostic_sha256=diagnostic,
        control_now_ms=2_000,
    )
    assert replay == finished
    with pytest.raises(ValueError, match="provider-finish replay mismatch"):
        finish_settlement_attempt_provider_invocation(
            path,
            source_id="lx",
            account="lx",
            case_id="case-1",
            claim_id="claim-1",
            invocation_id=str(finished["invocation_id"]),
            outcome=outcome,
            outcome_code=4,
            semantic_fingerprint=None,
            receipt_sha256=None,
            diagnostic_sha256=diagnostic,
            control_now_ms=2_000,
        )
    assert get_settlement_attempt_state(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
    ) == finished


def test_provider_result_replacement_replays_or_reclassifies_once(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    finished = _failure_finished(path)
    original_outcome = _outcome("unknown_error")
    original_diagnostic = lifecycle_attempt_diagnostic_sha256(
        reason_code=original_outcome.reason_code,
        provider_code=original_outcome.provider_code,
        error_class=original_outcome.error_class,
    )
    original_kwargs = {
        "source_id": "lx",
        "account": "lx",
        "case_id": "case-1",
        "claim_id": "claim-1",
        "invocation_id": str(finished["invocation_id"]),
        "outcome": original_outcome,
        "outcome_code": 7,
        "semantic_fingerprint": None,
        "receipt_sha256": None,
        "diagnostic_sha256": original_diagnostic,
        "control_now_ms": 2_000,
    }

    assert replace_finished_settlement_attempt_provider_invocation(
        path,
        **original_kwargs,
    ) == finished

    stale_outcome = _outcome("stale_generation")
    stale_diagnostic = lifecycle_attempt_diagnostic_sha256(
        reason_code=stale_outcome.reason_code,
        provider_code=stale_outcome.provider_code,
        error_class=stale_outcome.error_class,
    )
    stale_kwargs = {
        **original_kwargs,
        "outcome": stale_outcome,
        "outcome_code": 6,
        "diagnostic_sha256": stale_diagnostic,
        "control_now_ms": 2_100,
    }
    replaced = replace_finished_settlement_attempt_provider_invocation(
        path,
        **stale_kwargs,
    )
    assert replaced["invocation_writer_epoch"] == (
        finished["invocation_writer_epoch"] + 1
    )
    assert replaced["pending_outcome_code"] == 6
    assert replaced["pending_outcome_kind"] == "stale_generation"
    assert replaced["pending_diagnostic_sha256"] == stale_diagnostic
    assert replaced["classification"] == "unclassified"
    assert replaced["committed_audit_ordinal"] is None
    assert replaced["committed_chain_sha256"] is None
    assert replace_finished_settlement_attempt_provider_invocation(
        path,
        **stale_kwargs,
    ) == replaced


@pytest.mark.parametrize("wrong_state", [False, True])
def test_provider_result_replacement_loses_wrong_owner_or_state(
    tmp_path: Path,
    wrong_state: bool,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    state = _reserve_started(path) if wrong_state else _failure_finished(path)
    outcome = _outcome("stale_generation")
    diagnostic = lifecycle_attempt_diagnostic_sha256(
        reason_code=outcome.reason_code,
        provider_code=outcome.provider_code,
        error_class=outcome.error_class,
    )
    before = get_settlement_attempt_state(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
    )

    with pytest.raises(SettlementAttemptClaimOwnershipLost):
        replace_finished_settlement_attempt_provider_invocation(
            path,
            source_id="lx",
            account="lx",
            case_id="case-1",
            claim_id="claim-wrong" if not wrong_state else "claim-1",
            invocation_id=str(state["invocation_id"]),
            outcome=outcome,
            outcome_code=6,
            semantic_fingerprint=None,
            receipt_sha256=None,
            diagnostic_sha256=diagnostic,
            control_now_ms=2_100,
        )
    assert get_settlement_attempt_state(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
    ) == before


def test_current_invocation_writer_advances_epoch_once_per_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    initial = upsert_settlement_attempt_state(path, state=_state())
    assert initial["invocation_writer_epoch"] == 0

    reserved = reserve_settlement_attempt_invocation(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint="case-scope-1",
        claim_id="claim-1",
        now_ms=1_000,
        lease_ms=1,
    )
    assert reserved is not None
    assert reserved["invocation_writer_epoch"] == 1
    assert renew_settlement_attempt_claim(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint="case-scope-1",
        claim_id="claim-1",
        now_ms=1_100,
        lease_ms=1,
    )
    renewed = get_settlement_attempt_state(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
    )
    assert renewed is not None
    assert renewed["invocation_writer_epoch"] == 2

    started = mark_settlement_attempt_provider_started(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        claim_id="claim-1",
        invocation_id=str(reserved["invocation_id"]),
        attempted_at_ms=1_500,
    )
    assert started["invocation_writer_epoch"] == 3
    outcome = _outcome("unknown_error")
    diagnostic = lifecycle_attempt_diagnostic_sha256(
        reason_code=outcome.reason_code,
        provider_code=outcome.provider_code,
        error_class=outcome.error_class,
    )
    finish_kwargs = {
        "source_id": "lx",
        "account": "lx",
        "case_id": "case-1",
        "claim_id": "claim-1",
        "invocation_id": str(started["invocation_id"]),
        "outcome": outcome,
        "outcome_code": 7,
        "semantic_fingerprint": None,
        "receipt_sha256": None,
        "diagnostic_sha256": diagnostic,
        "control_now_ms": 2_000,
    }
    finished = finish_settlement_attempt_provider_invocation(
        path,
        **finish_kwargs,
    )
    assert finished["invocation_writer_epoch"] == 4
    assert finish_settlement_attempt_provider_invocation(
        path,
        **finish_kwargs,
    ) == finished

    audit = _failure_audit(finished)
    committed = reconcile_settlement_attempt_invocation(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        invocation_id=str(finished["invocation_id"]),
        audit=audit,
    )
    assert committed["invocation_writer_epoch"] == 5
    assert reconcile_settlement_attempt_invocation(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        invocation_id=str(finished["invocation_id"]),
        audit=audit,
    ) == committed

    second = reserve_settlement_attempt_invocation(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint="case-scope-1",
        claim_id="claim-2",
        now_ms=400_000,
        lease_ms=1,
    )
    assert second is not None
    assert second["invocation_writer_epoch"] == 6
    updates = settlement_attempt_updates_after_outcome(
        second,
        outcome=_outcome("blocked_static"),
        now_ms=401_000,
        case_scope_fingerprint_value="case-scope-1",
        provider_input_scope_fingerprint_value="provider-scope-1",
        provider_attempted=False,
    )
    cleared = complete_settlement_attempt(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        claim_id="claim-2",
        updates=updates,
    )
    assert cleared["invocation_writer_epoch"] == 7
    assert cleared["invocation_state"] is None


def test_reserved_restart_is_safe_only_without_audit(tmp_path: Path) -> None:
    path = tmp_path / "inbox.sqlite3"
    reserved = _reserved(path)

    assert reconcile_settlement_attempt_invocation(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        invocation_id=str(reserved["invocation_id"]),
        audit=None,
    ) == reserved
    with pytest.raises(ValueError, match="conflicts with audit"):
        reconcile_settlement_attempt_invocation(
            path,
            source_id="lx",
            account="lx",
            case_id="case-1",
            invocation_id=str(reserved["invocation_id"]),
            audit={"case_id": "case-1"},
        )
    assert get_settlement_attempt_state(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
    ) == reserved


@pytest.mark.parametrize(
    ("finish_provider", "audit_present"),
    [(False, False), (False, True), (True, False)],
)
def test_unprovable_post_start_restart_becomes_unclaimed_ambiguous(
    tmp_path: Path,
    finish_provider: bool,
    audit_present: bool,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    state = _failure_finished(path) if finish_provider else _reserve_started(path)

    ambiguous = reconcile_settlement_attempt_invocation(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        invocation_id=str(state["invocation_id"]),
        audit={"case_id": "wrong-case"} if audit_present else None,
    )
    assert ambiguous["invocation_state"] == "ambiguous_provider_result"
    assert ambiguous["claim_id"] is None
    assert ambiguous["claim_until_ms"] is None
    summary = settlement_attempt_summary(
        path,
        source_id="lx",
        account="lx",
        case_ids=("case-1",),
        now_ms=999_999,
    )
    assert summary["ambiguous_provider_result_count"] == 1
    assert summary["eligible_count"] == 0


def test_stale_after_call_ambiguity_is_counted_but_not_eligible(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    started = _reserve_started(path)
    outcome = _outcome("stale_generation")
    diagnostic = lifecycle_attempt_diagnostic_sha256(
        reason_code=outcome.reason_code,
        provider_code=outcome.provider_code,
        error_class=outcome.error_class,
    )
    finished = finish_settlement_attempt_provider_invocation(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        claim_id="claim-1",
        invocation_id=str(started["invocation_id"]),
        outcome=outcome,
        outcome_code=6,
        semantic_fingerprint=None,
        receipt_sha256=None,
        diagnostic_sha256=diagnostic,
        control_now_ms=2_000,
    )
    assert finished["classification"] == "unclassified"

    ambiguous = reconcile_settlement_attempt_invocation(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        invocation_id=str(started["invocation_id"]),
        audit=None,
    )
    assert ambiguous["claim_id"] is None
    summary = settlement_attempt_summary(
        path,
        source_id="lx",
        account="lx",
        case_ids=("case-1",),
        now_ms=999_999,
    )
    assert summary["provider_required_count"] == 0
    assert summary["ambiguous_provider_result_count"] == 1
    assert summary["eligible_count"] == 0


def test_reconcile_rejects_historical_or_corrupt_pending_control(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    finished = _failure_finished(path)
    with pytest.raises(ValueError, match="not the current head"):
        reconcile_settlement_attempt_invocation(
            path,
            source_id="lx",
            account="lx",
            case_id="case-1",
            invocation_id=str(finished["invocation_id"]),
            audit=_failure_audit(finished, ordinal=1, last_ordinal=2),
        )
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            UPDATE lifecycle_settlement_attempt_state
            SET pending_retry_after_ms = 600000,
                invocation_writer_epoch = invocation_writer_epoch + 1
            WHERE source_id = 'lx' AND account = 'lx' AND case_id = 'case-1'
            """
        )
    with pytest.raises(
        ValueError,
        match="pending settlement control projection mismatch",
    ):
        get_settlement_attempt_state(
            path,
            source_id="lx",
            account="lx",
            case_id="case-1",
        )


def test_invocation_storage_rejects_text_hash_and_noninteger_control(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    finished = _failure_finished(path)
    with sqlite3.connect(path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                UPDATE lifecycle_settlement_attempt_state
                SET pending_diagnostic_sha256 = ?,
                    invocation_writer_epoch = invocation_writer_epoch + 1
                WHERE source_id = 'lx' AND account = 'lx' AND case_id = 'case-1'
                """,
                ("d" * 32,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                UPDATE lifecycle_settlement_attempt_state
                SET pending_control_now_ms = ?,
                    invocation_writer_epoch = invocation_writer_epoch + 1
                WHERE source_id = 'lx' AND account = 'lx' AND case_id = 'case-1'
                """,
                ("not-an-integer",),
            )
    assert get_settlement_attempt_state(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
    ) == finished


def test_attempt_reads_are_scoped_to_current_candidate_ids(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    upsert_settlement_attempt_state(path, state=_state())
    stale_case_ids = [f"terminal-{index}" for index in range(1_200)]
    with sqlite3.connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO lifecycle_settlement_attempt_state (
              source_id, account, case_id, case_scope_fingerprint,
              provider_input_scope_fingerprint,
              collector_contract_version, capability_fingerprint,
              classification, outcome_kind, reason_code, provider_code,
              error_class, attempt_count, no_progress_count,
              next_attempt_at_ms, last_attempt_at_ms,
              last_semantic_fingerprint, claim_id, claim_until_ms,
              updated_at_ms
            )
            SELECT
              source_id, account, ?, case_scope_fingerprint,
              provider_input_scope_fingerprint,
              collector_contract_version, capability_fingerprint,
              classification, 'blocked_static',
              'historical_terminal_case', provider_code,
              'missing_static', attempt_count, no_progress_count,
              next_attempt_at_ms, last_attempt_at_ms,
              last_semantic_fingerprint, claim_id, claim_until_ms,
              updated_at_ms
            FROM lifecycle_settlement_attempt_state
            WHERE source_id = 'lx' AND account = 'lx'
              AND case_id = 'case-1'
            """,
            [(case_id,) for case_id in stale_case_ids],
        )
        query_plan = conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT case_id
            FROM lifecycle_settlement_attempt_state
            WHERE source_id = ? AND account = ?
              AND case_id IN (?, ?)
            """,
            ("lx", "lx", "case-1", "missing-case"),
        ).fetchall()

    states = list_settlement_attempt_states(
        path,
        source_id="lx",
        account="lx",
        case_ids=("case-1", "missing-case"),
    )
    batched_states = list_settlement_attempt_states(
        path,
        source_id="lx",
        account="lx",
        case_ids=("case-1", *stale_case_ids[:450]),
    )
    summary = settlement_attempt_summary(
        path,
        source_id="lx",
        account="lx",
        case_ids=("case-1", "missing-case"),
        now_ms=1_000,
    )
    empty_summary = settlement_attempt_summary(
        path,
        source_id="lx",
        account="lx",
        case_ids=(),
        now_ms=1_000,
    )

    assert set(states) == {"case-1"}
    assert len(batched_states) == 451
    assert summary["provider_required_count"] == 1
    assert summary["blocked_count"] == 0
    assert summary["last_state_change"]["case_id"] == "case-1"
    assert empty_summary["provider_required_count"] == 0
    assert empty_summary["last_state_change"] is None
    assert any(
        "SEARCH lifecycle_settlement_attempt_state" in str(row[3])
        and "source_id=? AND account=? AND case_id=?" in str(row[3])
        for row in query_plan
    )


def test_claim_renewal_extends_only_the_current_owners_lease(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    upsert_settlement_attempt_state(path, state=_state())
    assert claim_settlement_attempt(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint="case-scope-1",
        claim_id="claim-1",
        now_ms=1_000,
        lease_ms=120_000,
    )
    claimed = get_settlement_attempt_state(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
    )
    assert claimed is not None

    assert renew_settlement_attempt_claim(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint="case-scope-1",
        claim_id="claim-1",
        now_ms=120_000,
        lease_ms=120_000,
    )
    renewed = get_settlement_attempt_state(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
    )
    assert renewed is not None
    assert renewed["claim_until_ms"] == 240_000
    assert renewed["updated_at_ms"] == claimed["updated_at_ms"]
    assert not renew_settlement_attempt_claim(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint="case-scope-1",
        claim_id="stale-owner",
        now_ms=121_001,
        lease_ms=120_000,
    )
    assert not claim_settlement_attempt(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint="case-scope-1",
        claim_id="competing-worker",
        now_ms=121_001,
        lease_ms=120_000,
    )
    assert claim_settlement_attempt(
        path,
        source_id="lx",
        account="lx",
        case_id="case-1",
        case_scope_fingerprint="case-scope-1",
        claim_id="competing-worker",
        now_ms=240_000,
        lease_ms=120_000,
    )


def test_provider_batch_lease_is_source_account_scoped_and_owner_checked(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"

    assert claim_settlement_provider_batch(
        path,
        source_id="lx",
        account="LX",
        claim_id="batch-1",
        now_ms=1_000,
        lease_ms=1,
    )
    assert not claim_settlement_provider_batch(
        path,
        source_id="lx",
        account="lx",
        claim_id="batch-2",
        now_ms=120_999,
        lease_ms=120_000,
    )
    assert renew_settlement_provider_batch_claim(
        path,
        source_id="lx",
        account="lx",
        claim_id="batch-1",
        now_ms=121_000,
        lease_ms=120_000,
    )
    assert not renew_settlement_provider_batch_claim(
        path,
        source_id="lx",
        account="lx",
        claim_id="stale-owner",
        now_ms=122_000,
        lease_ms=120_000,
    )
    with pytest.raises(
        SettlementAttemptClaimOwnershipLost,
        match="batch claim ownership changed",
    ):
        release_settlement_provider_batch_claim(
            path,
            source_id="lx",
            account="lx",
            claim_id="stale-owner",
        )
    assert not claim_settlement_provider_batch(
        path,
        source_id="lx",
        account="lx",
        claim_id="batch-2",
        now_ms=240_999,
        lease_ms=120_000,
    )
    assert claim_settlement_provider_batch(
        path,
        source_id="lx",
        account="lx",
        claim_id="batch-2",
        now_ms=241_000,
        lease_ms=120_000,
    )
    with pytest.raises(SettlementAttemptClaimOwnershipLost):
        release_settlement_provider_batch_claim(
            path,
            source_id="lx",
            account="lx",
            claim_id="batch-1",
        )
    release_settlement_provider_batch_claim(
        path,
        source_id="lx",
        account="lx",
        claim_id="batch-2",
    )
    assert claim_settlement_provider_batch(
        path,
        source_id="lx",
        account="lx",
        claim_id="batch-3",
        now_ms=241_001,
        lease_ms=120_000,
    )


def _contract_and_capability() -> tuple[
    SettlementCollectorContract,
    SettlementCapabilitySnapshot,
]:
    contract = SettlementCollectorContract(
        required_capability_keys=("synthetic",)
    )
    capability = SettlementCapabilitySnapshot(
        contract_version=contract.contract_version,
        gateway_adapter_version="adapter.v1",
        provider_sdk_version="sdk.v1",
        capability_fingerprint="capability-1",
        capabilities={"synthetic": "supported"},
    )
    return contract, capability


@pytest.mark.parametrize(
    ("error_class", "expected_kind"),
    [
        ("transient", "retryable_error"),
        ("rate_limit", "retryable_error"),
        ("auth_expired", "retryable_error"),
        ("need_2fa", "retryable_error"),
        ("timeout", "retryable_error"),
        ("provider_unavailable", "retryable_error"),
        ("malformed_response", "unknown_error"),
        ("unknown", "unknown_error"),
    ],
)
def test_typed_receipt_errors_map_without_text_inference(
    error_class: str,
    expected_kind: str,
) -> None:
    contract, capability = _contract_and_capability()
    outcome = classify_observation_outcome(
        {
            "complete": False,
            "source_receipts": {
                "history_deals": {
                    "status": "incomplete",
                    "error": "arbitrary text must not classify",
                    "error_class": error_class,
                    "provider_code": "",
                    "retry_after_ms": 123_000,
                }
            },
        },
        source_id="lx",
        account="lx",
        case_id="case-1",
        contract=contract,
        capability=capability,
    )

    assert outcome.kind == expected_kind
    assert outcome.retry_after_ms == 123_000


@pytest.mark.parametrize(
    "provider_code",
    [
        "TRANSIENT",
        "RATE_LIMIT",
        "AUTH_EXPIRED",
        "NEED_2FA",
        "TIMEOUT",
        "PROVIDER_UNAVAILABLE",
    ],
)
def test_typed_provider_exception_codes_remain_retryable(
    provider_code: str,
) -> None:
    contract, capability = _contract_and_capability()

    class ProviderError(RuntimeError):
        code = provider_code
        retry_after_ms = "invalid"

    outcome = classify_exception_outcome(
        ProviderError("typed provider failure"),
        source_id="lx",
        account="lx",
        case_id="case-1",
        contract=contract,
        capability=capability,
    )

    assert outcome.kind == "retryable_error"
    assert outcome.provider_code == provider_code
    assert outcome.retry_after_ms is None


def test_explicit_allowlisted_provider_code_is_the_only_account_block(
    monkeypatch,
) -> None:
    import src.application.trades.settlement_attempts as mod

    contract, capability = _contract_and_capability()
    monkeypatch.setattr(
        mod,
        "EXPLICIT_ACCOUNT_BLOCK_PROVIDER_CODES",
        frozenset({"OPERATION_UNSUPPORTED"}),
    )
    blocked = classify_observation_outcome(
        {
            "complete": False,
            "source_receipts": {
                "history_deals": {
                    "status": "incomplete",
                    "error_class": "unknown",
                    "provider_code": "OPERATION_UNSUPPORTED",
                }
            },
        },
        source_id="lx",
        account="lx",
        case_id="case-1",
        contract=contract,
        capability=capability,
    )
    not_allowlisted = classify_observation_outcome(
        {
            "complete": False,
            "source_receipts": {
                "history_deals": {
                    "status": "incomplete",
                    "error_class": "unknown",
                    "provider_code": "SOME_OTHER_CODE",
                }
            },
        },
        source_id="lx",
        account="lx",
        case_id="case-1",
        contract=contract,
        capability=capability,
    )

    assert blocked.kind == "blocked_account_explicit"
    assert not_allowlisted.kind == "unknown_error"


def test_unclassified_exception_remains_unknown_retry() -> None:
    contract, capability = _contract_and_capability()
    outcome = classify_exception_outcome(
        RuntimeError("permission words in text are not evidence"),
        source_id="lx",
        account="lx",
        case_id="case-1",
        contract=contract,
        capability=capability,
    )

    assert outcome.kind == "unknown_error"
    assert outcome.provider_code is None
