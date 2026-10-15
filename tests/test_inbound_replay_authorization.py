from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import replace

import pytest

from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.bot.control import inbound_service
from src.application.bot.control.audit import InboundAuditStore
from src.application.bot.control.contracts import BotInboundRequest
from src.application.bot.control.runtime import handle_bot_turn
from src.application.bot.control.settings import BotSettings
from src.application.bot.contracts import AppResult


PRIVATE = "synthetic-private-result"
SETTINGS = BotSettings(enabled=True)


def _request(tmp_path, text="/status", **changes):
    return replace(BotInboundRequest(text=text, sender_id="sender_a", channel="feishu",
        message_id="message_a", config_key="us", audit_db=str(tmp_path / "inbound.sqlite3")), **changes)


@pytest.fixture
def business(monkeypatch, example_config_path):
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(example_config_path.parent))
    calls = []

    def tool(name, payload):
        calls.append(("tool", name, payload))
        return build_response(tool_name=name, ok=True, data={"private": PRIVATE})

    def bot(**kwargs):
        calls.append(("bot", kwargs["user_message"]))
        return AppResult(status="answered", user_response=PRIVATE)

    monkeypatch.setattr(inbound_service, "run_channel_request", bot)
    return tool, calls


def _rows(path):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM inbound_command_audit ORDER BY id")]


@pytest.mark.parametrize("text", ["/status", "请分析风险"])
@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("allowlist", ["", "feishu:sender_b"])
def test_revoked_replay_denies_without_exposing_or_overwriting_cache(tmp_path, business, text, expired, allowlist):
    tool, calls = business
    request = _request(tmp_path, text)
    first = handle_bot_turn(request, execute_tool_fn=tool, allowed_senders="feishu:sender_a", settings=SETTINGS)
    assert first.ok and PRIVATE in json.dumps(first.public_payload())
    original = _rows(request.audit_db)[0]["response_json"]

    denied_request = replace(request, received_monotonic=time.monotonic() - 181) if expired else request
    denied = handle_bot_turn(denied_request, execute_tool_fn=tool, allowed_senders=allowlist, settings=SETTINGS)
    assert not denied.ok and denied.error["code"] == "PERMISSION_DENIED"
    assert denied.error["details"]["reason"] == "sender_not_allowed"
    assert PRIVATE not in json.dumps(denied.public_payload())
    assert not denied.meta.get("idempotent_replay")
    row = _rows(request.audit_db)[0]
    assert row["response_json"] == original
    assert row["duplicate_count"] == (0 if expired else 1)
    if not expired:
        assert row["last_duplicate_decision"] == "denied"

    restored = handle_bot_turn(request, execute_tool_fn=tool, allowed_senders="feishu:sender_a", settings=SETTINGS)
    assert restored.ok and restored.meta["idempotent_replay"]
    assert PRIVATE in json.dumps(restored.public_payload())
    assert len(calls) == 1


@pytest.mark.parametrize("message_id", [None, "fresh_denied"])
def test_fresh_denial_is_durable_without_parsing_or_business(tmp_path, monkeypatch, message_id):
    def forbidden(*args, **kwargs):
        raise AssertionError("unauthorized request reached business processing")

    monkeypatch.setattr(inbound_service, "_parse_command", forbidden)
    monkeypatch.setattr(inbound_service, "_run_bot", forbidden)
    request = _request(tmp_path, message_id=message_id)
    denied = handle_bot_turn(request, execute_tool_fn=forbidden, allowed_senders="", settings=SETTINGS)
    assert denied.error["code"] == "PERMISSION_DENIED"
    rows = _rows(request.audit_db)
    assert len(rows) == 1
    assert (rows[0]["decision"], rows[0]["error_code"]) == ("denied", "PERMISSION_DENIED")
    assert json.loads(rows[0]["response_json"])["error"]["code"] == "PERMISSION_DENIED"


def test_authorization_is_normalized_once_before_each_lookup(tmp_path, monkeypatch, business):
    tool, calls = business
    events = []
    enforce = inbound_service.enforce_sender_allowed
    find = InboundAuditStore.find_by_message

    def auth(**kwargs):
        events.append(("auth", kwargs["channel"], kwargs["sender_id"]))
        return enforce(**kwargs)

    def lookup(store, **kwargs):
        events.append(("lookup", kwargs["channel"]))
        return find(store, **kwargs)

    monkeypatch.setattr(inbound_service, "enforce_sender_allowed", auth)
    monkeypatch.setattr(InboundAuditStore, "find_by_message", lookup)
    request = _request(tmp_path, channel=" FeIsHu ", sender_id=" sender_a ")
    for allowlist in ("feishu:sender_a", "feishu:sender_b"):
        inbound_service.handle_bot_request(request, execute_tool_fn=tool, allowed_senders=allowlist)
    assert events == [("auth", "feishu", "sender_a"), ("lookup", "feishu")]*2
    assert len(calls) == 1


def test_denied_duplicate_does_not_deserialize_old_response(tmp_path, monkeypatch, business):
    tool, calls = business
    request = _request(tmp_path)
    inbound_service.handle_bot_request(request, execute_tool_fn=tool, allowed_senders="feishu:sender_a")

    def forbidden(*args, **kwargs):
        raise AssertionError("unauthorized duplicate consumed cache or entered business")

    monkeypatch.setattr(inbound_service, "_duplicate_response", forbidden)
    monkeypatch.setattr(inbound_service, "_parse_command", forbidden)
    denied = inbound_service.handle_bot_request(replace(request, text="different text"),
        execute_tool_fn=forbidden, allowed_senders="")
    assert denied["error"]["code"] == "PERMISSION_DENIED"
    assert PRIVATE not in json.dumps(denied)
    assert len(calls) == 1


