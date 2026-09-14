from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from src.application.assistant.audit import InboundAuditStore
from src.application.bot import channel_facade
from src.application.bot.host_store import BotHostStore
from src.application.inbound import feishu_ws
from src.application.inbound.feishu import prepare_feishu_analysis_control
from tests.bot_pi_test_support import ModelTurn, run_contract
from tests.test_inbound_feishu_ws import _message_payload


def _settings(tmp_path, config_path, **kwargs):
    return feishu_ws.FeishuWsSettings(config_path=str(config_path), audit_db=str(tmp_path / "inbound.sqlite3"),
        allowed_senders="feishu:ou_1,feishu:ou_2", app_id="test", app_secret="test", **kwargs)


def _preflight(payload, settings):
    return prepare_feishu_analysis_control(payload, allowed_senders=settings.allowed_senders,
        config_key=settings.config_key, config_path=settings.config_path, audit_db=settings.audit_db,
        received_monotonic=time.monotonic())


def _contract(settings, *, sender="ou_1", conversation=None):
    key, path, authority = channel_facade.resolve_trusted_config_scope(config_key=None, config_path=settings.config_path)
    request = channel_facade._channel_request(user_message="调查运行状态", config_key=key, config_path=path,
        request_id="original", context_messages=(), channel="feishu", sender_id=sender,
        authenticated_sender_id=sender, conversation_id=conversation, authority_scope=authority)
    from src.application.bot.service import prepare_contract
    contract = prepare_contract(request)
    session = channel_facade._channel_session_key(channel="feishu", sender_id=sender,
        conversation_id=conversation, authority_scope=authority)
    return contract, session


@pytest.mark.parametrize("text", ["再看一下", "为什么说取消分析", "‘取消分析’", '"停止分析"',
    "取消执行", "取消交易", "停止分析后会怎样", "不要取消分析", "> 取消分析"])
def test_only_explicit_analysis_control_is_recognized(text):
    assert channel_facade.analysis_control_replacement(text) is None


