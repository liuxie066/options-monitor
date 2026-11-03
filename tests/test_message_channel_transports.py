"""Facade injection and SDK registration tests never connect to real services."""
from __future__ import annotations

import json
from types import ModuleType, SimpleNamespace
import sys

import pytest

from src.infrastructure import feishu_bot, feishu_ws_client
from src.application.channels.wechat_clawbot import ilink_client


def test_rest_wrapper_uses_token_dependency_changed_after_plugin_import(monkeypatch):
    from src.application.channels.plugins import load_channel_plugin

    load_channel_plugin("feishu", module="bot")
    tokens, calls = [], []

    def token_retry(app_id, app_secret, send):
        tokens.append((app_id, app_secret))
        return send("current-token")

    monkeypatch.setattr(feishu_bot, "with_tenant_token_retry", token_retry)
    result = feishu_bot.send_text_message(
        app_id="fixture-app", app_secret="fixture-secret", open_id="fixture-recipient",
        text="正文", uuid="fixture-key",
        http_json_fn=lambda *args, **kwargs: calls.append((args, kwargs)) or {"code": 0},
    )
    assert result == {"code": 0}
    assert tokens == [("fixture-app", "fixture-secret")]
    assert calls[0][1]["headers"]["Authorization"] == "Bearer current-token"
    assert calls[0][0][2]["receive_id"] == "fixture-recipient"
    assert calls[0][1]["retry_max_attempts"] == 3


def test_text_reply_wrapper_still_uses_current_reply_message(monkeypatch):
    calls = []
    monkeypatch.setattr(feishu_bot, "reply_message", lambda **kwargs: calls.append(kwargs) or {"code": 0})
    feishu_bot.reply_text_message(app_id="fixture", app_secret="fixture", message_id="m", text="正文")
    assert calls[0]["msg_type"] == "text" and calls[0]["text"] == "正文"


@pytest.mark.parametrize("allow_retry", [True, False])
def test_reaction_guard_runs_after_each_token_resolution_before_http(monkeypatch, allow_retry):
    from src.application.agent_tool_contracts import AgentToolError
    from src.infrastructure import feishu_bitable

    events = []

    def token(app_id, app_secret, **kwargs):
        events.append("refresh" if kwargs.get("force_refresh") else "token")
        return "fixture-token"

    def guard():
        events.append("guard")
        if not allow_retry and events.count("guard") == 2:
            raise AgentToolError(code="CHANNEL_RECEIVE_DISABLED", message="fixture disabled after first attempt")

    def http(*args, **kwargs):
        events.append("http")
        assert events[-2] == "guard"
        if events.count("http") == 1:
            raise feishu_bitable.FeishuAuthError("fixture token expired")
        assert kwargs["timeout"] == 2 and kwargs["retry_max_attempts"] == 1
        return {"code": 0}

    monkeypatch.setattr(feishu_bitable, "get_tenant_access_token", token)
    kwargs = dict(app_id="fixture", app_secret="fixture", message_id="m", emoji_type="Typing",
                  before_send=guard, http_json_fn=http)
    if allow_retry:
        assert feishu_bot.add_message_reaction(**kwargs) == {"code": 0}
        assert events == ["token", "guard", "http", "refresh", "guard", "http"]
    else:
        with pytest.raises(AgentToolError) as failure:
            feishu_bot.add_message_reaction(**kwargs)
        assert failure.value.code == "CHANNEL_RECEIVE_DISABLED"
        assert events == ["token", "guard", "http", "refresh", "guard"]


def test_wechat_facade_preserves_injected_http_and_shared_error():
    from om_wechat_channel.ilink_client import WechatClawbotError

    assert WechatClawbotError is ilink_client.WechatClawbotError
    calls = []
    client = ilink_client.WechatClawbotClient(
        bot_token="fixture-token", base_url="https://fixture.invalid/", timeout=7,
        http_json_fn=lambda *args, **kwargs: calls.append((args, kwargs)) or {"ret": 0},
    )
    result = client.send_text_message(to_user_id="fixture-user", context_token="context",
                                      text="正文", group_id="fixture-group", client_id="frozen-id")
    assert result == {"ret": 0}
    assert client.base_url == "https://fixture.invalid"
    args, kwargs = calls[0]
    assert args[:2] == ("POST", "https://fixture.invalid/ilink/bot/sendmessage")
    assert args[2]["msg"]["client_id"] == "frozen-id"
    assert args[2]["msg"]["group_id"] == "fixture-group"
    assert kwargs["timeout"] == 7 and kwargs["headers"]["Authorization"] == "Bearer fixture-token"


def _fake_sdk(monkeypatch, event):
    captured = {}
    sdk = ModuleType("lark_oapi")
    core = ModuleType("lark_oapi.core")
    enums = ModuleType("lark_oapi.core.enum")
    levels = SimpleNamespace(INFO=object(), DEBUG=object())
    enums.LogLevel = levels

    class Builder:
        def register_p2_im_message_receive_v1(self, callback):
            captured["callback"] = callback
            return self

        def build(self):
            return "fixture-handler"

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        def start(self):
            captured["callback"](event)

    sdk.EventDispatcherHandler = SimpleNamespace(builder=lambda *args: Builder())
    sdk.ws = SimpleNamespace(Client=Client)
    for name, module in (("lark_oapi", sdk), ("lark_oapi.core", core), ("lark_oapi.core.enum", enums)):
        monkeypatch.setitem(sys.modules, name, module)
    return captured, levels


@pytest.mark.parametrize("level", ["debug", "unknown"])
def test_sdk_connector_preserves_registration_conversion_and_start(monkeypatch, level):
    event = SimpleNamespace(event=SimpleNamespace(message=SimpleNamespace(content=json.dumps({"text": "正文"}))))
    captured, levels = _fake_sdk(monkeypatch, event)
    payloads = []
    feishu_ws_client.start_feishu_ws_client(
        app_id="fixture-app", app_secret="fixture-secret", on_event=payloads.append, log_level=level,
    )
    assert captured["client"] == {
        "app_id": "fixture-app", "app_secret": "fixture-secret", "event_handler": "fixture-handler",
        "log_level": levels.DEBUG if level == "debug" else levels.INFO,
    }
    assert payloads == [feishu_ws_client.feishu_event_model_to_payload(event)]


def test_sdk_callback_error_is_logged_without_killing_connector(monkeypatch, caplog):
    _fake_sdk(monkeypatch, SimpleNamespace())

    def failing_callback(payload):
        raise ValueError("fixture callback failure")

    feishu_ws_client.start_feishu_ws_client(app_id="fixture", app_secret="fixture", on_event=failing_callback)
    assert "failed to enqueue Feishu WebSocket event" in caplog.text
