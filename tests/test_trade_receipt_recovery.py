from dataclasses import replace
from functools import partial
import hashlib
import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.application.ledger.api import record_normalized_trade_event, record_trade_event_void
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades import auto_intake, inbox, receipt
from src.application.trades.deal_identity import broker_deal_key_from_payload, completed_ledger_execution_events
from src.application.trades.file_intake import run_execution_file
from src.application.trades.normalizer import normalize_trade_deal
from src.application.trades.state import load_trade_intake_state, write_trade_intake_state


_ACCOUNT = {"broker_id": "futu", "external_account_id": "123", "environment": "REAL",
            "broker_account_id": "futu:REAL:123", "account_label": "lx"}


def _payload(deal_id="opening", *, effect="open"):
    return {
        "schema_version": "trade_execution.v1", "broker_account_ref": dict(_ACCOUNT),
        "instrument_ref": {"asset_type": "option", "market": "US", "symbol": "NVDA",
                           "currency": "USD", "option_type": "put", "strike": "100",
                           "expiration_ymd": "2026-09-18", "multiplier": "100"},
        "external_id_namespace": "futu.deal", "external_execution_id": deal_id,
        "external_order_namespace": "futu.order", "external_order_id": f"order-{deal_id}",
        "side": "sell" if effect == "open" else "buy", "position_effect": effect,
        "quantity": "1", "price": "2.50", "currency": "USD",
        "occurred_at_utc": "2026-09-07T02:30:00Z" if effect == "open" else "2026-09-07T03:30:00Z",
    }


def _processor(tmp_path, repo, monkeypatch, sender, *, routed=True):
    cfg = {"notifications": {"provider": "wechat_clawbot", "target": "wechat:offline-test" if routed else ""}}
    monkeypatch.setattr(auto_intake, "send_trade_intake_receipt", partial(
        receipt.send_trade_intake_receipt, send_fn=sender, normalize_fn=lambda **kwargs: kwargs,
    ))
    callback = auto_intake._build_receipt_callback(
        base=tmp_path, cfg=cfg, receipt_config={"enabled": True}, repo=repo,
    )
    return partial(auto_intake._process_payload, repo=repo, state_path=tmp_path / "state.json",
                   audit_path=tmp_path / "audit.jsonl", account_mapping={"123": "lx"},
                   futu_account_ids=["123"], host="127.0.0.1", port=11111, on_result_fn=callback,
                   apply_changes=True, allow_external_lookup=False)


def _successful_sender(calls):
    def send(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "command_ok": True, "delivery_confirmed": True, "message_id": "offline-message"}
    return send


def _inbox_id(payload):
    key = broker_deal_key_from_payload(payload, account_mapping={"123": "lx"})
    return hashlib.sha256(key.encode()).hexdigest()


def _stored(tmp_path, payload):
    return inbox.read_trade_payload(tmp_path / "ledger.sqlite3.trade_intake_inbox.sqlite3", inbox_id=_inbox_id(payload))


def test_ledger_commit_before_state_crash_recovers_original_open_and_one_stable_receipt(tmp_path, monkeypatch):
    payload = _payload()
    calls = []
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    process = _processor(tmp_path, repo, monkeypatch, _successful_sender(calls))
    write_state = auto_intake.update_trade_intake_state_entries
    monkeypatch.setattr(auto_intake, "update_trade_intake_state_entries", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("state crash")))
    with pytest.raises(RuntimeError, match="state crash"):
        process(payload)
    before_events, before_lots = repo.list_trade_events(), repo.list_position_lots()
    assert len(before_events) == len(before_lots) == 1
    assert calls == []
    assert _stored(tmp_path, payload)["receipt"] is None
    monkeypatch.setattr(auto_intake, "update_trade_intake_state_entries", write_state)
    restarted = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    process = _processor(tmp_path, restarted, monkeypatch, _successful_sender(calls))
    recovered = process(payload)
    assert recovered["status"] == "applied"
    assert recovered["reason"] == "applied_open"
    assert recovered["receipt"]["delivery_confirmed"] is True
    assert restarted.list_trade_events() == before_events
    assert restarted.list_position_lots() == before_lots
    assert len(calls) == 1
    frozen = _stored(tmp_path, payload)["receipt"]
    assert frozen["receipt_id"] == f"trade-receipt:{_inbox_id(payload)}"
    assert frozen["message"] == calls[0]["message"]
    assert "权利金毛流入 USD 250.00" in frozen["message"]
    assert process(payload)["reason"] == "duplicate"
    assert _stored(tmp_path, payload)["receipt"] == frozen
    assert len(calls) == 1


