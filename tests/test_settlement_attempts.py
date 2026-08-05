from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from src.application.trades.inbox import (
    SettlementAttemptClaimOwnershipLost,
    claim_settlement_attempt,
    claim_settlement_provider_batch,
    complete_settlement_attempt,
    get_settlement_attempt_state,
    list_settlement_attempt_states,
    renew_settlement_attempt_claim,
    renew_settlement_provider_batch_claim,
    release_settlement_provider_batch_claim,
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
