"""Receipt acceptance through public Host and real bounded source readers; no provider."""
from __future__ import annotations

import json
import sqlite3

import pytest

from src.application.bot.contracts import AppResult, BotRequest, BotScope, new_id
from src.application.bot.host import run_contract
from src.application.bot.service import prepare_contract
from tests.bot_pi_test_support import _TEST_MODEL

DEAL_ID = "7258806397173991645"  # Sanitized historical fixture, never a live lookup.
BODY = "成交未记录：exception:OperationalError。保留的历史回执没有更具体的异常原因。"
ANSWER = "该成交的历史回执为：" + BODY


@pytest.fixture
def receipt_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    config = tmp_path / "config.hk.json"
    config.write_text(json.dumps({"accounts": ["sy"], "_generated": {"market": "hk", "source_format": "yaml"}}))
    state = tmp_path / "output_shared/state"
    state.mkdir(parents=True)
    ledger = state / "option_positions.sqlite3"
    with sqlite3.connect(ledger) as conn:
        for table, identity in (("trade_lifecycle_notification_outbox", "outbox_id"),
                                ("trade_lifecycle_notification_delivery_batches", "batch_id")):
            conn.execute(f"CREATE TABLE {table} ({identity} TEXT, payload_json TEXT, provider_receipt_json TEXT, created_at_ms INTEGER)")
    inbox = ledger.with_name(ledger.name + ".trade_intake_inbox.sqlite3")
    with sqlite3.connect(inbox) as conn:
        conn.execute("CREATE TABLE trade_inbox(inbox_id TEXT, deal_id TEXT, payload_json TEXT, result_json TEXT, receipt_json TEXT, received_at_ms INTEGER, updated_at_ms INTEGER)")
        for account, deal in (("sy", DEAL_ID), ("lx", "other-account-deal")):
            payload = {"broker_account_ref": {"account_label": account}, "instrument_ref": {"market": "HK", "symbol": "0700.HK"}}
            receipt = {"schema_version": 2, "current_result_key": "manual_required", "receipts": {
                "manual_required": {"receipt_id": "historical-" + account, "message": BODY if account == "sy" else "PRIVATE_OTHER_ACCOUNT",
                                    "business_result": {"reason": "exception:OperationalError"}, "status": "failed"}}}
            conn.execute("INSERT INTO trade_inbox VALUES (?,?,?,?,?,?,?)", (account, deal, json.dumps(payload), "{}", json.dumps(receipt), 1788934554000, 1788934555000))
    snapshots = {path: path.read_bytes() for path in (config, ledger, inbox)}
    yield config
    assert all(path.read_bytes() == original for path, original in snapshots.items())


def _contract(config, *, prior_reference=""):
    prepared = prepare_contract(BotRequest(
        request_id=new_id("receipt_test"), source_entry="test", user_message=f"查询成交 {DEAL_ID} 的历史回执，不推断当前故障原因。{prior_reference}",
        explicit_scope=BotScope(config_key="hk", config_path=str(config)),
    ), reference_year=2026)
    assert not isinstance(prepared, AppResult)
    return prepared


def _read(**filters):
    return {"call_id": new_id("read"), "tool_name": "receipt_read", "arguments": {"type": "trade", "deal_id": DEAL_ID, **filters}}


def _submit(ref, *, kind="historical_fact", scope="point", text=ANSWER):
    return {"call_id": new_id("submit"), "tool_name": "submit_answer", "arguments": {
        "mode": "evidence", "status": "complete", "answer_markdown": text,
        "claims": [{"text": text, "kind": kind, "observation_ids": [ref], "required_scope": scope}],
    }}


def _run(monkeypatch, contract, turns):
    """Replay model turns at Pi boundary, feeding each real tool reply into the next."""
    failures = []
    replies = []
    def process(start, *, on_tool_call, on_proposed, **kwargs):
        try:
            assert DEAL_ID in start["user_message"]
            for turn in turns:
                request = turn(tuple(replies))
                replies.append(on_tool_call(request))
            approved = replies[-1].get("approved_answer")
            if not approved:
                return {"ok": False, "error": {"code": "MODEL_ERROR", "stage": "model", "message": "fixture stopped after rejected answer"}}
            proposal = {"status": "answered", "text": approved["text"], "control_request": None, "termination_reason": "stop", "usage": {}}
            decision = on_proposed(proposal)
            return {"ok": True, "result": {**proposal, "committed": decision == "commit"}}
        except Exception as exc:
            failures.append(exc)
            raise
    monkeypatch.setattr("src.application.bot.host.run_pi_agent", process)
    result = run_contract(contract, model_settings=_TEST_MODEL)
    if failures:
        raise failures[0]
    return result, replies


def _historical_evidence(replies):
    observation = replies[-1]
    assert observation["ok"] is True, observation
    assert observation["freshness"]["status"] == "historical"
    assert observation["coverage"]["status"] == "complete"
    row = observation["value"]["rows"][0]
    assert row["deal_id"] == "..." + DEAL_ID[-4:] and row["receipt_body"] == BODY
    assert row["diagnostic_code"] == "exception:OperationalError"
    assert row["body_provenance"] == "frozen" and row["occurred_at"].startswith("2026-09-09")
    assert "PRIVATE_OTHER_ACCOUNT" not in json.dumps(observation)
    return observation["ref"]


@pytest.mark.parametrize("repeat", range(3))
def test_receipt_reaches_model_and_answer_cites_this_run(receipt_runtime, monkeypatch, repeat):
    result, replies = _run(monkeypatch, _contract(receipt_runtime), [
        lambda _: _read(), lambda replies: _submit(_historical_evidence(replies)),
    ])
    assert replies[-1]["observation"] == {"ok": True, "status": "answer_accepted"}
    assert result.ok and result.status == "answered"
    assert ANSWER in result.user_response and "数据库锁" not in result.user_response
    assert any(event.type == "tool_result" for event in result.events)


