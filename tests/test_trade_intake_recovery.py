from __future__ import annotations

import multiprocessing
import json
import os
import sqlite3
import threading
import time
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades.auto_intake import _process_payload
from src.application.trades.deal_identity import broker_deal_key_from_payload
from src.application.trades.inbox import (
    TradePayloadClaimLost,
    claim_trade_payload,
    claim_trade_payload_refresh_intent,
    enqueue_trade_payload,
    list_retryable_trade_payloads,
    mark_trade_payload_retryable,
    mark_trade_payload_review,
    read_trade_payload,
    resume_trade_payload,
    save_trade_payload_result,
    trade_payload_commit_scope,
)
from src.application.trades.inbox_authority import resolve_execution_inbox_path


def _execution(*, physical: str = "123", deal_id: str = "fill-1", price: str = "2.50") -> dict:
    return {
        "schema_version": "trade_execution.v1",
        "broker_account_ref": {
            "broker_id": "futu", "external_account_id": physical, "environment": "REAL",
            "broker_account_id": f"futu:REAL:{physical}", "account_label": "lx",
        },
        "instrument_ref": {
            "asset_type": "option", "market": "US", "symbol": "NVDA", "currency": "USD",
            "option_type": "put", "strike": "100", "expiration_ymd": "2026-09-18", "multiplier": "100",
        },
        "external_id_namespace": "futu.deal", "external_execution_id": deal_id,
        "external_order_namespace": "futu.order", "external_order_id": f"order-{deal_id}",
        "side": "sell", "position_effect": "open", "quantity": "1", "price": price,
        "currency": "USD", "occurred_at_utc": "2026-09-07T02:30:00Z",
    }


def _process(repo, root: Path, entry: str, payload: dict, **kwargs):
    return _process_payload(
        payload, repo=repo, state_path=root / entry / "state.json",
        audit_path=root / entry / "audit.jsonl", inbox_path=root / entry / "inbox.sqlite3",
        account_mapping={"123": "lx"}, futu_account_ids=["123"], apply_changes=True,
        host="127.0.0.1", port=11111, allow_external_lookup=False, **kwargs,
    )


def _enqueue_legacy_source_conflict(monkeypatch, inbox, *, payload, key, repo):
    from src.application.trades import inbox as inbox_module

    original = inbox_module._inbox_execution_content
    # Reproduce the baseline public reception policy: source identity errors did
    # not participate in conflict classification (economic hashes omit errors).
    def legacy_content(*args, **kwargs):
        content = original(*args, **kwargs)
        return {**content, "errors": [error for error in content.get("errors", ())
                                     if error != "invalid:source_execution_identity"]}
    with monkeypatch.context() as patch:
        patch.setattr(inbox_module, "_inbox_execution_content", legacy_content)
        return enqueue_trade_payload(inbox, payload=payload, source="file", broker_deal_key=key, repo=repo)


def _recorded_source_pending(repo, root):
    from src.application.trades.state import load_trade_intake_state, write_trade_intake_state

    payload = _execution()
    result = _process(repo, root, "push", payload)
    assert result["status"] == "applied"
    inbox = resolve_execution_inbox_path(repo, root / "inbox.sqlite3")
    path = root / "push" / "state.json"
    state = load_trade_intake_state(path)
    key, previous = state["processed_deal_ids"].popitem()
    state["unresolved_deal_ids"][key] = {**previous, "status": "unresolved", "reason": "verification_pending"}
    write_trade_intake_state(path, state)
    source = {"account": "lx", "account_mapping": {"123": "lx"}, "state_path": path, "inbox_path": inbox}
    return payload, result["inbox_id"], key, source


@pytest.mark.parametrize("missing", ["rows", "table"])
def test_receipt_recovery_requires_historical_evidence(tmp_path, missing):
    from src.application.trades.auto_intake import recover_trade_intake_receipts
    from src.application.trades.inbox import begin_trade_receipt_attempt

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _, inbox_id, _, source = _recorded_source_pending(repo, tmp_path)
    inbox = source["inbox_path"]
    source.update(futu_account_ids=["123"], receipt={"enabled": True})
    before = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    with sqlite3.connect(inbox) as conn:
        conn.create_function("trade_inbox_writer_version", 0, lambda: 2)
        conn.execute("DROP TABLE trade_inbox_evidence" if missing == "table"
                     else "DELETE FROM trade_inbox_evidence")
    assert begin_trade_receipt_attempt(inbox, inbox_id=inbox_id, route={"target": "test"}, message="old") == {
        "claimed": False, "status": "suppressed", "reason": "inbox_source_evidence_missing",
    }
    assert recover_trade_intake_receipts(repo=repo, source=source,
        receipt_callback=lambda _: pytest.fail("missing evidence must not reach delivery"),
        stop_event=threading.Event()) == {"checked": 0, "sent": 0, "errors": []}
    assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == before


def test_receipt_recovery_rejects_changed_evidence_metadata(tmp_path, monkeypatch):
    from src.application.trades import auto_intake

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _, inbox_id, _, source = _recorded_source_pending(repo, tmp_path)
    inbox = source["inbox_path"]
    source.update(futu_account_ids=["123"], receipt={"enabled": True})
    before = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    prepare = auto_intake.prepare_trade_receipt_result
    def changed_before_prepare(*args, **kwargs):
        with sqlite3.connect(inbox) as conn:
            conn.create_function("trade_inbox_writer_version", 0, lambda: 2)
            conn.execute("UPDATE trade_inbox_evidence SET received_at_ms=received_at_ms+1 WHERE inbox_id=?", (inbox_id,))
        return prepare(*args, **kwargs)
    monkeypatch.setattr(auto_intake, "prepare_trade_receipt_result", changed_before_prepare)
    assert auto_intake.recover_trade_intake_receipts(repo=repo, source=source,
        receipt_callback=lambda _: pytest.fail("stale evidence must not reach delivery"),
        stop_event=threading.Event()) == {"checked": 0, "sent": 0, "errors": []}
    assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == before


@pytest.mark.parametrize("outcome, expected_status", [
    ({"delivery_confirmed": True}, "sent"),
    ({"delivery_confirmed": False, "explicit_pre_acceptance_failure": True}, "failed"),
    ({"delivery_confirmed": False}, "unknown"),
])
def test_later_identity_conflict_keeps_existing_receipt_outcome(tmp_path, monkeypatch, outcome, expected_status):
    from src.application.trades.inbox import begin_trade_receipt_attempt, finish_trade_receipt_attempt

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload, inbox_id, key, source = _recorded_source_pending(repo, tmp_path)
    inbox = source["inbox_path"]
    attempt = begin_trade_receipt_attempt(inbox, inbox_id=inbox_id, route={"target": "test"}, message="old")
    assert attempt["claimed"]
    _enqueue_legacy_source_conflict(monkeypatch, inbox,
        payload={**payload, "source_deal_id": "other-fill"}, key=key, repo=repo)
    finish_trade_receipt_attempt(inbox, inbox_id=inbox_id, attempt_id=attempt["attempt_id"], result=outcome)
    row = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    assert row["receipt"]["result"] == outcome
    assert row["receipt"]["status"] == expected_status
    assert row["receipt"]["attempt_id"] == attempt["attempt_id"]
    assert not begin_trade_receipt_attempt(inbox, inbox_id=inbox_id, route={"target": "test"}, message="old")["claimed"]
    assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == row


@pytest.mark.parametrize("field", ["source_deal_id", "deal_id", "futu_deal_id"])
@pytest.mark.parametrize("receipt_status", ["pending", "failed", "sent", "unknown"])
def test_historical_identity_conflict_blocks_receipt_and_refresh_claims(
    tmp_path, monkeypatch, field, receipt_status,
):
    from src.application.trades.auto_intake import _reconcile_source_completion, recover_trade_intake_receipts
    from src.application.trades.inbox import (
        begin_trade_receipt_attempt, list_trade_receipt_recovery_rows,
        list_unclaimed_trade_payload_refresh_intents, prepare_trade_receipt_result,
        record_trade_payload_refresh_intent,
    )

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload, inbox_id, key, source = _recorded_source_pending(repo, tmp_path)
    inbox = source["inbox_path"]
    source.update(futu_account_ids=["123"], receipt={"enabled": True})
    row = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    envelope = row["receipt_envelope"]
    envelope["receipts"][envelope["current_result_key"]].update(
        status=receipt_status, attempt_count=1, result={"delivery_confirmed": receipt_status == "sent"},
    )
    with sqlite3.connect(inbox) as conn:
        conn.create_function("trade_inbox_writer_version", 0, lambda: 2)
        conn.execute("UPDATE trade_inbox SET receipt_json=? WHERE inbox_id=?", (json.dumps(envelope), inbox_id))
    record_trade_payload_refresh_intent(inbox, inbox_id=inbox_id, intent={"account": "lx", "request_id": "old-intent"})
    _enqueue_legacy_source_conflict(monkeypatch, inbox, payload={**payload, field: "other-fill"}, key=key, repo=repo)
    before = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    assert before["status"] == "handled"
    state_bytes = source["state_path"].read_bytes()
    reconciled = _reconcile_source_completion(source=source, repo=repo, apply_changes=True)
    assert reconciled["deferred"] == [{"deal_id": key, "reason": "inbox_source_identity_conflict"}]
    assert source["state_path"].read_bytes() == state_bytes
    assert list_trade_receipt_recovery_rows(inbox, account_ids=["123"]) == []
    assert list_unclaimed_trade_payload_refresh_intents(inbox, account_mapping={"123": "lx"}) == []
    recovered = recover_trade_intake_receipts(repo=repo, source=source,
        receipt_callback=lambda _: pytest.fail("historical conflict must not reach delivery"), stop_event=threading.Event())
    assert recovered == {"checked": 0, "sent": 0, "errors": []}
    with pytest.raises(TradePayloadClaimLost, match="source evidence"):
        prepare_trade_receipt_result(inbox, inbox_id=inbox_id, result=before["result"],
            expected_payload_version=before["payload_version"], expected_result=before["result"])
    assert begin_trade_receipt_attempt(inbox, inbox_id=inbox_id, route={"target": "test"}, message="old") == {
        "claimed": False, "status": "suppressed", "reason": "inbox_source_identity_conflict",
    }
    assert claim_trade_payload_refresh_intent(inbox, inbox_id=inbox_id) is None
    assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == before


