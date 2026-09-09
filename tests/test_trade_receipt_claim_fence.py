from functools import partial
from pathlib import Path
import threading
import time

import pytest

from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades import auto_intake, inbox, receipt
from src.application.trades.deal_identity import broker_deal_key_from_payload
from src.application.trades.inbox_authority import resolve_execution_inbox_path
from src.application.trades.order_fee_sync import recover_order_fee_targets



def _advance_retry_clock(monkeypatch, seconds=61):
    after_due = time.time() + seconds
    monkeypatch.setattr(inbox.time, "time", lambda: after_due)


def _execution():
    return {
        "schema_version": "trade_execution.v1",
        "broker_account_ref": {"broker_id": "futu", "external_account_id": "123", "environment": "REAL",
                               "broker_account_id": "futu:REAL:123", "account_label": "lx"},
        "instrument_ref": {"asset_type": "option", "market": "US", "symbol": "NVDA", "currency": "USD",
                           "option_type": "put", "strike": "100", "expiration_ymd": "2026-09-18", "multiplier": "100"},
        "external_id_namespace": "futu.deal", "external_execution_id": "fill-1",
        "external_order_id": None, "external_order_namespace": None,
        "side": "sell", "position_effect": "open", "quantity": "1", "price": "2.50",
        "currency": "USD", "occurred_at_utc": "2026-09-07T02:30:00Z",
    }


def _process(repo, root, entry, payload, callback, **kwargs):
    return auto_intake._process_payload(
        payload, repo=repo, state_path=root / entry / "state.json", audit_path=root / entry / "audit.jsonl",
        account_mapping={"123": "lx"}, futu_account_ids=["123"], apply_changes=True,
        host="127.0.0.1", port=11111, allow_external_lookup=False, source="push",
        on_result_fn=callback, **kwargs,
    )


def _receipt_callback(root, repo, monkeypatch, calls, *, before_send=None, notify_unresolved=True, notify_failed=True):
    def sender(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "command_ok": True, "delivery_confirmed": True, "message_id": "offline-message"}
    actual = partial(receipt.send_trade_intake_receipt, send_fn=sender, normalize_fn=lambda **kwargs: kwargs)
    def send(**kwargs):
        if before_send:
            before_send(kwargs)
        return actual(**kwargs)
    monkeypatch.setattr(auto_intake, "send_trade_intake_receipt", send)
    return auto_intake._build_receipt_callback(
        base=root, cfg={"notifications": {"provider": "wechat_clawbot", "target": "wechat:offline-test"}},
        receipt_config={"enabled": True, "notify_unresolved": notify_unresolved, "notify_failed": notify_failed}, repo=repo,
    )


def _start_worker(fn, errors):
    def run():
        try:
            fn()
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(name="stale-worker", target=run)
    worker.start()
    return worker


def test_two_valid_executions_do_not_replace_each_others_state(tmp_path, monkeypatch):
    from src.application.trades.state import load_trade_intake_state

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    initial_read = auto_intake.load_trade_intake_state
    both_loaded = threading.Barrier(2)
    results, errors = [], []

    def synchronized_initial_read(path):
        state = initial_read(path)
        both_loaded.wait(timeout=5)
        return state

    def run(payload):
        try:
            results.append(_process(repo, tmp_path, "shared", payload, None))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(auto_intake, "load_trade_intake_state", synchronized_initial_read)
    payloads = [_execution(), {**_execution(), "external_execution_id": "fill-2"}]
    workers = [threading.Thread(target=run, args=(payload,)) for payload in payloads]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert len(results) == len(repo.list_trade_events()) == 2
    state = load_trade_intake_state(tmp_path / "shared/state.json")
    assert len(state["processed_deal_ids"]) == 2
    authoritative = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    assert all(
        inbox.read_trade_payload(authoritative, inbox_id=result["inbox_id"])["status"] == "handled"
        for result in results
    )