def test_historical_exception_cannot_claim_current_cause(receipt_runtime, monkeypatch):
    result, replies = _run(monkeypatch, _contract(receipt_runtime), [
        lambda _: _read(), lambda replies: _submit(_historical_evidence(replies), kind="current_fact", text="当前失败原因是数据库锁。"),
    ])
    assert replies[-1]["observation"]["reason"] == "claim_freshness_not_supported"
    assert not result.ok and result.error == {"code": "ANSWER_ADMISSION_FAILED"}
    assert "数据库锁" not in result.user_response


@pytest.mark.parametrize("filter", [{"account": "lx"}, {"market": "US"}, {"config_path": "/tmp/forged.json"}])
def test_receipt_scope_denial_is_not_usable_evidence(receipt_runtime, monkeypatch, filter):
    def denied(replies):
        observation = replies[-1]
        assert observation["ok"] is False
        assert "PRIVATE_OTHER_ACCOUNT" not in json.dumps(observation)
        return _submit(observation["ref"])
    result, replies = _run(monkeypatch, _contract(receipt_runtime), [lambda _: _read(**filter), denied])
    assert replies[-1]["observation"]["reason"] == "observation_outside_request"
    assert not result.ok


def test_prior_run_id_quoted_in_new_request_requires_new_read(receipt_runtime, monkeypatch):
    first, replies = _run(monkeypatch, _contract(receipt_runtime), [lambda _: _read(), lambda replies: _submit(_historical_evidence(replies))])
    assert first.ok
    old_ref = replies[0]["ref"]
    contract = _contract(receipt_runtime, prior_reference=f"上次答案引用 {old_ref}，请继续核实。")
    def reread(replies):
        assert replies[-1]["observation"]["reason"] == "observation_outside_request"
        return _read()
    def cite_new(replies):
        fresh_ref = _historical_evidence(replies)
        assert fresh_ref != old_ref
        return _submit(fresh_ref)
    result, replies = _run(monkeypatch, contract, [lambda _: _submit(old_ref), reread, cite_new])
    assert result.ok and replies[-1]["observation"]["status"] == "answer_accepted"


@pytest.mark.parametrize("read_count", [0, 1])
def test_budget_failure_returns_durable_read_progress(receipt_runtime, monkeypatch, tmp_path, read_count):
    from src.application.bot.host_store import BotHostStore

    def process(start, *, on_tool_call, on_event, **kwargs):
        if read_count:
            _historical_evidence([on_tool_call(_read())])
        on_event({"event_type": "forced_final_activated", "data": {"reason": "tool_call_limit"}})
        return {"ok": False, "error": {"code": "BUDGET_EXHAUSTED", "stage": "model", "message": "fixture budget"}}

    monkeypatch.setattr("src.application.bot.host.run_pi_agent", process)
    store = BotHostStore(tmp_path / "host.db")
    result = run_contract(_contract(receipt_runtime), model_settings=_TEST_MODEL, host_store=store)
    assert not result.ok and result.status == "failed"
    assert "达到本次查询次数上限" in result.user_response
    assert "未完成：" in result.user_response and f"继续 {result.run_id}" in result.user_response
    assert ("读取 1 份证据" if read_count else "尚未取得可用的业务证据") in result.user_response
    row = store.run_record(result.run_id)
    progress = json.loads(row["progress_json"])
    assert progress["completed_checks"]["read_count"] == read_count
    assert progress["completed_checks"]["partial_count"] == 0
    assert progress["termination_reason"] == "tool_call_limit"
    assert json.loads(row["response_json"])["user_response"] == result.user_response
    assert row["admission_state"] != "commit"


def test_complete_receipt_redaction_precedes_host_pagination(tmp_path, monkeypatch):
    from src.application.bot.host_store import BotHostStore
    from src.application.research.redaction import redact_text

    secret = "FIXTURE_SECRET_SHOULD_BE_REDACTED"
    body = "x" * 1793 + "\n" + "token=" + secret
    monkeypatch.setitem(receipt_runtime.__wrapped__.__globals__, "BODY", body)
    monkeypatch.setattr("src.application.agent_tools.receipts._cursor_key", lambda: "fixture-cursor-key")
    fixture = receipt_runtime.__wrapped__(tmp_path, monkeypatch)
    config = next(fixture)
    replies = []
    def process(start, *, on_tool_call, **kwargs):
        replies.append(on_tool_call(_read()))
        replies.append(on_tool_call(_read(cursor=replies[-1]["value"]["next_cursor"])))
        return {"ok": False, "error": {"code": "BUDGET_EXHAUSTED", "stage": "model", "message": "fixture stops after two reads"}}
    monkeypatch.setattr("src.application.bot.host.run_pi_agent", process)
    store = BotHostStore(tmp_path / "host.db")
    result = run_contract(_contract(config), model_settings=_TEST_MODEL, host_store=store)
    assert all(reply["ok"] for reply in replies), replies
    assert replies[0]["value"]["next_cursor"] and replies[1]["value"]["body_complete"]
    assert "".join(reply["value"]["rows"][0]["receipt_body"] for reply in replies) == redact_text(body)
    assert replies[1]["value"]["body_range"] == {"start": 1800, "end": len(redact_text(body)), "total": len(redact_text(body))}
    assert secret not in json.dumps(replies)
    assert secret not in str(result.events)
    assert secret not in json.dumps(store.run_record(result.run_id))
    with pytest.raises(StopIteration):
        next(fixture)  # Source fixture verifies original config/ledger/inbox bytes are unchanged.
