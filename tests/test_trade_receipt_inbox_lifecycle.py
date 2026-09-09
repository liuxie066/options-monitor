from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from src.application.trades import inbox


_ROUTE = {"provider": "offline", "channel": "test", "target": "fixture"}
_FAILED = {"status": "failed", "reason": "sqlite_busy", "diagnostics": {"retryable": True},
           "_receipt_payload": {"account": "lx", "symbol": "NVDA"}}
_RECORDED = {"status": "applied", "reason": "applied_open", "_receipt_payload": {"account": "lx"}}


def _new(path, *, account="123"):
    key = inbox.enqueue_trade_payload(path, payload={"deal_id": "one", "futu_account_id": account},
                                     source="push", broker_deal_key=f"futu:lx:{account}:one")
    # This fixture models a live reception already proven to precede its ledger effect.
    with inbox._connect(path) as conn:
        conn.execute("UPDATE trade_inbox SET receipt_recovery_allowed = 1 WHERE inbox_id = ?", (key,))
    return key


def _save(path, key, result):
    claim = inbox.claim_trade_payload(path, inbox_id=key)
    assert claim is not None
    enriched = inbox.save_trade_payload_result(path, claim=claim, result=result)
    inbox.settle_trade_payload_result(path, inbox_id=key, claim=claim, result=enriched)
    return enriched


def _attempt(path, key, *, result_key=None, route=_ROUTE, message="frozen"):
    return inbox.begin_trade_receipt_attempt(path, inbox_id=key, route=route, message=message,
                                             result_key=result_key)


def _advance(monkeypatch, clock, seconds=60):
    clock[0] += seconds
    monkeypatch.setattr(inbox.time, "time", lambda: clock[0])


def test_claim_budget_due_and_settle_use_one_authoritative_counter(tmp_path, monkeypatch):
    clock = [1000.0]
    _advance(monkeypatch, clock, 0)
    path = tmp_path / "inbox.db"
    key = _new(path)
    for count in range(1, 21):
        enriched = _save(path, key, _FAILED)
        assert enriched["retry_policy"] == {"attempt_count": count, "max_attempts": 20,
                                            "remaining": 20 - count, "retryable": count < 20,
                                            "retry_delay_sec": 60}
        assert inbox.claim_trade_payload(path, inbox_id=key) is None
        _advance(monkeypatch, clock)
    row = inbox.read_trade_payload(path, inbox_id=key)
    assert row["attempt_count"] == 20
    assert row["receipt"]["receipt_kind"] == "manual_required"
    assert set(row["receipt_envelope"]["receipts"]) == {"pending_retry", "manual_required"}
    assert row["receipt_envelope"]["receipts"]["pending_retry"]["superseded_by"] == "manual_required"
    assert inbox.claim_trade_payload(path, inbox_id=key) is None


def test_last_claim_crash_has_readback_path_but_no_economic_claim(tmp_path, monkeypatch):
    clock = [1000.0]
    _advance(monkeypatch, clock, 0)
    path = tmp_path / "inbox.db"
    key = _new(path)
    with inbox._connect(path) as conn:
        conn.execute("UPDATE trade_inbox SET attempt_count = 19, updated_at_ms = 0 WHERE inbox_id = ?", (key,))
    claim = inbox.claim_trade_payload(path, inbox_id=key)
    assert claim["attempt_count"] == 20
    assert inbox.list_trade_receipt_recovery_rows(path, account_ids=["123"]) == []
    with pytest.raises(inbox.TradePayloadClaimLost):
        inbox.prepare_trade_receipt_result(path, inbox_id=key, result=_RECORDED,
                                            expected_payload_version=claim["payload_version"])
    _advance(monkeypatch, clock, 121)
    assert inbox.claim_trade_payload(path, inbox_id=key) is None
    rows = inbox.list_trade_receipt_recovery_rows(path, account_ids=["123"])
    assert [row["inbox_id"] for row in rows] == [key]
    verification = inbox.prepare_trade_receipt_result(path, inbox_id=key,
        result={"status": "unresolved", "reason": "ledger_unavailable", "receipt_kind": "verification_pending"},
        expected_payload_version=claim["payload_version"])
    assert verification["retry_policy"]["attempt_count"] == 20
    attempt = _attempt(path, key)
    inbox.finish_trade_receipt_attempt(path, inbox_id=key, attempt_id=attempt["attempt_id"],
                                       result={"delivery_confirmed": True})
    _advance(monkeypatch, clock)
    assert inbox.list_trade_receipt_recovery_rows(path, account_ids=["123"])
    recovered = inbox.prepare_trade_receipt_result(path, inbox_id=key, result=_RECORDED,
                                                     expected_result=verification)
    assert recovered["receipt_kind"] == "recorded"
    assert _attempt(path, key)["claimed"] is True
    assert inbox.read_trade_payload(path, inbox_id=key)["attempt_count"] == 20