def test_denied_lookup_failure_keeps_current_permission_and_original_cache(tmp_path, monkeypatch, business):
    tool, calls = business
    request = _request(tmp_path)
    handle_bot_turn(request, execute_tool_fn=tool, allowed_senders="feishu:sender_a", settings=SETTINGS)
    original = _rows(request.audit_db)[0]["response_json"]

    def failed_lookup(*args, **kwargs):
        raise AgentToolError(code="CONFIG_ERROR", message="synthetic unavailable audit read")

    monkeypatch.setattr(InboundAuditStore, "find_by_message", failed_lookup)
    denied = handle_bot_turn(request, execute_tool_fn=tool, allowed_senders="", settings=SETTINGS)
    assert denied.error["code"] == "PERMISSION_DENIED"
    assert _rows(request.audit_db)[0]["response_json"] == original
    assert PRIVATE not in json.dumps(denied.public_payload())
    assert len(calls) == 1


def test_allowed_expired_duplicate_replays_but_new_delivery_exhausts_budget(tmp_path, business):
    tool, calls = business
    request = _request(tmp_path)
    handle_bot_turn(request, execute_tool_fn=tool, allowed_senders="feishu:sender_a", settings=SETTINGS)
    expired = replace(request, received_monotonic=time.monotonic() - 181)
    replay = handle_bot_turn(expired, execute_tool_fn=tool, allowed_senders="feishu:sender_a", settings=SETTINGS)
    assert replay.ok and replay.meta["idempotent_replay"]
    new = handle_bot_turn(replace(expired, message_id="new_expired"), execute_tool_fn=tool,
        allowed_senders="feishu:sender_a", settings=SETTINGS)
    assert new.error["code"] == "BUDGET_EXHAUSTED"
    assert len(calls) == 1


def test_allowed_sender_conflict_and_channel_separation_remain(tmp_path, business):
    tool, calls = business
    request = _request(tmp_path)
    allowlist = "feishu:sender_a,feishu:sender_b,wechat:sender_a"
    handle_bot_turn(request, execute_tool_fn=tool, allowed_senders=allowlist, settings=SETTINGS)
    conflict = handle_bot_turn(replace(request, sender_id="sender_b"), execute_tool_fn=tool,
        allowed_senders=allowlist, settings=SETTINGS)
    assert conflict.error["code"] == "PERMISSION_DENIED"
    assert PRIVATE not in json.dumps(conflict.public_payload())
    assert _rows(request.audit_db)[0]["last_duplicate_decision"] == "sender_conflict"
    separate = handle_bot_turn(replace(request, channel="wechat"), execute_tool_fn=tool,
        allowed_senders=allowlist, settings=SETTINGS)
    assert separate.ok and not separate.meta.get("idempotent_replay")
    assert len(_rows(request.audit_db)) == len(calls) == 2


def test_business_permission_error_remains_rendered_and_audited(tmp_path):
    def tool(name, payload):
        return build_response(tool_name=name, ok=False,
            error={"code": "PERMISSION_DENIED", "message": "synthetic resource denial"})

    request = _request(tmp_path)
    denied = handle_bot_turn(request, execute_tool_fn=tool, allowed_senders="feishu:sender_a", settings=SETTINGS)
    assert not denied.ok and denied.error["code"] == "PERMISSION_DENIED"
    assert denied.response_text
    assert denied.meta["bot"]["turn_result"]["error"]["code"] == "PERMISSION_DENIED"
    row = _rows(request.audit_db)[0]
    assert row["error_code"] == "PERMISSION_DENIED"
    assert json.loads(row["response_json"])["data"]["tool_result"]["error"]["code"] == "PERMISSION_DENIED"


def test_public_cli_rejects_revoked_cached_reply(tmp_path, monkeypatch, capsys, business):
    from src.interfaces.cli import bot_control_ops, main as cli

    tool, calls = business
    allowlist = "feishu:sender_a"
    monkeypatch.setattr(bot_control_ops, "_bot_settings_for_cli", lambda **kwargs: SETTINGS)

    def turn(request, **kwargs):
        return handle_bot_turn(request, execute_tool_fn=tool, allowed_senders=allowlist, **kwargs)

    monkeypatch.setattr(cli, "handle_bot_turn", turn)
    args = ["bot", "handle", "--text", "/status", "--sender", "sender_a", "--channel", "feishu",
        "--message-id", "cli_message", "--config-key", "us", "--audit-db", str(tmp_path / "cli.sqlite3"),
        "--no-local-env-file"]
    assert cli.main(args) == 0
    capsys.readouterr()
    original = _rows(tmp_path / "cli.sqlite3")[0]["response_json"]
    allowlist = ""
    assert cli.main(args) == 2
    output = capsys.readouterr().out
    assert PRIVATE not in output
    assert json.loads(output)["error"]["code"] == "PERMISSION_DENIED"
    assert _rows(tmp_path / "cli.sqlite3")[0]["response_json"] == original
    assert len(calls) == 1