@pytest.mark.parametrize("stage", ["prepare", "send", "refresh"])
def test_historical_evidence_only_race_blocks_external_claim(tmp_path, monkeypatch, stage):
    from src.application.trades import auto_intake
    from src.application.trades.inbox import (
        begin_trade_receipt_attempt, list_unclaimed_trade_payload_refresh_intents,
        record_trade_payload_refresh_intent,
    )

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload, inbox_id, key, source = _recorded_source_pending(repo, tmp_path)
    inbox = source["inbox_path"]
    source.update(futu_account_ids=["123"], receipt={"enabled": True})
    before_claim = []
    def append_conflict():
        before = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
        _enqueue_legacy_source_conflict(monkeypatch, inbox,
            payload={**payload, "source_deal_id": "other-fill"}, key=key, repo=repo)
        assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == before
        before_claim.append(before)
    if stage == "refresh":
        record_trade_payload_refresh_intent(inbox, inbox_id=inbox_id, intent={"account": "lx", "request_id": "old-intent"})
        assert len(list_unclaimed_trade_payload_refresh_intents(inbox, account_mapping={"123": "lx"})) == 1
        append_conflict()
        assert claim_trade_payload_refresh_intent(inbox, inbox_id=inbox_id) is None
    else:
        if stage == "prepare":
            prepare = auto_intake.prepare_trade_receipt_result
            def prepare_after_conflict(*args, **kwargs):
                append_conflict()
                return prepare(*args, **kwargs)
            monkeypatch.setattr(auto_intake, "prepare_trade_receipt_result", prepare_after_conflict)
        def callback(context):
            assert stage == "send", "stale recovery must stop before the callback"
            append_conflict()
            attempt = begin_trade_receipt_attempt(inbox, inbox_id=inbox_id,
                route={"target": "test"}, message="old", result_key=context["result"].get("receipt_result_key"))
            assert attempt == {"claimed": False, "status": "suppressed", "reason": "inbox_source_identity_conflict"}
            return {"status": "skipped", "delivery_confirmed": False}
        recovered = auto_intake.recover_trade_intake_receipts(repo=repo, source=source,
            receipt_callback=callback, stop_event=threading.Event())
        assert recovered == {"checked": int(stage == "send"), "sent": 0, "errors": []}
    assert len(before_claim) == 1
    assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == before_claim[0]


@pytest.mark.parametrize("field", ["source_deal_id", "deal_id", "futu_deal_id"])
def test_reconciliation_rejects_legacy_durable_source_conflict(tmp_path, monkeypatch, field):
    from src.application.trades.auto_intake import _reconcile_source_completion

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload, inbox_id, key, source = _recorded_source_pending(repo, tmp_path)
    inbox = source["inbox_path"]
    _enqueue_legacy_source_conflict(monkeypatch, inbox, payload={**payload, field: "other-fill"}, key=key, repo=repo)
    before = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    assert before["status"] == "handled"
    state_bytes = source["state_path"].read_bytes()
    events, lots = repo.list_trade_events(), repo.list_position_lots()
    for apply in (False, True):
        result = _reconcile_source_completion(source=source, repo=repo, apply_changes=apply)
        assert result["planned_count"] == result["applied_count"] == result["inbox_updated_count"] == 0
        assert result["deferred"] == [{"deal_id": key, "reason": "inbox_source_identity_conflict"}]
        assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == before
        assert source["state_path"].read_bytes() == state_bytes
        assert (repo.list_trade_events(), repo.list_position_lots()) == (events, lots)


@pytest.mark.parametrize("change", ["append", "replace", "delete"])
def test_reconciliation_rejects_evidence_only_race(tmp_path, monkeypatch, change):
    from src.application.trades import auto_intake

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload, inbox_id, key, source = _recorded_source_pending(repo, tmp_path)
    inbox = source["inbox_path"]
    before = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    state_bytes = source["state_path"].read_bytes()
    settle = auto_intake.settle_reconciled_trade_payload
    def changed_before_commit(*args, **kwargs):
        if change == "append":
            _enqueue_legacy_source_conflict(monkeypatch, inbox,
                payload={**payload, "source_deal_id": "other-fill"}, key=key, repo=repo)
        else:
            with sqlite3.connect(inbox) as conn:
                conn.create_function("trade_inbox_writer_version", 0, lambda: 2)
                if change == "replace":
                    conn.execute("UPDATE trade_inbox_evidence SET received_at_ms=received_at_ms+1 WHERE inbox_id=?", (inbox_id,))
                else:
                    conn.execute("DELETE FROM trade_inbox_evidence WHERE inbox_id=?", (inbox_id,))
        assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == before
        return settle(*args, **kwargs)
    monkeypatch.setattr(auto_intake, "settle_reconciled_trade_payload", changed_before_commit)
    result = auto_intake._reconcile_source_completion(source=source, repo=repo, apply_changes=True)
    assert result["applied_count"] == result["inbox_updated_count"] == 0
    assert result["deferred"] == [{"deal_id": key, "reason": "inbox_observation_changed"}]
    assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == before
    assert source["state_path"].read_bytes() == state_bytes


@pytest.mark.parametrize("missing", ["rows", "table"])
def test_reconciliation_requires_historical_source_evidence(tmp_path, missing):
    from src.application.trades.auto_intake import _reconcile_source_completion

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _, inbox_id, key, source = _recorded_source_pending(repo, tmp_path)
    inbox = source["inbox_path"]
    with sqlite3.connect(inbox) as conn:
        conn.create_function("trade_inbox_writer_version", 0, lambda: 2)
        if missing == "table":
            conn.execute("DROP TABLE trade_inbox_evidence")
        else:
            conn.execute("DELETE FROM trade_inbox_evidence WHERE inbox_id=?", (inbox_id,))
    state_bytes = source["state_path"].read_bytes()
    for apply in (False, True):
        if missing == "table":
            with pytest.raises(sqlite3.DatabaseError, match="trade_inbox_evidence"):
                _reconcile_source_completion(source=source, repo=repo, apply_changes=apply)
        else:
            result = _reconcile_source_completion(source=source, repo=repo, apply_changes=apply)
            assert result["planned_count"] == result["applied_count"] == 0
            assert result["deferred"] == [{"deal_id": key, "reason": "inbox_source_evidence_missing"}]
        assert source["state_path"].read_bytes() == state_bytes


@pytest.mark.parametrize("field", ["source_deal_id", "deal_id", "futu_deal_id"])
def test_handled_inbox_replay_preserves_conflicting_source_identity_for_review(tmp_path, field):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = {**_execution(), "external_order_id": None, "external_order_namespace": None}
    recorded = _process(repo, tmp_path, "push", payload)
    assert recorded["status"] == "applied"
    inbox = resolve_execution_inbox_path(repo, tmp_path / "inbox.sqlite3")
    before_row = read_trade_payload(inbox, inbox_id=recorded["inbox_id"], read_only=True)
    before = (repo.list_trade_events(), repo.list_position_lots(), repo.list_trade_lifecycle_notifications())
    result = _process(repo, tmp_path, "file", {**payload, field: "other-fill"}, source="file")
    assert (result["status"], result["reason"]) == ("unresolved", "inbox_conflict")
    row = read_trade_payload(inbox, inbox_id=recorded["inbox_id"], read_only=True)
    assert row["status"] == "conflict"
    assert row["result"] == before_row["result"]
    assert row.get("receipt") == before_row.get("receipt")
    clean_retry = _process(repo, tmp_path, "retry", payload)
    assert (clean_retry["status"], clean_retry["reason"]) == ("unresolved", "inbox_conflict")
    assert read_trade_payload(inbox, inbox_id=recorded["inbox_id"], read_only=True)["status"] == "conflict"
    assert (repo.list_trade_events(), repo.list_position_lots(), repo.list_trade_lifecycle_notifications()) == before


@pytest.mark.parametrize("legacy_key", [False, True])
def test_canonical_completion_retries_inbox_before_source_without_economic_replay(tmp_path, monkeypatch, legacy_key):
    from src.application.trades import auto_intake, state_reconcile
    from src.application.trades.state import load_trade_intake_state, write_trade_intake_state
    from src.application.ledger.api import open_trade_reconciliation_evidence_repo

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = _execution()
    recorded = _process(repo, tmp_path, "push", payload)
    assert recorded["status"] == "applied"
    inbox = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    key = broker_deal_key_from_payload(payload, account_mapping={"123": "lx"})
    source = {"account": "lx", "account_mapping": {"123": "lx"},
              "state_path": tmp_path / "push" / "state.json",
              "audit_path": tmp_path / "push" / "audit.jsonl",
              "inbox_path": tmp_path / "unused.sqlite3"}
    observed_state = load_trade_intake_state(source["state_path"])
    old = observed_state["processed_deal_ids"].pop(key)
    if legacy_key:
        key = "futu:lx:123:fill-1"
    observed_state["unresolved_deal_ids"][key] = {
        **old, "status": "unresolved", "reason": "verification_pending",
    }
    write_trade_intake_state(source["state_path"], observed_state)
    with sqlite3.connect(inbox) as conn:
        conn.create_function("trade_inbox_writer_version", 0, lambda: 2)
        conn.execute("UPDATE trade_inbox SET status='pending', result_json=?, result_status='unresolved' WHERE inbox_id=?",
                     (json.dumps({"status": "unresolved", "reason": "verification_pending"}), recorded["inbox_id"]))
    original_row = read_trade_payload(inbox, inbox_id=recorded["inbox_id"], read_only=True)
    events = repo.list_trade_events()
    lots = repo.list_position_lots()
    preview = auto_intake._reconcile_source_completion(
        source=source, repo=open_trade_reconciliation_evidence_repo(repo.db_path), apply_changes=False,
    )
    assert preview["planned_count"] == 1
    assert preview["applied_count"] == 0
    assert read_trade_payload(inbox, inbox_id=recorded["inbox_id"], read_only=True) == original_row
    assert load_trade_intake_state(source["state_path"]) == observed_state

    # Crash after the Inbox commit but before the source file write.
    original_reconcile = auto_intake.reconcile_trade_intake_state
    def interrupted(**kwargs):
        def fail_write(*args, **kwargs):
            raise OSError("source disk unavailable")
        return original_reconcile(**kwargs, update_state_fn=fail_write)
    monkeypatch.setattr(auto_intake, "reconcile_trade_intake_state", interrupted)
    with pytest.raises(OSError, match="source disk unavailable"):
        auto_intake._reconcile_source_completion(source=source, repo=repo, apply_changes=True)
    settled_row = read_trade_payload(inbox, inbox_id=recorded["inbox_id"], read_only=True)
    assert settled_row["status"] == "handled"
    assert settled_row["receipt_envelope"] == original_row["receipt_envelope"]
    assert load_trade_intake_state(source["state_path"]) == observed_state
    monkeypatch.setattr(auto_intake, "reconcile_trade_intake_state", state_reconcile.reconcile_trade_intake_state)
    retried = auto_intake._reconcile_source_completion(source=source, repo=repo, apply_changes=True)
    assert retried["applied_count"] == 1
    assert retried["inbox_updated_count"] == 0
    assert auto_intake._reconcile_source_completion(source=source, repo=repo, apply_changes=True)["applied_count"] == 0
    assert repo.list_trade_events() == events
    assert repo.list_position_lots() == lots
    assert read_trade_payload(inbox, inbox_id=recorded["inbox_id"], read_only=True) == settled_row