@pytest.mark.parametrize("takeover", [False, True])
def test_normalize_failure_state_write_requires_current_claim(tmp_path, monkeypatch, takeover):
    from src.application.trades.state import load_trade_intake_state
    from tests.test_trade_receipt_recovery import _legacy_payload

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    path = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    payload = _legacy_payload()
    paused, release = threading.Event(), threading.Event()
    normalize = auto_intake.normalize_trade_deal
    calls, errors, results = [], [], []
    callback = _receipt_callback(tmp_path, repo, monkeypatch, calls)

    def interrupted_normalize(*args, **kwargs):
        deal = normalize(*args, **kwargs)
        if threading.current_thread().name == "stale-worker":
            paused.set()
            assert release.wait(5)
            raise OSError("offline metadata read interrupted")
        return deal

    monkeypatch.setattr(auto_intake, "normalize_trade_deal", interrupted_normalize)
    worker = _start_worker(
        lambda: results.append(_process(repo, tmp_path, "shared", payload, callback)), errors,
    )
    state_path = tmp_path / "shared/state.json"
    try:
        assert paused.wait(5)
        if takeover:
            _advance_retry_clock(monkeypatch)
            known = {**payload, "order_id": "late-order", "external_order_namespace": "futu.order"}
            completed = _process(repo, tmp_path, "shared", known, callback)
            assert completed["status"] == "applied" and completed["receipt"]["delivery_confirmed"] is True
            state_before = state_path.read_bytes()
            saved_before = inbox.read_trade_payload(path, inbox_id=completed["inbox_id"])
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert len(calls) == 1
    if takeover:
        assert results == []
        assert len(errors) == 1 and isinstance(errors[0], inbox.TradePayloadClaimLost)
        assert state_path.read_bytes() == state_before
        assert inbox.read_trade_payload(path, inbox_id=completed["inbox_id"]) == saved_before
        assert len(repo.list_trade_events()) == 1
    else:
        assert errors == []
        assert len(results) == 1 and results[0]["status"] == "failed"
        state = load_trade_intake_state(state_path)
        assert state["processed_deal_ids"] == {}
        failed = state["failed_deal_ids"][payload["deal_id"]]
        assert failed["reason"] == "exception:OSError"
        assert failed["receipt"]["delivery_confirmed"] is True
        saved = inbox.read_trade_payload(path, inbox_id=results[0]["inbox_id"])
        assert saved["status"] == "handled" and saved["result"]["status"] == "failed"
        assert saved["result"]["receipt_kind"] == "manual_required"
        assert saved["result"]["retry_policy"]["retryable"] is False
        assert saved["receipt"]["status"] == "sent"
        assert repo.list_trade_events() == []
        recovered = _process(
            repo,
            tmp_path,
            "recovery",
            payload,
            callback,
            retry_failed_deal=True,
        )
        assert recovered["status"] == "applied"
        assert len(repo.list_trade_events()) == 1
        assert len(calls) == 2
        current = inbox.read_trade_payload(path, inbox_id=results[0]["inbox_id"])
        assert current["receipt"]["receipt_kind"] == "recorded"
        assert current["receipt"]["status"] == "sent"
        assert current["receipt_envelope"]["receipts"]["manual_required"]["status"] == "sent"


def test_revoked_claim_exits_before_failed_state_or_receipt_and_new_worker_notifies(tmp_path, monkeypatch):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    path = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    payload = {**_execution(), "position_effect": None}
    paused, release = threading.Event(), threading.Event()
    normalize = auto_intake.normalize_trade_deal
    calls, errors = [], []
    callback = _receipt_callback(tmp_path, repo, monkeypatch, calls)
    def pause_normalize(*args, **kwargs):
        deal = normalize(*args, **kwargs)
        if threading.current_thread().name == "stale-worker":
            paused.set()
            assert release.wait(5)
        return deal
    monkeypatch.setattr(auto_intake, "normalize_trade_deal", pause_normalize)
    worker = _start_worker(lambda: _process(repo, tmp_path, "old", payload, callback), errors)
    try:
        assert paused.wait(5)
        key = broker_deal_key_from_payload(payload, account_mapping={"123": "lx"})
        inbox_id = inbox.enqueue_trade_payload(path, payload=payload, source="push", broker_deal_key=key, repo=repo)
        before = inbox.read_trade_payload(path, inbox_id=inbox_id)
        known = {**payload, "position_effect": "open", "external_order_id": "late-order", "external_order_namespace": "futu.order"}
        inbox.enqueue_trade_payload(path, payload=known, source="backfill", broker_deal_key=key, repo=repo)
        after = inbox.read_trade_payload(path, inbox_id=inbox_id)
        assert after["claim_id"] is None and after["payload_version"] > before["payload_version"]
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], inbox.TradePayloadClaimLost)
    assert calls == [] and repo.list_trade_events() == []
    assert not (tmp_path / "old/state.json").exists()
    assert inbox.read_trade_payload(path, inbox_id=inbox_id)["receipt"] is None
    _advance_retry_clock(monkeypatch)
    recovered = _process(repo, tmp_path, "new", payload, callback)
    assert recovered["status"] == "applied" and recovered["receipt"]["delivery_confirmed"] is True
    assert len(calls) == len(repo.list_trade_events()) == 1
    assert inbox.read_trade_payload(path, inbox_id=inbox_id)["receipt"]["status"] == "sent"


