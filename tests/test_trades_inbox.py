from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from src.application.trades.auto_intake import (
    _cached_trade_inbox_summary,
)

from src.application.trades.inbox import (
    TradePayloadClaimLost,
    begin_trade_receipt_attempt,
    claim_trade_payload,
    claim_trade_payload_refresh_intent,
    enqueue_trade_payload,
    list_retryable_trade_payloads,
    list_trade_receipt_recovery_rows,
    list_unclaimed_trade_payload_refresh_intents,
    mark_trade_payload_handled,
    mark_trade_payload_retryable,
    read_trade_source_evidence,
    read_trade_payload,
    read_trade_payloads_for_reconciliation,
    record_trade_payload_refresh_intent,
    settle_trade_payload_result,
    settle_reconciled_trade_payload,
    trade_payload_evidence_ref,
    trade_inbox_revision,
    trade_inbox_summary,
)


def test_reconciliation_discovery_is_read_only_and_keeps_all_identities(tmp_path: Path) -> None:
    path = tmp_path / "inbox.sqlite3"
    assert read_trade_payloads_for_reconciliation(path, deal_ids=["same"]) == []
    assert not path.exists()
    for key in ("futu:lx:1001:same", "futu:sy:1002:same", None):
        enqueue_trade_payload(path, payload={"deal_id": "same"}, source="push", broker_deal_key=key)
    with sqlite3.connect(path) as conn:
        conn.create_function("trade_inbox_writer_version", 0, lambda: 2)
        conn.execute("UPDATE trade_inbox SET status='conflict' WHERE broker_deal_key='futu:sy:1002:same'")
    before = path.read_bytes()
    rows = read_trade_payloads_for_reconciliation(path, deal_ids=["same", "futu:lx:1001:same"])
    assert len(rows) == 3
    assert {row["status"] for row in rows} == {"pending", "conflict", "identity_needs_review"}
    assert path.read_bytes() == before
    assert len(read_trade_payloads_for_reconciliation(path, deal_ids=["futu:lx:1001:same"])) == 1

    unavailable = tmp_path / "missing-schema.sqlite3"
    with sqlite3.connect(unavailable):
        pass
    with pytest.raises(sqlite3.DatabaseError, match="schema unavailable"):
        read_trade_payloads_for_reconciliation(unavailable, deal_ids=["same"])


@pytest.mark.parametrize("changed", ["payload_version", "economic_payload_hash", "result_json", "receipt_json", "claim_id", "identity_status", "status"])
def test_reconciliation_rejects_changed_observation(tmp_path: Path, changed: str) -> None:
    path = tmp_path / "inbox.sqlite3"
    inbox_id = enqueue_trade_payload(path, payload={"deal_id": "one"}, source="push", broker_deal_key="futu:lx:1001:one")
    observed = read_trade_payloads_for_reconciliation(path, deal_ids=["one"])[0]
    values = {"payload_version": 99, "economic_payload_hash": "changed", "result_json": '{"status":"failed"}',
              "receipt_json": '{"status":"unknown"}', "claim_id": "another-worker", "identity_status": "identity_needs_review", "status": "conflict"}
    with sqlite3.connect(path) as conn:
        conn.create_function("trade_inbox_writer_version", 0, lambda: 2)
        conn.execute(f"UPDATE trade_inbox SET {changed}=? WHERE inbox_id=?", (values[changed], inbox_id))
    current = read_trade_payload(path, inbox_id=inbox_id, read_only=True)
    with pytest.raises(TradePayloadClaimLost):
        settle_reconciled_trade_payload(path, observed=observed, result={"status": "applied"})
    assert read_trade_payload(path, inbox_id=inbox_id, read_only=True) == current


@pytest.mark.parametrize("ineligible", ["claim", "conflict", "identity_needs_review"])
def test_reconciliation_rejects_observed_ineligible_row(tmp_path: Path, ineligible: str) -> None:
    path = tmp_path / "inbox.sqlite3"
    inbox_id = enqueue_trade_payload(path, payload={"deal_id": "one"}, source="push", broker_deal_key="futu:lx:1001:one")
    if ineligible == "claim":
        assert claim_trade_payload(path, inbox_id=inbox_id)
    else:
        with sqlite3.connect(path) as conn:
            conn.create_function("trade_inbox_writer_version", 0, lambda: 2)
            conn.execute("UPDATE trade_inbox SET status=? WHERE inbox_id=?", (ineligible, inbox_id))
    observed = read_trade_payloads_for_reconciliation(path, deal_ids=["one"])[0]
    with pytest.raises(TradePayloadClaimLost):
        settle_reconciled_trade_payload(path, observed=observed, result={"status": "applied"})
    assert read_trade_payloads_for_reconciliation(path, deal_ids=["one"])[0] == observed


