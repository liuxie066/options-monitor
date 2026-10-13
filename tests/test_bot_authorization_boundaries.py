"""Synthetic persistence/facade regressions for scoped cache and reply authorization."""
from __future__ import annotations

import json
import socket
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.bot.config_scope import resolve_replay_authorization
from src.application.bot.control.audit import InboundAuditStore
from src.application.bot.control.contracts import BotInboundRequest
from src.application.bot.control.policy import ReplyAuthorizationError
from src.application.bot.control.runtime import handle_bot_turn
from src.application.bot.control.settings import BotSettings
from src.application.bot.host_store import BotHostStore
from src.application.channels.feishu_reply_renderer import render_feishu_conversation_reply
from src.application.channels.wechat_clawbot import inbound as wechat
from src.application.inbound import feishu_ws


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("synthetic tests must not open network connections")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setenv("OM_ENV_FILE", "")


@pytest.fixture
def scoped(tmp_path, monkeypatch, example_config_path):
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(example_config_path.parent))
    request = BotInboundRequest(text="/status", sender_id="synthetic-sender", channel="feishu",
        message_id="synthetic-message", conversation_id="synthetic-chat", config_key="us",
        audit_db=str(tmp_path / "audit.sqlite3"))
    calls = []
    def tool(name, payload):
        calls.append((name, payload))
        return build_response(tool_name=name, ok=True, data={"private": "synthetic-private"})
    return request, tool, calls


def _turn(request, tool, *, store=None):
    return handle_bot_turn(request, execute_tool_fn=tool, audit_store=store,
        allowed_senders=f"{request.channel}:{request.sender_id}", settings=BotSettings(enabled=True))


def _audit(path):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM inbound_command_audit ORDER BY id LIMIT 1").fetchone())


@pytest.mark.parametrize("change", ["conversation", "config", "account", "generation", "legacy"])
def test_cached_result_requires_unchanged_trusted_scope(scoped, tmp_path, change):
    request, tool, calls = scoped
    if change == "generation":
        bot = tmp_path / "config.bot.json"
        bot.write_text(json.dumps({"bot": {"enabled": False}}))
        request = replace(request, bot_config_path=str(bot))
    assert _turn(request, tool).ok
    before = _audit(request.audit_db)
    assert _turn(request, tool).meta["idempotent_replay"]
    if change == "conversation":
        altered = replace(request, conversation_id="different-chat")
    elif change == "config":
        clone = tmp_path / "other" / "config.us.json"
        clone.parent.mkdir()
        clone.write_bytes((tmp_path / "config.us.json").read_bytes())
        altered = replace(request, config_key=None, config_path=str(clone))
    elif change == "account":
        path = tmp_path / "config.us.json"
        cfg = json.loads(path.read_text())
        cfg["accounts"] = [*cfg["accounts"], "synthetic_other"]
        path.write_text(json.dumps(cfg))
        assert resolve_replay_authorization(request)["scope"] != json.loads(before["replay_authorization_json"])["scope"]
        altered = request
    elif change == "generation":
        bot.write_text(json.dumps({"bot": {"enabled": False, "read_markets": ["us"]}}))
        altered = request
    else:
        with sqlite3.connect(request.audit_db) as conn:
            conn.execute("UPDATE inbound_command_audit SET replay_authorization_json=NULL")
        altered = request
    rejected = _turn(altered, tool)
    assert rejected.error["code"] == "CHANNEL_NOT_READY"
    assert "synthetic-private" not in json.dumps(rejected.public_payload())
    assert len(calls) == 1
    assert _audit(request.audit_db)["response_json"] == before["response_json"]


def test_cache_generation_is_stable_and_paths_are_canonical(scoped, tmp_path):
    request, _, _ = scoped
    path = tmp_path / "config.us.json"
    alias = tmp_path / "alias.json"
    alias.symlink_to(path)
    first = resolve_replay_authorization(replace(request, config_key=None, config_path=str(path)))
    same = resolve_replay_authorization(replace(request, config_key=None, config_path=str(alias)))
    assert first == same == resolve_replay_authorization(replace(request, config_key=None, config_path=str(path)))