def test_sender_process_exit_leaves_unknown_and_restart_never_resends(tmp_path, monkeypatch):
    payload = _payload()
    calls = []
    def terminated_sender(**kwargs):
        calls.append(kwargs)
        raise SystemExit("process terminated during send")
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    process = _processor(tmp_path, repo, monkeypatch, terminated_sender)
    with pytest.raises(SystemExit):
        process(payload)
    frozen = _stored(tmp_path, payload)["receipt"]
    assert frozen["status"] == "unknown"
    assert len(calls) == 1
    before = repo.list_trade_events(), repo.list_position_lots()
    resumed_time = time.time() + 121
    monkeypatch.setattr(inbox.time, "time", lambda: resumed_time)
    restarted = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    replay = _processor(tmp_path, restarted, monkeypatch, _successful_sender(calls))(payload)
    assert replay["receipt"]["reason"] == "durable_receipt_unknown"
    assert _stored(tmp_path, payload)["receipt"] == frozen
    assert (restarted.list_trade_events(), restarted.list_position_lots()) == before
    assert len(calls) == 1


def test_finish_failure_after_confirmed_send_remains_unknown_and_never_resends(tmp_path, monkeypatch):
    payload = _payload()
    calls = []
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    finish = inbox.finish_trade_receipt_attempt
    monkeypatch.setattr(inbox, "finish_trade_receipt_attempt", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("finish failed")))
    result = _processor(tmp_path, repo, monkeypatch, _successful_sender(calls))(payload)
    assert result["receipt"]["reason"] == "receipt_callback_exception"
    frozen = _stored(tmp_path, payload)["receipt"]
    assert frozen["status"] == "unknown"
    monkeypatch.setattr(inbox, "finish_trade_receipt_attempt", finish)
    restarted = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _processor(tmp_path, restarted, monkeypatch, _successful_sender(calls))(payload)
    assert _stored(tmp_path, payload)["receipt"] == frozen
    assert len(calls) == 1


@pytest.mark.parametrize("outcome", ["rejected", "no_route", "unconfirmed"])
def test_receipt_recovery_preserves_known_rejection_no_route_and_unknown_evidence(tmp_path, monkeypatch, outcome):
    calls = []
    def sender(**kwargs):
        calls.append(kwargs)
        return {"ok": False, "command_ok": outcome == "unconfirmed", "delivery_confirmed": False,
                "error_code": "SEND_UNCONFIRMED" if outcome == "unconfirmed" else "PROVIDER_REJECTED",
                "explicit_pre_acceptance_failure": outcome == "rejected",
                "returncode": 0 if outcome == "unconfirmed" else 1}
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    result = _processor(tmp_path, repo, monkeypatch, sender, routed=outcome != "no_route")(_payload())
    stored = _stored(tmp_path, _payload())
    if outcome == "no_route":
        assert result["receipt"]["reason"] == "skipped_no_route"
        assert stored["receipt"] is None
        assert stored["result"]["receipt"]["reason"] == "skipped_no_route"
        assert calls == []
    else:
        assert result["receipt"]["status"] == ("unconfirmed" if outcome == "unconfirmed" else "failed")
        assert stored["receipt"]["result"]["error_code"] == ("SEND_UNCONFIRMED" if outcome == "unconfirmed" else "PROVIDER_REJECTED")
        assert stored["receipt"]["status"] == ("unknown" if outcome == "unconfirmed" else "failed")
    restarted = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _processor(tmp_path, restarted, monkeypatch, _successful_sender(calls))(_payload())
    assert len(calls) == int(outcome != "no_route")


@pytest.mark.parametrize("live_first", [False, True])
def test_historical_close_and_existing_live_outbox_keep_one_receipt_owner(tmp_path, monkeypatch, live_first):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    record_normalized_trade_event(repo, normalize_trade_deal(_payload()))
    calls = []
    process = _processor(tmp_path, repo, monkeypatch, _successful_sender(calls))
    close = _payload("closing", effect="close")
    if live_first:
        first = process(close, source="push")
        assert first["receipt"]["status"] == "outbox_managed"
    outbox_before = repo.list_trade_lifecycle_notifications()
    assert len(outbox_before) == int(live_first)
    path = tmp_path / "closing.jsonl"
    path.write_text(json.dumps(close) + "\n", encoding="utf-8")
    for _ in range(2):
        run_execution_file(path, process_payload_fn=process, configured_accounts=[_ACCOUNT], dry_run=False)
    assert repo.list_trade_lifecycle_notifications() == outbox_before
    assert _stored(tmp_path, close)["receipt"] is None
    assert calls == []