@pytest.mark.parametrize("finished", [None, {"delivery_confirmed": True}, {"explicit_pre_acceptance_failure": True}])
def test_superseded_failure_cannot_send_after_recorded_but_late_callback_retained(tmp_path, monkeypatch, finished):
    clock = [1000.0]
    _advance(monkeypatch, clock, 0)
    path = tmp_path / "inbox.db"
    key = _new(path)
    failed = _save(path, key, _FAILED)
    prior = _attempt(path, key, result_key="pending_retry")
    if finished:
        inbox.finish_trade_receipt_attempt(path, inbox_id=key, attempt_id=prior["attempt_id"], result=finished)
    recovered = inbox.prepare_trade_receipt_result(path, inbox_id=key, result=_RECORDED, expected_result=failed)
    success = _attempt(path, key, result_key="recorded", message="recovered")
    assert success["claimed"] is True
    assert success["receipt_id"].endswith(":recorded")
    with pytest.raises(inbox.TradePayloadClaimLost):
        _attempt(path, key, result_key="pending_retry")
    if not finished:
        inbox.finish_trade_receipt_attempt(path, inbox_id=key, attempt_id=prior["attempt_id"],
                                          result={"explicit_pre_acceptance_failure": True})
    row = inbox.read_trade_payload(path, inbox_id=key)
    assert row["result"] == recovered
    assert row["receipt"]["attempt_id"] == success["attempt_id"]
    assert row["receipt_envelope"]["receipts"]["pending_retry"]["stop_reason"] == "result_superseded"
    assert _attempt(path, key)["claimed"] is False


def test_unsent_failure_superseded_and_original_success_survives_callback_exception(tmp_path):
    path = tmp_path / "inbox.db"
    key = _new(path)
    failed = _save(path, key, _FAILED)
    inbox.prepare_trade_receipt_result(path, inbox_id=key, result=_RECORDED, expected_result=failed)
    with pytest.raises(inbox.TradePayloadClaimLost):
        _attempt(path, key, result_key="pending_retry")
    assert inbox.read_trade_payload(path, inbox_id=key)["receipt_envelope"]["receipts"]["pending_retry"]["attempt_count"] == 0
    second = _new(tmp_path / "other.db")
    claim = inbox.claim_trade_payload(tmp_path / "other.db", inbox_id=second)
    stored = inbox.save_trade_payload_result(tmp_path / "other.db", claim=claim, result=_RECORDED)
    inbox.mark_trade_payload_retryable(tmp_path / "other.db", inbox_id=second, claim=claim, error="callback failed")
    row = inbox.read_trade_payload(tmp_path / "other.db", inbox_id=second)
    assert row["status"] == "handled"
    assert row["result"] == stored
    assert row["attempt_count"] == 1


def test_notification_rejection_budget_is_independent_and_route_message_freeze(tmp_path, monkeypatch):
    clock = [1000.0]
    _advance(monkeypatch, clock, 0)
    path = tmp_path / "inbox.db"
    key = _new(path)
    _save(path, key, _RECORDED)
    for count in range(1, 21):
        attempt = _attempt(path, key, message="original" if count == 1 else "changed")
        assert attempt["claimed"] is True
        assert attempt["attempt_count"] == count
        assert attempt["message"] == "original"
        inbox.finish_trade_receipt_attempt(path, inbox_id=key, attempt_id=attempt["attempt_id"],
                                          result={"explicit_pre_acceptance_failure": True})
        assert _attempt(path, key)["claimed"] is False
        _advance(monkeypatch, clock)
    row = inbox.read_trade_payload(path, inbox_id=key)
    assert row["attempt_count"] == 1
    assert row["receipt"]["stop_reason"] == "notification_attempts_exhausted"
    assert len(row["receipt"]["attempts"]) == 19
    assert _attempt(path, key)["claimed"] is False


