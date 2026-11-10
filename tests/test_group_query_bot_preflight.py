"""Offline S1 behavior via installed plugin admission; all effects are injected."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
from types import SimpleNamespace as NS

import pytest

from src.application.plugin_runtime import require_plugin
from src.application.secret_store import GROUP_QUERY_BOT_FEISHU_APP_SECRET
from src.infrastructure.secret_store.memory import InMemorySecretProvider

require_plugin("group-query-bot")
from om_group_query_bot.cli import run_preflight
from om_group_query_bot.config import load_preflight_settings
from om_group_query_bot.feishu import EventRejected, normalize_sdk_event, resolve_bot_identity
from om_group_query_bot.preflight import FIXED_REPLY, PreflightSession, PreflightWorker, run_live

NOW = datetime(2026, 11, 10, tzinfo=timezone.utc)


def config_file(tmp_path):
    raw = {"instance_id": "test-group", "data_dir": str(tmp_path / "data"),
           "feishu": {"app_id": "app_new", "app_secret_ref": GROUP_QUERY_BOT_FEISHU_APP_SECRET,
                      "allowed_chat_ids": ["chat_allowed"]}}
    path = tmp_path / "preflight.json"
    path.write_text(json.dumps(raw))
    return path, raw


def event(*, sender="sender_1", message="message_1", thread=None):
    return {"schema": "2.0", "header": {"app_id": "app_new", "tenant_key": "app_tenant",
            "event_type": "im.message.receive_v1", "event_id": "event_1"},
            "event": {"sender": {"sender_type": "user", "sender_id": {"open_id": sender},
                                   "tenant_key": "sender_tenant"},
                      "message": {"message_id": message, "chat_id": "chat_allowed", "chat_type": "group",
                                  "thread_id": thread, "message_type": "text",
                                  "content": "PRIVATE_INPUT_NOT_FOR_STORAGE",
                                  "mentions": [{"key": "@_user_1", "id": {"open_id": "bot_new"},
                                                "tenant_key": "app_tenant", "name": "PRIVATE_NAME"}]}}}


def ack(**kwargs):
    kwargs["before_send"]()
    return {"code": 0, "data": {"message_id": "platform_reply"}}


def session(tmp_path, *, reply=ack, clock=lambda: NOW):
    path, _ = config_file(tmp_path)
    return PreflightSession(load_preflight_settings(path), "bot_new", secret="LOCAL_TEST_ONLY",
                            reply_fn=reply, clock=clock)


def forbid(*args, **kwargs):
    raise AssertionError("unexpected external dependency")


def test_preview_uses_explicit_config_without_secret_network_or_writes(tmp_path, monkeypatch):
    import src.application.secret_store as secrets
    from src.interfaces.cli import main as cli
    monkeypatch.setattr(secrets, "resolve_secret", forbid)
    monkeypatch.setattr(cli, "command_environment", forbid)
    path, raw = config_file(tmp_path)
    result = run_preflight(path)
    assert result["status"] == "live_not_run" and result["live_g2_verified"] is False
    assert not Path(raw["data_dir"]).exists()
    assert cli.main(["group-query-bot", "preflight", "--config", str(path)], discover_local=True) == 0
    assert not Path(raw["data_dir"]).exists()


@pytest.mark.parametrize("case", ["unknown", "model", "query_api", "secret", "wrong_ref", "empty_app", "empty_chats", "duplicate_chats", "relative_dir", "missing"])
def test_strict_preflight_config_rejects_other_features_and_credentials(tmp_path, case):
    path, raw = config_file(tmp_path)
    if case in {"unknown", "model", "query_api"}:
        raw[case] = {}
    elif case == "secret":
        raw["feishu"]["app_secret"] = "LOCAL_TEST_ONLY"
    elif case == "wrong_ref":
        raw["feishu"]["app_secret_ref"] = "feishu.bot.app_secret"
    elif case == "empty_app":
        raw["feishu"]["app_id"] = " "
    elif case == "empty_chats":
        raw["feishu"]["allowed_chat_ids"] = []
    elif case == "duplicate_chats":
        raw["feishu"]["allowed_chat_ids"] *= 2
    elif case == "relative_dir":
        raw["data_dir"] = "data"
    else:
        del raw["instance_id"]
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        run_preflight(path)
    assert not (tmp_path / "data").exists()


def test_duplicate_json_fields_are_rejected(tmp_path):
    path, _ = config_file(tmp_path)
    path.write_text('{"instance_id":"one","instance_id":"two"}')
    with pytest.raises(ValueError, match="duplicate"):
        run_preflight(path)


def test_identity_lookup_is_fixed_and_uses_configured_application(tmp_path):
    path, _ = config_file(tmp_path)
    calls = []
    def transport(*args, **kwargs):
        calls.append((args, kwargs))
        return {"code": 0, "bot": {"open_id": "bot_new"}}
    settings = load_preflight_settings(path)
    assert resolve_bot_identity(settings, "LOCAL_TEST_ONLY", transport) == "bot_new"
    assert calls == [(("GET", "https://open.feishu.cn/open-apis/bot/v3/info"),
                      {"app_id": "app_new", "app_secret": "LOCAL_TEST_ONLY"})]
    for result in [{}, {"code": 0, "data": {"bot": {"open_id": "wrong_shape"}}},
                   {"code": True, "bot": {"open_id": "bot_new"}}, {"code": 1, "bot": {"open_id": "bot_new"}}]:
        with pytest.raises(ValueError):
            resolve_bot_identity(settings, "LOCAL_TEST_ONLY", lambda *a, **k: result)


@pytest.mark.parametrize("branch", ["schema", "event_type", "app", "chat", "chat_type", "tenant", "sender", "sender_type", "message_id", "mention", "text_only_mention", "nontext", "thread"])
def test_invalid_sdk_events_do_not_send_or_resolve_credentials(tmp_path, monkeypatch, branch):
    probe = session(tmp_path, reply=forbid)
    import src.application.secret_store as secrets
    monkeypatch.setattr(secrets, "resolve_secret", forbid)
    payload = event()
    header, sender, message = payload["header"], payload["event"]["sender"], payload["event"]["message"]
    if branch == "schema": payload["schema"] = "other"
    elif branch == "event_type": header["event_type"] = "other"
    elif branch == "app": header["app_id"] = "app_old"
    elif branch == "chat": message["chat_id"] = "chat_other"
    elif branch == "chat_type": message["chat_type"] = "p2p"
    elif branch == "tenant": header.pop("tenant_key")
    elif branch == "sender": sender["sender_id"].pop("open_id")
    elif branch == "sender_type": sender["sender_type"] = "app"
    elif branch == "message_id": message.pop("message_id")
    elif branch == "mention": message["mentions"][0]["id"]["open_id"] = "bot_old"
    elif branch == "text_only_mention": message["mentions"] = []; message["content"] = "@bot_new"
    elif branch == "nontext": message["message_type"] = "image"
    else: message["thread_id"] = {}
    assert probe.handle_event(payload)["status"] == "rejected"
    assert probe.store.counts(NOW) == {}


def test_header_tenant_is_scope_and_sender_tenant_may_be_absent():
    payload = event()
    payload["event"]["sender"].pop("tenant_key")
    normalized = normalize_sdk_event(payload, "app_new", "bot_new", ("chat_allowed",))
    assert normalized.tenant_key == "app_tenant"
    assert normalized.sender_tenant_key is None
    assert normalized.thread_id is None
    original = normalized.scope_sha256("instance_1")
    for name, value in [("app_id", "other_app"), ("tenant_key", "other_tenant"),
                        ("chat_id", "other_chat"), ("sender_open_id", "other_sender"), ("thread_id", "thread")]:
        assert replace(normalized, **{name: value}).scope_sha256("instance_1") != original
    assert normalized.scope_sha256("instance_2") != original


def test_sdk_adapter_preserves_real_mentions_without_names():
    from src.infrastructure.feishu_ws_client import feishu_event_model_to_payload
    sdk = NS(schema="2.0", header=NS(app_id="app_new", tenant_key="app_tenant", event_type="im.message.receive_v1"),
             event=NS(sender=NS(sender_type="user", sender_id=NS(open_id="sender_1")),
                      message=NS(message_id="message_1", chat_id="chat_allowed", chat_type="group", message_type="text",
                                 mentions=[NS(key="@_user_1", id=NS(open_id="bot_new", user_id="u1", union_id="union1"),
                                              tenant_key="app_tenant", name="PRIVATE_NAME")])) )
    payload = feishu_event_model_to_payload(sdk)
    assert payload["event"]["message"]["mentions"] == [{"key": "@_user_1", "id": {"open_id": "bot_new", "user_id": "u1", "union_id": "union1"}, "tenant_key": "app_tenant"}]
    assert normalize_sdk_event(payload, "app_new", "bot_new", ("chat_allowed",)).sender_open_id == "sender_1"


def test_fixed_reply_dedup_restart_and_sender_separation(tmp_path):
    calls = []
    def reply(**kwargs):
        calls.append(kwargs)
        return ack(**kwargs)
    probe = session(tmp_path, reply=reply)
    first = probe.handle_event(event())
    assert first["status"] == "acknowledged"
    assert probe.handle_event(event())["status"] == "duplicate"
    restarted = session(tmp_path, reply=reply)
    assert restarted.handle_event(event())["status"] == "duplicate"
    second = restarted.handle_event(event(sender="sender_2"))
    assert second["status"] == "acknowledged" and second["scope_sha256"] != first["scope_sha256"]
    assert len(calls) == 2
    assert {call["text"] for call in calls} == {FIXED_REPLY}
    assert {call["message_id"] for call in calls} == {"message_1"}
    assert calls[0]["uuid"] != calls[1]["uuid"]
    report = json.loads(restarted.evidence_path.read_text())
    sample = report["observations"][-1]["sample"]
    assert sample["message"]["mentions"][0]["id"]["open_id"] == report["bot_open_id_alias"]
    serialized = restarted.evidence_path.read_text()
    for sensitive in ["PRIVATE_INPUT_NOT_FOR_STORAGE", "PRIVATE_NAME", "LOCAL_TEST_ONLY", "sender_1", "sender_2"]:
        assert sensitive not in serialized
    with probe.store.connect() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM deliveries")]
    assert len(rows) == 2 and all(row["state"] == "acknowledged" for row in rows)
    assert not any("content" in key or "secret" in key for key in rows[0])
    assert all("PRIVATE_INPUT_NOT_FOR_STORAGE" not in str(row) for row in rows)


@pytest.mark.parametrize("mode,expected", [("timeout_after", "outcome_unknown"), ("timeout_before", "failed"),
                                           ("explicit_error", "failed"), ("missing_ack", "outcome_unknown")])
def test_failure_or_ambiguous_delivery_never_resends(tmp_path, mode, expected):
    calls = []
    def reply(**kwargs):
        calls.append(kwargs)
        if mode == "timeout_before": raise TimeoutError("PRIVATE_PROVIDER_DETAIL")
        kwargs["before_send"]()
        if mode == "timeout_after": raise TimeoutError("PRIVATE_PROVIDER_DETAIL")
        if mode == "explicit_error": return {"code": 999}
        return {"code": 0}
    probe = session(tmp_path, reply=reply)
    assert probe.handle_event(event())["status"] == expected
    assert probe.handle_event(event())["delivery_state"] == expected
    assert session(tmp_path, reply=reply).handle_event(event())["delivery_state"] == expected
    assert len(calls) == 1
    assert "PRIVATE_PROVIDER_DETAIL" not in probe.evidence_path.read_text()


def test_metadata_seven_day_expiry_does_not_change_at_24_hours(tmp_path):
    current = [NOW]
    probe = session(tmp_path, clock=lambda: current[0])
    probe.handle_event(event())
    with probe.store.connect() as db:
        before = dict(db.execute("SELECT * FROM deliveries").fetchone())
    current[0] += timedelta(hours=24)
    probe.write_evidence()
    assert json.loads(probe.evidence_path.read_text())["observations"] == []
    with probe.store.connect() as db:
        assert dict(db.execute("SELECT * FROM deliveries").fetchone()) == before
    assert probe.handle_event(event())["status"] == "duplicate"
    current[0] = NOW + timedelta(days=7)
    probe.write_evidence()
    assert probe.store.counts(current[0]) == {}


def test_queue_is_bounded_and_callback_does_not_wait_for_io(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def slow_reply(**kwargs):
        kwargs["before_send"]()
        entered.set()
        assert release.wait(5)
        return {"code": 0, "data": {"message_id": "reply"}}
    probe = session(tmp_path, reply=slow_reply)
    worker = PreflightWorker(probe, queue_size=1)
    try:
        assert worker.submit(event())
        assert entered.wait(2)
        assert worker.submit(event(message="message_2"))
        assert worker.submit(event(message="message_3")) is False
        assert probe.queue_rejections == 1
        release.set()
        worker.queue.join()
    finally:
        release.set()
        assert worker.close()
    assert probe.store.counts(NOW) == {"acknowledged": 2}


def test_live_facade_with_in_memory_credentials_and_fake_transports(tmp_path):
    path, _ = config_file(tmp_path)
    provider = InMemorySecretProvider({GROUP_QUERY_BOT_FEISHU_APP_SECRET: "LOCAL_TEST_ONLY"})
    sent = threading.Event()
    calls = []
    def reply(**kwargs):
        calls.append(kwargs)
        value = ack(**kwargs)
        sent.set()
        return value
    def start_ws(**kwargs):
        assert kwargs["app_id"] == "app_new" and kwargs["app_secret"] == "LOCAL_TEST_ONLY"
        assert kwargs["on_event"](event())
        assert sent.wait(2)
    result = run_preflight(path, confirm=True, secret_provider=provider,
                           identity_transport=lambda *a, **k: {"code": 0, "bot": {"open_id": "bot_new"}},
                           reply_fn=reply, start_ws_fn=start_ws, clock=lambda: NOW)
    assert result["status"] == "stopped" and result["live_g2_verified"] is False
    assert len(calls) == 1


def test_missing_dedicated_secret_does_not_fallback_or_call_transport(tmp_path):
    path, _ = config_file(tmp_path)
    provider = InMemorySecretProvider({"feishu.bot.app_secret": "LOCAL_TEST_ONLY"})
    with pytest.raises(ValueError, match="dedicated_feishu_credential_missing"):
        run_live(load_preflight_settings(path), secret_provider=provider,
                 identity_transport=forbid, reply_fn=forbid, start_ws_fn=forbid)
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("http_status,body,expected", [
    (200, {"code": 99991401}, "failed"),
    (403, {}, "failed"),
    (429, {"code": 99991400}, "failed"),
    (401, {"code": 99991663}, "failed"),
    (400, {"code": 230001}, "failed"),
    (503, {}, "outcome_unknown"),
    (503, {"code": 230001}, "outcome_unknown"),
])
def test_platform_rejections_through_real_reply_adapter(tmp_path, monkeypatch, http_status, body, expected):
    from src.infrastructure import feishu_bot
    from src.infrastructure.feishu_bitable import _classify_error
    monkeypatch.setattr(feishu_bot, "with_tenant_token_retry",
                        lambda app_id, secret, fn: fn("LOCAL_TOKEN_ONLY"))
    calls = []
    def http(method, url, payload, **kwargs):
        assert method == "POST" and url.endswith("/messages/message_1/reply")
        assert kwargs["retry_max_attempts"] == 1
        calls.append(payload)
        raise _classify_error(http_status=http_status, body_text=json.dumps(body), parsed=body, url=url)
    def reply(**kwargs):
        return feishu_bot.reply_message(**kwargs, http_json_fn=http)
    probe = session(tmp_path, reply=reply)
    assert probe.handle_event(event())["status"] == expected
    assert probe.store.counts(NOW) == {expected: 1}
    assert json.loads(probe.evidence_path.read_text())["deliveries"] == {expected: 1}
    assert probe.handle_event(event())["delivery_state"] == expected
    assert session(tmp_path, reply=reply).handle_event(event())["delivery_state"] == expected
    assert len(calls) == 1