@pytest.mark.parametrize("replacement", [False, True])
def test_receiver_cancels_before_paused_model_and_queue_preserves_deadline(monkeypatch, tmp_path, example_config_path, replacement):
    settings = _settings(tmp_path, example_config_path)
    started, release, done = threading.Event(), threading.Event(), threading.Event()
    calls, results, deliveries, received, contracts = [], [], [], [], []
    monkeypatch.setattr(channel_facade, "_channel_model_gate", lambda path: None)

    def model(request):
        user = next(message["content"] for message in request.messages if message["role"] == "user")
        calls.append(user)
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
        return ModelTurn(text="晚到旧答案" if len(calls) == 1 else "新问题答案")

    def prepared(contract, **kwargs):
        contracts.append(contract)
        result = run_contract(contract, model_runner=model,
            host_store=kwargs["host_store"], session_key=kwargs["session_key"],
            reply_builder=kwargs.get("reply_builder"))
        results.append(result)
        return result

    monkeypatch.setattr(channel_facade, "run_prepared_contract", prepared)
    original_handler = feishu_ws.handle_feishu_ws_event

    def handler(payload, **kwargs):
        received.append((payload["event"]["message"]["message_id"], kwargs["received_monotonic"], time.monotonic()))
        out = original_handler(payload, **kwargs)
        if payload["event"]["message"]["message_id"] == "cancel":
            done.set()
        return out

    monkeypatch.setattr(feishu_ws, "handle_feishu_ws_event", handler)

    def client(**kwargs):
        on_event = kwargs["on_event"]
        on_event(_message_payload(text="调查运行状态", message_id="first"))
        assert started.wait(5)
        with sqlite3.connect(settings.audit_db) as conn:
            run_id = conn.execute("SELECT run_id FROM bot_runs").fetchone()[0]
        store = BotHostStore(settings.audit_db)
        assert _preflight(_message_payload(sender="ou_2", text="取消分析", message_id="other"), settings)["status"] == "no_active_run"
        for text in ("普通追问", '"取消分析"', "取消执行"):
            assert _preflight(_message_payload(text=text), settings) is None
        assert not store.is_cancel_requested(run_id)
        text = "取消当前分析，改为解释新问题" if replacement else "取消分析"
        before = time.monotonic()
        event = _message_payload(text=text, message_id="cancel")
        on_event(event)
        assert store.is_cancel_requested(run_id)
        assert not release.is_set()
        on_event(event)
        time.sleep(0.03)
        release.set()
        assert done.wait(5)
        cancel_received = next(row for row in received if row[0] == "cancel")
        assert before <= cancel_received[1] < cancel_received[2]
        assert cancel_received[2] - cancel_received[1] >= 0.02
        # Both deliveries may enter the worker; inbound dedup prevents a second run.
        assert [row[0] for row in received].count("cancel") >= 1

    try:
        feishu_ws.serve_feishu_ws(settings, start_client_fn=client,
            reply_fn=lambda **kwargs: deliveries.append(kwargs) or {"code": 0, "data": {"message_id": "reply"}},
            lock_path=tmp_path / "ws.lock")
    finally:
        release.set()
    assert [row[0] for row in received].count("cancel") == 2
    assert results[0].status == "cancelled"
    assert len(calls) == (2 if replacement else 1)
    if replacement:
        assert calls[1] == "解释新问题"
        expected_received = next(row[1] for row in received if row[0] == "cancel")
        assert contracts[1].received_monotonic == expected_received
        assert contracts[1].deadline_monotonic == expected_received + 180
    assert "晚到旧答案" not in json.dumps(deliveries, ensure_ascii=False)
    with sqlite3.connect(settings.audit_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM bot_reply_outbox WHERE run_id=?", (results[0].run_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM bot_session_runs").fetchone()[0] == 0


def test_cancel_message_dedup_keeps_exact_target_and_commit_winner(tmp_path, example_config_path):
    settings = _settings(tmp_path, example_config_path)
    contract, session = _contract(settings)
    store = BotHostStore(settings.audit_db)
    store.start_run("first", contract=contract, session_key=session)
    event = _message_payload(text="停止分析", message_id="cancel")
    first = _preflight(event, settings)
    assert first["status"] == "cancelled" and first["target_run_id"] == "first"
    with sqlite3.connect(settings.audit_db) as conn:
        conn.execute("UPDATE bot_runs SET status='cancelled' WHERE run_id='first'")
    store.start_run("second", contract=contract, session_key=session)
    duplicate = _preflight(event, settings)
    assert duplicate["duplicate"] and duplicate["target_run_id"] == "first"
    assert not store.is_cancel_requested("second")
    assert store.claim_admission_decision("second", "commit") == "commit"
    committed = _preflight(_message_payload(text="取消分析", message_id="another"), settings)
    assert committed["status"] == "completed"
    assert not store.is_cancel_requested("second")