def test_lookup_failure_cannot_replace_success_and_restore_replays(scoped, monkeypatch):
    request, tool, calls = scoped
    assert _turn(request, tool).ok
    before = _audit(request.audit_db)
    store = InboundAuditStore(request.audit_db)
    def unavailable(**kwargs):
        raise AgentToolError(code="CONFIG_ERROR", message="synthetic lookup unavailable")
    with monkeypatch.context() as patch:
        patch.setattr(store, "find_by_message", unavailable)
        failure = _turn(request, tool, store=store)
    assert failure.error["code"] == "CONFIG_ERROR"
    assert "synthetic-private" not in json.dumps(failure.public_payload())
    assert _audit(request.audit_db) == before
    replay = _turn(request, tool)
    assert replay.ok and replay.meta["idempotent_replay"] and len(calls) == 1


def test_new_legal_failure_is_still_recorded_and_replayable(scoped):
    request, _, _ = scoped
    def fail(name, payload):
        return build_response(tool_name=name, ok=False, error={"code": "SYNTHETIC_FAILURE", "message": "safe error"})
    first = _turn(request, fail)
    assert not first.ok
    row = _audit(request.audit_db)
    assert json.loads(row["response_json"])["meta"]["bot"]["turn_result"]
    replay = _turn(request, fail)
    assert not replay.ok and replay.meta["idempotent_replay"]


def _pending(scoped, channel, *, sender="synthetic-sender", message="synthetic-message", legacy=False):
    request, _, _ = scoped
    identity = "synthetic-app" if channel == "feishu" else None
    request = replace(request, channel=channel, sender_id=sender, message_id=message,
                      reply_context={"reply_sender_identity": identity})
    authorization = None if legacy else resolve_replay_authorization(request)
    payload = render_feishu_conversation_reply(message_id=message, text="synthetic-private-reply",
        reply_in_thread=False, max_chars=3500, render_route="bot") if channel == "feishu" else {
            "to_user_id": sender, "context_token": "synthetic-context", "group_id": None,
            "text": "synthetic-private-reply"}
    store = BotHostStore(request.audit_db)
    key = f"{channel}:{message}"
    store.enqueue_reply(delivery_key=key, channel=channel, payload=payload, authorization=authorization)
    return request, store, key


def _retry(request, channel, allowlist, sends, *, reader=None):
    current = reader or (lambda: allowlist)
    if channel == "feishu":
        settings = feishu_ws.FeishuWsSettings(config_key=request.config_key, config_path=request.config_path,
            bot_config_path=request.bot_config_path, audit_db=request.audit_db, app_id="synthetic-app",
            app_secret="synthetic-secret", allowed_senders_fn=current)
        def reply(**kwargs):
            sends.append(kwargs)
            return {"code": 0, "data": {"message_id": "synthetic-receipt"}}
        return feishu_ws._retry_pending_feishu_reply(settings=settings, reply_fn=reply)
    class Client:
        def send_text_message(self, **kwargs):
            sends.append(kwargs)
            return {"ret": 0, "data": {"message_id": "synthetic-receipt"}}
    return wechat._retry_pending_wechat_reply(audit_db=request.audit_db, client=Client(),
        config_key=request.config_key, config_path=request.config_path, bot_config_path=request.bot_config_path,
        allowed_senders_fn=current)


def _due(store, key):
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE bot_reply_outbox SET next_attempt_at='2000-01-01T00:00:00+00:00' WHERE delivery_key=?", (key,))


def _reply_row(store, key):
    return next(row for row in store.list_replies() if row["delivery_key"] == key)


@pytest.mark.parametrize("channel", ["feishu", "wechat"])
@pytest.mark.parametrize("problem", ["revoked", "legacy", "destination", "config"])
def test_retry_requires_current_authority_without_claiming(scoped, channel, problem):
    request, store, key = _pending(scoped, channel, legacy=problem == "legacy")
    if problem == "destination":
        with sqlite3.connect(store.path) as conn:
            row = _reply_row(store, key)
            payload = json.loads(row["payload_json"])
            payload["message_id" if channel == "feishu" else "to_user_id"] = "different-recipient"
            conn.execute("UPDATE bot_reply_outbox SET payload_json=? WHERE delivery_key=?", (json.dumps(payload), key))
    if problem == "config":
        request = replace(request, config_key="hk")
    before = _reply_row(store, key)
    sends = []
    result = _retry(request, channel, "" if problem == "revoked" else f"{channel}:synthetic-sender", sends)
    after = _reply_row(store, key)
    assert result["reason"] == "authorization_blocked" and not result["attempted"] and not sends
    for field in ("status", "payload_json", "authorization_json", "attempt_count", "replay_until", "last_error", "created_at"):
        assert after[field] == before[field]