@pytest.mark.parametrize("legacy_key", [False, True])
@pytest.mark.parametrize("prepare_receipt_again", [False, True])
def test_order_enrichment_preserves_interrupted_reconciliation_proof(
    tmp_path, monkeypatch, legacy_key, prepare_receipt_again,
):
    from src.application.trades import auto_intake, state_reconcile
    from src.application.trades.inbox import (
        begin_trade_receipt_attempt, list_trade_receipt_recovery_rows,
        list_unclaimed_trade_payload_refresh_intents, prepare_trade_receipt_result,
        record_trade_payload_refresh_intent,
    )
    from src.application.trades.state import load_trade_intake_state, write_trade_intake_state

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = _execution()
    payload.pop("external_order_id")
    payload.pop("external_order_namespace")
    recorded = _process(repo, tmp_path, "push", payload)
    assert recorded["status"] == "applied"
    inbox = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    inbox_id = recorded["inbox_id"]
    previous = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    envelope = previous["receipt_envelope"]
    envelope["receipts"][envelope["current_result_key"]].update(
        status="unknown", attempt_id="previous-send", attempt_count=1,
    )
    with sqlite3.connect(inbox) as conn:
        conn.create_function("trade_inbox_writer_version", 0, lambda: 2)
        conn.execute("UPDATE trade_inbox SET receipt_json=? WHERE inbox_id=?", (json.dumps(envelope), inbox_id))
    record_trade_payload_refresh_intent(inbox, inbox_id=inbox_id, intent={"account": "lx", "request_id": "old-intent"})
    previous = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    path = tmp_path / "push" / "state.json"
    state = load_trade_intake_state(path)
    key, old = state["processed_deal_ids"].popitem()
    if legacy_key:
        key = "futu:lx:123:fill-1"
    state["unresolved_deal_ids"][key] = {**old, "status": "unresolved"}
    write_trade_intake_state(path, state)
    source = {"account": "lx", "account_mapping": {"123": "lx"}, "state_path": path, "inbox_path": inbox}
    def fail_write(*args, **kwargs):
        raise OSError("source write interrupted")
    monkeypatch.setattr(auto_intake, "reconcile_trade_intake_state", lambda **kwargs:
        state_reconcile.reconcile_trade_intake_state(**kwargs, update_state_fn=fail_write))
    with pytest.raises(OSError, match="source write interrupted"):
        auto_intake._reconcile_source_completion(source=source, repo=repo, apply_changes=True)
    closed = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    assert closed["status"] == "handled"
    assert load_trade_intake_state(path) == state
    monkeypatch.setattr(auto_intake, "reconcile_trade_intake_state", state_reconcile.reconcile_trade_intake_state)

    def unexpected_delivery(*args, **kwargs):
        pytest.fail("association enrichment must not send")
    enriched = _process(repo, tmp_path, "backfill", {
        **payload, "external_order_id": "added-order", "external_order_namespace": "futu.order",
    }, source="backfill", on_result_fn=unexpected_delivery)
    assert (enriched["status"], enriched["reason"]) == ("skipped", "ledger_recorded")
    after = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    if prepare_receipt_again:
        # Exercise the sibling result-persistence entry without an economic claim.
        prepare_trade_receipt_result(inbox, inbox_id=inbox_id,
            result={"status": "skipped", "reason": "ledger_recorded", "diagnostics": {"readback": "complete"}},
            expected_payload_version=after["payload_version"], expected_result=after["result"])
        after = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
        assert after["result"]["diagnostics"]["readback"] == "complete"
    diagnostics = after["result"]["diagnostics"]
    assert after["result"]["receipt_suppression_reason"] == "reconciled_from_ledger"
    assert diagnostics["reconciliation_result"] == closed["result"]["diagnostics"]["reconciliation_result"]
    assert diagnostics["previous_result"] == previous["result"]
    assert diagnostics["previous_receipt_envelope"] == previous["receipt_envelope"]
    assert after["receipt_envelope"] == previous["receipt_envelope"]
    assert after["portfolio_refresh_intent_json"] == previous["portfolio_refresh_intent_json"]
    assert after["portfolio_refresh_attempted_at_ms"] is None
    assert list_trade_receipt_recovery_rows(inbox, account_ids=["123"]) == []
    assert list_unclaimed_trade_payload_refresh_intents(inbox, account_mapping={"123": "lx"}) == []
    assert claim_trade_payload_refresh_intent(inbox, inbox_id=inbox_id) is None
    assert not begin_trade_receipt_attempt(inbox, inbox_id=inbox_id, route={}, message="old")["claimed"]
    events, lots = repo.list_trade_events(), repo.list_position_lots()
    assert len(events) == len(lots) == 1
    assert events[0]["raw_payload"]["execution_input"]["external_order_id"] == "added-order"
    preview = auto_intake._reconcile_source_completion(source=source, repo=repo, apply_changes=False)
    assert preview["planned_count"] == 1 and preview["applied_count"] == 0
    assert load_trade_intake_state(path) == state
    applied = auto_intake._reconcile_source_completion(source=source, repo=repo, apply_changes=True)
    assert applied["applied_count"] == 1 and applied["inbox_updated_count"] == 0
    assert not applied["deferred"]
    assert key in load_trade_intake_state(path)["processed_deal_ids"]
    assert not load_trade_intake_state(path)["unresolved_deal_ids"]
    state_bytes = path.read_bytes()
    retried = auto_intake._reconcile_source_completion(source=source, repo=repo, apply_changes=True)
    assert retried["applied_count"] == retried["inbox_updated_count"] == 0
    assert path.read_bytes() == state_bytes
    assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == after
    assert (repo.list_trade_events(), repo.list_position_lots()) == (events, lots)


@pytest.mark.parametrize("separate_sources", [False, True])
@pytest.mark.parametrize("legacy_first", [False, True])
def test_reconciliation_aliases_share_one_inbox_completion(tmp_path, separate_sources, legacy_first):
    from src.application.trades.auto_intake import _reconcile_intake_sources
    from src.application.trades.state import load_trade_intake_state, write_trade_intake_state

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = _execution()
    recorded = _process(repo, tmp_path, "push", payload)
    path = tmp_path / "push" / "state.json"
    state = load_trade_intake_state(path)
    canonical, old = state["processed_deal_ids"].popitem()
    keys = ["futu:lx:123:fill-1", canonical] if legacy_first else [canonical, "futu:lx:123:fill-1"]
    sources = []
    for index, key in enumerate(keys):
        source_path = path if not separate_sources or index == 0 else tmp_path / "backfill" / "state.json"
        if separate_sources:
            write_trade_intake_state(source_path, {"unresolved_deal_ids": {key: {**old, "status": "unresolved"}}})
        if separate_sources or index == 0:
            sources.append({"id": str(index), "account": "lx", "account_mapping": {"123": "lx"},
                "state_path": source_path, "audit_path": source_path.with_suffix(".jsonl"),
                "inbox_path": tmp_path / "unused.sqlite3"})
    if not separate_sources:
        write_trade_intake_state(path, {"unresolved_deal_ids": {key: {**old, "status": "unresolved"} for key in keys}})
    inbox = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    previous = read_trade_payload(inbox, inbox_id=recorded["inbox_id"], read_only=True)
    events, lots = repo.list_trade_events(), repo.list_position_lots()
    kwargs = dict(sources=sources, repo=repo, account="lx", deal_ids=[], runtime_root=tmp_path, runtime_root_source="test")
    assert _reconcile_intake_sources(**kwargs, apply_changes=False)["planned_count"] == 2
    result = _reconcile_intake_sources(**kwargs, apply_changes=True)
    assert result["applied_count"] == 2
    assert sum(item["inbox_updated_count"] for item in result["sources"]) == 1
    assert all(not item["deferred"] for item in result["sources"])
    for source in sources:
        assert not load_trade_intake_state(source["state_path"])["unresolved_deal_ids"]
    closed = read_trade_payload(inbox, inbox_id=recorded["inbox_id"], read_only=True)
    assert closed["result"]["diagnostics"]["reconciled_source_key"] == canonical
    assert closed["result"]["deal_id"] == "fill-1"
    assert closed["receipt_envelope"] == previous["receipt_envelope"]
    assert closed["result"]["diagnostics"]["previous_result"] == previous["result"]
    assert _reconcile_intake_sources(**kwargs, apply_changes=True)["applied_count"] == 0
    assert read_trade_payload(inbox, inbox_id=recorded["inbox_id"], read_only=True) == closed
    assert (repo.list_trade_events(), repo.list_position_lots()) == (events, lots)


def test_reconciliation_reports_inbox_commit_when_source_cas_misses(tmp_path, monkeypatch):
    from src.application.trades import auto_intake, state_reconcile
    from src.application.trades.state import (
        compare_and_update_trade_intake_state_entries, load_trade_intake_state, write_trade_intake_state,
    )

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    recorded = _process(repo, tmp_path, "push", _execution())
    path = tmp_path / "push" / "state.json"
    state = load_trade_intake_state(path)
    key, old = state["processed_deal_ids"].popitem()
    state["unresolved_deal_ids"][key] = {**old, "status": "unresolved"}
    write_trade_intake_state(path, state)
    def concurrent_update(path, desired, *, deal_ids, expected_state):
        latest = load_trade_intake_state(path)
        latest["unresolved_deal_ids"][key]["updated_at"] = "concurrent receipt readback"
        write_trade_intake_state(path, latest)
        return compare_and_update_trade_intake_state_entries(path, desired, deal_ids=deal_ids, expected_state=expected_state)
    monkeypatch.setattr(auto_intake, "reconcile_trade_intake_state", lambda **kwargs:
        state_reconcile.reconcile_trade_intake_state(**kwargs, update_state_fn=concurrent_update))
    source = {"id": "lx", "account": "lx", "account_mapping": {"123": "lx"},
        "state_path": path, "audit_path": path.with_suffix(".jsonl"), "inbox_path": tmp_path / "unused.sqlite3"}
    kwargs = dict(sources=[source], repo=repo, account="lx", deal_ids=[], runtime_root=tmp_path, runtime_root_source="test", apply_changes=True)
    result = auto_intake._reconcile_intake_sources(**kwargs)
    assert result["applied_count"] == 0 and result["inbox_updated_count"] == 1
    assert result["write_applied"] is True
    assert "retry" in result["rollback_hint"] and "does not undo Inbox" in result["rollback_hint"]
    assert load_trade_intake_state(path)["unresolved_deal_ids"][key]["updated_at"] == "concurrent receipt readback"
    inbox = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    closed = read_trade_payload(inbox, inbox_id=recorded["inbox_id"], read_only=True)
    assert closed["status"] == "handled" and closed["receipt_recovery_allowed"] == 0
    monkeypatch.setattr(auto_intake, "reconcile_trade_intake_state", state_reconcile.reconcile_trade_intake_state)
    retry = auto_intake._reconcile_intake_sources(**kwargs)
    assert retry["applied_count"] == 1 and retry["inbox_updated_count"] == 0 and retry["write_applied"]
    assert not auto_intake._reconcile_intake_sources(**kwargs)["write_applied"]
    assert read_trade_payload(inbox, inbox_id=recorded["inbox_id"], read_only=True) == closed