@pytest.mark.parametrize("receipt_status", [None, "pending", "sent", "unknown"])
def test_reconciliation_preserves_history_and_suppresses_all_delivery(tmp_path: Path, receipt_status: str | None) -> None:
    path = tmp_path / "inbox.sqlite3"
    inbox_id = enqueue_trade_payload(path, payload={"deal_id": "one", "futu_account_id": "1001"}, source="push", broker_deal_key="futu:lx:1001:one")
    old_result = {"status": "unresolved", "reason": "waiting_settlement_evidence", "receipt_kind": "manual_required"}
    envelope = ({"schema_version": 2, "current_result_key": "manual_required", "receipts": {
        "manual_required": {"receipt_id": "old-receipt", "status": receipt_status, "attempt_count": 1,
                            "result": {"delivery_confirmed": receipt_status == "sent"}, "business_result": old_result},
    }} if receipt_status else None)
    record_trade_payload_refresh_intent(path, inbox_id=inbox_id, intent={"account": "lx", "request_id": "refresh-one"})
    with sqlite3.connect(path) as conn:
        conn.create_function("trade_inbox_writer_version", 0, lambda: 2)
        conn.execute("UPDATE trade_inbox SET result_json=?, receipt_json=?, receipt_recovery_allowed=1, attempt_count=1 WHERE inbox_id=?",
                     (json.dumps(old_result), json.dumps(envelope) if envelope else None, inbox_id))
    observed = read_trade_payloads_for_reconciliation(path, deal_ids=["one"])[0]
    result = {"status": "applied", "reason": "lifecycle_case_already_recorded", "applied_record_ids": ["lot-one"]}
    assert settle_reconciled_trade_payload(path, observed=observed, result=result)
    closed = read_trade_payload(path, inbox_id=inbox_id, read_only=True)
    assert closed["status"] == "handled"
    assert closed["receipt_envelope"] == envelope
    assert closed["result"]["diagnostics"]["previous_result"] == old_result
    assert closed["result"]["diagnostics"]["previous_receipt_envelope"] == envelope
    assert closed["portfolio_refresh_intent_json"] == observed["portfolio_refresh_intent_json"]
    assert closed["portfolio_refresh_attempted_at_ms"] is None
    closed_observation = read_trade_payloads_for_reconciliation(path, deal_ids=["one"])[0]
    assert not settle_reconciled_trade_payload(path, observed=closed_observation, result=result)
    assert read_trade_payload(path, inbox_id=inbox_id, read_only=True) == closed
    assert list_retryable_trade_payloads(path, retry_delay_sec=0) == []
    assert list_trade_receipt_recovery_rows(path, account_ids=["1001"]) == []
    assert list_unclaimed_trade_payload_refresh_intents(path, account_mapping={"1001": "lx"}) == []
    assert claim_trade_payload_refresh_intent(path, inbox_id=inbox_id) is None
    assert not begin_trade_receipt_attempt(path, inbox_id=inbox_id, route={"route": "test"}, message="old")["claimed"]
    assert read_trade_payload(path, inbox_id=inbox_id, read_only=True) == closed
    next_id = enqueue_trade_payload(path, payload={"deal_id": "two"}, source="push", broker_deal_key="futu:lx:1001:two")
    assert claim_trade_payload(path, inbox_id=next_id)