def test_no_route_does_not_consume_and_changed_route_stops(tmp_path, monkeypatch):
    clock = [1000.0]
    _advance(monkeypatch, clock, 0)
    path = tmp_path / "inbox.db"
    key = _new(path)
    _save(path, key, _RECORDED)
    assert _attempt(path, key, route={})["claimed"] is False
    assert inbox.read_trade_payload(path, inbox_id=key)["receipt"]["attempt_count"] == 0
    sent = _attempt(path, key)
    inbox.finish_trade_receipt_attempt(path, inbox_id=key, attempt_id=sent["attempt_id"],
                                      result={"explicit_pre_acceptance_failure": True})
    _advance(monkeypatch, clock)
    changed = _attempt(path, key, route={**_ROUTE, "target": "other"})
    assert changed["claimed"] is False
    assert changed["stop_reason"] == "route_changed_requires_review"
    assert changed["attempt_count"] == 1
    assert _attempt(path, key)["claimed"] is False


def test_parallel_attempt_and_result_cas(tmp_path):
    path = tmp_path / "inbox.db"
    key = _new(path)
    failed = _save(path, key, _FAILED)
    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(lambda _: _attempt(path, key), range(2)))
    assert sum(attempt["claimed"] for attempt in attempts) == 1
    inbox.prepare_trade_receipt_result(path, inbox_id=key, result=_RECORDED, expected_result=failed)
    with pytest.raises(inbox.TradePayloadClaimLost):
        inbox.prepare_trade_receipt_result(path, inbox_id=key, result=_FAILED, expected_result=failed)


@pytest.mark.parametrize("legacy_status", ["sent", "unknown", "failed"])
def test_legacy_failed_business_allows_independent_success_only_with_proof(tmp_path, legacy_status):
    path = tmp_path / "inbox.db"
    key = _new(path)
    legacy = {"status": legacy_status, "receipt_id": f"trade-receipt:{key}", "attempt_id": "legacy"}
    with inbox._connect(path) as conn:
        conn.execute("UPDATE trade_inbox SET receipt_json = ?, result_json = ? WHERE inbox_id = ?",
                     (json.dumps(legacy), json.dumps(_FAILED), key))
    assert inbox.read_trade_payload(path, inbox_id=key)["receipt"] == legacy
    inbox.prepare_trade_receipt_result(path, inbox_id=key, result=_RECORDED, expected_result=_FAILED)
    row = inbox.read_trade_payload(path, inbox_id=key)
    assert row["receipt_envelope"]["receipts"]["pending_retry"]["status"] == legacy_status
    assert _attempt(path, key)["claimed"] is True


@pytest.mark.parametrize("legacy", [None, {"status": "sent"}, {"status": "unknown"}])
def test_legacy_historical_success_not_recovered_on_upgrade(tmp_path, legacy):
    path = tmp_path / "inbox.db"
    key = _new(path)
    with inbox._connect(path) as conn:
        conn.execute("UPDATE trade_inbox SET receipt_json = ?, result_json = ?, status = 'handled', receipt_recovery_allowed = 0 WHERE inbox_id = ?",
                     (json.dumps(legacy) if legacy else None, json.dumps(_RECORDED), key))
    result = inbox.prepare_trade_receipt_result(path, inbox_id=key, result=_RECORDED, expected_result=_RECORDED)
    assert result["receipt_suppression_reason"] == "legacy_receipt_history_unproven"
    assert _attempt(path, key)["claimed"] is False
    assert inbox.list_trade_receipt_recovery_rows(path, account_ids=["123"]) == []


