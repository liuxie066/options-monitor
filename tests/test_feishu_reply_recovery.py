from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest

from src.application.bot.host_store import BotHostStore
from src.application.channels.feishu_reply_renderer import render_feishu_conversation_reply
from src.application.inbound import feishu_ws
from src.application.inbound.feishu_ws import FeishuWsSettings


@pytest.fixture
def recovery(tmp_path, monkeypatch, example_config_path):
    from src.application.bot.config_scope import resolve_replay_authorization
    from src.application.bot.control.contracts import BotInboundRequest

    monkeypatch.setenv("OM_RUNTIME_ROOT", str(example_config_path.parent))
    # Explicit store and synthetic credentials; no SDK, model, or real transport.
    settings = FeishuWsSettings(audit_db=str(tmp_path / "host.sqlite3"),
                               config_key="us", allowed_senders="synthetic-user",
                               app_id="synthetic-app", app_secret="synthetic-secret")
    store = BotHostStore(settings.audit_db)
    envelope = render_feishu_conversation_reply(
        message_id="synthetic-inbound", text="合成恢复回复", reply_in_thread=False,
        max_chars=3500, render_route="bot",
    )
    authorization = resolve_replay_authorization(BotInboundRequest(text="", channel="feishu",
        sender_id="synthetic-user", message_id="synthetic-inbound", config_key="us",
        reply_context={"reply_sender_identity": settings.app_id}))
    store.enqueue_reply(delivery_key="feishu:synthetic", channel="feishu", payload=envelope,
                        authorization=authorization)
    monkeypatch.setattr(feishu_ws, "FEISHU_OUTBOX_RETRY_INTERVAL_SECONDS", 0.02, raising=False)
    return settings, store, envelope