@pytest.mark.parametrize("channel", ["feishu", "wechat"])
def test_blocked_head_does_not_starve_other_sender_and_same_scope_recovers(scoped, channel):
    request, store, key = _pending(scoped, channel)
    _, _, other = _pending(scoped, channel, sender="other-sender", message="other-message")
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE bot_reply_outbox SET created_at='2026-10-13T00:00:00+00:00' WHERE delivery_key=?", (key,))
    sends = []
    assert _retry(request, channel, f"{channel}:other-sender", sends)["reason"] == "authorization_blocked"
    assert _retry(request, channel, f"{channel}:other-sender", sends)["ok"]
    assert _reply_row(store, other)["status"] == "delivered" and len(sends) == 1
    _due(store, key)
    assert _retry(request, channel, f"{channel}:synthetic-sender", sends)["ok"]
    assert _reply_row(store, key)["status"] == "delivered" and len(sends) == 2


@pytest.mark.parametrize("channel", ["feishu", "wechat"])
def test_revocation_after_claim_blocks_send_without_unknown_or_payload_loss(scoped, channel):
    request, store, key = _pending(scoped, channel)
    reads = []
    def reader():
        reads.append(1)
        return f"{channel}:synthetic-sender" if len(reads) == 1 else ""
    sends = []
    result = _retry(request, channel, None, sends, reader=reader)
    row = _reply_row(store, key)
    assert result["reason"] == "authorization_blocked" and not sends
    assert row["attempt_count"] == 1 and row["status"] == "retryable_failed"
    assert "synthetic-private-reply" in row["payload_json"]
    assert row["last_error"] != "delivery_outcome_unknown"
    _due(store, key)
    assert _retry(request, channel, f"{channel}:synthetic-sender", sends)["ok"]
    assert len(sends) == 1


def test_feishu_unknown_retry_revoke_preserves_window_and_unknown(scoped):
    request, store, key = _pending(scoped, "feishu")
    claimed = store.claim_reply(delivery_key=key)
    store.mark_reply_failed(key, error="unknown", retryable=True, delivery_unknown=True, attempt_count=claimed["attempt_count"])
    _due(store, key)
    before = _reply_row(store, key)
    assert _retry(request, "feishu", "", [])["reason"] == "authorization_blocked"
    after = _reply_row(store, key)
    assert before["last_error"] == after["last_error"] == "delivery_outcome_unknown"
    assert before["replay_until"] == after["replay_until"]
    assert before["attempt_count"] == after["attempt_count"]


@pytest.mark.parametrize("problem", ["delete_key", "missing", "malformed"])
def test_feishu_live_file_source_never_falls_back_to_bootstrap_acl(tmp_path, monkeypatch, problem):
    path = tmp_path / "synthetic.env"
    path.write_text("OM_FEISHU_BOT_ALLOWED_OPEN_IDS=synthetic-sender\n")
    monkeypatch.setattr(feishu_ws, "_load_bot_behavior_config", lambda **kwargs: {})
    settings = feishu_ws.build_feishu_ws_settings(environ={"OM_FEISHU_BOT_ALLOWED_OPEN_IDS": "synthetic-sender"},
        env_file=path, metadata_only=True)
    assert settings.current_allowed_senders() == "feishu:synthetic-sender"
    if problem == "delete_key":
        path.write_text("# ACL removed\n")
        assert settings.current_allowed_senders() == ""
    else:
        if problem == "missing":
            path.rename(tmp_path / "preserved.env")
        else:
            path.write_text("not valid env\n")
        with pytest.raises(ReplyAuthorizationError):
            settings.current_allowed_senders()