def test_pre_receipt_inbox_migration_does_not_infer_permission_to_resend(tmp_path, monkeypatch):
    payload = _payload()
    path = tmp_path / "ledger.sqlite3.trade_intake_inbox.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE trade_inbox (
            inbox_id TEXT PRIMARY KEY, source TEXT NOT NULL, deal_id TEXT,
            broker_deal_key TEXT, payload_json TEXT NOT NULL, status TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0, received_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL, last_error TEXT, result_status TEXT, result_reason TEXT)""")
        conn.execute("""INSERT INTO trade_inbox
            (inbox_id, source, deal_id, broker_deal_key, payload_json, status, received_at_ms, updated_at_ms)
            VALUES (?, 'push', 'opening', ?, ?, 'pending', 1, 1)""",
            (_inbox_id(payload), broker_deal_key_from_payload(payload, account_mapping={"123": "lx"}), json.dumps(payload)))
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    record_normalized_trade_event(repo, normalize_trade_deal(payload))
    before = repo.list_trade_events(), repo.list_position_lots()
    calls = []
    result = _processor(tmp_path, repo, monkeypatch, _successful_sender(calls))(payload)
    stored = _stored(tmp_path, payload)
    assert stored["receipt_recovery_allowed"] == 0
    assert stored["receipt"] is None
    assert result["receipt"]["reason"] == "legacy_receipt_history_unproven"
    assert calls == []
    assert (repo.list_trade_events(), repo.list_position_lots()) == before


def _legacy_payload():
    return {
        "acc_id": "123", "broker_account_id": "futu:REAL:123", "environment": "REAL",
        "external_id_namespace": "futu.deal", "deal_id": "old-fill",
        "code": "US.NVDA260918P00100000", "qty": "1", "price": "2.50",
        "multiplier": "100", "trd_side": "SELL_SHORT", "create_time": "2026-09-07 10:30:00",
    }


@pytest.mark.parametrize("execution_metadata", [False, True])
@pytest.mark.parametrize("prior_receipt", ["unproven", "sent", "unknown"])
def test_existing_economic_effect_without_inbox_never_gains_first_receipt_permission(
    tmp_path, monkeypatch, execution_metadata, prior_receipt,
):
    payload = _legacy_payload()
    deal = normalize_trade_deal(payload, futu_account_mapping={"123": "lx"}, allow_opend_refresh=False)
    assert not deal.execution_input["errors"]
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    record_normalized_trade_event(repo, deal if execution_metadata else replace(deal, execution_input={}))
    events, lots = repo.list_trade_events(), repo.list_position_lots()
    assert len(events) == 1
    assert bool(events[0]["raw_payload"].get("execution_input")) is execution_metadata
    assert completed_ledger_execution_events(events, deal) == events
    old_key = "futu:lx:123:old-fill"
    if prior_receipt != "unproven":
        write_trade_intake_state(tmp_path / "state.json", {"processed_deal_ids": {
            old_key: {"status": "applied", "action": "open", "reason": "applied_open",
                      "account": "lx", "futu_account_id": "123", "source_deal_id": "old-fill",
                      "receipt": {"status": prior_receipt, "delivery_confirmed": prior_receipt == "sent",
                                  "message_id": "old-message" if prior_receipt == "sent" else None}},
        }})
    prior_state = load_trade_intake_state(tmp_path / "state.json")
    assert not (tmp_path / "ledger.sqlite3.trade_intake_inbox.sqlite3").exists()
    calls = []
    process = _processor(tmp_path, repo, monkeypatch, _successful_sender(calls))
    result = process(payload, source="backfill")
    stored = _stored(tmp_path, payload)
    assert result["reason"] == "ledger_recorded"
    assert result["receipt"]["reason"] == "legacy_receipt_history_unproven"
    assert stored["receipt_recovery_allowed"] == 0 and stored["receipt"] is None
    process(payload, source="push")
    assert _stored(tmp_path, payload)["receipt_recovery_allowed"] == 0
    assert calls == []
    assert (repo.list_trade_events(), repo.list_position_lots()) == (events, lots)
    assert load_trade_intake_state(tmp_path / "state.json")["processed_deal_ids"].get(old_key) == prior_state["processed_deal_ids"].get(old_key)


@pytest.mark.parametrize("missing_scope", ["environment", "external_id_namespace", "acc_id"])
def test_legacy_fill_with_incomplete_scope_cannot_prove_new_receipt_eligibility(tmp_path, missing_scope):
    payload = _legacy_payload()
    old = dict(payload)
    old.pop(missing_scope)
    old["account"] = "lx"
    # Do not recover physical scope from the account label or broker_account_id string.
    repo = SimpleNamespace(list_trade_events=lambda: [{"raw_payload": old}], list_assigned_stock_events=lambda: [])
    key = broker_deal_key_from_payload(payload, account_mapping={"123": "lx"})
    path = tmp_path / "inbox.sqlite3"
    inbox_id = inbox.enqueue_trade_payload(path, payload=payload, source="push", broker_deal_key=key, repo=repo)
    assert inbox.read_trade_payload(path, inbox_id=inbox_id)["receipt_recovery_allowed"] == 0


@pytest.mark.parametrize("execution_metadata", [False, True])
def test_voided_economic_effect_does_not_restore_first_receipt_permission(tmp_path, execution_metadata):
    payload = _legacy_payload()
    deal = normalize_trade_deal(payload, futu_account_mapping={"123": "lx"}, allow_opend_refresh=False)
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    record_normalized_trade_event(repo, deal if execution_metadata else replace(deal, execution_input={}))
    record_trade_event_void(repo, event_id=repo.list_trade_events()[0]["event_id"], reason="offline correction")
    before = repo.list_trade_events(), repo.list_position_lots()
    path = tmp_path / "inbox.sqlite3"
    inbox_id = inbox.enqueue_trade_payload(path, payload=payload, source="backfill", repo=repo,
                                         broker_deal_key=broker_deal_key_from_payload(payload, account_mapping={"123": "lx"}))
    assert inbox.read_trade_payload(path, inbox_id=inbox_id)["receipt_recovery_allowed"] == 0
    assert (repo.list_trade_events(), repo.list_position_lots()) == before


@pytest.mark.parametrize("field,value", [("acc_id", "456"), ("environment", "SIMULATE"),
                                         ("external_id_namespace", "futu.another-feed")])
@pytest.mark.parametrize("event_kind", ["option", "stock"])
def test_proven_other_execution_scope_does_not_block_first_receipt(tmp_path, field, value, event_kind):
    payload = _legacy_payload()
    old = {**payload, field: value}
    # Stored source aliases must be recognized without inventing an account mapping.
    old["source_deal_id"] = old.pop("deal_id")
    event = {"raw_payload": old} if event_kind == "option" else {**old, "stock_event_id": "stock-event"}
    repo = SimpleNamespace(list_trade_events=lambda: [event] if event_kind == "option" else [],
                           list_assigned_stock_events=lambda: [event] if event_kind == "stock" else [])
    key = broker_deal_key_from_payload(payload, account_mapping={"123": "lx"})
    path = tmp_path / "inbox.sqlite3"
    inbox_id = inbox.enqueue_trade_payload(path, payload=payload, source="push", broker_deal_key=key, repo=repo)
    assert inbox.read_trade_payload(path, inbox_id=inbox_id)["receipt_recovery_allowed"] == 1


@pytest.mark.parametrize("repo", [None, SimpleNamespace(list_trade_events=lambda: []), MagicMock(),
                                  SimpleNamespace(list_trade_events=lambda: None, list_assigned_stock_events=lambda: [])])
def test_missing_ledger_read_evidence_is_retained_without_receipt_permission(tmp_path, repo):
    payload = _payload()
    key = broker_deal_key_from_payload(payload, account_mapping={"123": "lx"})
    path = tmp_path / "inbox.sqlite3"
    inbox_id = inbox.enqueue_trade_payload(path, payload=payload, source="push", broker_deal_key=key, repo=repo)
    stored = inbox.read_trade_payload(path, inbox_id=inbox_id)
    assert stored["payload"] == payload and stored["receipt_recovery_allowed"] == 0
    ready_repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    inbox.enqueue_trade_payload(path, payload=payload, source="push", broker_deal_key=key, repo=ready_repo)
    assert inbox.read_trade_payload(path, inbox_id=inbox_id)["receipt_recovery_allowed"] == 0


def test_new_order_after_commit_crash_preserves_original_first_receipt_permission(tmp_path, monkeypatch):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    calls = []
    process = _processor(tmp_path, repo, monkeypatch, _successful_sender(calls))
    unknown_order = {**_payload(), "external_order_id": None, "external_order_namespace": None}

    def crash_after_commit(_result):
        raise SystemExit("offline crash after economic commit")

    with pytest.raises(SystemExit, match="offline crash"):
        process(unknown_order, before_receipt_fn=crash_after_commit)
    events, lots = repo.list_trade_events(), repo.list_position_lots()
    assert len(events) == 1 and calls == []
    assert _stored(tmp_path, unknown_order)["receipt_recovery_allowed"] == 1
    recovered = process(_payload(), source="backfill")
    assert recovered["reason"] == "applied_open" and recovered["receipt"]["delivery_confirmed"] is True
    stored = _stored(tmp_path, unknown_order)
    assert stored["receipt_recovery_allowed"] == 1 and stored["receipt"]["status"] == "sent"
    after = repo.list_trade_events()
    assert len(after) == 1 and after[0]["event_id"] == events[0]["event_id"]
    assert after[0]["raw_payload"]["order_id"] == _payload()["external_order_id"]
    assert after[0]["raw_payload"]["cash_conversions"] == events[0]["raw_payload"]["cash_conversions"]
    assert repo.list_position_lots() == lots
    process(unknown_order)
    assert len(calls) == 1