def _wait(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("worker did not reach expected state")


def _worker(settings, reply):
    def forbidden(*args, **kwargs):
        raise AssertionError("recovery must not use inbound, model, or reaction")
    return feishu_ws._FeishuWsWorker(settings=settings, reply_fn=reply,
                                   reaction_fn=forbidden, execute_tool_fn=forbidden)


def _set_due(store):
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE bot_reply_outbox SET next_attempt_at = '2000-01-01T00:00:00+00:00'")


def _request(kwargs):
    return {key: value for key, value in kwargs.items() if key != "before_send"}


@pytest.mark.parametrize("restart", [False, True])
def test_worker_recovers_outage_without_inbound(recovery, restart):
    settings, store, envelope = recovery
    healthy = threading.Event()
    calls = []
    effects = {}

    def reply(**kwargs):
        calls.append(kwargs)
        if not healthy.is_set():
            raise feishu_ws.FeishuTransientError("synthetic outage")
        effects.setdefault(kwargs["uuid"], kwargs["content"])
        return {"code": 0, "data": {"message_id": "synthetic-receipt"}}

    worker = _worker(settings, reply)
    worker.start()
    try:
        _wait(lambda: store.list_replies()[0]["status"] == "retryable_failed")
        failed = store.list_replies()[0]
        assert failed["attempt_count"] == 1
        assert json.loads(failed["payload_json"]) == envelope
        assert datetime.fromisoformat(failed["next_attempt_at"]) > datetime.now(timezone.utc)
        healthy.set()
        # Healthy provider cannot bypass the stored 30-second backoff.
        time.sleep(0.15)
        assert len(calls) == 1
        if restart:
            worker.stop()
        _set_due(store)
        if restart:
            worker = _worker(settings, reply)
            worker.start()
        _wait(lambda: store.list_replies()[0]["status"] == "delivered")
    finally:
        worker.stop()
    delivered = store.list_replies()[0]
    assert delivered["attempt_count"] == 2
    assert delivered["payload_json"] == "{}"
    assert len(calls) == 2
    assert _request(calls[0]) == _request(calls[1])
    assert effects == {"feishu:synthetic": envelope["transport"]["content"]}


def test_worker_replays_unknown_outcome_without_second_fake_effect(recovery, tmp_path):
    settings, store, envelope = recovery
    calls = []
    provider_db = tmp_path / "fake-provider.sqlite3"
    with sqlite3.connect(provider_db) as conn:
        conn.execute("CREATE TABLE effects (uuid TEXT PRIMARY KEY, body TEXT NOT NULL)")
    def reply(**kwargs):
        calls.append(kwargs)
        with sqlite3.connect(provider_db) as conn:
            conn.execute("INSERT OR IGNORE INTO effects VALUES (?, ?)",
                         (kwargs["uuid"], json.dumps(kwargs["content"], sort_keys=True)))
        return {} if len(calls) == 1 else {"code": 0, "data": {"message_id": "same-receipt"}}
    worker = _worker(settings, reply)
    worker.start()
    try:
        _wait(lambda: store.list_replies()[0]["status"] == "retryable_failed")
        assert json.loads(store.list_replies()[0]["payload_json"]) == envelope
        _set_due(store)
        _wait(lambda: store.list_replies()[0]["status"] == "delivered")
    finally:
        worker.stop()
    assert len(calls) == 2 and _request(calls[0]) == _request(calls[1])
    with sqlite3.connect(provider_db) as conn:
        assert conn.execute("SELECT uuid, body FROM effects").fetchall() == [
            ("feishu:synthetic", json.dumps(envelope["transport"]["content"], sort_keys=True))]


def test_recovery_is_bound_to_store_and_feishu_channel(recovery, tmp_path):
    settings, store, envelope = recovery
    store.enqueue_reply(delivery_key="wechat:synthetic", channel="wechat", payload=envelope)
    other = BotHostStore(tmp_path / "other-host.sqlite3")
    other.enqueue_reply(delivery_key="feishu:synthetic", channel="feishu", payload=envelope)
    before = other.list_replies()
    calls = []
    worker = _worker(settings, lambda **kw: calls.append(kw) or
                     {"code": 0, "data": {"message_id": "synthetic-receipt"}})
    worker.start()
    try:
        _wait(lambda: any(r["status"] == "delivered" for r in store.list_replies()))
        time.sleep(0.15)
    finally:
        worker.stop()
    assert len(calls) == 1
    assert other.list_replies() == before
    wechat = next(r for r in store.list_replies() if r["channel"] == "wechat")
    assert wechat["attempt_count"] == 0 and wechat["status"] == "pending"


@pytest.mark.parametrize("status", ["pending", "retryable_failed", "delivering"])
def test_expired_capability_never_sends_even_after_lease_recovery(recovery, status):
    settings, store, _ = recovery
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE bot_reply_outbox SET status=?, created_at=?, updated_at=?",
                     (status, "2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00"))
    calls = []
    worker = _worker(settings, lambda **kw: calls.append(kw))
    worker.start()
    try:
        expected = "delivery_unknown" if status == "delivering" else "expired"
        _wait(lambda: store.list_replies()[0]["status"] == expected)
    finally:
        worker.stop()
    assert calls == []
    if status == "delivering":
        assert store.list_replies()[0]["payload_json"] != "{}"
    else:
        assert store.list_replies()[0]["payload_json"] == "{}"


@pytest.mark.parametrize("overrides,reason", [
    ({"reply_enabled": False}, "reply_disabled"),
    ({"app_secret": ""}, "missing_app_credentials"),
])
def test_disabled_recovery_never_claims_or_changes_payload(recovery, monkeypatch, overrides, reason):
    settings, store, _ = recovery
    before = store.list_replies()
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled recovery must not claim or send")
    monkeypatch.setattr(BotHostStore, "claim_reply", forbidden)
    disabled = replace(settings, **overrides)
    assert feishu_ws._retry_pending_feishu_reply(settings=disabled, reply_fn=forbidden)["reason"] == reason
    worker = _worker(disabled, forbidden)
    worker.start()
    time.sleep(0.25)
    worker.stop()
    assert store.list_replies() == before