def test_cancel_dedup_is_atomic_across_receivers(tmp_path, example_config_path):
    settings = _settings(tmp_path, example_config_path)
    contract, session = _contract(settings)
    store = BotHostStore(settings.audit_db)
    store.start_run("active", contract=contract, session_key=session)
    audit = InboundAuditStore(settings.audit_db)
    audit.find_by_message(channel="feishu", message_id="unused")
    event = _message_payload(text="取消分析", message_id="same")
    barrier = threading.Barrier(3)
    outcomes = []

    def receive():
        barrier.wait()
        outcomes.append(_preflight(event, settings))

    threads = [threading.Thread(target=receive) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    assert len(outcomes) == 2
    assert sorted(result["duplicate"] for result in outcomes) == [False, True]
    assert {result["target_run_id"] for result in outcomes} == {"active"}


def test_cancellation_wrong_identity_and_database_fail_closed(tmp_path, example_config_path):
    settings = _settings(tmp_path, example_config_path)
    contract, session = _contract(settings)
    store = BotHostStore(settings.audit_db)
    store.start_run("active", contract=contract, session_key=session)
    denied = _message_payload(sender="not-allowed", text="取消分析")
    from src.application.agent_tool_contracts import AgentToolError
    with pytest.raises(AgentToolError, match="not authorized"):
        _preflight(denied, settings)
    other_chat = _message_payload(text="取消分析", message_id="other-chat")
    other_chat["event"]["message"]["chat_id"] = "other-chat"
    assert _preflight(other_chat, settings)["status"] == "no_active_run"
    assert not store.is_cancel_requested("active")
    other_config = tmp_path / "other-config.us.json"
    other_config.write_bytes(example_config_path.read_bytes())
    other_settings = _settings(tmp_path, other_config)
    assert _preflight(_message_payload(text="取消分析", message_id="other-config"), other_settings)["status"] == "no_active_run"
    assert not store.is_cancel_requested("active")
    with sqlite3.connect(tmp_path / "other.sqlite3") as conn:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="same inbound database"):
            store.cancel_session_run(session, connection=conn, trusted_identity={})


def test_queue_full_still_cancels_without_claiming_replacement_was_queued(monkeypatch, tmp_path, example_config_path, caplog):
    settings = _settings(tmp_path, example_config_path, queue_size=1)
    contract, session = _contract(settings)
    store = BotHostStore(settings.audit_db)
    store.start_run("blocked", contract=contract, session_key=session)
    started, release = threading.Event(), threading.Event()
    handled = []

    def handler(payload, **kwargs):
        handled.append(payload["event"]["message"]["message_id"])
        started.set()
        assert release.wait(3)
        return {"ok": True}

    monkeypatch.setattr(feishu_ws, "handle_feishu_ws_event", handler)

    def client(**kwargs):
        on_event = kwargs["on_event"]
        on_event(_message_payload(text="调查", message_id="first"))
        assert started.wait(3)
        on_event(_message_payload(text="普通追问", message_id="queued"))
        on_event(_message_payload(text="取消当前分析，改为新问题", message_id="cancel-full"))
        assert store.is_cancel_requested("blocked")
        assert "business=queue_full" in caplog.text
        assert "cancel-full" not in handled
        release.set()

    try:
        feishu_ws.serve_feishu_ws(settings, start_client_fn=client, lock_path=tmp_path / "ws.lock")
    finally:
        release.set()


def test_commit_winner_reply_is_deterministic_and_never_retracted(monkeypatch, tmp_path, example_config_path):
    from src.application.assistant.inbound_service import handle_assistant_request
    from src.application.inbound.feishu import feishu_payload_to_inbound_request

    settings = _settings(tmp_path, example_config_path)
    contract, session = _contract(settings)
    store = BotHostStore(settings.audit_db)
    store.start_run("committed", contract=contract, session_key=session)
    assert store.claim_admission_decision("committed", "commit") == "commit"
    event = _message_payload(text="取消分析", message_id="late-cancel")
    assert _preflight(event, settings)["status"] == "completed"

    def unexpected(*args, **kwargs):
        raise AssertionError("pure cancellation must not invoke a model")

    monkeypatch.setattr("src.application.assistant.inbound_service._run_bot", unexpected)
    request = feishu_payload_to_inbound_request(event, config_path=settings.config_path, audit_db=settings.audit_db)
    response = handle_assistant_request(request, allowed_senders=settings.allowed_senders)
    assert response["ok"], response
    assert "已完成" in response["data"]["response_text"]
    assert "不会撤回" in response["data"]["response_text"]
    assert store.run_record("committed")["admission_state"] == "commit"