def test_wechat_live_config_reader_and_explicit_override_are_distinct(tmp_path):
    path = tmp_path / "config.bot.json"
    path.write_text(json.dumps({"bot": {"enabled": False}, "inbound": {"wechat_clawbot": {"allowed_senders": "wechat:first"}}}))
    dynamic = wechat.build_wechat_clawbot_serve_settings(base=tmp_path, bot_config_path=str(path))
    fixed = wechat.build_wechat_clawbot_serve_settings(base=tmp_path, bot_config_path=str(path), allowed_senders="wechat:fixed")
    assert dynamic.allowed_senders_fn() == "wechat:first"
    path.write_text(json.dumps({"bot": {"enabled": False}, "inbound": {"wechat_clawbot": {"allowed_senders": "wechat:second"}}}))
    assert dynamic.allowed_senders_fn() == "wechat:second" and fixed.allowed_senders_fn() == "wechat:fixed"


@pytest.mark.parametrize("output", ["json", "text"])
def test_real_cli_scoped_replay_and_lookup_error(scoped, tmp_path, monkeypatch, capsys, output):
    from src.interfaces.cli.main import main

    request, _, _ = scoped
    bot = tmp_path / "config.bot.json"
    bot.write_text(json.dumps({"bot": {"enabled": True}}))
    monkeypatch.setenv("OM_FEISHU_BOT_ALLOWED_OPEN_IDS", request.sender_id)
    args = ["bot", "handle", "--text", "/help", "--channel", "feishu", "--sender", request.sender_id,
            "--message-id", request.message_id, "--conversation-id", request.conversation_id,
            "--config-key", "us", "--bot-config", str(bot), "--audit-db", request.audit_db, "--format", output]
    assert main(args) == 0
    original = capsys.readouterr().out
    before = _audit(request.audit_db)
    def unavailable(self, **kwargs):
        raise AgentToolError(code="CONFIG_ERROR", message="synthetic lookup unavailable")
    with monkeypatch.context() as patch:
        patch.setattr(InboundAuditStore, "find_by_message", unavailable)
        assert main(args) == 2
    assert "synthetic lookup unavailable" in capsys.readouterr().out
    assert _audit(request.audit_db) == before
    assert main(args) == 0
    replay = capsys.readouterr().out
    assert json.loads(replay)["meta"]["idempotent_replay"] if output == "json" else replay == original
    args[args.index("--conversation-id") + 1] = "other-chat"
    assert main(args) == 2
    rejected = capsys.readouterr().out
    assert "原消息的作用域无法验证" in rejected
    assert _audit(request.audit_db)["response_json"] == before["response_json"]


@pytest.mark.parametrize("collision", [False, True])
def test_first_unscoped_live_reply_cannot_consume_legacy_collision(scoped, tmp_path, collision):
    request, tool, _ = scoped
    request = replace(request, config_key=None, config_path=str(tmp_path / "missing-config.json"),
                      reply_context={"reply_sender_identity": "synthetic-app"})
    turn = _turn(request, tool)
    assert turn.ok and json.loads(_audit(request.audit_db)["replay_authorization_json"])["scope"] is None
    inbound = build_response(tool_name="inbound.feishu", ok=True, data={"kind": "message",
        "request": turn.data["request"], "response_text": turn.response_text, "inbound_result": turn.public_payload()})
    settings = feishu_ws.FeishuWsSettings(config_path=request.config_path, audit_db=request.audit_db,
        app_id="synthetic-app", app_secret="synthetic-secret", allowed_senders="feishu:synthetic-sender")
    store = BotHostStore(request.audit_db)
    key = f"feishu:{turn.command_id}"
    if collision:
        store.enqueue_reply(delivery_key=key, channel="feishu", payload={"message_id": request.message_id,
            "text": "legacy-private-must-not-send", "reply_in_thread": False})
    sends = []
    def failure(**kwargs):
        sends.append(kwargs)
        raise feishu_ws.FeishuTransientError("synthetic outage")
    result = feishu_ws._maybe_reply(inbound=inbound, settings=settings, reply_fn=failure)
    assert result["reason"] == ("authorization_blocked" if collision else "reply_failed")
    assert len(sends) == (0 if collision else 1)
    if collision:
        assert "legacy-private-must-not-send" in _reply_row(store, key)["payload_json"]
    else:
        _due(store, key)
        assert feishu_ws._retry_pending_feishu_reply(settings=settings, reply_fn=failure)["reason"] == "authorization_blocked"
        assert len(sends) == 1