@pytest.mark.parametrize("response", [
    {}, {"code": 0}, {"code": 0, "data": {}}, {"code": "230001"},
    {"code": "0", "data": {"message_id": "synthetic"}},
    {"code": False, "data": {"message_id": "synthetic"}},
    {"code": 0, "data": {"message_id": "  "}},
    {"code": 0, "data": {"message_id": 123}},
    {"code": 0, "data": {"client_msg_id": "feishu:synthetic"}},
    {"code": 0, "id": "synthetic"},
    {"code": 230001, "data": {"message_id": "synthetic"}},
])
@pytest.mark.parametrize("initial", [True, False])
def test_unconfirmed_response_retains_envelope_for_identical_replay(recovery, response, initial):
    settings, store, envelope = recovery
    calls = []
    def reply(**kwargs):
        calls.append(kwargs)
        return response if len(calls) == 1 else {"code": 0, "data": {"message_id": "confirmed"}}
    if initial:
        inbound = {"data": {"kind": "message", "request": {"message_id": "synthetic-inbound"},
                   "response_text": "合成恢复回复", "inbound_result": {"ok": True,
                   "command_id": "synthetic", "response_text": "合成恢复回复", "render_route": "bot"}},
                   "ok": True}
        out = feishu_ws._maybe_reply(inbound=inbound, settings=settings, reply_fn=reply)
    else:
        out = feishu_ws._retry_pending_feishu_reply(settings=settings, reply_fn=reply)
    assert out["ok"] is False
    assert not feishu_ws._reply_receipt_payload(data={}, reply_status=out).get("delivery_confirmed")
    failed = store.list_replies()[0]
    assert failed["status"] == "retryable_failed"
    assert json.loads(failed["payload_json"]) == envelope
    _set_due(store)
    assert feishu_ws._retry_pending_feishu_reply(settings=settings, reply_fn=reply)["ok"] is True
    assert _request(calls[0]) == _request(calls[1])
    assert store.list_replies()[0]["payload_json"] == "{}"


def test_competing_workers_and_active_lease_keep_one_effect(recovery):
    settings, store, _ = recovery
    active, release = threading.Event(), threading.Event()
    calls, effects = [], {}
    def reply(**kwargs):
        calls.append(kwargs)
        effects.setdefault(kwargs["uuid"], kwargs["content"])
        active.set()
        assert release.wait(3)
        return {"code": 0, "data": {"message_id": "synthetic-receipt"}}
    first, second = _worker(settings, reply), _worker(settings, reply)
    first.start()
    try:
        assert active.wait(2)
        second.start()
        time.sleep(0.25)
        assert store.list_replies()[0]["status"] == "delivering"
        assert len(calls) == 1
        second.stop()
        release.set()
        _wait(lambda: store.list_replies()[0]["status"] == "delivered")
    finally:
        release.set()
        first.stop()
        if second._thread.ident is not None:
            second.stop()
    assert len(effects) == 1
    assert store.list_replies()[0]["attempt_count"] == 1


def test_restart_recovers_crashed_lease_with_original_identity(recovery):
    settings, store, envelope = recovery
    assert store.claim_reply(channel="feishu")
    calls = []
    reply = lambda **kw: calls.append(kw) or {"code": 0, "data": {"message_id": "synthetic-receipt"}}
    assert feishu_ws._retry_pending_feishu_reply(settings=settings, reply_fn=reply)["attempted"] is False
    with sqlite3.connect(store.path) as conn:
        expired = datetime.now(timezone.utc) - timedelta(seconds=301)
        conn.execute("UPDATE bot_reply_outbox SET updated_at=?", (expired.isoformat(),))
    worker = _worker(settings, reply)
    worker.start()
    try:
        _wait(lambda: store.list_replies()[0]["status"] == "delivered")
    finally:
        worker.stop()
    assert calls[0]["uuid"] == "feishu:synthetic"
    assert calls[0]["content"] == envelope["transport"]["content"]
    assert store.list_replies()[0]["attempt_count"] == 2


