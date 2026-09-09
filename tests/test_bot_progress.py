from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import replace

import pytest

from src.application.bot.contracts import AppEvent, AppResult, BotRequest, BotScope, new_id, utc_now_iso
from src.application.bot.host_store import BotHostStore
from src.application.bot.memory import scope_from_contract
from src.application.bot.service import prepare_contract


def contract(message="分析这条成交回执", sender="user", conversation="chat"):
    return prepare_contract(BotRequest(request_id=new_id("req"), source_entry="channel", user_message=message,
        explicit_scope=BotScope(config_key="hk"), execution_environment="channel",
        trusted_tool_scope={"authenticated_channel": "feishu", "authenticated_sender_id": sender,
                            "authenticated_conversation_id": conversation}))


def finish(store, run_id, status, **kwargs):
    if status == "answered":
        store.claim_admission_decision(run_id, "commit")
    return store.finish_run(AppResult(run_id=run_id, status=status, ok=status == "answered",
        user_response="回答", error=None if status == "answered" else {"code": "BUDGET_EXHAUSTED"}), **kwargs)


def test_progress_persists_without_model_commit_and_is_scoped(tmp_path):
    store = BotHostStore(tmp_path / "host.db")
    prepared = contract()
    store.start_run("run_original", contract=prepared, session_key="old")
    store.append_event(AppEvent(event_id="event", run_id="run_original", type="tool_result",
        timestamp=utc_now_iso(), payload={"ok": True, "tool_name": "receipt_read", "ref": "obv_current",
        "content_hash": "sha256:a", "as_of": "2026-09-09T06:15:54Z", "tool_input": {"account": "sy"}}))
    finish(store, "run_original", "failed")
    owner = scope_from_contract(contract(conversation="new"), ["sy"])
    rows = store.unfinished_progress(owner)
    assert len(rows) == 1 and rows[0]["evidence_refs"][0]["ref"] == "obv_current"
    assert store.unfinished_progress(scope_from_contract(contract(sender="other"), ["sy"])) == []
    assert store.unfinished_progress(scope_from_contract(prepared, [])) == []
    before = store.run_record("run_original")
    store.append_event(AppEvent(event_id="late", run_id="run_original", type="tool_result",
        timestamp=utc_now_iso(), payload={"ok": True}))
    assert store.run_record("run_original")["events_json"] == before["events_json"]


@pytest.mark.parametrize("repeat", range(3))
def test_answer_outbox_and_progress_resolution_are_one_transaction(tmp_path, repeat):
    store = BotHostStore(tmp_path / f"host{repeat}.db")
    prepared = contract()
    store.start_run("run_original", contract=prepared, session_key="one")
    finish(store, "run_original", "failed")
    store.start_run("run_linked", contract=contract("继续 run_original", conversation="two"),
                    session_key="two", resumed_from="run_original")
    resolution = {"progress_ref": "run_original", "expected_revision": 1,
                  "covered_goal": prepared.input["user_message"], "claim_indexes": [0]}
    reply = {"delivery_key": "feishu:command", "channel": "feishu", "payload": {"text": "回答", "route": "trusted"}}
    with store._connect() as conn:
        conn.execute("CREATE TRIGGER fail_reply BEFORE INSERT ON bot_reply_outbox BEGIN SELECT RAISE(ABORT, 'fixture'); END")
    with pytest.raises(sqlite3.IntegrityError):
        finish(store, "run_linked", "answered", progress_resolution=resolution, reply=reply, progress_scope=scope_from_contract(prepared))
    assert store.run_record("run_linked")["finished_at"] is None
    assert json.loads(store.run_record("run_original")["progress_json"])["resolved_by"] is None
    assert store.list_replies() == ()
    with store._connect() as conn:
        conn.execute("DROP TRIGGER fail_reply")
    finish(store, "run_linked", "answered", progress_resolution=resolution, reply=reply, progress_scope=scope_from_contract(prepared))
    assert store.run_record("run_original")["status"] == "failed"
    progress = json.loads(store.run_record("run_original")["progress_json"])
    assert progress["resolved_by"] == "run_linked" and progress["revision"] == 2
    assert store.list_replies()[0]["run_id"] == "run_linked"
    assert store.reply_payload("feishu:command") == reply["payload"]
    store.finish_run(AppResult(run_id="run_linked", status="answered", user_response="重复", ok=True), reply=reply)
    assert len(store.list_replies()) == 1


def test_stale_recovery_and_wrong_revision_do_not_close_progress(tmp_path):
    store = BotHostStore(tmp_path / "host.db")
    prepared = contract()
    store.start_run("run_stale", contract=prepared, session_key="old")
    with store._connect() as conn:
        conn.execute("UPDATE bot_runs SET started_at='2000-01-01T00:00:00+00:00' WHERE run_id='run_stale'")
    assert store.mark_stale_runs_interrupted() == 1
    assert json.loads(store.run_record("run_stale")["progress_json"])["goal"] == prepared.input["user_message"]
    store.start_run("run_new", contract=prepared, session_key="new", resumed_from="run_stale")
    finish(store, "run_new", "answered", progress_resolution={"progress_ref": "run_stale",
        "expected_revision": 2, "covered_goal": prepared.input["user_message"], "claim_indexes": [0]})
    assert json.loads(store.run_record("run_stale")["progress_json"])["resolved_by"] is None
    store.start_run("run_expired", contract=prepared, session_key="new")
    with pytest.raises(TimeoutError):
        finish(store, "run_expired", "answered", deadline_monotonic=time.monotonic() - 1,
               reply={"delivery_key":"expired","channel":"feishu","payload":{}})
    assert not store.reply_payload("expired")

def test_closure_requires_current_business_queries_not_memory_or_side_question():
    from src.application.bot.host import _progress_claims_cover
    progress = {"evidence_refs": [{"tool_name":"receipt_read", "query":{"account":"sy","deal_id":"7258806397173991645"}}]}
    claims = [{"observation_ids":["current"]}]
    assert not _progress_claims_cover(progress, claims, [0], {"current":{"tool_name":"bot_memory","evidence_kind":"memory_operation"}})
    assert not _progress_claims_cover(progress, claims, [0], {"current":{"tool_name":"runtime_status","query":{}}})
    assert not _progress_claims_cover(progress, claims, [0], {"current":{"tool_name":"receipt_read","query":{"account":"lx","deal_id":"other"}}})
    assert _progress_claims_cover(progress, claims, [0], {"current":{"tool_name":"receipt_read","coverage":{"status":"complete","complete_for":"point"},"query":{"account":"sy","deal_id":"7258806397173991645"}}})