def test_takeover_between_saved_result_and_receipt_freeze_cannot_send_or_rewrite_state(tmp_path, monkeypatch):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    path = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    payload = _execution()
    paused, release = threading.Event(), threading.Event()
    calls, errors = [], []
    def before_send(kwargs):
        assert kwargs["inbox_claim"]["claim_id"]
        if threading.current_thread().name == "stale-worker":
            paused.set()
            assert release.wait(5)
    callback = _receipt_callback(tmp_path, repo, monkeypatch, calls, before_send=before_send)
    worker = _start_worker(lambda: _process(repo, tmp_path, "old", payload, callback), errors)
    try:
        assert paused.wait(5)
        key = broker_deal_key_from_payload(payload, account_mapping={"123": "lx"})
        inbox_id = inbox.enqueue_trade_payload(path, payload=payload, source="push", broker_deal_key=key, repo=repo)
        before = inbox.read_trade_payload(path, inbox_id=inbox_id)
        state_path = tmp_path / "old/state.json"
        state_before = state_path.read_bytes()
        assert before["result"]["status"] == "applied" and before["receipt"]["status"] == "pending"
        assert before["receipt"]["attempt_count"] == 0
        assert inbox.resume_trade_payload(path, inbox_id=inbox_id, operator="test-takeover", repo=repo)
        successor = inbox.claim_trade_payload(path, inbox_id=inbox_id, owner="new-worker", repo=repo)
        assert successor["claim_id"] != before["claim_id"]
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], inbox.TradePayloadClaimLost)
    assert state_path.read_bytes() == state_before
    assert calls == [] and inbox.read_trade_payload(path, inbox_id=inbox_id)["receipt"] == before["receipt"]
    assert inbox.resume_trade_payload(path, inbox_id=inbox_id, operator="test-process-successor", repo=repo)
    _advance_retry_clock(monkeypatch)
    recovered = _process(repo, tmp_path, "new", payload, callback)
    assert recovered["receipt"]["delivery_confirmed"] is True
    assert len(calls) == len(repo.list_trade_events()) == 1


def test_handled_association_enrichment_preserves_existing_receipt_without_new_send(tmp_path, monkeypatch):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = _execution()
    calls = []
    callback = _receipt_callback(tmp_path, repo, monkeypatch, calls)
    initial = _process(repo, tmp_path, "initial", payload, callback)
    assert initial["status"] == "applied" and len(calls) == 1
    path = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    frozen = inbox.read_trade_payload(path, inbox_id=initial["inbox_id"])["receipt"]
    known = {**payload, "external_order_id": "late-order", "external_order_namespace": "futu.order"}
    key = broker_deal_key_from_payload(known, account_mapping={"123": "lx"})
    inbox.enqueue_trade_payload(path, payload=known, source="backfill", broker_deal_key=key, repo=repo)
    pending = inbox.read_trade_payload(path, inbox_id=initial["inbox_id"])
    assert pending["status"] == "pending"
    _advance_retry_clock(monkeypatch)
    result = _process(repo, tmp_path, "enriched", payload, callback)
    assert result["receipt"]["reason"] == "execution_association_enrichment"
    assert len(calls) == len(repo.list_trade_events()) == 1
    assert inbox.read_trade_payload(path, inbox_id=initial["inbox_id"])["receipt"] == frozen
    raw = repo.list_trade_events()[0]["raw_payload"]
    assert raw["order_id"] == "late-order"
    assert raw["execution_input"]["external_order_id"] == "late-order"
    assert raw["execution_input"]["external_order_namespace"] == "futu.order"
    recovered = recover_order_fee_targets(repo, account="lx", allowed_futu_account_ids=["123"])
    assert recovered["targets"] == [("富途", "lx", "123", "late-order")]