@pytest.mark.parametrize("outcome", ["confirmed", "transient", "unknown"])
def test_stop_during_transport_settles_current_but_does_not_claim_next(recovery, monkeypatch, outcome):
    settings, store, envelope = recovery
    store.enqueue_reply(delivery_key="feishu:second", channel="feishu", payload=envelope,
        authorization=json.loads(store.list_replies()[0]["authorization_json"]))
    active, release = threading.Event(), threading.Event()
    calls = []
    def reply(**kw):
        calls.append(kw)
        active.set()
        assert release.wait(3)
        if outcome == "transient":
            raise feishu_ws.FeishuTransientError("synthetic failure during stop")
        if outcome == "unknown":
            return {}
        return {"code": 0, "data": {"message_id": "synthetic-receipt"}}
    worker = _worker(settings, reply)
    def forbidden(*args, **kwargs):
        raise AssertionError("queued inbound must not start after stop")
    monkeypatch.setattr(feishu_ws, "handle_feishu_ws_event", forbidden)
    worker.start()
    stopper = None
    try:
        assert active.wait(2)
        assert worker.submit({"synthetic": True}) == "accepted"
        stopper = threading.Thread(target=worker.stop)
        stopper.start()
        assert worker._stop_requested.wait(2)
        release.set()
        stopper.join(2)
        assert not stopper.is_alive()
    finally:
        release.set()
        worker.stop()
        if stopper:
            stopper.join(2)
    assert len(calls) == 1
    assert worker.submit({}) == "stopped"
    records = {row["delivery_key"]: row for row in store.list_replies()}
    expected = "delivered" if outcome == "confirmed" else "retryable_failed"
    assert records["feishu:synthetic"]["status"] == expected
    if outcome != "confirmed":
        assert json.loads(records["feishu:synthetic"]["payload_json"]) == envelope
    assert records["feishu:second"]["attempt_count"] == 0
    _set_due(store)
    restarted = _worker(settings, lambda **kw: {"code": 0, "data": {"message_id": "second"}})
    restarted.start()
    try:
        _wait(lambda: all(r["status"] == "delivered" for r in store.list_replies()))
    finally:
        restarted.stop()


def test_stop_between_claim_and_send_preserves_payload(recovery, monkeypatch):
    settings, store, envelope = recovery
    stopped = threading.Event()
    original = BotHostStore.claim_reply
    def claim(self, **kwargs):
        record = original(self, **kwargs)
        stopped.set()
        return record
    monkeypatch.setattr(BotHostStore, "claim_reply", claim)
    def forbidden(**kwargs):
        raise AssertionError("no transport after stop")
    out = feishu_ws._retry_pending_feishu_reply(settings=settings, reply_fn=forbidden, stop_requested=stopped)
    assert out["reason"] == "stopped"
    record = store.list_replies()[0]
    assert record["status"] == "retryable_failed"
    assert json.loads(record["payload_json"]) == envelope
    before = store.list_replies()
    feishu_ws._retry_pending_feishu_reply(settings=settings, reply_fn=forbidden, stop_requested=stopped)
    assert store.list_replies() == before


def test_idle_recovery_has_bounded_cadence(recovery, monkeypatch):
    settings, _, _ = recovery
    monkeypatch.setattr(feishu_ws, "FEISHU_OUTBOX_RETRY_INTERVAL_SECONDS", 1.0)
    entered = threading.Event()
    calls = []
    def retry(**kwargs):
        calls.append(time.monotonic())
        entered.set()
    monkeypatch.setattr(feishu_ws, "_retry_pending_feishu_reply", retry)
    worker = _worker(settings, None)
    worker.start()
    try:
        assert entered.wait(2)
        time.sleep(0.35)
    finally:
        worker.stop()
    assert len(calls) == 1


def test_recovery_errors_do_not_starve_inbound_or_kill_worker(recovery, monkeypatch):
    settings, _, _ = recovery
    recovered, handled = threading.Event(), threading.Event()
    def retry(**kwargs):
        recovered.set()
        raise RuntimeError("synthetic store failure")
    def handle(*args, **kwargs):
        handled.set()
        return {"ok": True}
    monkeypatch.setattr(feishu_ws, "_retry_pending_feishu_reply", retry)
    monkeypatch.setattr(feishu_ws, "handle_feishu_ws_event", handle)
    worker = _worker(settings, None)
    assert worker.submit({}) == "accepted"
    worker.start()
    try:
        assert recovered.wait(2)
        assert handled.wait(2)
        assert worker._thread.is_alive()
    finally:
        worker.stop()