def test_trade_inbox_claims_portfolio_refresh_intent_once(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    inbox_id = enqueue_trade_payload(
        path,
        payload={"deal_id": "stock-1"},
        source="push",
        broker_deal_key="futu:lx:REAL_1:stock-1",
    )
    intent = {"account": "lx", "request_id": "stock-refresh:abc"}

    record_trade_payload_refresh_intent(
        path,
        inbox_id=inbox_id,
        intent=intent,
    )
    assert claim_trade_payload_refresh_intent(
        path,
        inbox_id=inbox_id,
    ) == intent
    record_trade_payload_refresh_intent(
        path,
        inbox_id=inbox_id,
        intent=intent,
    )
    assert claim_trade_payload_refresh_intent(path, inbox_id=inbox_id) is None


def test_trade_inbox_is_idempotent_and_retries_callback_exception(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    payload = {"deal_id": "deal-1", "code": "US.NVDA260821P100000"}

    first_id = enqueue_trade_payload(
        path,
        payload=payload,
        source="push",
        broker_deal_key="futu:lx:REAL_1:deal-1",
    )
    second_id = enqueue_trade_payload(
        path,
        payload=payload,
        source="push",
        broker_deal_key="futu:lx:REAL_1:deal-1",
    )
    assert first_id == second_id
    assert trade_inbox_summary(path)["pending_count"] == 1

    claim = claim_trade_payload(path, inbox_id=first_id)
    mark_trade_payload_retryable(
        path,
        inbox_id=first_id,
        error="RuntimeError: callback failed",
        result={"status": "failed", "reason": "sqlite_busy", "diagnostics": {"retryable": True}},
        claim=claim,
    )
    retry = list_retryable_trade_payloads(path, retry_delay_sec=0)
    assert len(retry) == 1
    assert retry[0]["attempt_count"] == 1
    assert retry[0]["payload"] == payload

    mark_trade_payload_handled(
        path,
        inbox_id=first_id,
        result={"status": "applied", "reason": "applied_open"},
    )
    third_id = enqueue_trade_payload(
        path,
        payload=payload,
        source="backfill",
        broker_deal_key="futu:lx:REAL_1:deal-1",
    )
    assert third_id == first_id
    mark_trade_payload_handled(
        path,
        inbox_id=third_id,
        result={"status": "skipped", "reason": "duplicate"},
    )
    assert list_retryable_trade_payloads(path, retry_delay_sec=0) == []
    summary = trade_inbox_summary(path)
    assert summary["pending_count"] == 0
    assert summary["handled_count"] == 1
    assert summary["max_attempt_count"] == 1


def test_trade_inbox_migrates_old_evidence_without_guessing_adapter_version(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    payload = {"deal_id": "legacy-deal"}
    inbox_id = enqueue_trade_payload(
        path,
        payload=payload,
        source="push",
        broker_deal_key="futu:lx:REAL_1:legacy-deal",
        adapter_version="om.trade-intake.push.v1",
    )
    with sqlite3.connect(path) as conn:
        conn.create_function("trade_inbox_writer_version", 0, lambda: 2)
        conn.execute(
            "UPDATE trade_inbox_evidence SET evidence_id = NULL, evidence_json = NULL"
        )

    enqueue_trade_payload(
        path,
        payload=payload,
        source="push",
        broker_deal_key="futu:lx:REAL_1:legacy-deal",
        adapter_version="om.trade-intake.push.v1",
    )
    evidence = read_trade_source_evidence(
        path,
        evidence_ref=trade_payload_evidence_ref(inbox_id),
        read_only=True,
    )
    assert len(evidence) == 1
    assert evidence[0]["adapter_version"] == "legacy/unversioned"
    with sqlite3.connect(path) as conn:
        plan = conn.execute(
            """EXPLAIN QUERY PLAN
            SELECT e.rowid
            FROM trade_inbox_evidence e
            LEFT JOIN trade_inbox i ON i.inbox_id = e.inbox_id
            WHERE e.evidence_id IS NULL OR e.evidence_json IS NULL"""
        ).fetchall()
    assert any(
        "idx_trade_inbox_evidence_missing_envelope" in str(row[-1])
        for row in plan
    )


def test_trade_inbox_handles_lifecycle_pending_after_evidence_acceptance(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    inbox_id = enqueue_trade_payload(
        path,
        payload={"deal_id": "deal-waiting"},
        source="push",
        broker_deal_key="futu:lx:REAL_1:deal-waiting",
    )

    settle_trade_payload_result(
        path,
        inbox_id=inbox_id,
        result={
            "status": "unresolved",
            "reason": "waiting_settlement_evidence",
            "diagnostics": {
                "retryable": True,
                "broker_evidence_accepted": True,
            },
        },
    )

    assert list_retryable_trade_payloads(path, retry_delay_sec=0) == []
    summary = trade_inbox_summary(path)
    assert summary["handled_count"] == 1
    assert summary["pending_count"] == 0


def test_trade_inbox_quarantines_missing_canonical_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    inbox_id = enqueue_trade_payload(
        path,
        payload={"deal_id": "raw-only"},
        source="push",
    )

    summary = trade_inbox_summary(path)
    assert summary["pending_count"] == 0
    assert summary["identity_needs_review_count"] == 1
    assert summary["identity_attention"] == [{
        "inbox_id": inbox_id, "deal_id": "raw-only", "source": "push",
        "received_at_ms": summary["identity_attention"][0]["received_at_ms"],
        "reason": "canonical_broker_identity_missing",
        "retryable": False, "next_action": "verify_broker_identity_before_replay",
    }]
    assert summary["identity_attention"][0]["received_at_ms"] > 0
    assert claim_trade_payload(path, inbox_id=inbox_id) is None
    assert list_retryable_trade_payloads(path, retry_delay_sec=0) == []


def test_trade_inbox_scopes_same_deal_id_by_broker_account(tmp_path: Path) -> None:
    path = tmp_path / "inbox.sqlite3"
    lx_id = enqueue_trade_payload(
        path,
        payload={"deal_id": "same-id"},
        source="push",
        broker_deal_key="futu:lx:REAL_1:same-id",
    )
    sy_id = enqueue_trade_payload(
        path,
        payload={"deal_id": "same-id"},
        source="push",
        broker_deal_key="futu:sy:REAL_2:same-id",
    )

    assert lx_id != sy_id
    assert trade_inbox_summary(path)["pending_count"] == 2


def test_trade_inbox_quarantines_same_key_economic_drift(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    source_key = "futu:lx:REAL_1:stock-1"
    first = {
        "deal_id": "stock-1",
        "code": "US.NVDA",
        "trd_side": "BUY",
        "qty": 100,
        "price": "100",
        "trade_time_ms": 1_800_000_000_000,
    }
    enqueue_trade_payload(
        path,
        payload=first,
        source="push",
        broker_deal_key=source_key,
    )
    enqueue_trade_payload(
        path,
        payload={**first, "price": "100.01"},
        source="poll",
        broker_deal_key=source_key,
    )

    summary = trade_inbox_summary(path)
    assert summary["pending_count"] == 0
    assert summary["conflict_count"] == 1
    assert list_retryable_trade_payloads(
        path,
        retry_delay_sec=0,
    ) == []


def test_trade_inbox_summary_cache_is_revision_gated(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.auto_intake as auto_intake

    path = tmp_path / "inbox.sqlite3"
    first_id = enqueue_trade_payload(
        path,
        payload={"deal_id": "seed"},
        source="push",
        broker_deal_key="futu:lx:REAL_1:seed",
    )
    mark_trade_payload_handled(
        path,
        inbox_id=first_id,
        result={"status": "applied", "reason": "seed"},
    )
    with sqlite3.connect(path) as conn:
        conn.create_function("trade_inbox_writer_version", 0, lambda: 2)
        conn.executemany(
            """
            INSERT INTO trade_inbox (
              inbox_id, source, deal_id, broker_deal_key,
              identity_status, payload_json, economic_payload_hash,
              status, attempt_count, received_at_ms, updated_at_ms,
              last_error, result_status, result_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    f"historical-{index}",
                    "backfill",
                    f"historical-{index}",
                    f"futu:lx:REAL_1:historical-{index}",
                    "bound",
                    json.dumps({"deal_id": f"historical-{index}"}),
                    f"hash-{index}",
                    "handled",
                    0,
                    index + 1,
                    index + 1,
                    None,
                    "applied",
                    "historical",
                )
                for index in range(1_200)
            ],
        )

    summary_reads = 0
    original_summary = auto_intake.trade_inbox_summary

    def counted_summary(summary_path: Path) -> dict:
        nonlocal summary_reads
        summary_reads += 1
        return original_summary(summary_path)

    monkeypatch.setattr(
        auto_intake,
        "trade_inbox_summary",
        counted_summary,
    )
    cache: dict = {}
    for _ in range(10):
        summary = _cached_trade_inbox_summary(path, cache=cache)
    assert summary_reads == 1
    assert summary["handled_count"] == 1_201

    revision_before = trade_inbox_revision(path)
    pending_id = enqueue_trade_payload(
        path,
        payload={"deal_id": "new"},
        source="push",
        broker_deal_key="futu:lx:REAL_1:new",
    )
    assert trade_inbox_revision(path) == revision_before + 1
    summary = _cached_trade_inbox_summary(path, cache=cache)
    assert summary_reads == 2
    assert summary["pending_count"] == 1

    duplicate_revision = trade_inbox_revision(path)
    assert enqueue_trade_payload(
        path,
        payload={"deal_id": "new"},
        source="push",
        broker_deal_key="futu:lx:REAL_1:new",
    ) == pending_id
    assert trade_inbox_revision(path) == duplicate_revision
    _cached_trade_inbox_summary(path, cache=cache)
    assert summary_reads == 2

    mark_trade_payload_handled(
        path,
        inbox_id=pending_id,
        result={"status": "applied", "reason": "new"},
    )
    _cached_trade_inbox_summary(path, cache=cache)
    assert summary_reads == 3
    with sqlite3.connect(path) as conn:
        conn.create_function("trade_inbox_writer_version", 0, lambda: 2)
        conn.execute(
            "DELETE FROM trade_inbox WHERE inbox_id = ?",
            (first_id,),
        )
    final_summary = _cached_trade_inbox_summary(path, cache=cache)
    assert summary_reads == 4
    assert final_summary["handled_count"] == 1_201

    cache.clear()
    stable_revision = trade_inbox_revision(path)
    racing_revisions = iter(
        (stable_revision, stable_revision + 1)
    )
    monkeypatch.setattr(
        auto_intake,
        "trade_inbox_revision",
        lambda _path: next(racing_revisions),
    )
    _cached_trade_inbox_summary(path, cache=cache)
    assert summary_reads == 5
    assert cache == {}

    monkeypatch.setattr(
        auto_intake,
        "trade_inbox_revision",
        lambda _path: stable_revision + 1,
    )
    _cached_trade_inbox_summary(path, cache=cache)
    _cached_trade_inbox_summary(path, cache=cache)
    assert summary_reads == 6