def test_processed_provider_message_cannot_be_reused_to_cancel(tmp_path, example_config_path):
    settings = _settings(tmp_path, example_config_path)
    contract, session = _contract(settings)
    store = BotHostStore(settings.audit_db)
    store.start_run("active", contract=contract, session_key=session)
    audit = InboundAuditStore(settings.audit_db)
    audit.record_result({"command_id": "previous", "channel": "feishu", "sender_id": "ou_1",
        "message_id": "previous", "raw_text": "之前的问题", "decision": "bot", "response": {"ok": True}})
    result = _preflight(_message_payload(text="取消分析", message_id="previous"), settings)
    assert result["duplicate"]
    assert not store.is_cancel_requested("active")


def test_failed_cancel_transaction_rolls_back_and_cannot_retry_from_queue(monkeypatch, tmp_path, example_config_path, caplog):
    settings = _settings(tmp_path, example_config_path)
    contract, session = _contract(settings)
    store = BotHostStore(settings.audit_db)
    store.start_run("active", contract=contract, session_key=session)
    original = BotHostStore.cancel_session_run
    handled = []

    def fail_after_cas(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("transaction interrupted")

    monkeypatch.setattr(BotHostStore, "cancel_session_run", fail_after_cas)
    monkeypatch.setattr(feishu_ws, "handle_feishu_ws_event", lambda *args, **kwargs: handled.append(args) or {"ok": True})
    feishu_ws.serve_feishu_ws(settings,
        start_client_fn=lambda **kwargs: kwargs["on_event"](_message_payload(text="取消分析", message_id="failed")),
        lock_path=tmp_path / "ws.lock")
    assert handled == []
    assert not store.is_cancel_requested("active")
    audit = InboundAuditStore(settings.audit_db)
    assert audit.find_by_message(channel="feishu:analysis_control", message_id="failed") is None
    assert "analysis control unavailable" in caplog.text


@pytest.mark.parametrize("interruption", ["queue_full", "after_cas"])
@pytest.mark.parametrize("replacement", [False, True])
def test_control_redelivery_completes_request_once_after_dispatch_loss(
    monkeypatch, tmp_path, example_config_path, interruption, replacement,
):
    from src.application.bot.contracts import AppResult

    settings = _settings(tmp_path, example_config_path, queue_size=1)
    contract, session = _contract(settings)
    store = BotHostStore(settings.audit_db)
    store.start_run("original", contract=contract, session_key=session)
    blocked, release, drained, processed = (threading.Event() for _ in range(4))
    model_calls, results, handled, deliveries = [], [], [], []
    text = "取消当前分析，改为解释新问题" if replacement else "取消分析"
    event = _message_payload(text=text, message_id="redelivered-control")
    monkeypatch.setattr(channel_facade, "_channel_model_gate", lambda path: None)

    def model(request):
        model_calls.append(next(message["content"] for message in request.messages if message["role"] == "user"))
        return ModelTurn(text="新问题答案")

    def prepared(contract, **kwargs):
        result = run_contract(contract, model_runner=model,
            host_store=kwargs["host_store"], session_key=kwargs["session_key"],
            reply_builder=kwargs.get("reply_builder"))
        results.append(result)
        return result

    monkeypatch.setattr(channel_facade, "run_prepared_contract", prepared)
    original_handler = feishu_ws.handle_feishu_ws_event

    def handler(payload, **kwargs):
        message_id = payload["event"]["message"]["message_id"]
        handled.append(message_id)
        if message_id == "blocker":
            blocked.set()
            assert release.wait(5)
            return {"ok": True}
        if message_id == "queued":
            drained.set()
            return {"ok": True}
        response = original_handler(payload, **kwargs)
        processed.set()
        return response

    monkeypatch.setattr(feishu_ws, "handle_feishu_ws_event", handler)

    def client(**kwargs):
        receive = kwargs["on_event"]
        if interruption == "queue_full":
            receive(_message_payload(text="调查", message_id="blocker"))
            assert blocked.wait(5)
            receive(_message_payload(text="普通追问", message_id="queued"))
            receive(event)
            assert "redelivered-control" not in handled
        else:
            # Simulate exit after the committed CAS, before the in-memory enqueue.
            first = _preflight(event, settings)
            assert not first["duplicate"] and first["target_run_id"] == "original"
        assert store.is_cancel_requested("original")
        audit = InboundAuditStore(settings.audit_db)
        assert audit.find_by_message(channel="feishu", message_id="redelivered-control") is None
        store.finish_run(AppResult(run_id="original", status="cancelled", ok=False,
            user_response="取消", error={"code": "CANCELLED"}))
        if interruption == "queue_full":
            release.set()
            assert drained.wait(5)
        receive(event)
        assert processed.wait(5)
        assert audit.find_by_message(channel="feishu", message_id="redelivered-control") is not None
        # A later active run must never become the duplicate message's new target.
        store.start_run("later-active", contract=contract, session_key=session)
        processed.clear()
        receive(event)
        assert processed.wait(5)
        replay = _preflight(event, settings)
        assert replay["duplicate"] and replay["target_run_id"] == "original"
        assert not store.is_cancel_requested("later-active")

    try:
        feishu_ws.serve_feishu_ws(settings, start_client_fn=client,
            reply_fn=lambda **kwargs: deliveries.append(kwargs) or {"code": 0, "data": {"message_id": "reply"}},
            lock_path=tmp_path / "ws.lock")
    finally:
        release.set()
    assert handled.count("redelivered-control") == 2
    assert model_calls == (["解释新问题"] if replacement else [])
    assert len(results) == int(replacement)
    assert len(deliveries) == 1
    if replacement:
        assert results[0].status == "answered"
        assert store.run_record(results[0].run_id)["status"] == "answered"
    else:
        assert "已请求取消" in json.dumps(deliveries, ensure_ascii=False)


@pytest.mark.parametrize("phase", ["prepare", "resolve"])
def test_analysis_control_deadline_rolls_back_before_commit(
    monkeypatch, tmp_path, example_config_path, phase,
):
    from types import SimpleNamespace

    from src.application.agent_tool_contracts import AgentToolError
    from src.application.assistant import audit as audit_module

    settings = _settings(tmp_path, example_config_path)
    contract, session = _contract(settings)
    store = BotHostStore(settings.audit_db)
    store.start_run("active", contract=contract, session_key=session)
    clock = [time.monotonic()]
    audit = InboundAuditStore(settings.audit_db, deadline_monotonic=clock[0] + 1)
    original_prepare = audit._ensure_schema
    original_resolve = store.cancel_session_run
    resolved = []
    monkeypatch.setattr(audit_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def prepare():
        original_prepare()
        if phase == "prepare":
            clock[0] += 2

    def resolve(connection):
        resolved.append(True)
        identity = {key: str(contract.input.get(key) or "") for key in (
            "authenticated_channel", "authenticated_sender_id", "authenticated_conversation_id",
            "authority_scope", "config_path")}
        result = original_resolve(session, connection=connection, trusted_identity=identity)
        assert result["status"] == "cancelled"
        clock[0] += 2
        return result

    monkeypatch.setattr(audit, "_ensure_schema", prepare)
    with pytest.raises(AgentToolError) as error:
        audit.record_analysis_control_once(channel="feishu", sender_id="ou_1",
            conversation_id="feishu:ou_1", message_id="expired-control", text="取消分析",
            scope=session, resolve=resolve)
    assert error.value.code == "BUDGET_EXHAUSTED"
    assert len(resolved) == (1 if phase == "resolve" else 0)
    assert not store.is_cancel_requested("active")
    assert store.run_record("active")["admission_state"] == "open"
    assert InboundAuditStore(settings.audit_db).find_by_message(
        channel="feishu:analysis_control", message_id="expired-control") is None