def test_cli_no_reply_blocks_old_outbox_and_new_response(recovery, monkeypatch):
    import src.interfaces.cli.main as cli
    settings, store, _ = recovery
    before = store.list_replies()
    def build(**kwargs):
        assert kwargs["reply_enabled"] is False
        return replace(settings, reply_enabled=kwargs["reply_enabled"])
    def forbidden(*args, **kwargs):
        raise AssertionError("--no-reply must not claim or send")
    class Channel:
        def handle_inbound(self, *args, **kwargs):
            return {"ok": True, "data": {"kind": "message", "request": {"message_id": "new"},
                    "response_text": "合成新回复", "inbound_result": {"ok": True,
                    "command_id": "new", "response_text": "合成新回复"}}}
    def serve(disabled, **kwargs):
        result = feishu_ws.handle_feishu_ws_event({}, settings=disabled, reply_fn=forbidden,
                                                 reaction_fn=forbidden, channel_service=Channel())
        assert result["data"]["outbox_retry"]["reason"] == "reply_disabled"
        assert result["data"]["reply"]["attempted"] is False
    monkeypatch.setattr(BotHostStore, "claim_reply", forbidden)
    monkeypatch.setattr(cli, "build_feishu_ws_settings", build)
    monkeypatch.setattr(cli, "serve_feishu_ws", serve)
    assert cli.main(["inbound", "feishu-ws", "--no-reply", "--audit-db", settings.audit_db]) == 0
    assert store.list_replies() == before