@pytest.mark.parametrize("obstacle", ["claimed", "economic_conflict", "other_account"])
def test_source_completion_respects_inbox_claims_economics_and_account_scope(tmp_path, obstacle):
    from src.application.trades.auto_intake import _reconcile_source_completion
    from src.application.trades.state import load_trade_intake_state, write_trade_intake_state

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = _execution()
    recorded = _process(repo, tmp_path, "push", payload)
    inbox = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    key = broker_deal_key_from_payload(payload, account_mapping={"123": "lx"})
    source = {"account": "lx", "account_mapping": {"123": "lx"},
              "state_path": tmp_path / "push" / "state.json", "inbox_path": inbox}
    state = load_trade_intake_state(source["state_path"])
    old = state["processed_deal_ids"].pop(key)
    state["unresolved_deal_ids"][key] = {**old, "status": "unresolved"}
    write_trade_intake_state(source["state_path"], state)
    if obstacle == "claimed":
        with sqlite3.connect(inbox) as conn:
            conn.create_function("trade_inbox_writer_version", 0, lambda: 2)
            conn.execute("UPDATE trade_inbox SET status='pending', result_json=NULL, next_attempt_at_ms=0 WHERE inbox_id=?",
                         (recorded["inbox_id"],))
        assert claim_trade_payload(inbox, inbox_id=recorded["inbox_id"], repo=repo)
    elif obstacle == "economic_conflict":
        enqueue_trade_payload(inbox, payload=_execution(price="9.00"), source="backfill", broker_deal_key=key, repo=repo)
    else:
        foreign = _execution(physical="456")
        foreign["broker_account_ref"]["account_label"] = "sy"
        foreign_id = enqueue_trade_payload(inbox, payload=foreign, source="push", repo=repo,
            broker_deal_key=broker_deal_key_from_payload(foreign, account_mapping={"456": "sy"}))
        foreign_before = read_trade_payload(inbox, inbox_id=foreign_id, read_only=True)
    before = read_trade_payload(inbox, inbox_id=recorded["inbox_id"], read_only=True)
    events = repo.list_trade_events()
    preview = _reconcile_source_completion(source=source, repo=repo, apply_changes=False)
    result = _reconcile_source_completion(source=source, repo=repo, apply_changes=True)
    assert preview["planned_count"] == result["applied_count"] == int(obstacle == "other_account")
    assert repo.list_trade_events() == events
    if obstacle == "other_account":
        assert read_trade_payload(inbox, inbox_id=foreign_id, read_only=True) == foreign_before
    else:
        assert result["deferred"]
        assert load_trade_intake_state(source["state_path"]) == state
        assert read_trade_payload(inbox, inbox_id=recorded["inbox_id"], read_only=True) == before


@pytest.mark.parametrize("failure", ["gateway", "state_write"])
def test_listener_recovers_source_independently_of_gateway_and_seal_failures(tmp_path, monkeypatch, failure):
    from src.application.trades import auto_intake
    from src.application.trades.state import load_trade_intake_state, write_trade_intake_state

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = _execution()
    recorded = _process(repo, tmp_path, "push", payload)
    assert recorded["status"] == "applied"
    source = {"id": "lx", "account": "lx", "account_mapping": {"123": "lx"},
              "futu_account_ids": ["123"], "host": "127.0.0.1", "port": 11111,
              "state_path": tmp_path / "push" / "state.json", "audit_path": tmp_path / "push" / "audit.jsonl",
              "status_path": tmp_path / "status.json", "inbox_path": tmp_path / "unused.sqlite3",
              "receipt": {"enabled": False}, "backfill": {"enabled": False},
              "settlement_observation": {"enabled": failure == "gateway"}, "combo_reconciliation_mode": "off"}
    state = load_trade_intake_state(source["state_path"])
    key, old = next(iter(state["processed_deal_ids"].items()))
    state["processed_deal_ids"] = {}
    state["unresolved_deal_ids"][key] = {**old, "status": "unresolved"}
    write_trade_intake_state(source["state_path"], state)
    stop = threading.Event()
    class Listener:
        checks = 0
        def __init__(self, **kwargs):
            pass
        def start(self, **kwargs):
            pass
        def check_health(self):
            self.checks += 1
            if self.checks == 2:
                stop.set()
        def close(self):
            pass
    class History:
        def __init__(self, **kwargs):
            pass
        def close(self):
            pass
    monkeypatch.setattr(auto_intake, "OpenDTradePushListener", Listener)
    monkeypatch.setattr(auto_intake, "OpenDHistoryDealClient", History)
    clock = iter(range(0, 100_000, 61))
    monkeypatch.setattr(auto_intake.time, "monotonic", lambda: next(clock))
    checkpoints = []
    monkeypatch.setattr(auto_intake, "append_lifecycle_attempt_checkpoint_seal",
                        lambda *args, **kwargs: checkpoints.append(kwargs["reason"]))
    def gateway(**kwargs):
        raise ConnectionError("OpenD unavailable")
    monkeypatch.setattr(auto_intake, "build_futu_gateway", gateway)
    monkeypatch.setattr(auto_intake, "reconcile_due_lifecycle_cases_for_source", lambda *args, **kwargs: {})
    monkeypatch.setattr(auto_intake, "recover_order_fee_targets", lambda *args, **kwargs:
                        {"targets": [], "selection_cursor": {"after": None}, "candidate_count": 0, "issues": []})
    original_reconcile = auto_intake._reconcile_source_completion
    attempts = []
    def reconcile(**kwargs):
        attempts.append(1)
        if failure == "state_write" and len(attempts) == 1:
            raise OSError("state file unavailable")
        return original_reconcile(**kwargs)
    monkeypatch.setattr(auto_intake, "_reconcile_source_completion", reconcile)
    def unexpected_delivery(*args, **kwargs):
        pytest.fail("local source reconciliation must not deliver")
    before = repo.list_trade_events(), repo.list_position_lots()
    assert auto_intake._run_listener_source_loop(
        source=source, repo=repo, cfg={}, cfg_path=tmp_path / "config.json",
        runtime_root=tmp_path, runtime_root_source="test", intake_cfg={"mode": "apply", "enabled": True},
        apply_changes=True, receipt_callback=unexpected_delivery,
        process_lock=threading.RLock(), stop_event=stop,
    ) == 0
    assert len(attempts) >= 2
    assert not load_trade_intake_state(source["state_path"])["unresolved_deal_ids"]
    status = json.loads(source["status_path"].read_text())
    assert "last_intake_state_reconciliation_error" not in status
    if failure == "gateway":
        assert "OpenD unavailable" in status["last_lifecycle_due_error"]
    else:
        assert checkpoints == ["process_startup"]
        assert "last_lifecycle_due_error" not in status
    assert (repo.list_trade_events(), repo.list_position_lots()) == before


@pytest.mark.parametrize("obstacle", [None, "claimed", "economics", "other_account"])
def test_bare_source_identity_closes_matching_inbox_before_source(tmp_path, monkeypatch, obstacle):
    from test_trades_state_reconcile import _completed_canonical_repo
    from src.application.trades import auto_intake, state_reconcile
    from src.application.trades.state import load_trade_intake_state, write_trade_intake_state

    repo = _completed_canonical_repo()
    payload = _execution(physical="1001", deal_id="option-1", price="0")
    payload["instrument_ref"].update(symbol="FUTU", strike="120", expiration_ymd="2026-08-21")
    payload.update(side="buy", position_effect="close", occurred_at_utc="2023-11-14T22:13:20.100Z")
    payload.pop("external_order_id")
    payload.pop("external_order_namespace")
    if obstacle == "economics":
        payload["price"] = "1"
    inbox = tmp_path / "inbox.sqlite3"
    source = {"account": "lx", "account_mapping": {"1001": "lx"},
              "state_path": tmp_path / "state.json", "inbox_path": inbox}
    state = {"unresolved_deal_ids": {"option-1": {"account": "lx", "status": "unresolved",
        "futu_account_id": "1001", "source_deal_id": "option-1"}}}
    write_trade_intake_state(source["state_path"], state)
    original_state = load_trade_intake_state(source["state_path"])
    inbox_id = enqueue_trade_payload(inbox, payload=payload, source="push", repo=repo,
        broker_deal_key=broker_deal_key_from_payload(payload, account_mapping=source["account_mapping"]))
    if obstacle == "claimed":
        assert claim_trade_payload(inbox, inbox_id=inbox_id, repo=repo)
    if obstacle == "other_account":
        foreign = {**payload, "broker_account_ref": {**payload["broker_account_ref"],
            "external_account_id": "1002", "broker_account_id": "futu:REAL:1002", "account_label": "sy"}}
        foreign_id = enqueue_trade_payload(inbox, payload=foreign, source="push", repo=repo,
            broker_deal_key=broker_deal_key_from_payload(foreign, account_mapping={"1002": "sy"}))
        assert claim_trade_payload(inbox, inbox_id=foreign_id, repo=repo)
        foreign_before = read_trade_payload(inbox, inbox_id=foreign_id, read_only=True)
    original_row = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    before_events = repo.list_trade_events()
    preview = auto_intake._reconcile_source_completion(source=source, repo=repo, apply_changes=False)
    assert load_trade_intake_state(source["state_path"]) == original_state
    assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == original_row
    if obstacle in {"claimed", "economics"}:
        assert preview["planned_count"] == 0
        result = auto_intake._reconcile_source_completion(source=source, repo=repo, apply_changes=True)
        assert result["applied_count"] == 0 and result["deferred"]
        assert load_trade_intake_state(source["state_path"]) == original_state
        assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == original_row
    else:
        assert preview["planned_count"] == 1
        def fail_write(*args, **kwargs):
            raise OSError("source write interrupted")
        monkeypatch.setattr(auto_intake, "reconcile_trade_intake_state", lambda **kwargs:
            state_reconcile.reconcile_trade_intake_state(**kwargs, update_state_fn=fail_write))
        with pytest.raises(OSError, match="source write interrupted"):
            auto_intake._reconcile_source_completion(source=source, repo=repo, apply_changes=True)
        closed = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
        assert closed["status"] == "handled"
        assert closed["result"]["receipt_suppression_reason"] == "reconciled_from_ledger"
        assert load_trade_intake_state(source["state_path"]) == original_state
        monkeypatch.setattr(auto_intake, "reconcile_trade_intake_state", state_reconcile.reconcile_trade_intake_state)
        retried = auto_intake._reconcile_source_completion(source=source, repo=repo, apply_changes=True)
        assert retried["applied_count"] == 1 and retried["inbox_updated_count"] == 0
        assert auto_intake._reconcile_source_completion(source=source, repo=repo, apply_changes=True)["applied_count"] == 0
        assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == closed
        if obstacle == "other_account":
            assert read_trade_payload(inbox, inbox_id=foreign_id, read_only=True) == foreign_before
    assert repo.list_trade_events() == before_events