def test_feishu_known_primary_failure_then_revoke_prevents_fallback(scoped):
    request, store, key = _pending(scoped, "feishu")
    envelope = render_feishu_conversation_reply(message_id=request.message_id,
        text="| synthetic | value |\n|---|---|\n| private | secret |", reply_in_thread=False,
        max_chars=3500, render_route="bot")
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE bot_reply_outbox SET payload_json=? WHERE delivery_key=?", (json.dumps(envelope), key))
    allowed = [True]
    settings = feishu_ws.FeishuWsSettings(config_key="us", audit_db=request.audit_db,
        app_id="synthetic-app", app_secret="synthetic-secret",
        allowed_senders_fn=lambda: "feishu:synthetic-sender" if allowed[0] else "")
    sends = []
    def primary(**kwargs):
        sends.append(kwargs)
        allowed[0] = False
        raise feishu_ws.FeishuPermanentError("synthetic invalid card", code=230001,
                                             response={"http_status": 400, "code": 230001})
    result = feishu_ws._retry_pending_feishu_reply(settings=settings, reply_fn=primary)
    row = _reply_row(store, key)
    assert result["reason"] == "authorization_blocked" and result["attempted"] and len(sends) == 1
    assert sends[0]["uuid"] == key
    assert row["attempt_count"] == 1 and row["status"] == "retryable_failed"
    assert row["last_error"] != "delivery_outcome_unknown" and row["replay_until"]
    assert json.loads(row["payload_json"])["fallback_selected"] is True


def test_secondary_market_account_config_participates_in_replay_scope(scoped, tmp_path):
    from src.application.config_yaml import build_yaml_runtime_config_file
    root = Path(__file__).resolve().parents[1]
    path = tmp_path / "config.hk.json"
    build_yaml_runtime_config_file(repo_root=root, market="hk", config_path=root / "configs/examples/config.yaml.example",
                                   output_config_path=path)
    bot = tmp_path / "config.bot.json"
    bot.write_text(json.dumps({"bot": {"enabled": False, "read_markets": ["us", "hk"]}}))
    request, tool, calls = scoped
    request = replace(request, bot_config_path=str(bot))
    assert _turn(request, tool).ok
    before = _audit(request.audit_db)
    cfg = json.loads(path.read_text())
    cfg["accounts"] = [*cfg["accounts"], "synthetic_other"]
    path.write_text(json.dumps(cfg))
    assert resolve_replay_authorization(request)["scope"] != json.loads(before["replay_authorization_json"])["scope"]
    assert _turn(request, tool).error["code"] == "CHANNEL_NOT_READY"
    assert len(calls) == 1 and _audit(request.audit_db)["response_json"] == before["response_json"]


def test_live_default_market_without_runtime_scope_is_not_retry_proof(scoped, tmp_path, monkeypatch):
    request, tool, _ = scoped
    bot = tmp_path / "default-bot.json"
    bot.write_text(json.dumps({"bot": {"enabled": True, "default_market_scope": "us"}}))
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "missing-runtime"))
    request = replace(request, config_key=None, config_path=None, bot_config_path=str(bot),
                      reply_context={"reply_sender_identity": "synthetic-app"})
    turn = _turn(request, tool)
    # Use the same default settings owner that a public channel loads.
    turn = handle_bot_turn(request, execute_tool_fn=tool, allowed_senders="feishu:synthetic-sender",
                          settings=BotSettings(enabled=True, default_market_scope="us"))
    assert not turn.ok  # The first no-default delivery was already recorded; it cannot be upgraded/replayed.
    new_request = replace(request, message_id="new-default-message")
    turn = handle_bot_turn(new_request, execute_tool_fn=tool, allowed_senders="feishu:synthetic-sender",
                          settings=BotSettings(enabled=True, default_market_scope="us"))
    assert turn.ok
    inbound = build_response(tool_name="inbound.feishu", ok=True, data={"kind": "message", "request": turn.data["request"],
        "response_text": turn.response_text, "inbound_result": turn.public_payload()})
    settings = feishu_ws.FeishuWsSettings(bot_config_path=str(bot), audit_db=request.audit_db,
        app_id="synthetic-app", app_secret="synthetic-secret", allowed_senders="feishu:synthetic-sender")
    calls = []
    result = feishu_ws._maybe_reply(inbound=inbound, settings=settings,
        reply_fn=lambda **kwargs: calls.append(kwargs) or {"code": 0, "data": {"message_id": "new-receipt"}})
    assert result["ok"] and len(calls) == 1