@pytest.mark.parametrize("channel", ["feishu", "wechat"])
@pytest.mark.parametrize("state", ["processing", "expired", "incomplete"])
def test_claim_observations_do_not_occupy_final_outbox_identity(
    tmp_path, monkeypatch, example_config_path, channel, state,
):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from src.application.agent_tool_contracts import build_response
    from src.application.bot.control.audit import InboundAuditStore
    from src.application.bot.control.settings import BotSettings
    from src.application.channels.wechat_clawbot import inbound as wechat
    from src.application.inbound.feishu import handle_feishu_payload
    from tests.test_inbound_feishu_ws import _message_payload

    path = str(tmp_path / "observations.sqlite3")
    settings = FeishuWsSettings(config_path=str(example_config_path), audit_db=path,
        allowed_senders="feishu:ou_1", app_id="synthetic-app", app_secret="synthetic-secret")
    state_dir = str(tmp_path / "synthetic-wechat-state")
    message = {"from_user_id": "ou_1", "message_id": "observation", "context_token": "synthetic-context",
               "item_list": [{"type": 1, "text_item": {"text": "/status"}}]}
    event = _message_payload(text="/status", message_id="observation")
    started, release = threading.Event(), threading.Event()
    effects, sends = [], []
    counts = {"enqueue": 0, "claim": 0}
    enqueue, claim = BotHostStore.enqueue_reply, BotHostStore.claim_reply
    def enqueue_counted(self, *args, **kwargs):
        counts["enqueue"] += 1
        return enqueue(self, *args, **kwargs)
    def claim_counted(self, *args, **kwargs):
        counts["claim"] += 1
        return claim(self, *args, **kwargs)
    monkeypatch.setattr(BotHostStore, "enqueue_reply", enqueue_counted)
    monkeypatch.setattr(BotHostStore, "claim_reply", claim_counted)
    def tool(name, payload):
        effects.append("effect")
        started.set()
        assert release.wait(10)
        if state == "incomplete":
            raise RuntimeError("effect observed but final result lost")
        return build_response(tool_name=name, ok=True, data={"state": "final-business-result"})
    def receive():
        kwargs = dict(config_path=str(example_config_path), audit_db=path, allowed_senders=f"{channel}:ou_1",
                      execute_tool_fn=tool, bot_settings=BotSettings(enabled=True))
        if channel == "feishu":
            return handle_feishu_payload(event, bot_reply_options={"reply_sender_identity": settings.app_id}, **kwargs)
        return wechat.handle_wechat_clawbot_message(message, base=tmp_path, label="synthetic",
            state_dir=state_dir, **kwargs)
    def deliver(inbound):
        if channel == "feishu":
            def send(**kwargs):
                guard = kwargs.pop("before_send", None)
                if guard is not None:
                    guard()
                sends.append(kwargs)
                return {"code": 0, "data": {"message_id": "final-receipt"}}
            return feishu_ws._maybe_reply(inbound=inbound, settings=settings, reply_fn=send)
        def send(**kwargs):
            sends.append(kwargs)
            return {"ret": 0, "message_id": "final-receipt"}
        return wechat._maybe_reply(message=message, inbound=inbound,
            client=SimpleNamespace(send_text_message=send), reply_enabled=True, max_reply_chars=3500,
            audit_db=path, allowed_senders_fn=lambda: "wechat:ou_1", config_path=str(example_config_path),
            transport_identity=state_dir)
    store = BotHostStore(path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(receive)
        try:
            assert started.wait(10)
            observation = receive()
            assert observation["error"]["code"] == "INBOUND_PROCESSING"
            status = deliver(observation)
            assert status == {"attempted": False, "ok": True, "reason": "inbound_processing"}
            assert counts == {"enqueue": 0, "claim": 0} and sends == []
            assert not store.list_replies()
            if state == "expired":
                with sqlite3.connect(path) as conn:
                    conn.execute("UPDATE inbound_command_audit SET execution_expires_at_utc='2000-01-01T00:00:00+00:00'")
                observation = receive()
                assert observation["error"]["code"] == "INBOUND_INCOMPLETE"
                assert deliver(observation)["reason"] == "inbound_incomplete"
                assert not store.list_replies() and counts == {"enqueue": 0, "claim": 0}
                assert InboundAuditStore(path).list_recent()[0]["execution_state"] == "processing"
        finally:
            release.set()
        final = future.result(timeout=10)
    if state == "incomplete":
        assert not final["ok"] and final["error"]["code"] == "INBOUND_INCOMPLETE"
        assert deliver(final)["reason"] == "inbound_incomplete"
        assert receive()["error"]["code"] == "INBOUND_INCOMPLETE"
        assert sends == [] and not store.list_replies()
        assert counts == {"enqueue": 0, "claim": 0}
    else:
        assert final["ok"], final
        assert deliver(final)["ok"] is True
        rows = store.list_replies()
        assert len(rows) == 1 and rows[0]["status"] == "delivered"
        command_id = final["data"]["inbound_result"]["command_id"]
        assert rows[0]["delivery_key"] == f"{channel}:{command_id}"
        replay = receive()
        assert replay["meta"]["idempotent_replay"]
        assert deliver(replay)["reason"] == "idempotent_replay"
        assert len(sends) == 1
        assert "处理中" not in json.dumps(sends, ensure_ascii=False)
        assert "结果未确认" not in json.dumps(sends, ensure_ascii=False)
    assert effects == ["effect"]


@pytest.mark.parametrize(
    ("code", "enabled", "expected"),
    [("INBOUND_PROCESSING", True, "inbound_processing"),
     ("INBOUND_INCOMPLETE", True, "inbound_incomplete"),
     ("INBOUND_PROCESSING", False, "reply_disabled"),
     ("SYNTHETIC_FAILURE", True, "send"),
     ("PERMISSION_DENIED", True, "permission_denied")],
)
def test_observation_filter_preserves_other_shared_reply_decisions(code, enabled, expected):
    from src.application.channels.reply_decision import decide_inbound_reply
    error = {"code": code, "message": "sender is not allowed to use Bot control" if code == "PERMISSION_DENIED" else "known result"}
    inbound = {"ok": False, "data": {"kind": "message", "response_text": "visible response",
        "inbound_result": {"ok": False, "error": error}}}
    decision = decide_inbound_reply(inbound, reply_enabled=enabled, max_reply_chars=100)
    if expected == "send":
        assert decision.should_send and decision.text == "visible response"
    else:
        assert not decision.should_send and decision.status["reason"] == expected
    assert inbound["data"]["response_text"] == "visible response"
    assert inbound["data"]["inbound_result"]["error"]["code"] == code