def test_old_open_connection_and_downgrade_cannot_write_after_atomic_guard_migration(tmp_path):
    path = tmp_path / "inbox.db"
    key = _new(path)
    old = sqlite3.connect(path)
    old.create_function("trade_inbox_writer_version", 0, lambda: 1)
    try:
        # Restore the prior guard to model a database opened before this migration.
        with inbox._connect(path) as conn:
            guards = conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name LIKE '%writer_guard'").fetchall()
            for name, sql in guards:
                conn.execute(f"DROP TRIGGER {name}")
                conn.execute(sql.replace("trade_inbox_writer_version() != 2", "trade_inbox_writer_version() != 1"))
        old.execute("UPDATE trade_inbox SET last_error = 'old' WHERE inbox_id = ?", (key,))
        old.commit()
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(lambda _: inbox.read_trade_payload(path, inbox_id=key), range(2)))
        assert len(rows) == 2
        with pytest.raises(sqlite3.IntegrityError, match="compatible writer"):
            old.execute("UPDATE trade_inbox SET receipt_json = '{}' WHERE inbox_id = ?", (key,))
        old.rollback()
        with sqlite3.connect(path) as downgraded:
            downgraded.create_function("trade_inbox_writer_version", 0, lambda: 1)
            with pytest.raises(sqlite3.IntegrityError, match="compatible writer"):
                downgraded.execute("DELETE FROM trade_inbox WHERE inbox_id = ?", (key,))
    finally:
        old.close()


def test_unknown_schema_and_source_account_scope_fail_closed(tmp_path):
    path = tmp_path / "inbox.db"
    key = _new(path)
    _save(path, key, _RECORDED)
    assert inbox.list_trade_receipt_recovery_rows(path, account_ids=["456"]) == []
    assert inbox.list_trade_receipt_recovery_rows(path, account_ids=["123"])[0]["inbox_id"] == key
    with inbox._connect(path) as conn:
        conn.execute("UPDATE trade_inbox SET receipt_json = ? WHERE inbox_id = ?",
                     ('{"schema_version":3,"receipts":{},"current_result_key":"recorded"}', key))
    with pytest.raises(ValueError, match="unsupported"):
        _attempt(path, key)


def test_lifecycle_handoff_suppresses_ordinary_and_old_pending_failure(tmp_path):
    path = tmp_path / "inbox.db"
    key = _new(path)
    failed = _save(path, key, _FAILED)
    result = {**_RECORDED, "diagnostics": {"notification_authority": "lifecycle_outbox"}}
    inbox.prepare_trade_receipt_result(path, inbox_id=key, result=result, expected_result=failed)
    assert _attempt(path, key)["claimed"] is False
    assert inbox.read_trade_payload(path, inbox_id=key)["receipt"]["stop_reason"] == "lifecycle_outbox_handoff"
    other = tmp_path / "other.db"
    other_key = _new(other)
    _save(other, other_key, result)
    assert inbox.read_trade_payload(other, inbox_id=other_key)["receipt"] is None


def test_recorded_verification_requires_explicit_unknown_economics_and_keeps_send_evidence(tmp_path):
    path = tmp_path / "inbox.db"
    key = _new(path)
    recorded = _save(path, key, _RECORDED)
    attempt = _attempt(path, key)
    with pytest.raises(inbox.TradePayloadClaimLost):
        inbox.prepare_trade_receipt_result(path, inbox_id=key, result=_FAILED, expected_result=recorded)
    verifying = inbox.prepare_trade_receipt_result(path, inbox_id=key,
        result={"status": "unresolved", "receipt_kind": "verification_pending",
                "diagnostics": {"verification_pending": True}}, expected_result=recorded)
    inbox.prepare_trade_receipt_result(path, inbox_id=key, result=_RECORDED, expected_result=verifying)
    current = _attempt(path, key)
    assert current["claimed"] is False
    assert current["attempt_id"] == attempt["attempt_id"]


def test_resume_resets_economic_budget_without_reopening_unknown_delivery(tmp_path):
    path = tmp_path / "inbox.db"
    key = _new(path)
    with inbox._connect(path) as conn:
        conn.execute("UPDATE trade_inbox SET attempt_count = 19, updated_at_ms = 0 WHERE inbox_id = ?", (key,))
    exhausted = _save(path, key, _FAILED)
    assert exhausted["receipt_kind"] == "manual_required"
    attempt = _attempt(path, key)
    assert inbox.resume_trade_payload(path, inbox_id=key, operator="operator") is True
    assert _attempt(path, key)["claimed"] is False
    assert inbox.claim_trade_payload(path, inbox_id=key)["attempt_count"] == 1
    summary = inbox.trade_inbox_summary(path)
    assert summary["receipt_status_counts"]["unknown"] == 1
    assert summary["receipt_attention"][0]["last_attempt_at_ms"] == attempt["attempted_at_ms"]