@pytest.mark.parametrize("recovery_entry", ["initial", "other-source"])
def test_unresolved_execution_new_evidence_recovers_once_across_retry_and_state_paths(tmp_path, monkeypatch, recovery_entry):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    unknown = {**_execution(), "position_effect": None, "new_associations": True}
    calls = []
    callback = _receipt_callback(tmp_path, repo, monkeypatch, calls, notify_unresolved=False)
    first = _process(repo, tmp_path, "initial", unknown, callback)
    assert first["reason"] == "unknown_position_effect"
    assert calls == repo.list_trade_events() == []
    path = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    original_resolve = auto_intake.resolve_trade_deal
    def interrupt(*args, **kwargs):
        assert kwargs["retry_with_new_associations"] is True
        raise inbox.TradePayloadClaimLost("offline interruption before economic commit")
    monkeypatch.setattr(auto_intake, "resolve_trade_deal", interrupt)
    known = {**unknown, "position_effect": "open"}
    _advance_retry_clock(monkeypatch)
    with pytest.raises(inbox.TradePayloadClaimLost):
        _process(repo, tmp_path, recovery_entry, known, callback)
    pending = inbox.read_trade_payload(path, inbox_id=first["inbox_id"])
    assert pending["status"] == "pending" and pending["receipt_recovery_allowed"] == 1
    assert repo.list_trade_events() == []
    monkeypatch.setattr(auto_intake, "resolve_trade_deal", original_resolve)
    # Restart consumes the original unknown payload and the accepted durable evidence.
    _advance_retry_clock(monkeypatch, 121)
    recovered = _process(repo, tmp_path, recovery_entry, unknown, callback)
    assert recovered["status"] == "applied" and recovered["action"] == "open"
    assert recovered["receipt"]["delivery_confirmed"] is True
    frozen = inbox.read_trade_payload(path, inbox_id=first["inbox_id"])["receipt"]
    for entry, payload in (("initial", unknown), ("third-source", known)):
        _process(repo, tmp_path, entry, payload, callback)
    assert len(calls) == len(repo.list_trade_events()) == 1
    assert inbox.read_trade_payload(path, inbox_id=first["inbox_id"])["receipt"] == frozen
    _process(repo, tmp_path, "conflict", {**known, "price": "3.00"}, callback)
    assert inbox.read_trade_payload(path, inbox_id=first["inbox_id"])["status"] == "conflict"
    assert len(calls) == len(repo.list_trade_events()) == 1


def test_raw_new_association_flag_cannot_retry_old_unresolved_state(tmp_path):
    from src.application.trades.state import write_trade_intake_state

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = {**_execution(), "new_associations": True, "retry_with_new_associations": True}
    key = broker_deal_key_from_payload(payload, account_mapping={"123": "lx"})
    write_trade_intake_state(tmp_path / "initial/state.json", {
        "unresolved_deal_ids": {key: {"status": "unresolved", "reason": "unknown_position_effect", "retryable": False}},
    })
    result = _process(repo, tmp_path, "initial", payload, None)
    assert result["reason"] == "duplicate_deal_id"
    assert repo.list_trade_events() == []


def test_new_association_after_rolled_back_first_write_keeps_first_receipt_eligible(tmp_path, monkeypatch):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    calls = []
    callback = _receipt_callback(tmp_path, repo, monkeypatch, calls, notify_failed=False)
    upsert = repo.upsert_trade_event
    def rollback_after_insert(event, **kwargs):
        assert kwargs.get("conn") is not None
        upsert(event, **kwargs)
        raise RuntimeError("offline transaction failure after insert")
    monkeypatch.setattr(repo, "upsert_trade_event", rollback_after_insert)
    initial = _process(repo, tmp_path, "failed", _execution(), callback)
    assert initial["status"] == "failed"
    assert repo.list_trade_events() == [] and calls == []
    monkeypatch.setattr(repo, "upsert_trade_event", upsert)
    known = {**_execution(), "external_order_id": "late-order", "external_order_namespace": "futu.order"}
    _advance_retry_clock(monkeypatch)
    recovered = _process(repo, tmp_path, "failed", known, callback)
    assert recovered["status"] == "applied" and recovered["receipt"]["delivery_confirmed"] is True
    _process(repo, tmp_path, "another-source", known, callback)
    assert len(repo.list_trade_events()) == len(calls) == 1


@pytest.mark.parametrize("claim_enrichment", [False, True])
def test_pm_enrichment_suppression_consumes_internal_claim_only(tmp_path, monkeypatch, claim_enrichment):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = {**_execution(), "_trade_intake_association_enrichment": not claim_enrichment,
               "instrument_ref": {"asset_type": "stock", "market": "US", "symbol": "NVDA", "currency": "USD"}}
    claim_fn = auto_intake.claim_trade_payload
    def claim(*args, **kwargs):
        value = claim_fn(*args, **kwargs)
        return {**value, "association_enrichment": claim_enrichment}
    monkeypatch.setattr(auto_intake, "claim_trade_payload", claim)
    monkeypatch.setattr(auto_intake, "is_portfolio_management_enabled", lambda _: True)
    result = _process(repo, tmp_path, "stock", payload, None)
    assert ("portfolio_refresh_intent" in result) is (not claim_enrichment)
    path = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    saved = inbox.read_trade_payload(path, inbox_id=result["inbox_id"])
    assert (saved["portfolio_refresh_intent_json"] is not None) is (not claim_enrichment)