@pytest.mark.parametrize("failure", ["start", "wait", "health"])
def test_listener_local_recovery_retries_while_opend_unavailable(tmp_path, monkeypatch, failure):
    from src.application.trades import auto_intake
    from src.application.trades.state import load_trade_intake_state, write_trade_intake_state

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    recorded = _process(repo, tmp_path, "push", _execution())
    assert recorded["status"] == "applied"
    source = {"id": "lx", "account": "lx", "account_mapping": {"123": "lx"},
              "futu_account_ids": ["123"], "host": "127.0.0.1", "port": 11111,
              "state_path": tmp_path / "push" / "state.json", "audit_path": tmp_path / "push" / "audit.jsonl",
              "status_path": tmp_path / "status.json", "receipt": {"enabled": False},
              "backfill": {"enabled": False}, "settlement_observation": {"enabled": False}}
    state = load_trade_intake_state(source["state_path"])
    key, old = next(iter(state["processed_deal_ids"].items()))
    state["processed_deal_ids"] = {}
    state["unresolved_deal_ids"][key] = {**old, "status": "unresolved"}
    write_trade_intake_state(source["state_path"], state)
    clock = [0.0]
    monkeypatch.setattr(auto_intake.time, "monotonic", lambda: clock[0])
    class Stop(threading.Event):
        def wait(self, timeout=None):
            # Reconnect maintenance must finish before its first wait, without OpenD.
            assert not load_trade_intake_state(source["state_path"])["unresolved_deal_ids"]
            self.set()
            return True
    stop = Stop()
    class Listener:
        def __init__(self, **kwargs):
            pass
        def start(self, *, on_wait, **kwargs):
            if failure == "health":
                return
            clock[0] = 61
            if failure == "wait":
                on_wait()
                stop.wait()
                raise auto_intake.TradeIntakeStartCancelled("test finished")
            raise ConnectionError("listener start unavailable")
        def check_health(self):
            clock[0] = 61
            raise ConnectionError("listener health unavailable")
        def close(self):
            pass
    class History:
        def __init__(self, **kwargs):
            pass
        def close(self):
            pass
    monkeypatch.setattr(auto_intake, "OpenDTradePushListener", Listener)
    monkeypatch.setattr(auto_intake, "OpenDHistoryDealClient", History)
    actual = auto_intake._reconcile_source_completion
    attempts = []
    def reconcile(**kwargs):
        attempts.append(clock[0])
        if len(attempts) == 1:
            raise OSError("temporary state read failure")
        return actual(**kwargs)
    monkeypatch.setattr(auto_intake, "_reconcile_source_completion", reconcile)
    def unexpected(*args, **kwargs):
        pytest.fail("offline recovery must not collect or deliver")
    monkeypatch.setattr(auto_intake, "reconcile_due_lifecycle_cases_for_source", unexpected)
    monkeypatch.setattr(auto_intake, "_dispatch_portfolio_refresh_intent", unexpected)
    before = repo.list_trade_events(), repo.list_position_lots()
    assert auto_intake._run_listener_source_loop(
        source=source, repo=repo, cfg={}, cfg_path=tmp_path / "config.json", runtime_root=tmp_path,
        runtime_root_source="test", intake_cfg={"mode": "apply", "enabled": True}, apply_changes=True,
        receipt_callback=unexpected, process_lock=threading.RLock(), stop_event=stop,
    ) == 0
    assert attempts == [0, 61]
    assert not load_trade_intake_state(source["state_path"])["unresolved_deal_ids"]
    assert (repo.list_trade_events(), repo.list_position_lots()) == before
    status = json.loads(source["status_path"].read_text())
    assert "last_intake_state_reconciliation_error" not in status