@pytest.mark.parametrize("status", ["confirmed", "unknown"])
def test_prior_compensation_evidence_suppresses_new_recorded_automatic_send(tmp_path, status):
    path = tmp_path / "inbox.db"
    key = _new(path)
    _save(path, key, {**_RECORDED, "_receipt_legacy_evidence": {
        "blocked": True, "status": status, "records": ["compensation-1"], "result_key": "recorded"}})
    current = _attempt(path, key)
    assert current["claimed"] is False
    assert current["status"] == ("sent" if status == "confirmed" else "unknown")
    assert current["stop_reason"] == "legacy_compensation_owned"
    assert current["attempt_count"] == 0


def test_enrichment_timestamp_does_not_postpone_already_due_crash_recovery(tmp_path, monkeypatch):
    clock = [1000.0]
    _advance(monkeypatch, clock, 0)
    path = tmp_path / "inbox.db"
    key = _new(path)
    claim = inbox.claim_trade_payload(path, inbox_id=key)
    _advance(monkeypatch, clock, 121)
    # Enqueue's metadata update must not change the deadline established by the economic claim.
    with inbox._connect(path) as conn:
        conn.execute("UPDATE trade_inbox SET updated_at_ms = ?, payload_version = payload_version + 1, claim_id = NULL, claim_until_ms = NULL WHERE inbox_id = ?",
                     (int(clock[0] * 1000), key))
    recovered = inbox.claim_trade_payload(path, inbox_id=key)
    assert recovered["attempt_count"] == 2
    assert recovered["payload_version"] == claim["payload_version"] + 1


def test_late_callback_for_archived_attempt_does_not_change_current_attempt(tmp_path, monkeypatch):
    clock = [1000.0]
    _advance(monkeypatch, clock, 0)
    path = tmp_path / "inbox.db"
    key = _new(path)
    _save(path, key, _RECORDED)
    first = _attempt(path, key)
    inbox.finish_trade_receipt_attempt(path, inbox_id=key, attempt_id=first["attempt_id"],
                                      result={"explicit_pre_acceptance_failure": True})
    _advance(monkeypatch, clock)
    second = _attempt(path, key)
    inbox.finish_trade_receipt_attempt(path, inbox_id=key, attempt_id=first["attempt_id"],
                                      result={"delivery_confirmed": True})
    current = inbox.read_trade_payload(path, inbox_id=key)["receipt"]
    assert current["attempt_id"] == second["attempt_id"]
    assert current["status"] == "unknown"
    assert current["attempts"][0]["status"] == "failed"


def test_explicit_empty_result_observation_is_compared_not_treated_as_omitted(tmp_path):
    path = tmp_path / "inbox.db"
    key = _new(path)
    observed = inbox.read_trade_payload(path, inbox_id=key)
    assert observed["result"] is None
    _save(path, key, _RECORDED)
    with pytest.raises(inbox.TradePayloadClaimLost):
        inbox.prepare_trade_receipt_result(path, inbox_id=key,
            result={"status": "unresolved", "receipt_kind": "verification_pending",
                    "diagnostics": {"verification_pending": True}},
            expected_payload_version=observed["payload_version"], expected_result=observed["result"])


def test_readback_of_due_retry_cannot_postpone_economic_claim(tmp_path, monkeypatch):
    clock = [1000.0]
    _advance(monkeypatch, clock, 0)
    path = tmp_path / "inbox.db"
    key = _new(path)
    failed = _save(path, key, _FAILED)
    deadline = inbox.read_trade_payload(path, inbox_id=key)["next_attempt_at_ms"]
    _advance(monkeypatch, clock, 61)
    inbox.prepare_trade_receipt_result(path, inbox_id=key, result=failed, expected_result=failed)
    assert inbox.read_trade_payload(path, inbox_id=key)["next_attempt_at_ms"] == deadline
    assert inbox.claim_trade_payload(path, inbox_id=key)["attempt_count"] == 2