def test_identity_quarantine_preserves_evidence_when_trusted_history_recovers(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    raw = {
        "deal_id": "fill-1", "code": "US.NVDA260918P100000",
        "_trade_intake_source_identity_errors": ["missing:push_physical_account"],
        "_trade_intake_source_account_evidence": {"host": "127.0.0.1", "port": 11112},
    }
    rejected = _process(repo, tmp_path, "push", raw)
    inbox = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    original = read_trade_payload(inbox, inbox_id=rejected["inbox_id"], read_only=True)
    assert original["status"] == "identity_needs_review"
    assert original["attempt_count"] == 0
    assert repo.list_trade_events() == []
    assert not resume_trade_payload(inbox, inbox_id=rejected["inbox_id"], operator="test", repo=repo)

    recovered = _process(repo, tmp_path, "history", _execution(), source="backfill")
    assert recovered["status"] == "applied"
    assert recovered["account"] == "lx"
    assert recovered["inbox_id"] != rejected["inbox_id"]
    replay = _process(repo, tmp_path, "push", _execution())
    assert replay["reason"] == "duplicate"
    assert len(repo.list_trade_events()) == 1
    assert len(repo.list_position_lots()) == 1
    assert read_trade_payload(inbox, inbox_id=rejected["inbox_id"], read_only=True) == original


def _candidate_event(repo, payload, *, stock: bool, event_id: str, conn=None):
    from domain.domain.ledger import ContractKey, TradeEvent
    from src.application.ledger.api import execution_identity_from_input

    raw = {"execution_input": payload, "execution_id": execution_identity_from_input(payload)}
    if stock:
        event = {"stock_event_id": event_id, "account": "lx", "event_type": "sale",
                 "trade_time_ms": 1_000, **raw}
        repo.upsert_assigned_stock_event(event, conn=conn)
        return event
    event = TradeEvent(
        event_id=event_id, event_type="open", event_time_ms=1_000,
        contract_key=ContractKey.from_values(
            broker="futu", account="lx", underlying_symbol="NVDA", option_type="put", strike=100, expiration_ymd="2026-09-18",
                ),
        contracts=1, price=2.5, currency="USD", source="test", multiplier=100,
        lot_id=f"lot-{event_id}", raw_payload=raw,
    )
    repo.upsert_trade_event(event, conn=conn)
    return event.to_dict()


@pytest.mark.parametrize("stock", [False, True])
def test_inbox_candidates_bound_decoding_and_keep_durable_conflicts(tmp_path, monkeypatch, stock):
    from src.application.ledger.api import execution_identity_from_input

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = _execution()
    if stock:
        payload = {**payload, "instrument_ref": {"asset_type": "stock", "symbol": "NVDA", "currency": "USD", "market": "US"},
                   "side": "sell", "position_effect": "close"}
    with repo._writer_connection(begin_immediate=True) as conn:
        for i in range(30):
            _candidate_event(repo, {**payload, "external_execution_id": f"unrelated-{i}"},
                             stock=stock, event_id=f"unrelated-{i}", conn=conn)
        _candidate_event(repo, payload, stock=stock, event_id="target", conn=conn)
    before = repo.list_trade_events(), repo.list_assigned_stock_events()
    proxy = SimpleNamespace(list_trade_events=lambda: before[0], list_assigned_stock_events=lambda: before[1])
    seen = []
    targeted_name = "list_assigned_stock_events_for_execution" if stock else "list_trade_events_for_execution"
    read = getattr(repo, targeted_name)

    def candidates(identity):
        rows = read(identity)
        seen.append(len(rows))
        return rows

    def unexpected(**_kwargs):
        raise AssertionError("indexed reception must not read all events")

    monkeypatch.setattr(repo, targeted_name, candidates)
    monkeypatch.setattr(repo, "list_trade_events", unexpected)
    monkeypatch.setattr(repo, "list_assigned_stock_events", unexpected)
    for i, incoming in enumerate((payload, {**payload, "external_order_id": "conflicting-order"})):
        identity = execution_identity_from_input(incoming)
        results = []
        for name, owner in (("oracle", proxy), ("indexed", repo)):
            path = tmp_path / f"{name}-{i}.sqlite3"
            inbox_id = enqueue_trade_payload(path, payload=incoming, source="push", repo=owner, broker_deal_key=identity)
            assert enqueue_trade_payload(path, payload=incoming, source="backfill", repo=owner, broker_deal_key=identity) == inbox_id
            row = read_trade_payload(path, inbox_id=inbox_id, read_only=True)
            results.append((row["status"], row["last_error"], row["receipt_recovery_allowed"]))
        assert results[0] == results[1]
        assert results[1][2] == 0
        assert results[1][0] == ("conflict" if i else "pending")
    assert seen and set(seen) == {1}


@pytest.mark.parametrize("stock", [False, True])
def test_indexed_legacy_candidates_preserve_existence_and_receipt_qualification(tmp_path, stock):
    from src.application.ledger.api import execution_identity_from_input
    from src.application.trades.inbox import _has_persisted_execution

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = _execution()
    stored = _candidate_event(repo, payload, stock=stock, event_id="legacy")
    table, key = ("assigned_stock_events", "stock_event_id") if stock else ("trade_events", "event_id")
    aliases = ["source_deal_id", "deal_id", "futu_deal_id", "external_execution_id", "dealID", "dealId", "id"]
    variants = [({}, False), ({"execution_id": None}, False), ({"execution_id": ""}, False),
                ({"execution_input": payload}, True)]
    variants += [({alias: payload["external_execution_id"]}, None) for alias in aliases]
    variants += [({"stock_settlement": {"source_event_id": payload["external_execution_id"]}}, None)] if not stock else []
    for field, value in (("external_account_id", "other"), ("environment", "SIMULATE"), ("broker_id", "other")):
        other = {**payload, "broker_account_ref": {**payload["broker_account_ref"], field: value}}
        variants.append(({"execution_input": other, "execution_id": execution_identity_from_input(other)}, False))
    other = {**payload, "external_id_namespace": "other.deals"}
    variants.append(({"execution_input": other, "execution_id": execution_identity_from_input(other)}, False))
    for i, (raw, expected) in enumerate(variants):
        event = {**stored, **raw} if stock else {**stored, "raw_payload": raw}
        if stock:
            event.pop("execution_input", None)
            event.pop("execution_id", None)
            event.update(raw)
        with repo._writer_connection(begin_immediate=True) as conn:
            conn.execute(f"UPDATE {table} SET event_json=? WHERE {key}=?", (json.dumps(event), "legacy"))
        proxy = SimpleNamespace(list_trade_events=repo.list_trade_events, list_assigned_stock_events=repo.list_assigned_stock_events)
        identity = execution_identity_from_input(payload)
        assert _has_persisted_execution(proxy, identity, payload) is expected
        assert _has_persisted_execution(repo, identity, payload) is expected
        path = tmp_path / f"inbox-{i}.sqlite3"
        inbox_id = enqueue_trade_payload(path, payload=payload, source="push", repo=repo, broker_deal_key=identity)
        assert read_trade_payload(path, inbox_id=inbox_id, read_only=True)["receipt_recovery_allowed"] == int(expected is False)


def _worker(root_text: str, mode: str, ready, release, results) -> None:
    import src.application.trades.auto_intake as intake

    root = Path(root_text)
    repo = SQLiteOptionPositionsRepository(root / "ledger.sqlite3")
    normalize = intake.normalize_trade_deal
    claim = intake.claim_trade_payload

    def short_claim(*args, **kwargs):
        return claim(*args, **{**kwargs, "lease_ms": 1})

    def normalize_with_boundary(*args, **kwargs):
        value = normalize(*args, **kwargs)
        if mode == "crash_before_commit":
            os._exit(81)
        if mode == "pause_before_commit":
            ready.set()
            if not release.wait(15):
                raise RuntimeError("test commit barrier timed out")
        return value

    def before_receipt(result):
        if mode == "crash_after_commit":
            os._exit(82)
        return result

    intake.normalize_trade_deal = normalize_with_boundary
    if mode.startswith("crash_"):
        intake.claim_trade_payload = short_claim
    try:
        result = _process(repo, root, "push", _execution(), source="push", before_receipt_fn=before_receipt)
        results.put({"result": result})
    except BaseException as exc:
        results.put({"error": type(exc).__name__, "message": str(exc)})


def _stop(process, release) -> None:
    release.set()
    process.join(10)
    if process.is_alive():
        process.terminate()
        process.join(5)


def test_execution_inbox_path_is_ledger_owned_and_protocol_repos_keep_requested_path(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    expected = tmp_path / "ledger.sqlite3.trade_intake_inbox.sqlite3"
    assert resolve_execution_inbox_path(repo, tmp_path / "a" / "inbox.sqlite3") == expected
    assert resolve_execution_inbox_path(SimpleNamespace(primary_repo=repo), tmp_path / "b" / "inbox.sqlite3") == expected
    other_repo = SQLiteOptionPositionsRepository(tmp_path / "other.sqlite3")
    assert resolve_execution_inbox_path(other_repo, tmp_path / "a" / "inbox.sqlite3") == tmp_path / "other.sqlite3.trade_intake_inbox.sqlite3"
    requested = tmp_path / "protocol.sqlite3"
    assert resolve_execution_inbox_path(SimpleNamespace(), requested) == requested


def test_same_stem_ledgers_have_independent_intake_completion(tmp_path: Path) -> None:
    first = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    second = SQLiteOptionPositionsRepository(tmp_path / "ledger.db")
    requested = tmp_path / "source" / "inbox.sqlite3"
    first_inbox = resolve_execution_inbox_path(first, requested)
    second_inbox = resolve_execution_inbox_path(second, requested)
    assert first_inbox != second_inbox
    for index, repo in enumerate((first, second)):
        result = _process(repo, tmp_path, str(index), _execution(), source="file")
        assert result["status"] == "applied"
        assert len(repo.list_trade_events()) == 1
        replay = _process(repo, tmp_path, f"retry-{index}", _execution(), source="file")
        assert replay["reason"] == "duplicate"
        assert len(repo.list_trade_events()) == 1
    assert first_inbox.exists() and second_inbox.exists()


@pytest.mark.parametrize("stage", ["normalize", "resolve"])
def test_transient_processing_exception_recovers_unchanged_once(
    tmp_path: Path,
    monkeypatch,
    stage: str,
) -> None:
    from src.application.trades import auto_intake

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = _execution()
    owner = "normalize_trade_deal" if stage == "normalize" else "resolve_trade_deal"
    original = getattr(auto_intake, owner)
    monkeypatch.setattr(
        auto_intake,
        owner,
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
    )

    first = _process(repo, tmp_path, "initial", payload, source="push")
    inbox = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    saved = read_trade_payload(inbox, inbox_id=first["inbox_id"], read_only=True)
    assert first["status"] == "failed"
    assert saved["status"] == "pending"
    assert saved["result"]["diagnostics"]["retryable"] is True
    assert [row["inbox_id"] for row in list_retryable_trade_payloads(
        inbox, retry_delay_sec=0
    )] == [first["inbox_id"]]
    assert repo.list_trade_events() == []

    monkeypatch.setattr(auto_intake, owner, original)
    assert resume_trade_payload(
        inbox,
        inbox_id=first["inbox_id"],
        operator="offline-recovery",
        repo=repo,
    )
    recovered = _process(
        repo,
        tmp_path,
        "recovery",
        payload,
        source="manual",
    )
    assert recovered["status"] == "applied"
    assert len(repo.list_trade_events()) == 1

    replay = _process(
        repo,
        tmp_path,
        "replay",
        payload,
        source="manual",
        retry_failed_deal=True,
    )
    assert replay["reason"] == "duplicate"
    assert len(repo.list_trade_events()) == 1


@pytest.mark.parametrize("requested_name", ["source/inbox.sqlite3", "ledger.sqlite3.trade_intake_inbox.sqlite3"])
def test_former_derived_inbox_cannot_be_abandoned_by_new_authority(tmp_path: Path, requested_name: str) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    former = repo.db_path.with_suffix(".trade_intake_inbox.sqlite3")
    payload = _execution()
    inbox_id = enqueue_trade_payload(former, payload=payload, source="push", repo=repo,
                                    broker_deal_key=broker_deal_key_from_payload(payload, account_mapping={"123": "lx"}))
    before = read_trade_payload(former, inbox_id=inbox_id, read_only=True)
    with pytest.raises(ValueError, match="legacy_inbox_migration_required"):
        resolve_execution_inbox_path(repo, tmp_path / requested_name)
    with pytest.raises(ValueError, match="legacy_inbox_migration_required"):
        _process(repo, tmp_path, "restart", payload, source="push")
    assert read_trade_payload(former, inbox_id=inbox_id, read_only=True) == before
    assert repo.list_trade_events() == []
    assert not (tmp_path / "ledger.sqlite3.trade_intake_inbox.sqlite3").exists()


@pytest.mark.parametrize("namespace", ["futu.deal", "verified.partition.deal"])
def test_explicit_open_recovers_after_commit_with_one_receipt(tmp_path: Path, monkeypatch, namespace: str) -> None:
    from src.application.trades import auto_intake, receipt

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = {**_execution(), "position_effect": "open", "side": "buy", "external_id_namespace": namespace}
    payload["instrument_ref"] = {**payload["instrument_ref"], "option_type": "call"}
    calls = []

    def sender(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "command_ok": True, "delivery_confirmed": True, "message_id": "offline-message"}

    monkeypatch.setattr(auto_intake, "send_trade_intake_receipt", partial(
        receipt.send_trade_intake_receipt, send_fn=sender, normalize_fn=lambda **kwargs: kwargs,
    ))
    callback = auto_intake._build_receipt_callback(
        base=tmp_path, cfg={"notifications": {"provider": "wechat_clawbot", "target": "wechat:offline-test"}},
        receipt_config={"enabled": True}, repo=repo,
    )

    class Crash(BaseException):
        pass

    def after_commit(_):
        raise Crash()

    with pytest.raises(Crash):
        _process(repo, tmp_path, "initial", payload, source="push", on_result_fn=callback, before_receipt_fn=after_commit)
    events, lots = repo.list_trade_events(), repo.list_position_lots()
    assert len(events) == 1 and events[0]["event_type"] == "open"
    assert events[0]["raw_payload"]["execution_input"]["position_effect"] == "open"
    assert calls == [] and not (tmp_path / "initial/state.json").exists()
    path = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    after_lease = time.time() + 121
    monkeypatch.setattr("src.application.trades.inbox.time.time", lambda: after_lease)
    pending = list_retryable_trade_payloads(path, retry_delay_sec=0)[0]
    stored = read_trade_payload(path, inbox_id=pending["inbox_id"], read_only=True)
    assert stored["result"] is None and stored["receipt"] is None
    assert resume_trade_payload(path, inbox_id=pending["inbox_id"], operator="offline-recovery", repo=repo)
    renamed = {**payload, "broker_account_ref": {**payload["broker_account_ref"], "account_label": "renamed"}}
    recovered = _process_payload(
        renamed, repo=repo, state_path=tmp_path / "recovery/state.json", audit_path=tmp_path / "recovery/audit.jsonl",
        account_mapping={"123": "renamed"}, futu_account_ids=["123"], apply_changes=True,
        host="127.0.0.1", port=11111, allow_external_lookup=False, source="push", on_result_fn=callback,
    )
    assert (recovered["status"], recovered["action"], recovered["reason"]) == ("applied", "open", "applied_open")
    assert recovered["receipt"]["delivery_confirmed"] is True
    assert len(calls) == 1
    assert read_trade_payload(path, inbox_id=pending["inbox_id"])["receipt"]["status"] == "sent"
    assert _process(repo, tmp_path, "again", payload, source="push", on_result_fn=callback)["reason"] == "duplicate"
    assert len(calls) == 1
    assert repo.list_trade_events() == events and repo.list_position_lots() == lots


def test_file_review_cannot_revoke_claim_during_economic_commit(tmp_path: Path, monkeypatch) -> None:
    from src.application.ledger.api import record_normalized_trade_event
    from src.application.trades import auto_intake
    from src.application.trades.file_intake import run_execution_file
    from src.application.trades.normalizer import normalize_trade_deal

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    inbox = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    payload = _execution()
    received, committing, finished = threading.Event(), threading.Event(), threading.Event()

    def pause_review(*args, **kwargs):
        received.set()
        assert committing.wait(5)
        mark_trade_payload_review(*args, **kwargs)
        finished.set()

    monkeypatch.setattr(auto_intake, "mark_trade_payload_review", pause_review)
    path = tmp_path / "input.jsonl"
    path.write_text(json.dumps({**payload, "data_type": "order_summary"}) + "\n")
    results = []
    worker = threading.Thread(target=lambda: results.append(run_execution_file(
        path, process_payload_fn=partial(
            _process_payload, repo=repo, state_path=tmp_path / "state.json",
            audit_path=tmp_path / "audit.jsonl", account_mapping={"123": "lx"},
            futu_account_ids=["123"], host="localhost", port=11111,
        ), configured_accounts=[payload["broker_account_ref"]], dry_run=False,
    )))
    worker.start()
    try:
        assert received.wait(5)
        inbox_id = enqueue_trade_payload(
            inbox, payload=payload, source="push", repo=repo,
            broker_deal_key=broker_deal_key_from_payload(payload, account_mapping={"123": "lx"}),
        )
        claim = claim_trade_payload(inbox, inbox_id=inbox_id, repo=repo)
        assert claim is not None
        with trade_payload_commit_scope(inbox, claim=claim, repo=repo):
            committing.set()
            assert not finished.wait(0.2)
            current = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
            assert current["status"] == "pending"
            assert current["claim_id"] == claim["claim_id"]
            record_normalized_trade_event(repo, normalize_trade_deal(payload))
        assert finished.wait(5)
    finally:
        committing.set()
        worker.join(5)
    assert not worker.is_alive()
    assert len(results) == 1
    assert len(repo.list_trade_events()) == 1
    assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)["status"] == "identity_needs_review"
    with pytest.raises(TradePayloadClaimLost):
        with trade_payload_commit_scope(inbox, claim=claim, repo=repo):
            pytest.fail("review completed before this second commit")


def test_saved_result_keeps_pm_intent_atomic_and_claimable_once(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    inbox = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    payload = _execution()
    inbox_id = enqueue_trade_payload(
        inbox, payload=payload, source="push", repo=repo,
        broker_deal_key=broker_deal_key_from_payload(payload, account_mapping={"123": "lx"}),
    )
    claim = claim_trade_payload(inbox, inbox_id=inbox_id, repo=repo)
    intent = {"account": "lx", "request_id": "stock-refresh:atomic"}
    result = {"status": "skipped", "reason": "not_option", "portfolio_refresh_intent": intent}
    enriched_result = save_trade_payload_result(inbox, claim=claim, result=result)
    current = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    assert current["result"] == enriched_result
    assert enriched_result["portfolio_refresh_intent"] == result["portfolio_refresh_intent"]
    assert json.loads(current["portfolio_refresh_intent_json"]) == intent
    assert current["portfolio_refresh_attempted_at_ms"] is None

    with pytest.raises(ValueError, match="portfolio refresh intent conflict"):
        save_trade_payload_result(inbox, claim=claim, result={
            "status": "changed", "portfolio_refresh_intent": {**intent, "request_id": "different"},
        })
    assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == current
    # A recovery result may omit the already-saved intent; it must remain available.
    save_trade_payload_result(inbox, claim=claim, result={"status": "skipped"})
    assert claim_trade_payload_refresh_intent(inbox, inbox_id=inbox_id) == intent
    assert claim_trade_payload_refresh_intent(inbox, inbox_id=inbox_id) is None


def test_new_known_associations_fence_old_claim_and_survive_original_payload_retry(tmp_path: Path, monkeypatch) -> None:
    from src.application.trades import auto_intake

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    inbox = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    initial = _execution()
    initial.update(side="buy", position_effect=None, external_order_id=None, external_order_namespace=None)
    initial["instrument_ref"]["option_type"] = "call"
    normalized, release = threading.Event(), threading.Event()
    normalize = auto_intake.normalize_trade_deal
    results = []
    failures = []

    def pause_normalize(*args, **kwargs):
        deal = normalize(*args, **kwargs)
        if threading.current_thread().name == "unknown-effect-writer":
            normalized.set()
            assert release.wait(5)
        return deal

    monkeypatch.setattr(auto_intake, "normalize_trade_deal", pause_normalize)
    def stale_worker():
        try:
            results.append(_process(repo, tmp_path, "push", initial, source="push"))
        except TradePayloadClaimLost as exc:
            failures.append(exc)

    worker = threading.Thread(name="unknown-effect-writer", target=stale_worker)
    worker.start()
    try:
        assert normalized.wait(5)
        key = broker_deal_key_from_payload(initial, account_mapping={"123": "lx"})
        inbox_id = enqueue_trade_payload(inbox, payload=initial, source="push", broker_deal_key=key, repo=repo)
        before = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
        assert before["claim_id"]
        for evidence in (
            {**initial, "position_effect": "close"},
            {**initial, "external_order_id": "late-order", "external_order_namespace": "futu.order"},
        ):
            assert enqueue_trade_payload(inbox, payload=evidence, source="backfill", broker_deal_key=key, repo=repo) == inbox_id
        enriched = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
        assert enriched["claim_id"] is None
        assert enriched["payload_version"] == before["payload_version"] + 2
        assert enriched["payload"]["position_effect"] is None
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert results == []
    assert len(failures) == 1
    assert repo.list_trade_events() == []
    after_due = time.time() + 61
    monkeypatch.setattr("src.application.trades.inbox.time.time", lambda: after_due)
    retry = _process(repo, tmp_path, "retry", initial, source="backfill")
    assert retry["status"] == "unresolved"
    assert repo.list_trade_events() == []
    audit = [json.loads(line) for line in (tmp_path / "retry" / "audit.jsonl").read_text().splitlines()]
    deal = next(row["deal"] for row in audit if row["phase"] == "normalized")
    assert deal["position_effect"] == "close"
    assert deal["order_id"] == "late-order"
    assert deal["execution_input"]["external_order_namespace"] == "futu.order"
    assert deal["raw_payload"]["position_effect"] is None


def test_nonempty_legacy_inbox_requires_migration_without_mutation(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    old = tmp_path / "legacy" / "inbox.sqlite3"
    key = broker_deal_key_from_payload(_execution(), account_mapping={"123": "lx"})
    inbox_id = enqueue_trade_payload(old, payload=_execution(), source="push", broker_deal_key=key, repo=repo)
    before = read_trade_payload(old, inbox_id=inbox_id, read_only=True)

    with pytest.raises(ValueError, match="legacy_inbox_migration_required"):
        resolve_execution_inbox_path(repo, old)

    assert read_trade_payload(old, inbox_id=inbox_id, read_only=True) == before
    assert not (tmp_path / "ledger.sqlite3.trade_intake_inbox.sqlite3").exists()


def test_empty_execution_inbox_keeps_legacy_lifecycle_control_in_place(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    old = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(old) as conn:
        conn.execute("CREATE TABLE trade_inbox (inbox_id TEXT)")
        conn.execute("CREATE TABLE lifecycle_control (source_id TEXT)")
        conn.execute("INSERT INTO lifecycle_control VALUES ('lx')")
    before = old.read_bytes()
    assert resolve_execution_inbox_path(repo, old) == tmp_path / "ledger.sqlite3.trade_intake_inbox.sqlite3"
    assert old.read_bytes() == before


def test_retry_scope_reads_past_foreign_backlog_without_starving_own_account(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.sqlite3"
    for index in range(105):
        payload = _execution(physical="456", deal_id=f"foreign-{index}")
        enqueue_trade_payload(inbox, payload=payload, source="backfill", broker_deal_key=broker_deal_key_from_payload(payload, account_mapping={"456": "sy"}))
    own = _execution()
    target = enqueue_trade_payload(inbox, payload=own, source="file", broker_deal_key=broker_deal_key_from_payload(own, account_mapping={"123": "lx"}))
    retry = list_retryable_trade_payloads(inbox, account_ids=["123"], limit=1, retry_delay_sec=0)
    assert [row["inbox_id"] for row in retry] == [target]
    assert list_retryable_trade_payloads(inbox, account_ids=[], limit=1) == []


def test_new_inbox_rejects_old_writer_mutations_and_preserves_evidence(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.sqlite3"
    payload = _execution()
    inbox_id = enqueue_trade_payload(
        inbox, payload=payload, source="push",
        broker_deal_key=broker_deal_key_from_payload(payload, account_mapping={"123": "lx"}),
    )
    original = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    with sqlite3.connect(inbox) as old_writer:
        for table in ("trade_inbox", "trade_inbox_evidence"):
            before = old_writer.execute(f"SELECT * FROM {table}").fetchall()
            assert len(before) == 1
            for statement in (
                f"UPDATE {table} SET payload_json = '{{}}'",
                f"DELETE FROM {table}",
                f"INSERT INTO {table} SELECT * FROM {table}",
            ):
                with pytest.raises(sqlite3.OperationalError, match="trade_inbox_writer_version"):
                    old_writer.execute(statement)
                old_writer.rollback()
                assert old_writer.execute(f"SELECT * FROM {table}").fetchall() == before
    assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == original


def test_claim_takeover_and_exhaustion_require_safe_explicit_recovery(tmp_path: Path, monkeypatch) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    inbox = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    payload = _execution()
    key = broker_deal_key_from_payload(payload, account_mapping={"123": "lx"})
    inbox_id = enqueue_trade_payload(inbox, payload=payload, source="push", broker_deal_key=key, repo=repo)
    now = [time.time()]
    monkeypatch.setattr("src.application.trades.inbox.time.time", lambda: now[0])
    old_claim = claim_trade_payload(inbox, inbox_id=inbox_id, repo=repo, lease_ms=1)
    assert old_claim is not None
    now[0] += 61
    current_claim = claim_trade_payload(inbox, inbox_id=inbox_id, repo=repo)
    assert current_claim is not None
    assert current_claim["claim_id"] != old_claim["claim_id"]
    with pytest.raises(TradePayloadClaimLost):
        with trade_payload_commit_scope(inbox, claim=old_claim, repo=repo):
            pytest.fail("a replaced claim must not reach economic commit")
    with pytest.raises(TradePayloadClaimLost):
        save_trade_payload_result(inbox, claim=old_claim, result={"status": "applied"})
    pending = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    assert pending["claim_id"] == current_claim["claim_id"]
    assert pending["result"] is None

    # The interrupted first claim already consumed one of the twenty attempts.
    for attempt in range(19):
        if attempt:
            now[0] += 61
            current_claim = claim_trade_payload(inbox, inbox_id=inbox_id, repo=repo)
        assert current_claim is not None
        mark_trade_payload_retryable(inbox, inbox_id=inbox_id, error="offline failure", claim=current_claim,
                                     result={"status": "failed", "diagnostics": {"retryable": True}})
    exhausted = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    assert exhausted["status"] == "handled"
    assert exhausted["result"]["receipt_kind"] == "manual_required"
    assert exhausted["attempt_count"] == 20
    assert claim_trade_payload(inbox, inbox_id=inbox_id, repo=repo) is None
    assert list_retryable_trade_payloads(inbox, retry_delay_sec=0) == []

    assert resume_trade_payload(inbox, inbox_id=inbox_id, operator="test-operator", repo=repo)
    resumed = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    assert resumed["attempt_count"] == 0
    assert resumed["last_error"] == "resumed_by:test-operator"
    assert [row["inbox_id"] for row in list_retryable_trade_payloads(inbox, retry_delay_sec=0)] == [inbox_id]
    resumed_claim = claim_trade_payload(inbox, inbox_id=inbox_id, repo=repo)
    assert resumed_claim is not None
    with trade_payload_commit_scope(inbox, claim=resumed_claim, repo=repo):
        pass

    assert enqueue_trade_payload(inbox, payload=_execution(price="3"), source="file", broker_deal_key=key, repo=repo) == inbox_id
    conflict = read_trade_payload(inbox, inbox_id=inbox_id, read_only=True)
    assert conflict["status"] == "conflict"
    assert not resume_trade_payload(inbox, inbox_id=inbox_id, operator="test-operator", repo=repo)
    assert read_trade_payload(inbox, inbox_id=inbox_id, read_only=True) == conflict
    assert claim_trade_payload(inbox, inbox_id=inbox_id, repo=repo) is None
    with pytest.raises(TradePayloadClaimLost):
        save_trade_payload_result(inbox, claim=resumed_claim, result={"status": "applied"})
    with sqlite3.connect(inbox) as conn:
        assert conn.execute("SELECT inbox_id, operator FROM trade_inbox_recovery").fetchall() == [(inbox_id, "test-operator")]
        assert conn.execute("SELECT COUNT(*) FROM trade_inbox_evidence").fetchone()[0] == 2
    assert repo.list_trade_events() == []


def test_conflict_from_another_entry_invalidates_claim_before_economic_commit(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    ctx = multiprocessing.get_context("spawn")
    ready, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
    process = ctx.Process(target=_worker, args=(str(tmp_path), "pause_before_commit", ready, release, results))
    process.start()
    try:
        assert ready.wait(10), "writer failed to reach the precommit barrier"
        second = _process(repo, tmp_path, "file", _execution(price="3"), source="file")
        release.set()
        process.join(10)
        assert process.exitcode == 0
        assert second["reason"] == "inbox_conflict"
        assert repo.list_trade_events() == []
        authoritative = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
        assert read_trade_payload(authoritative, inbox_id=second["inbox_id"])["status"] == "conflict"
    finally:
        _stop(process, release)


def test_conflict_after_economic_commit_preserves_original_event_across_entries(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    ctx = multiprocessing.get_context("spawn")
    ready, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
    process = ctx.Process(target=_worker, args=(str(tmp_path), "commit", ready, release, results))
    process.start()
    try:
        process.join(10)
        assert process.exitcode == 0
        first = results.get(timeout=2)
        assert first["result"]["status"] == "applied"
        original = repo.list_trade_events()
        assert len(original) == 1
        second = _process(repo, tmp_path, "file", _execution(price="3"), source="file")
        assert second["reason"] == "inbox_conflict"
        assert repo.list_trade_events() == original
    finally:
        _stop(process, release)


@pytest.mark.parametrize("phase,exit_code", [("before_commit", 81), ("after_commit", 82)])
def test_process_crash_recovers_expired_claim_from_another_entry(tmp_path: Path, monkeypatch, phase: str, exit_code: int) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    ctx = multiprocessing.get_context("spawn")
    ready, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
    process = ctx.Process(target=_worker, args=(str(tmp_path), f"crash_{phase}", ready, release, results))
    process.start()
    try:
        process.join(10)
        assert process.exitcode == exit_code
        original = repo.list_trade_events()
        assert len(original) == int(phase == "after_commit")
        authoritative = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
        after_due = time.time() + 121
        monkeypatch.setattr("src.application.trades.inbox.time.time", lambda: after_due)
        pending = list_retryable_trade_payloads(authoritative, account_ids=["123"], retry_delay_sec=0)
        assert len(pending) == 1
        interrupted = read_trade_payload(authoritative, inbox_id=pending[0]["inbox_id"])
        assert interrupted["result"] is None
        assert interrupted["claim_id"] is not None
        replay = _process(repo, tmp_path, "manual", _execution(), source="manual")
        final = repo.list_trade_events()
        assert len(final) == 1
        if original:
            assert final == original
        assert replay["inbox_id"] == interrupted["inbox_id"]
        saved = read_trade_payload(authoritative, inbox_id=replay["inbox_id"])
        assert saved["status"] == "handled"
        assert saved["result"] is not None
        assert saved["claim_id"] is None
    finally:
        _stop(process, release)


@pytest.mark.parametrize("failure", ["postcommit", "combo"])
def test_converged_recorded_receipt_projects_success_and_known_auxiliary_failure(tmp_path, monkeypatch, failure):
    from src.application.agent_tools.runtime_status_impl import _trade_intake_summary
    from src.application.trades import auto_intake, receipt
    from src.application.trades.state import load_trade_intake_state

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    calls = []
    def sender(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "delivery_confirmed": True, "message_id": "offline-message"}
    monkeypatch.setattr(auto_intake, "send_trade_intake_receipt", partial(
        receipt.send_trade_intake_receipt, send_fn=sender, normalize_fn=lambda send_result: send_result))
    callback = auto_intake._build_receipt_callback(
        base=tmp_path, repo=repo, receipt_config={"enabled": True},
        cfg={"notifications": {"provider": "wechat_clawbot", "target": "wechat:offline-test"}})
    resolver = auto_intake.resolve_trade_deal
    def commit_then_raise(*args, **kwargs):
        resolver(*args, **kwargs)
        raise sqlite3.OperationalError("locking protocol")
    def failed_combo():
        raise RuntimeError("private diagnostic /secret/path")
    if failure == "postcommit":
        monkeypatch.setattr(auto_intake, "resolve_trade_deal", commit_then_raise)
    result = _process(repo, tmp_path, "initial", _execution(), source="push", on_result_fn=callback,
        before_receipt_fn=lambda current: auto_intake._attach_combo_reconciliation_after_open(
            current, apply_changes=True, mode="active" if failure == "combo" else "off", reconcile_fn=failed_combo))
    state = load_trade_intake_state(tmp_path / "initial/state.json")
    key = broker_deal_key_from_payload(_execution(), account_mapping={"123": "lx"})
    assert result["status"] == "applied" and result["receipt_kind"] == "recorded"
    assert state["processed_deal_ids"][key]["status"] == "applied"
    assert not state["failed_deal_ids"] and not state["unresolved_deal_ids"]
    assert _trade_intake_summary(state, {})["pending_count"] == 0
    audit = [json.loads(line) for line in (tmp_path / "initial/audit.jsonl").read_text().splitlines()]
    assert not [item for item in audit if item["phase"] == "failed"]
    assert len(repo.list_trade_events()) == len(repo.list_position_lots()) == len(calls) == 1
    message = calls[0]["message"]
    assert "✅ 已记录" in message and "无需重复录入" in message
    if failure == "combo":
        assert result["combo_reconciliation"]["status"] == "failed"
        assert "组合核对未完成" in message and "请检查组合核对服务" in message
        assert "private diagnostic" not in message and "/secret/path" not in message
    else:
        assert state["processed_deal_ids"][key]["diagnostics"]["recovered_from_ledger"]
    stored = read_trade_payload(resolve_execution_inbox_path(repo, tmp_path / "unused"), inbox_id=result["inbox_id"])
    assert stored["receipt"]["message"] == message and stored["receipt"]["status"] == "sent"


@pytest.mark.parametrize("source", ["push", "history_backfill"])
def test_unknown_buy_call_entry_point_preserves_evidence_without_open(tmp_path, source):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = {**_execution(), "position_effect": None, "side": "buy"}
    payload["instrument_ref"] = {**payload["instrument_ref"], "option_type": "call"}
    result = _process(repo, tmp_path, source, payload, source=source)
    assert result["status"] == "unresolved"
    assert repo.list_trade_events() == []
    assert repo.list_position_lots() == []
    inbox = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    stored = read_trade_payload(inbox, inbox_id=result["inbox_id"], read_only=True)
    assert stored["result"]["reason"] == "unknown_position_effect"


@pytest.mark.parametrize("failure", ["normalize", "proof"])
@pytest.mark.parametrize("bad_first", [True, False])
def test_reconcile_bad_row_does_not_block_good_rows_or_complete_its_action(tmp_path, monkeypatch, failure, bad_first):
    from src.application.trades import auto_intake
    from src.application.trades.state import load_trade_intake_state, write_trade_intake_state

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _, first_inbox, key, source = _recorded_source_pending(repo, tmp_path)
    second = _process(repo, tmp_path, "push", _execution(deal_id="fill-2"))
    state = load_trade_intake_state(source["state_path"])
    second_key, second_entry = state["processed_deal_ids"].popitem()
    state["unresolved_deal_ids"][second_key] = second_entry
    write_trade_intake_state(source["state_path"], state)
    read_rows = auto_intake.read_trade_payloads_for_reconciliation
    normalize = auto_intake.normalize_trade_deal
    proof = auto_intake.completed_ledger_execution_events
    bad_deals = []

    def rows(*args, **kwargs):
        result = read_rows(*args, **kwargs)
        if key in kwargs["deal_ids"]:
            bad = {**result[0], "payload": {**result[0]["payload"], "_test_bad_row": True}}
            return [bad, *result] if bad_first else [*result, bad]
        return result

    def normalize_row(payload, **kwargs):
        if payload.get("_test_bad_row") and failure == "normalize":
            raise ValueError("malformed evidence")
        deal = normalize(payload, **kwargs)
        if payload.get("_test_bad_row"):
            bad_deals.append(deal)
        return deal

    def prove(events, deal):
        if any(deal is bad for bad in bad_deals):
            raise RuntimeError("economic evidence unavailable")
        return proof(events, deal)

    monkeypatch.setattr(auto_intake, "read_trade_payloads_for_reconciliation", rows)
    monkeypatch.setattr(auto_intake, "normalize_trade_deal", normalize_row)
    monkeypatch.setattr(auto_intake, "completed_ledger_execution_events", prove)
    before_events = repo.list_trade_events()
    result = auto_intake._reconcile_source_completion(source=source, repo=repo, apply_changes=True)
    assert result["inbox_updated_count"] == 2
    assert result["applied_deal_ids"] == [second_key]
    assert result["deferred"] == [{"deal_id": key, "reason": "inbox_row_evidence_error", "error":
        "ValueError: malformed evidence" if failure == "normalize" else "RuntimeError: economic evidence unavailable"}]
    saved = load_trade_intake_state(source["state_path"])
    assert key in saved["unresolved_deal_ids"] and key not in saved["processed_deal_ids"]
    assert second_key in saved["processed_deal_ids"]
    for inbox_id in (first_inbox, second["inbox_id"]):
        assert read_trade_payload(source["inbox_path"], inbox_id=inbox_id, read_only=True)["status"] == "handled"
    retry = auto_intake._reconcile_source_completion(source=source, repo=repo, apply_changes=True)
    assert retry["inbox_updated_count"] == retry["applied_count"] == 0
    assert repo.list_trade_events() == before_events
